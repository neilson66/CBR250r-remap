"""ROM image + map definition: read and edit tables in a saved CBR250R ECU image. Works on files only.

  rom = Rom.load("backups/.../flash.bin")     # finds the definition by the Keihin code
  t = rom.table("fuel_a"); t.get(r, c); t.set(r, c, value)
  rom.save_as("my_tune.bin")                   # fixes the checksum byte, refuses to overwrite a backup

Definitions live in pc/definitions/<keihin code>.json; every table there carries its evidence and confidence.
"""

from __future__ import annotations

import hashlib
import json
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DEF_DIR = os.path.join(HERE, "definitions")
PROJECT = os.path.dirname(HERE)
PROTECTED_DIRS = [os.path.join(PROJECT, "backups"), os.path.join(PROJECT, "reference")]

SIZES = {"u8": 1, "u16be": 2}


def num(v) -> int:
    return int(v, 0) if isinstance(v, str) else int(v)


class RomError(Exception):
    pass


class Axis:
    def __init__(self, rom: "Rom", key: str, d: dict):
        self.rom, self.key, self.d = rom, key, d
        self.addr, self.count, self.type = num(d["addr"]), d["count"], d["type"]
        self.unit, self.scale, self.digits = d.get("unit", ""), float(d.get("scale", 1)), d.get("digits", 0)

    def raw(self) -> list[int]:
        return [self.rom.read(self.addr + i * SIZES[self.type], self.type) for i in range(self.count)]

    def values(self) -> list[float]:
        return [r * self.scale for r in self.raw()]

    def labels(self) -> list[str]:
        return [f"{v:.{self.digits}f}" for v in self.values()]


class Table:
    """A 2-D map: rows and columns follow axes, every cell the same type and scale."""
    kind = "map"

    def __init__(self, rom: "Rom", d: dict):
        self.rom, self.d = rom, d
        self.id, self.name, self.addr, self.type = d["id"], d["name"], num(d["addr"]), d["type"]
        self.rows_axis = rom.axes[d["rows"]]
        self.col_choices = [d["cols"]] + list(d.get("cols_alt", []))
        self.cols_axis = rom.axes[d["cols"]]
        self.nrows, self.ncols = self.rows_axis.count, self.cols_axis.count
        self.scale, self.offset = float(d.get("scale", 1)), float(d.get("offset", 0))
        self.unit, self.digits = d.get("unit", ""), d.get("digits", 0)
        self.confidence, self.evidence = d.get("confidence", "guess"), d.get("evidence", "")
        self.readonly = d.get("readonly", False)
        self.size = SIZES[self.type]

    def set_cols(self, key: str):
        axis = self.rom.axes[key]
        if axis.count != self.ncols:
            raise RomError(f"axis {key} has {axis.count} points, the table {self.ncols} columns")
        self.cols_axis = axis

    def row_labels(self):
        return self.rows_axis.labels()

    def col_labels(self):
        return self.cols_axis.labels()

    @property
    def row_unit(self):
        return self.rows_axis.unit

    @property
    def col_unit(self):
        return self.cols_axis.unit

    def cell_type(self, r: int, c: int) -> str:
        return self.type

    def cell_addr(self, r: int, c: int) -> int:
        return self.addr + (r * self.ncols + c) * self.size

    def end(self) -> int:
        return self.cell_addr(self.nrows - 1, self.ncols - 1) + self.size

    def cell_scale(self, r: int, c: int):
        return self.scale, self.offset, self.digits, self.unit

    def get_in(self, data, r: int, c: int) -> int:
        return Rom.read_from(data, self.cell_addr(r, c), self.cell_type(r, c))

    def get(self, r: int, c: int) -> int:
        return self.get_in(self.rom.data, r, c)

    def orig(self, r: int, c: int) -> int:
        return self.get_in(self.rom.original, r, c)

    def value(self, r: int, c: int, raw: int) -> float:
        k, o, _, _ = self.cell_scale(r, c)
        return raw * k + o

    def fmt(self, r: int, c: int, raw: int) -> str:
        return f"{self.value(r, c, raw):.{self.cell_scale(r, c)[2]}f}"

    def to_raw(self, r: int, c: int, x: float) -> int:
        k, o, _, _ = self.cell_scale(r, c)
        return int(round((x - o) / k))

    def set(self, r: int, c: int, raw: int):
        top = (1 << (8 * SIZES[self.cell_type(r, c)])) - 1
        raw = max(0, min(top, int(round(raw))))
        self.rom.write(self.cell_addr(r, c), self.cell_type(r, c), raw)

    def grid(self) -> list[list[int]]:
        return [[self.get(r, c) for c in range(self.ncols)] for r in range(self.nrows)]


