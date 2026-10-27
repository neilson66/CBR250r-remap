"""CBR250R map editor: view and edit fuel/ignition tables in a saved ECU image (.bin). Never talks to the ECU.

  python pc/map_editor.py [image.bin]

- Open a backup (backups/.../flash.bin) or a stock image (reference/stock_bins/...).
- Pick a table on the left: heat-map grid, rpm down the side, throttle or MAP across the top.
- Click or drag to select cells; type a value and Enter, or use -5% / -1% / +1% / +5% (keys - and +).
  Ctrl+Z undoes. Changed cells are drawn bold with a black border.
- Compare… loads a second image: cells that differ get a purple corner.
- Explorer: look at any address as a grid (read only) to hunt for more tables.
- Save as… writes a new file with the checksum fixed, plus <file>.changes.json listing every change.
  It refuses to overwrite the opened file or anything in backups/ or reference/.
- Off-road options are listed but stay disabled until their flags are found in the ROM (see the definition file).
"""

from __future__ import annotations

import os
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from rom import PROJECT, Rom, RomError, SIZES, num

CELL_W, CELL_H, HEAD_W, HEAD_H = 46, 19, 64, 34
CONF_TAG = {"confirmed": "✔", "likely": "~", "guess": "?"}


def heat(t: float) -> str:
    """0..1 → blue, cyan, green, yellow, red."""
    stops = [(0.0, (70, 110, 220)), (0.25, (80, 190, 230)), (0.5, (110, 200, 110)), (0.75, (240, 220, 80)),
             (1.0, (230, 80, 60))]
    t = max(0.0, min(1.0, t))
    for (a, ca), (b, cb) in zip(stops, stops[1:]):
        if t <= b:
            f = (t - a) / (b - a)
            c = [int(ca[i] + (cb[i] - ca[i]) * f) for i in range(3)]
            return "#%02x%02x%02x" % tuple(c)
    return "#e6503c"


class TableView:
    """A defined, editable table."""

    def __init__(self, table):
        self.t = table
        self.nrows, self.ncols = table.nrows, table.ncols
        self.editable = True
        self.title = table.name
        self.row_labels = table.rows_axis.labels()
        self.col_labels = table.cols_axis.labels()
        self.row_unit, self.col_unit = table.rows_axis.unit, table.cols_axis.unit
        g = [table.get_in(table.rom.original, r, c) for r in range(self.nrows) for c in range(self.ncols)]
        self.lo, self.hi = min(g), max(g)

    def get(self, r, c):
        return self.t.get(r, c)

    def orig(self, r, c):
        return self.t.get_in(self.t.rom.original, r, c)

    def get_in(self, data, r, c):
        return self.t.get_in(data, r, c)

    def addr(self, r, c):
        return self.t.cell_addr(r, c)


class RawView:
    """Explorer: any address shown as a grid, read only."""

    def __init__(self, rom: Rom, addr: int, width: int, rows: int, typ: str):
        self.rom, self.addr0, self.ncols, self.nrows, self.typ = rom, addr, width, rows, typ
        self.size = SIZES[typ]
        self.editable = False
        self.title = f"Explorer {addr:04X}, {width} wide, {typ}"
        self.row_labels = [f"{addr + r * width * self.size:04X}" for r in range(rows)]
        self.col_labels = [f"+{c * self.size:X}" for c in range(width)]
        self.row_unit, self.col_unit = "address", "offset"
        vals = [self.get(r, c) for r in range(rows) for c in range(width)]
        self.lo, self.hi = (min(vals), max(vals)) if vals else (0, 1)

    def addr(self, r, c):
        return self.addr0 + (r * self.ncols + c) * self.size

    def get_in(self, data, r, c):
        a = self.addr(r, c)
        return Rom.read_from(data, a, self.typ) if a + self.size <= len(data) else 0

    def get(self, r, c):
        return self.get_in(self.rom.data, r, c)

    def orig(self, r, c):
        return self.get_in(self.rom.original, r, c)


