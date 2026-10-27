"""CBR250R (MC41) ECU: identify it and make a verified backup of its flash. READ ONLY.

Nothing here erases or writes the ECU, and the RemapBridge firmware refuses those commands anyway.

  python pc/remap.py COM15 check              hardware check (bridge, K-line, echo, ECU init, table 0x11)
  python pc/remap.py COM15 id                 ECM ID (table 0x00) + the readable tables, saved to backups/<time>/
  python pc/remap.py COM15 backup --yes       read the whole flash twice, compare, checksum, save (Stage 1b)
  python pc/remap.py demo backup --yes        the same against a pretend ECU
  python pc/remap.py - verify <file.bin>      size, Honda checksum, ECM ID and Keihin code of a saved image
  python pc/remap.py - compare <a.bin> <b.bin>  list the address ranges where two images differ

Flash read protocol (from the open-source HondaECU, NOT yet tried on this bike, see README "Research"):
  after key-on: wake-up + init (72 05 00 F0), then security access
    27 0B E0 48 65 6C 6C 6F 48 6F <cs>      ("HelloHo")
    27 0B E0 77 41 72 65 59 6F 75 <cs>      ("wAreYou")
  then read: 82 82 00 09 <bank> <addr lo> <addr hi> <n> <cs>  -> 92 92 00? <len> <n data bytes> <cs>
  (the reply's length byte is byte 3). The image is 56 KB (0x0000-0xDFFF) and all its bytes sum to 0 mod 256.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time

from honda_dlc import DlcError, Ecu, SerialLink, SimLink, checksum, check, hexs

ROM_SIZE = 0xE000          # 56 KB (HondaECU table: CBR250R 2012, 38770-KYJ-971, checksum at 0xDFEF)
CHECKSUM_ADDR = 0xDFEF     # the byte that makes the whole image sum to 0 mod 256
ECMID_ADDR = 0xC3EE        # 5-byte ECM ID inside the image (HondaECU ecmids.py)
KEIHIN_ADDR = 0xDFF0       # Keihin software code, ASCII ("KYJA203 " in both known stock images)
READ_CHUNK = 8             # bytes per read request (HondaECU uses 8 to 12)

# ECM IDs (first 5 bytes of table 0x00) known for the MC41, from HondaECU's ecmids.py and the stock images.
KNOWN_ECMIDS = {
    bytes.fromhex("0100B80502"): "CBR250R 2011-14, 38770-KYJ-922",
    bytes.fromhex("0100B80F01"): "CBR250R 2011-14, 38770-KYJ-971",
}

PASSWORD = (bytes.fromhex("E0 48 65 6C 6C 6F 48 6F"), bytes.fromhex("E0 77 41 72 65 59 6F 75"))

DIAG_TABLES = [0x00, 0x10, 0x11, 0x17, 0x20, 0x21, 0x60, 0x61, 0x67, 0x70, 0x71, 0xD0, 0xD1]

HERE = os.path.dirname(os.path.abspath(__file__))
BACKUPS = os.path.join(os.path.dirname(HERE), "backups")


def image_sum_ok(img: bytes) -> bool:
    return sum(img) & 0xFF == 0


def image_info(img: bytes) -> dict:
    info = {"size": len(img), "sha256": hashlib.sha256(img).hexdigest(), "sum_ok": image_sum_ok(img)}
    if len(img) >= ROM_SIZE:
        ecmid = img[ECMID_ADDR:ECMID_ADDR + 5]
        info["ecmid"] = hexs(ecmid)
        info["ecmid_known_as"] = KNOWN_ECMIDS.get(ecmid, "unknown")
        info["keihin_code"] = img[KEIHIN_ADDR:KEIHIN_ADDR + 8].decode("latin1").rstrip("\x00 ")
        info["checksum_byte"] = f"{img[CHECKSUM_ADDR]:02X}"
    return info


def diff_ranges(a: bytes, b: bytes) -> list[tuple[int, int]]:
    """[(start, end_exclusive), ...] where a and b differ (gaps of < 4 equal bytes are merged)."""
    out = []
    for i in range(min(len(a), len(b))):
        if a[i] != b[i]:
            if out and i - out[-1][1] < 4:
                out[-1] = (out[-1][0], i + 1)
            else:
                out.append((i, i + 1))
    return out


def new_dir(tag: str) -> str:
    d = os.path.join(BACKUPS, time.strftime("%Y%m%d_%H%M%S") + "_" + tag)
    os.makedirs(d, exist_ok=True)
    return d


# ---------------------------------------------------------------- pretend ECU with a flash image

class SimFlashLink(SimLink):
    def __init__(self):
        super().__init__()
        img = bytearray((i * 7 + (i >> 8)) & 0xFF for i in range(ROM_SIZE))
        img[ECMID_ADDR:ECMID_ADDR + 5] = bytes.fromhex("0100B80502")
        img[KEIHIN_ADDR:KEIHIN_ADDR + 8] = b"KYJA203 "
        img[CHECKSUM_ADDR] = 0
        img[CHECKSUM_ADDR] = checksum(bytes(img))
        self.img = bytes(img)
        self.secure = 0

    def query(self, packet: bytes, len_pos: int = 1) -> bytes:
        if packet[0] == 0x27:
            self.secure += 1
            body = bytes([0x17, 0x04, packet[2]])
        elif packet[:3] == b"\x82\x82\x00":
            if self.secure < 2:
                raise DlcError("ERR TIMEOUT")
            addr = packet[4] << 16 | packet[6] << 8 | packet[5]
            data = self.img[addr:addr + packet[7]]
            body = bytes([0x92, 0x92, 0x00, 5 + len(data)]) + data
        elif packet[:4] == b"\x72\x05\x71\x00":
            data = self.img[ECMID_ADDR:ECMID_ADDR + 5] + bytes(11)
            body = bytes([0x02, 5 + len(data), 0x71, 0x00]) + data
        else:
            return super().query(packet)
        return body + bytes([checksum(body)])

    def level(self) -> str:
        return "OK LEVEL HIGH"


# ---------------------------------------------------------------- ECU steps

def line_level(link) -> str:
    return link.level() if isinstance(link, SimFlashLink) else link._cmd("L")


def wait_key_cycle(link):
    """The flash read is entered right after key-on (as in HondaECU): key off, then on."""
    if isinstance(link, SimFlashLink):
        return
    if line_level(link) == "OK LEVEL HIGH":
        print("Turn the key OFF (the K-line pull-up is fed from key-on 12 V) ...")
        while line_level(link) == "OK LEVEL HIGH":
            time.sleep(0.2)
    print("Now turn the key ON (kill switch at RUN, engine OFF) ...")
    while line_level(link) != "OK LEVEL HIGH":
        time.sleep(0.2)
    time.sleep(0.5)


def security_access(ecu: Ecu):
    for part in PASSWORD:
        frame = ecu.raw(bytes([0x27, len(part) + 3]) + part)
        print("  security access ->", hexs(frame))


def read_chunk(link, addr: int, n: int) -> bytes:
    pkt = bytes([0x82, 0x82, 0x00, 0x09, (addr >> 16) & 0xFF, addr & 0xFF, (addr >> 8) & 0xFF, n])
    frame = link.query(pkt, len_pos=3)
    if len(frame) < 5 or frame[3] != len(frame) or sum(frame) & 0xFF or frame[0] != 0x92:
        raise DlcError(f"bad read reply {hexs(frame)}")
    data = frame[4:-1]
    if len(data) != n:
        raise DlcError(f"asked for {n} bytes at {addr:05X}, got {len(data)}")
    return data


def read_flash(link, size: int = ROM_SIZE) -> bytes:
    img = bytearray()
    t0 = time.time()
    while len(img) < size:
        addr = len(img)
        n = min(READ_CHUNK, size - addr)
        for attempt in range(4):
            try:
                img += read_chunk(link, addr, n)
                break
            except DlcError as e:
                if attempt == 3:
                    raise DlcError(f"read failed at {addr:05X}: {e}")
        if addr % 0x400 == 0:
            rate = len(img) / max(0.001, time.time() - t0)
            print(f"\r  {addr:05X} / {size:05X}  {rate:.0f} B/s", end="", flush=True)
    print(f"\r  {size:05X} / {size:05X}  done in {time.time() - t0:.0f} s")
    return bytes(img)


def cmd_id(link) -> int:
    ecu = Ecu(link)
    print("init:", hexs(ecu.connect()))
    out = {}
    for t in DIAG_TABLES:
        try:
            out[f"{t:02X}"] = hexs(ecu.read_table(t))
        except DlcError as e:
            out[f"{t:02X}"] = f"no answer ({e})"
    t0 = out.get("00", "")
    ecmid = bytes.fromhex(t0)[:5] if not t0.startswith("no") else b""
    print("ECM ID:", hexs(ecmid) or "?", "=", KNOWN_ECMIDS.get(ecmid, "not in the known list"))
    for k, v in out.items():
        print(f"  table {k}: {v}")
    d = new_dir("id")
    with open(os.path.join(d, "id.json"), "w") as fh:
        json.dump({"ecmid": hexs(ecmid), "known_as": KNOWN_ECMIDS.get(ecmid), "tables": out}, fh, indent=1)
    print("saved", d)
    return 0


def cmd_backup(link) -> int:
    wait_key_cycle(link)
    ecu = Ecu(link)
    print("init:", hexs(ecu.connect()))
    security_access(ecu)
    d = new_dir("backup")
    reads = []
    for n in (1, 2):
        print(f"read {n} of 2")
        img = read_flash(link)
        with open(os.path.join(d, f"read{n}.bin"), "wb") as fh:
            fh.write(img)
        reads.append(img)
    info = {"read1": image_info(reads[0]), "read2": image_info(reads[1]),
            "identical": reads[0] == reads[1], "time": time.strftime("%Y-%m-%d %H:%M:%S")}
    ok = info["identical"] and info["read1"]["sum_ok"]
    if ok:
        with open(os.path.join(d, "flash.bin"), "wb") as fh:
            fh.write(reads[0])
        with open(os.path.join(d, "flash.bin.sha256"), "w") as fh:
            fh.write(f"{info['read1']['sha256']}  flash.bin\n")
    else:
        info["differences"] = [f"{a:05X}-{b - 1:05X}" for a, b in diff_ranges(*reads)][:50]
    with open(os.path.join(d, "info.json"), "w") as fh:
        json.dump(info, fh, indent=1)
    print(json.dumps(info, indent=1))
    print("VERIFIED BACKUP: flash.bin in" if ok else "NOT VERIFIED (reads differ or checksum bad): see", d)
    print("Copy the folder somewhere safe (USB stick + cloud) before anything else.")
    return 0 if ok else 2


def cmd_verify(path: str) -> int:
    img = open(path, "rb").read()
    info = image_info(img)
    print(json.dumps(info, indent=1))
    return 0 if info["sum_ok"] and info["size"] == ROM_SIZE else 2


def cmd_compare(a: str, b: str) -> int:
    x, y = open(a, "rb").read(), open(b, "rb").read()
    if len(x) != len(y):
        print(f"sizes differ: {len(x)} vs {len(y)}")
    rs = diff_ranges(x, y)
    for s, e in rs:
        print(f"{s:05X}-{e - 1:05X} ({e - s:3d} bytes)  {hexs(x[s:e])[:47]}  |  {hexs(y[s:e])[:47]}")
    print(len(rs), "ranges differ" if rs else "identical")
    return 0


def main(argv) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 1
    port, mode, rest = argv[0], argv[1], argv[2:]
    if mode == "verify":
        return cmd_verify(rest[0])
    if mode == "compare":
        return cmd_compare(rest[0], rest[1])
    if mode == "check":
        return check(port)
    if mode == "backup" and "--yes" not in rest:
        print("backup puts the ECU in its read (security access) mode. Engine off, key on, a good battery or a "
              "bench supply. Run again with --yes.")
        return 1
    link = SimFlashLink() if port.lower() == "demo" else SerialLink(port)
    try:
        if mode == "id":
            return cmd_id(link)
        if mode == "backup":
            return cmd_backup(link)
        print("unknown mode", mode)
        return 1
    except KeyboardInterrupt:
        return 1
    finally:
        link.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
