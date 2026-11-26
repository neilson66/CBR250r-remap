"""Live cell tracking for the map editor: where the engine is in a map, and what happened in each cell.

Sources (each runs in its own thread and puts Sample objects on a queue):
  EcuSource("COM6")   the HondaDLC scanner (or RemapBridge) over USB: table 0x11 every sample, O2 (table 0x20)
                      every 3rd. Read only: only "72 05 71 <table>" reads go to the ECU.
  EcuSource("demo")   the pretend ECU from honda_dlc.py (no O2).
  ReplaySource(csv)   a scanner log (SD live.csv or the phone's CSV), at real time x speed, or all at once.

Decoding is the same as ride_analysis.py: table 0x11 bytes 0-1 rpm, 3 throttle (/1.6 %), 9 MAP kPa, 14-15
injector ms (/250), 16 advance (/2 - 64); O2 = table 0x20 byte 0 x 5/256 V (FF = sensor still cold).

CellStats spreads each sample over the 4 cells around it with the same bilinear weights the ECU is assumed to
interpolate with, so "time in cell" shows which cells really feed the fuel and ignition the engine got.
"""

from __future__ import annotations

import csv
import queue
import threading
import time
from dataclasses import dataclass

from honda_dlc import DlcError, Ecu, open_link

O2_TABLE, O2_EVERY = 0x20, 3


@dataclass
class Sample:
    t: float                  # seconds since the source started (log: elapsed_s)
    rpm: int
    tps: float                # throttle %
    kpa: int                  # manifold pressure
    inj: float                # injector time, ms
    adv: float                # ignition advance, degrees
    o2: float | None = None   # narrowband O2 volts; None when not read this sample or still cold
    mark: str = ""

    def axis_value(self, key: str) -> float | None:
        return {"rpm": self.rpm, "tps": self.tps, "map": self.kpa}.get(key)


def decode(t: float, raw11: bytes, raw20: bytes | None = None, mark: str = "") -> Sample | None:
    if len(raw11) < 17:
        return None
    o2 = None
    if raw20 and raw20[0] != 0xFF:
        o2 = raw20[0] * 5 / 256
    return Sample(t, raw11[0] << 8 | raw11[1], raw11[3] / 1.6, raw11[9], (raw11[14] << 8 | raw11[15]) / 250,
                  raw11[16] / 2 - 64, o2, mark)


def axis_pos(points: list[float], v: float) -> float:
    """Fractional index of v on an ascending axis (2.5 = halfway between points 2 and 3), clamped to the ends."""
    if v <= points[0]:
        return 0.0
    for i in range(len(points) - 1):
        a, b = points[i], points[i + 1]
        if v <= b:
            return i + (v - a) / (b - a) if b > a else float(i)
    return float(len(points) - 1)


def weights(rpos: float, cpos: float, nrows: int, ncols: int) -> list[tuple[int, int, float]]:
    """The (row, col, weight) of the up-to-4 cells around a fractional position (bilinear)."""
    r0, c0 = int(rpos), int(cpos)
    fr, fc = rpos - r0, cpos - c0
    out = []
    for r, wr in ((r0, 1 - fr), (r0 + 1, fr)):
        for c, wc in ((c0, 1 - fc), (c0 + 1, fc)):
            w = wr * wc
            if w > 1e-6 and r < nrows and c < ncols:
                out.append((r, c, w))
    return out


class CellStats:
    """Per-cell totals for one pair of axes. Weighted by sample time share (bilinear)."""

    def __init__(self, row_points: list[float], col_points: list[float], row_key: str, col_key: str):
        self.rp, self.cp, self.rk, self.ck = row_points, col_points, row_key, col_key
        self.n, self.m = len(row_points), len(col_points)
        self.w = [[0.0] * self.m for _ in range(self.n)]      # time weight (samples)
        self.o2w = [[0.0] * self.m for _ in range(self.n)]    # weight of samples with an O2 reading
        self.o2s = [[0.0] * self.m for _ in range(self.n)]    # weighted O2 sum
        self.injs = [[0.0] * self.m for _ in range(self.n)]   # weighted injector ms sum
        self.total = 0

    def pos(self, s: Sample) -> tuple[float, float] | None:
        rv, cv = s.axis_value(self.rk), s.axis_value(self.ck)
        if rv is None or cv is None:
            return None
        return axis_pos(self.rp, rv), axis_pos(self.cp, cv)

    def add(self, s: Sample):
        if s.rpm < 300:          # engine not running: nothing to learn about the maps
            return
        p = self.pos(s)
        if p is None:
            return
        self.total += 1
        for r, c, w in weights(p[0], p[1], self.n, self.m):
            self.w[r][c] += w
            self.injs[r][c] += w * s.inj
            if s.o2 is not None:
                self.o2w[r][c] += w
                self.o2s[r][c] += w * s.o2

    def o2(self, r: int, c: int) -> float | None:
        return self.o2s[r][c] / self.o2w[r][c] if self.o2w[r][c] > 0.5 else None

    def inj(self, r: int, c: int) -> float | None:
        return self.injs[r][c] / self.w[r][c] if self.w[r][c] > 0.5 else None

    def max_w(self) -> float:
        return max(max(row) for row in self.w) or 1.0