class ListTable(Table):
    """A list of entries (rows), each with named fields (columns) of their own type and scale."""
    kind = "list"

    def __init__(self, rom: "Rom", d: dict):
        self.rom, self.d = rom, d
        self.id, self.name, self.addr = d["id"], d["name"], num(d["addr"])
        self.nrows, self.stride, self.fields = d["count"], d["stride"], d["fields"]
        self.ncols = len(self.fields)
        self.col_choices = []
        self.unit, self.digits = "", 0
        self.confidence, self.evidence = d.get("confidence", "guess"), d.get("evidence", "")
        self.readonly = d.get("readonly", False)
        self.type = self.fields[0]["type"]

    def row_labels(self):
        return self.d.get("row_names") or [str(i) for i in range(self.nrows)]

    def col_labels(self):
        return [f["name"] + (f" ({f['unit']})" if f.get("unit") else "") for f in self.fields]

    row_unit = "entry"
    col_unit = "field"

    def cell_type(self, r, c):
        return self.fields[c]["type"]

    def cell_addr(self, r, c):
        return self.addr + r * self.stride + self.fields[c]["offset"]

    def end(self):
        f = self.fields[-1]
        return self.addr + (self.nrows - 1) * self.stride + f["offset"] + SIZES[f["type"]]

    def cell_scale(self, r, c):
        f = self.fields[c]
        return float(f.get("scale", 1)), float(f.get("offset_value", 0)), f.get("digits", 0), f.get("unit", "")


