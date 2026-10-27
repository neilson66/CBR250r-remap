"""Honda PGM-FI K-line diagnostics through the ESP32 bridge (firmware/HondaKLine).

Protocol (Honda's own K-line, 10400 baud, as reverse-engineered by the community):
  wake-up      K-line low 70 ms, high 130 ms                     (done by the firmware)
  wake         FE 04 FF FF                                       (no reply)
  init         72 05 00 F0 99           -> 02 04 00 FA
  read table   72 05 71 <table> <cs>    -> 02 <len> 71 <table> <data...> <cs>
  faults       72 05 74 <n> <cs> (current) / 72 05 73 <n> <cs> (past)   [layout unverified]
Every frame: byte 1 is the total length, and all bytes sum to 0 mod 256.

Field layouts below are the commonly published ones for Honda bikes of this era. They are NOT yet checked against
a CBR250R (MC41): use the raw view / dump to confirm each byte moves the way its name says.

Command line:
  python honda_dlc.py COM5 check         step-by-step hardware check (bench or bike), explains failures
  python honda_dlc.py COM5 live          live data from table 0x11
  python honda_dlc.py COM5 dump          try every table 0x00-0xFF, save what answers
  python honda_dlc.py COM5 dtc           read fault-code tables
  python honda_dlc.py COM5 raw 72 05 71 11   send one packet (checksum added)
  python honda_dlc.py demo live          same, against a simulated ECU
"""

from __future__ import annotations

import csv
import json
import sys
import time
from dataclasses import dataclass


def checksum(data: bytes) -> int:
    return (-sum(data)) & 0xFF


def frame_ok(frame: bytes) -> bool:
    return len(frame) >= 3 and frame[1] == len(frame) and sum(frame) & 0xFF == 0


def hexs(b: bytes) -> str:
    return " ".join(f"{x:02X}" for x in b)


class DlcError(Exception):
    pass


# ---------------------------------------------------------------- links

class SerialLink:
    """Talks to the ESP32 bridge over USB serial."""

    def __init__(self, port: str, timeout: float = 2.0):
        import serial  # pyserial
        self.ser = serial.Serial(port, 115200, timeout=timeout)
        time.sleep(0.3)
        # Flush any half-typed line or boot text on the board, then ping (a couple of tries after a reset).
        self.ser.write(b"\n")
        time.sleep(0.2)
        self.ser.reset_input_buffer()
        reply = ""
        for _ in range(3):
            try:
                reply = self._cmd("P")
            except DlcError:
                continue
            if reply.startswith("OK HONDA-KLINE"):
                break
        if not reply.startswith("OK HONDA-KLINE"):
            raise DlcError(f"no bridge on {port} (got {reply!r})")

    def _cmd(self, line: str) -> str:
        self.ser.write((line + "\n").encode())
        reply = self.ser.readline().decode(errors="replace").strip()
        if not reply:
            raise DlcError("bridge did not answer")
        return reply

    def wake(self) -> bytes:
        reply = self._cmd("W")
        if not reply.startswith("OK"):
            raise DlcError(f"wake-up failed: {reply}")
        return bytes.fromhex(reply[2:])

    def query(self, packet: bytes) -> bytes:
        """Send packet (without checksum) and return the reply frame."""
        reply = self._cmd("T " + hexs(packet))
        if not reply.startswith("R "):
            raise DlcError(reply)
        return bytes.fromhex(reply[2:])

    def close(self):
        self.ser.close()


class SimLink:
    """A pretend ECU so the PC side can be tried without the bike."""

    def __init__(self):
        self.t0 = time.time()
        self.awake = False

    def wake(self) -> bytes:
        self.awake = True
        return bytes([0x02, 0x04, 0x00, 0xFA])

    def _table11(self) -> bytes:
        import math
        t = time.time() - self.t0
        rpm = int(1400 + 1200 * (1 + math.sin(t / 2)))
        tps = int(max(0, 40 * math.sin(t / 2)))
        d = bytearray(20)
        d[0:2] = rpm.to_bytes(2, "big")
        d[2] = 0x1A + tps            # TPS volts
        d[3] = int(tps * 1.6)        # TPS %
        d[4] = 0x50; d[5] = 40 + min(90, int(20 + t))   # ECT
        d[6] = 0x90; d[7] = 40 + 22                    # IAT
        d[8] = 0x60; d[9] = max(30, 101 - tps)          # MAP kPa
        d[12] = 138                  # battery x0.1 V
        d[13] = int(tps * 1.5)       # speed km/h
        d[14:16] = int(2.4 * 250).to_bytes(2, "big")   # injector
        d[16] = int((15 + 64) * 2)   # ignition advance
        return bytes(d)

    def query(self, packet: bytes) -> bytes:
        if not self.awake:
            raise DlcError("ERR TIMEOUT")
        if packet[:3] == b"\x72\x05\x71":
            table = packet[3]
            data = {0x11: self._table11(), 0x00: bytes(16), 0xD1: bytes([0, 0, 1, 0, 0, 0])}.get(table)
            if data is None:
                raise DlcError("ERR TIMEOUT")
            body = bytes([0x02, 5 + len(data), 0x71, table]) + data
        elif packet[:4] == b"\x72\x05\x60\x03":
            body = bytes([0x02, 0x04, 0x60])
        elif packet[:2] == b"\x72\x05" and packet[2] in (0x73, 0x74):
            body = bytes([0x02, 5 + 6, packet[2], packet[3]]) + bytes(6)
        else:
            raise DlcError("ERR TIMEOUT")
        return body + bytes([checksum(body)])

    def close(self):
        pass