class App(tk.Tk):
    def __init__(self, path=None):
        super().__init__()
        self.title("CBR250R map editor")
        self.geometry("1440x800")
        self.rom: Rom | None = None
        self.compare: bytes | None = None
        self.view = None
        self.sel = None            # (r0, c0, r1, c1)
        self.anchor = None
        self._build()
        if path:
            self.open(path)

    # ---------------------------------------------------------------- layout
    def _build(self):
        bar = ttk.Frame(self, padding=4)
        bar.pack(side="top", fill="x")
        for text, cmd in (("Open…", self.ask_open), ("Compare…", self.ask_compare), ("Save as…", self.ask_save),
                          ("Undo", self.do_undo)):
            ttk.Button(bar, text=text, command=cmd).pack(side="left", padx=2)
        self.info = ttk.Label(bar, text="Open an image (.bin) to start", font=("Segoe UI", 10))
        self.info.pack(side="left", padx=12)

        main = ttk.Frame(self)
        main.pack(fill="both", expand=True)
        left = ttk.Frame(main, padding=4, width=300)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)

        ttk.Label(left, text="Tables  (~ likely, ? guess)", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.tlist = tk.Listbox(left, height=8, exportselection=False, font=("Segoe UI", 10))
        self.tlist.pack(fill="x")
        self.tlist.bind("<<ListboxSelect>>", lambda e: self.pick_table())

        ex = ttk.LabelFrame(left, text="Explorer (read only)", padding=4)
        ex.pack(fill="x", pady=6)
        self.ex_addr, self.ex_w, self.ex_n, self.ex_t = (tk.StringVar(value=v) for v in ("A000", "22", "29", "u8"))
        for i, (lab, var) in enumerate((("address", self.ex_addr), ("width", self.ex_w), ("rows", self.ex_n))):
            ttk.Label(ex, text=lab).grid(row=0, column=2 * i, sticky="w")
            ttk.Entry(ex, textvariable=var, width=6).grid(row=0, column=2 * i + 1, padx=2)
        ttk.Combobox(ex, textvariable=self.ex_t, values=["u8", "u16be"], width=6, state="readonly").grid(
            row=1, column=0, columnspan=2, pady=3, sticky="w")
        ttk.Button(ex, text="Show", command=self.show_raw).grid(row=1, column=2, columnspan=2, sticky="w")

        op = ttk.LabelFrame(left, text="Off-road options", padding=4)
        op.pack(fill="x", pady=6)
        self.opt_frame = op
        ttk.Label(op, text="(open an image)").pack(anchor="w")

        ttk.Label(left, text="Evidence", font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(6, 0))
        self.evidence = tk.Text(left, height=12, wrap="word", font=("Segoe UI", 9), relief="flat", bg="#f4f4f4")
        self.evidence.pack(fill="both", expand=True)

        right = ttk.Frame(main)
        right.pack(side="left", fill="both", expand=True)
        self.heading = ttk.Label(right, text="", font=("Segoe UI", 11, "bold"), padding=(4, 4))
        self.heading.pack(anchor="w")
        holder = ttk.Frame(right)
        holder.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(holder, bg="white", highlightthickness=0)
        ys = ttk.Scrollbar(holder, orient="vertical", command=self.canvas.yview)
        xs = ttk.Scrollbar(holder, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        ys.pack(side="right", fill="y")
        xs.pack(side="bottom", fill="x")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Button-1>", self.on_click)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<Motion>", self.on_hover)

        edit = ttk.Frame(right, padding=4)
        edit.pack(fill="x")
        self.sel_label = ttk.Label(edit, text="", width=48)
        self.sel_label.pack(side="left")
        ttk.Label(edit, text="value").pack(side="left")
        self.value = tk.StringVar()
        ent = ttk.Entry(edit, textvariable=self.value, width=8)
        ent.pack(side="left", padx=3)
        ent.bind("<Return>", lambda e: self.set_value())
        ttk.Button(edit, text="Set", command=self.set_value).pack(side="left")
        for pct in (-5, -1, 1, 5):
            ttk.Button(edit, text=f"{pct:+d}%", width=5, command=lambda p=pct: self.scale(p)).pack(side="left", padx=1)
        ttk.Button(edit, text="Revert cells", command=self.revert).pack(side="left", padx=8)
        self.hover = ttk.Label(right, text="", padding=(4, 2), foreground="#444")
        self.hover.pack(anchor="w")

        self.bind_all("<Control-z>", lambda e: self.do_undo())
        self.bind("<KeyPress-plus>", lambda e: self.scale(1))
        self.bind("<KeyPress-equal>", lambda e: self.scale(1))
        self.bind("<KeyPress-minus>", lambda e: self.scale(-1))

    # ---------------------------------------------------------------- files
    def ask_open(self):
        p = filedialog.askopenfilename(initialdir=os.path.join(PROJECT, "backups") if os.path.isdir(
            os.path.join(PROJECT, "backups")) else PROJECT, filetypes=[("ECU image", "*.bin"), ("All", "*.*")])
        if p:
            self.open(p)

    def open(self, path):
        try:
            self.rom = Rom.load(path)
        except (RomError, OSError) as e:
            messagebox.showerror("Open", str(e))
            return
        self.compare = None
        self.tlist.delete(0, "end")
        for t in self.rom.tables:
            self.tlist.insert("end", f"{CONF_TAG.get(t.confidence, '?')} {t.name}")
        self._options()
        self.tlist.selection_set(0)
        self.pick_table()

    def _options(self):
        for w in self.opt_frame.winfo_children():
            w.destroy()
        for o in self.rom.defn.get("options", []):
            found = o.get("status") == "found"
            cb = ttk.Checkbutton(self.opt_frame, text=o["name"], state="normal" if found else "disabled")
            cb.pack(anchor="w")
            if not found:
                ttk.Label(self.opt_frame, text="   not found in the ROM yet: " + o.get("note", ""),
                          foreground="#888", wraplength=270, font=("Segoe UI", 8)).pack(anchor="w")

    def ask_compare(self):
        if not self.rom:
            return
        p = filedialog.askopenfilename(initialdir=os.path.dirname(self.rom.path),
                                       filetypes=[("ECU image", "*.bin"), ("All", "*.*")])
        if not p:
            return
        data = open(p, "rb").read()
        if len(data) != len(self.rom.data):
            messagebox.showerror("Compare", f"different size ({len(data)} vs {len(self.rom.data)} bytes)")
            return
        self.compare = data
        self.compare_name = os.path.basename(p)
        self.redraw()

    def ask_save(self):
        if not self.rom:
            return
        base = os.path.splitext(os.path.basename(self.rom.path))[0]
        p = filedialog.asksaveasfilename(initialdir=PROJECT, initialfile=base + "_tune.bin", defaultextension=".bin",
                                         filetypes=[("ECU image", "*.bin")])
        if not p:
            return
        try:
            info = self.rom.save_as(p)
        except RomError as e:
            messagebox.showerror("Save", str(e))
            return
        messagebox.showinfo("Saved", f"{os.path.basename(p)}\n{len(info['changes'])} cells changed, checksum byte "
                                     f"{info['checksum_byte']}\nChanges listed in {os.path.basename(p)}.changes.json\n\n"
                                     "Nothing has been written to the ECU.")

    # ---------------------------------------------------------------- views
    def pick_table(self):
        if not self.rom or not self.tlist.curselection():
            return
        t = self.rom.tables[self.tlist.curselection()[0]]
        self.view = TableView(t)
        self.sel = None
        self.evidence.delete("1.0", "end")
        self.evidence.insert("end", f"{t.name}\naddress {t.addr:04X}-{t.end() - 1:04X}, {t.nrows} x {t.ncols} "
                                    f"{t.type}\nconfidence: {t.confidence}\nunit: {t.unit}\n\n{t.evidence}\n\n"
                                    f"rows: {t.rows_axis.unit} ({t.rows_axis.d.get('confidence')}): "
                                    f"{t.rows_axis.d.get('evidence', '')}\n\ncolumns: {t.cols_axis.unit} "
                                    f"({t.cols_axis.d.get('confidence')}): {t.cols_axis.d.get('evidence', '')}")
        self.redraw()

    def show_raw(self):
        if not self.rom:
            return
        try:
            v = RawView(self.rom, num("0x" + self.ex_addr.get().strip().replace("0x", "")), int(self.ex_w.get()),
                        int(self.ex_n.get()), self.ex_t.get())
        except ValueError:
            messagebox.showerror("Explorer", "address in hex, width and rows as numbers")
            return
        self.view, self.sel = v, None
        self.tlist.selection_clear(0, "end")
        self.redraw()

    def redraw(self):
        c = self.canvas
        c.delete("all")
        v = self.view
        if not v or not self.rom:
            return
        changed = len(self.rom.changed_bytes())
        self.info.config(text=f"{os.path.basename(self.rom.path)}   ECM ID {self.rom.ecmid()} ({self.rom.part_number()})"
                              f"   checksum {'OK' if self.rom.sum_ok(self.rom.original) else 'BAD in file'}"
                              f"   {changed} bytes changed"
                              + (f"   comparing with {self.compare_name}" if self.compare else ""))
        self.heading.config(text=f"{v.title}    rows: {v.row_unit}    columns: {v.col_unit}")
        span = max(1, v.hi - v.lo)
        c.create_text(HEAD_W / 2, HEAD_H / 2, text="rpm \\ col" if v.editable else "addr", font=("Segoe UI", 8))
        for j, lab in enumerate(v.col_labels):
            x = HEAD_W + j * CELL_W
            c.create_rectangle(x, 0, x + CELL_W, HEAD_H, fill="#e8e8e8", outline="#ccc")
            c.create_text(x + CELL_W / 2, HEAD_H / 2, text=lab, font=("Segoe UI", 8))
        for i, lab in enumerate(v.row_labels):
            y = HEAD_H + i * CELL_H
            c.create_rectangle(0, y, HEAD_W, y + CELL_H, fill="#e8e8e8", outline="#ccc")
            c.create_text(HEAD_W - 4, y + CELL_H / 2, text=lab, anchor="e", font=("Segoe UI", 8))
            for j in range(v.ncols):
                x = HEAD_W + j * CELL_W
                val, org = v.get(i, j), v.orig(i, j)
                c.create_rectangle(x, y, x + CELL_W, y + CELL_H, fill=heat((val - v.lo) / span), outline="#ffffff")
                bold = val != org
                c.create_text(x + CELL_W / 2, y + CELL_H / 2, text=str(val),
                              font=("Segoe UI", 8, "bold" if bold else "normal"))
                if bold:
                    c.create_rectangle(x + 1, y + 1, x + CELL_W - 1, y + CELL_H - 1, outline="black", width=2)
                if self.compare is not None and v.get_in(self.compare, i, j) != val:
                    c.create_polygon(x + CELL_W - 10, y, x + CELL_W, y, x + CELL_W, y + 10, fill="#8e24aa")
        if self.sel:
            r0, c0, r1, c1 = self.sel
            c.create_rectangle(HEAD_W + c0 * CELL_W, HEAD_H + r0 * CELL_H, HEAD_W + (c1 + 1) * CELL_W,
                               HEAD_H + (r1 + 1) * CELL_H, outline="#1565c0", width=3)
        c.configure(scrollregion=(0, 0, HEAD_W + v.ncols * CELL_W + 4, HEAD_H + v.nrows * CELL_H + 4))
        self._sel_text()

    # ---------------------------------------------------------------- mouse / selection
    def cell_at(self, e):
        x, y = self.canvas.canvasx(e.x), self.canvas.canvasy(e.y)
        if not self.view or x < HEAD_W or y < HEAD_H:
            return None
        r, c = int((y - HEAD_H) // CELL_H), int((x - HEAD_W) // CELL_W)
        if r >= self.view.nrows or c >= self.view.ncols:
            return None
        return r, c

    def on_click(self, e):
        rc = self.cell_at(e)
        if not rc:
            return
        if e.state & 1 and self.anchor:   # shift: extend
            self._select(self.anchor, rc)
        else:
            self.anchor = rc
            self._select(rc, rc)
            self.value.set(str(self.view.get(*rc)))
        self.focus_set()

    def on_drag(self, e):
        rc = self.cell_at(e)
        if rc and self.anchor:
            self._select(self.anchor, rc)

    def _select(self, a, b):
        self.sel = (min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))
        self.redraw()

    def cells(self):
        if not self.sel:
            return []
        r0, c0, r1, c1 = self.sel
        return [(r, c) for r in range(r0, r1 + 1) for c in range(c0, c1 + 1)]

    def _sel_text(self):
        v, cells = self.view, self.cells()
        if not cells:
            self.sel_label.config(text="select cells (click, drag, shift-click)")
            return
        vals = [v.get(r, c) for r, c in cells]
        r0, c0, r1, c1 = self.sel
        self.sel_label.config(text=f"{len(cells)} cells, rpm {v.row_labels[r0]}-{v.row_labels[r1]}, "
                                   f"col {v.col_labels[c0]}-{v.col_labels[c1]}, avg {sum(vals) / len(vals):.1f}")

    def on_hover(self, e):
        rc = self.cell_at(e)
        if not rc:
            return
        v = self.view
        r, c = rc
        val, org = v.get(r, c), v.orig(r, c)
        txt = (f"{v.row_unit} {v.row_labels[r]}, {v.col_unit} {v.col_labels[c]}: {val}   (stock {org}"
               + (f", {100 * (val - org) / org:+.1f}%" if org and val != org else "") + f")   address {v.addr(r, c):04X}")
        if self.compare is not None:
            txt += f"   {self.compare_name}: {v.get_in(self.compare, r, c)}"
        self.hover.config(text=txt)

    # ---------------------------------------------------------------- editing
    def _edit(self, fn):
        v = self.view
        if not v or not v.editable:
            messagebox.showinfo("Edit", "Explorer is read only. Pick a table on the left to edit it.")
            return
        cells = self.cells()
        if not cells:
            return
        self.rom.begin()
        try:
            for r, c in cells:
                v.t.set(r, c, fn(v.get(r, c), v.orig(r, c)))
        except RomError as e:
            messagebox.showerror("Edit", str(e))
        self.rom.commit()
        self.redraw()

    def set_value(self):
        try:
            x = float(self.value.get())
        except ValueError:
            return
        self._edit(lambda cur, org: x)

    def scale(self, pct):
        self._edit(lambda cur, org: cur * (1 + pct / 100) if cur * abs(pct) >= 100 else cur + (1 if pct > 0 else -1))

    def revert(self):
        self._edit(lambda cur, org: org)

    def do_undo(self):
        if self.rom and self.rom.undo_last():
            self.redraw()


if __name__ == "__main__":
    App(sys.argv[1] if len(sys.argv) > 1 else None).mainloop()