class Rom:
    def __init__(self, data: bytes, path: str = "", definition: dict | None = None):
        self.data = bytearray(data)
        self.original = bytes(data)
        self.path = path
        self.defn = definition or {}
        self.axes = {k: Axis(self, k, v) for k, v in self.defn.get("axes", {}).items()}
        self.tables = [ListTable(self, t) if t.get("kind") == "list" else Table(self, t)
                       for t in self.defn.get("tables", [])]
        self.undo: list[list[tuple[int, int, int]]] = []   # groups of (addr, old byte, new byte)
        self._group: list[tuple[int, int, int]] | None = None

    # ---------------------------------------------------------------- loading
    @staticmethod
    def find_definition(data: bytes) -> dict | None:
        for name in sorted(os.listdir(DEF_DIR)):
            if not name.endswith(".json"):
                continue
            with open(os.path.join(DEF_DIR, name), encoding="utf-8") as fh:
                d = json.load(fh)
            r = d["rom"]
            a = num(r["keihin_addr"])
            code = r["keihin_code"].encode()
            if len(data) == r["size"] and data[a:a + len(code)] == code:
                return d
        return None

    @classmethod
    def load(cls, path: str) -> "Rom":
        with open(path, "rb") as fh:
            data = fh.read()
        defn = cls.find_definition(data)
        if defn is None:
            raise RomError(f"{os.path.basename(path)}: no definition matches (size {len(data)}, "
                           "Keihin code not known). Only KYJA203 images are described so far.")
        return cls(data, path, defn)

    # ---------------------------------------------------------------- bytes
    @staticmethod
    def read_from(data, addr: int, typ: str) -> int:
        return int.from_bytes(data[addr:addr + SIZES[typ]], "big")

    def read(self, addr: int, typ: str = "u8") -> int:
        return self.read_from(self.data, addr, typ)

    def write(self, addr: int, typ: str, value: int):
        new = value.to_bytes(SIZES[typ], "big")
        if not any(t.addr <= addr and addr + len(new) <= t.end() for t in self.tables):
            raise RomError(f"refusing to write {addr:04X}: not inside a defined table")
        group = self._group if self._group is not None else []
        for i, byte in enumerate(new):
            old = self.data[addr + i]
            if old != byte:
                group.append((addr + i, old, byte))
                self.data[addr + i] = byte
        if self._group is None and group:
            self.undo.append(group)

    def begin(self):
        """Group the following writes into one undo step."""
        self._group = []

    def commit(self):
        if self._group:
            self.undo.append(self._group)
        self._group = None

    def undo_last(self) -> bool:
        if not self.undo:
            return False
        for addr, old, _ in reversed(self.undo.pop()):
            self.data[addr] = old
        return True

    def table(self, tid: str) -> Table:
        return next(t for t in self.tables if t.id == tid)

    # ---------------------------------------------------------------- info / checksum
    def checksum_addr(self) -> int:
        return num(self.defn["rom"]["checksum_addr"])

    def sum_ok(self, data=None) -> bool:
        return sum(self.data if data is None else data) & 0xFF == 0

    def fixed_checksum(self) -> bytes:
        out = bytearray(self.data)
        a = self.checksum_addr()
        out[a] = 0
        out[a] = (-sum(out)) & 0xFF
        return bytes(out)

    def ecmid(self) -> str:
        a = num(self.defn["rom"]["ecmid_addr"])
        return " ".join(f"{x:02X}" for x in self.data[a:a + 5])

    def part_number(self) -> str:
        return self.defn["rom"]["ecmids"].get(self.ecmid(), "unknown part")

    def changed_bytes(self) -> list[int]:
        return [i for i in range(len(self.data)) if self.data[i] != self.original[i]]

    # ---------------------------------------------------------------- off-road options
    def option_cells(self, opt: dict) -> list[tuple[Table, int, int, int]]:
        """[(table, row, col, target raw), ...] the option changes; [] if it changes nothing in the ECU."""
        out = []
        for e in opt.get("edits", []):
            t = self.table(e["table"])
            out += [(t, r, e["field"], e["raw"]) for r in range(t.nrows)]
        if opt.get("dtc_off"):
            t = self.table("dtc_list")
            for code in opt["dtc_off"]:
                main, sub = (int(x) for x in code.split("-"))
                rows = [r for r in range(t.nrows) if (t.orig(r, 0), t.orig(r, 1)) == (main, sub)]
                if not rows:
                    raise RomError(f"fault code {code} not in this image's list")
                out += [(t, r, 0, 0) for r in rows] + [(t, r, 1, 0) for r in rows]
        return out

    def option_on(self, opt: dict) -> bool:
        cells = self.option_cells(opt)
        return bool(cells) and all(t.get(r, c) == raw for t, r, c, raw in cells)

    def set_option(self, opt: dict, on: bool):
        self.begin()
        for t, r, c, raw in self.option_cells(opt):
            t.set(r, c, raw if on else t.orig(r, c))
        self.commit()

    # ---------------------------------------------------------------- saving
    def save_as(self, path: str) -> dict:
        path = os.path.abspath(path)
        for d in PROTECTED_DIRS:
            try:
                inside = os.path.commonpath([path, d]) == d
            except ValueError:  # different drive
                inside = False
            if inside:
                raise RomError("won't save into backups/ or reference/: those hold the untouched images")
        if self.path and os.path.abspath(self.path) == path:
            raise RomError("won't overwrite the file you opened: save the tune under a new name")
        out = self.fixed_checksum()
        if not self.sum_ok(out):
            raise RomError("checksum fix failed")
        with open(path, "wb") as fh:
            fh.write(out)
        changes = []
        for t in self.tables:
            for r in range(t.nrows):
                for c in range(t.ncols):
                    old, new = t.get_in(self.original, r, c), t.get_in(out, r, c)
                    if old != new:
                        changes.append({"table": t.id, "row": t.row_labels()[r], "col": t.col_labels()[c],
                                        "old": t.fmt(r, c, old), "new": t.fmt(r, c, new), "old_raw": old,
                                        "new_raw": new})
        info = {"saved": time.strftime("%Y-%m-%d %H:%M:%S"), "from": self.path,
                "from_sha256": hashlib.sha256(self.original).hexdigest(),
                "sha256": hashlib.sha256(out).hexdigest(), "checksum_byte": f"{out[self.checksum_addr()]:02X}",
                "changes": changes}
        with open(path + ".changes.json", "w", encoding="utf-8") as fh:
            json.dump(info, fh, indent=1)
        return info