# ---------------------------------------------------------------- decoding

@dataclass
class Field:
    name: str
    unit: str
    offset: int
    size: int
    scale: float = 1.0
    add: float = 0.0
    fmt: str = "{:.0f}"

    def value(self, data: bytes):
        if self.offset + self.size > len(data):
            return None
        raw = int.from_bytes(data[self.offset:self.offset + self.size], "big")
        return raw * self.scale + self.add


# Table 0x11, commonly published layout. Unverified on the CBR250R.
TABLE11 = [
    Field("RPM", "rpm", 0, 2),
    Field("Throttle sensor", "V", 2, 1, 5 / 256, fmt="{:.2f}"),
    Field("Throttle", "%", 3, 1, 1 / 1.6, fmt="{:.1f}"),
    Field("Coolant sensor", "V", 4, 1, 5 / 256, fmt="{:.2f}"),
    Field("Coolant temp", "°C", 5, 1, add=-40),
    Field("Intake air sensor", "V", 6, 1, 5 / 256, fmt="{:.2f}"),
    Field("Intake air temp", "°C", 7, 1, add=-40),
    Field("MAP sensor", "V", 8, 1, 5 / 256, fmt="{:.2f}"),
    Field("Manifold pressure", "kPa", 9, 1),
    Field("Battery", "V", 12, 1, 0.1, fmt="{:.1f}"),
    Field("Speed", "km/h", 13, 1),
    Field("Injector time", "ms", 14, 2, 1 / 250, fmt="{:.2f}"),
    Field("Ignition advance", "°", 16, 1, 0.5, -64, fmt="{:.1f}"),
]


def decode(fields, data: bytes) -> dict:
    out = {}
    for f in fields:
        v = f.value(data)
        out[f.name] = (None if v is None else f.fmt.format(v), f.unit)
    return out


# ---------------------------------------------------------------- ECU

class Ecu:
    def __init__(self, link):
        self.link = link

    def connect(self) -> bytes:
        reply = self.link.wake()
        if reply != bytes([0x02, 0x04, 0x00, 0xFA]):
            raise DlcError(f"unexpected init reply {hexs(reply)}")
        return reply

    def raw(self, packet: bytes) -> bytes:
        frame = self.link.query(packet)
        if not frame_ok(frame):
            raise DlcError(f"bad frame {hexs(frame)}")
        return frame

    def read_table(self, table: int) -> bytes:
        frame = self.raw(bytes([0x72, 0x05, 0x71, table]))
        if frame[2] != 0x71 or frame[3] != table:
            raise DlcError(f"reply for wrong table: {hexs(frame)}")
        return frame[4:-1]

    def live(self) -> tuple[bytes, dict]:
        data = self.read_table(0x11)
        return data, decode(TABLE11, data)

    def fault_tables(self) -> dict:
        """Raw fault-code replies: {'current'|'past': [(index, payload), ...]}. Layout unverified."""
        out = {}
        for name, cmd in (("current", 0x74), ("past", 0x73)):
            rows = []
            for i in range(1, 0x0D):
                try:
                    frame = self.raw(bytes([0x72, 0x05, cmd, i]))
                except DlcError:
                    break
                rows.append((i, frame[4:-1]))
            out[name] = rows
        return out

    def clear_faults(self) -> bytes:
        """Ask the ECU to erase stored fault codes (72 05 60 03, as used by community Honda tools; unverified on
        the MC41). Writes nothing else. Returns the ECU's reply; re-read the fault tables to confirm."""
        return self.raw(bytes([0x72, 0x05, 0x60, 0x03]))

    @staticmethod
    def fault_codes(rows) -> list[str]:
        """Honda 'main-sub' codes (e.g. 7-1 = coolant sensor): byte 0 of each row is skipped (meaning unknown), then
        (main, sub) pairs. Seen on the MC41: past rows 01 01 02 08 01 09 02 = 1-2, 8-1, 9-2 and 00 0C 01 36 01 36 02
        = 12-1, 54-1, 54-2."""
        codes = []
        for _, payload in rows:
            for j in range(1, len(payload) - 1, 2):
                if payload[j]:
                    codes.append(f"{payload[j]}-{payload[j + 1]}")
        return codes

    def dump(self, tables=range(0x100), progress=None) -> dict:
        found = {}
        for t in tables:
            if progress:
                progress(t)
            try:
                found[t] = self.read_table(t)
            except DlcError:
                continue
        return found