# ---------------------------------------------------------------- sources

class Source:
    def __init__(self):
        self.q: queue.Queue = queue.Queue()
        self.status = "starting"
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self._stop.set()

    def stopped(self) -> bool:
        return self._stop.is_set()

    def _run(self):
        raise NotImplementedError


class EcuSource(Source):
    """Polls the ECU through the bridge. Reconnects (wake-up) after errors, e.g. key off and on again."""

    def __init__(self, port: str):
        super().__init__()
        self.port = port

    def _run(self):
        try:
            link = open_link(self.port)
        except Exception as e:  # noqa: BLE001 (any open failure is shown to the user)
            self.status = f"can't open {self.port}: {e}"
            return
        ecu, t0, k, awake = Ecu(link), time.time(), 0, False
        try:
            while not self.stopped():
                if not awake:
                    try:
                        ecu.connect()
                        awake = True
                        self.status = f"{self.port}: ECU connected"
                    except DlcError as e:
                        self.status = f"{self.port}: waiting for the ECU (key on?) {e}"
                        time.sleep(1.0)
                        continue
                try:
                    raw11 = ecu.read_table(0x11)
                    raw20 = None
                    if k % O2_EVERY == 0:
                        try:
                            raw20 = ecu.read_table(O2_TABLE)
                        except DlcError:
                            pass
                    k += 1
                    s = decode(time.time() - t0, raw11, raw20)
                    if s:
                        self.q.put(s)
                    if self.port.lower() == "demo":
                        time.sleep(0.08)   # the pretend ECU has no K-line delay: pace it like the real ~12/s

                except DlcError as e:
                    awake = False
                    self.status = f"{self.port}: lost the ECU ({e}), reconnecting"
        finally:
            link.close()
            if not self.status.startswith("can't"):
                self.status = f"{self.port}: stopped"


def read_log(path: str) -> list[Sample]:
    """All samples of a scanner log: SD live.csv (raw_11, raw_20 columns) or the phone CSV (raw_11)."""
    out = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            try:
                raw11 = bytes.fromhex(row.get("raw_11") or "")
                raw20 = bytes.fromhex(row.get("raw_20") or "")
                t = float(row.get("elapsed_s") or 0)
            except ValueError:
                continue
            s = decode(t, raw11, raw20 or None, row.get("mark") or "")
            if s:
                out.append(s)
    return out


class ReplaySource(Source):
    """Plays a log back. speed 0 = everything at once (for whole-ride cell statistics)."""

    def __init__(self, path: str, speed: float = 1.0):
        super().__init__()
        self.path, self.speed = path, speed

    def _run(self):
        try:
            samples = read_log(self.path)
        except OSError as e:
            self.status = f"can't read {self.path}: {e}"
            return
        if not samples:
            self.status = "no samples in that file (needs elapsed_s and raw_11 columns)"
            return
        name = self.path.replace("\\", "/").split("/")[-1]
        start, t_first = time.time(), samples[0].t
        for i, s in enumerate(samples):
            if self.stopped():
                self.status = f"replay stopped at {s.t:.1f} s"
                return
            if self.speed > 0:
                wait = (s.t - t_first) / self.speed - (time.time() - start)
                if wait > 0:
                    time.sleep(min(wait, 1.0))
                    while not self.stopped() and (s.t - t_first) / self.speed > time.time() - start:
                        time.sleep(0.02)
                self.status = f"replaying {name}: {s.t:.1f} / {samples[-1].t:.1f} s"
            self.q.put(s)
        self.status = f"replayed {name}: {len(samples)} samples, {samples[-1].t:.0f} s"