# ---------------------------------------------------------------- command line

def open_link(port: str):
    return SimLink() if port.lower() == "demo" else SerialLink(port)


CHECK_HELP = {
    "level": "K-line reads LOW at idle. Is 12 V on the pull-up (key on, kill switch RUN / bench supply)? Is Q1 "
             "always on (TX pin wiring, transistor legs) or the K-line shorted to GND? Check D1 points the right "
             "way. Pins: C6 TX 2 / RX 3, S3 TX 1 / RX 2, C3 TX 4 / RX 5.",
    "hold": "Holding the K-line low wasn't seen. If a multimeter on the K-line reads ~0 V now, the receive side "
            "(R3/R4/Q2/R5, RX pin) is at fault; if it still reads ~12 V, the send side (TX pin, R2, Q1) is.",
    "echo": "Bytes didn't come back. Check the RX pin / Q2 / R3 / R4 / R5 and that the TX pin reaches Q1 through "
            "R2. Transistor legs: 2N3904 is E-B-C with the flat face towards you.",
    "init": "Interface works but the ECU didn't answer. Key on, kill switch at RUN, side stand up. Is the "
            "K-line on the right DLC pin? Try again; some ECUs only answer within a few seconds of key-on.",
}


def check(port: str) -> int:
    """Step-by-step check of the bridge, the interface and then the ECU."""
    link = SerialLink(port)
    print("1. bridge on", port, "... OK")
    steps = [("level", "L", "OK LEVEL HIGH", "2. K-line idles high"),
             ("hold", "D 0;L", "OK LEVEL LOW", "3. pulling the K-line low is seen"),
             ("echo", "E", "OK ECHO", "4. send/receive echo"),
             ("init", "W", "OK 02 04 00 FA", "5. ECU wake-up and init")]
    try:
        for key, cmd, want, label in steps:
            replies = [link._cmd(c) for c in cmd.split(";")]
            reply = replies[-1]
            if key == "hold":
                link._cmd("D 1")
            if reply != want:
                print(f"{label} ... FAILED ({reply})")
                print("   ", CHECK_HELP[key])
                if key == "init":
                    print("    (Steps 1-4 passed, so the hardware is fine; on the bench without the ECU this is expected.)")
                return 1
            print(f"{label} ... OK")
        ecu = Ecu(link)
        data = ecu.read_table(0x11)
        print("6. table 0x11 ...", hexs(data))
        print("It's talking. Run 'live', 'dtc' or 'dump', or open dlc_gui.py.")
        return 0
    finally:
        link.close()


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1
    port, mode = argv[0], argv[1]
    if mode == "check":
        return check(port)
    link = open_link(port)
    ecu = Ecu(link)
    try:
        print("init:", hexs(ecu.connect()))
        if mode == "live":
            log_name = time.strftime("live_%Y%m%d_%H%M%S.csv")
            with open(log_name, "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(["time"] + [f"{f.name} ({f.unit})" for f in TABLE11] + ["raw"])
                print(f"logging to {log_name}, Ctrl+C to stop")
                while True:
                    data, vals = ecu.live()
                    w.writerow([f"{time.time():.2f}"] + [vals[f.name][0] for f in TABLE11] + [hexs(data)])
                    print("  ".join(f"{k} {v[0]}{v[1]}" for k, v in vals.items()))
                    time.sleep(0.1)
        elif mode == "dump":
            found = ecu.dump(progress=lambda t: print(f"\rtable {t:02X}", end="", flush=True))
            print()
            for t, d in found.items():
                print(f"{t:02X} ({len(d):2d}): {hexs(d)}")
            name = time.strftime("dump_%Y%m%d_%H%M%S.json")
            with open(name, "w") as fh:
                json.dump({f"{t:02X}": hexs(d) for t, d in found.items()}, fh, indent=1)
            print("saved", name)
        elif mode == "dtc":
            tables = ecu.fault_tables()
            for name, rows in tables.items():
                for i, payload in rows:
                    print(f"{name} {i:02X}: {hexs(payload)}")
                print(f"{name} codes:", ", ".join(Ecu.fault_codes(rows)) or "none")
        elif mode == "raw":
            print(hexs(ecu.raw(bytes.fromhex("".join(argv[2:])))))
        else:
            print("unknown mode", mode)
            return 1
    except KeyboardInterrupt:
        pass
    finally:
        link.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
