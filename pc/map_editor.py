"""CBR250R map editor: view and edit fuel/ignition tables in a saved ECU image (.bin). Never talks to the ECU.

  python pc/map_editor.py [image.bin]

- Open a backup (backups/.../flash.bin) or a stock image (reference/stock_bins/...).
- Pick a table on the left: heat-map grid in real units (ms of injector time, degrees of advance, rpm, %).
  Maps have rpm down the side and throttle (or MAP, see "columns") across the top.
- Click or drag to select cells; type a value in the table's units and Enter, or use -5% / -1% / +1% / +5% and
  -step / +step (one raw count). Keys: + and -. Ctrl+Z undoes. Changed cells are bold with a black border.
- Compare… loads a second image: cells that differ get a purple corner.
- Explorer: any address as a grid of raw numbers (read only), to hunt for more tables.
- Off-road options: tick to apply (O2 closed loop off, fault codes off); untick puts the stock values back.
- Save as… writes a new file with the checksum fixed, plus <file>.changes.json listing every change.
  It refuses to overwrite the opened file or anything in backups/ or reference/.
"""

from __future__ import annotations

import os
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from rom import PROJECT, Rom, RomError, SIZES, num

MAP_W, LIST_W, CELL_H, HEAD_W, HEAD_H = 46, 150, 19, 96, 34
CONF_TAG = {"confirmed": "✔", "likely": "~", "guess": "?"}


def heat(t: float) -> str:
    """0..1 → blue, cyan, green, yellow, red."""
    stops = [(0.0, (70, 110, 220)), (0.25, (80, 190, 230)), (0.5, (110, 200, 110)), (0.75, (240, 220, 80)),
             (1.0, (230, 80, 60))]
    t = max(0.0, min(1.0, t))
    for (a, ca), (b, cb) in zip(stops, stops[1:]):
        if t <= b:
            f = (t - a) / (b - a)
            return "#%02x%02x%02x" % tuple(int(ca[i] + (cb[i] - ca[i]) * f) for i in range(3))
    return "#e6503c"


class TableView:
    """A defined table (map or list), shown in its units."""

    def __init__(self, table):
        self.t = table
        self.nrows, self.ncols = table.nrows, table.ncols
        self.editable = not table.readonly
        self.cell_w = LIST_W if table.kind == "list" else MAP_W
        self.title = table.name + (f"  [{table.unit}]" if table.unit else "") + ("  (read only)" if table.readonly else "")
        self.refresh_labels()
        cols = range(self.ncols)
        if table.kind == "list":   # fields differ, so colour each column on its own
            self.lohi = {c: self._range([table.orig(r, c) for r in range(self.nrows)]) for c in cols}
        else:
            rng = self._range([table.orig(r, c) for r in range(self.nrows) for c in cols])
            self.lohi = {c: rng for c in cols}

    @staticmethod
    def _range(vals):
        return (min(vals), max(vals)) if vals else (0, 1)

    def refresh_labels(self):
        self.row_labels, self.col_labels = self.t.row_labels(), self.t.col_labels()
        self.row_unit, self.col_unit = self.t.row_unit, self.t.col_unit

    def get(self, r, c):
        return self.t.get(r, c)

    def orig(self, r, c):
        return self.t.orig(r, c)

    def get_in(self, data, r, c):
        return self.t.get_in(data, r, c)

    def addr(self, r, c):
        return self.t.cell_addr(r, c)

    def text(self, r, c, raw):
        return self.t.fmt(r, c, raw)


class RawView:
    """Explorer: any address shown as a grid of raw numbers, read only."""

    def __init__(self, rom: Rom, addr: int, width: int, rows: int, typ: str):
        self.rom, self.addr0, self.ncols, self.nrows, self.typ = rom, addr, width, rows, typ
        self.size = SIZES[typ]
        self.editable = False
        self.cell_w = MAP_W
        self.title = f"Explorer {addr:04X}, {width} wide, {typ} (raw numbers)"
        self.row_labels = [f"{addr + r * width * self.size:04X}" for r in range(rows)]
        self.col_labels = [f"+{c * self.size:X}" for c in range(width)]
        self.row_unit, self.col_unit = "address", "offset"
        vals = [self.get(r, c) for r in range(rows) for c in range(width)]
        rng = (min(vals), max(vals)) if vals else (0, 1)
        self.lohi = {c: rng for c in range(width)}

    def addr(self, r, c):
        return self.addr0 + (r * self.ncols + c) * self.size

    def get_in(self, data, r, c):
        a = self.addr(r, c)
        return Rom.read_from(data, a, self.typ) if a + self.size <= len(data) else 0

    def get(self, r, c):
        return self.get_in(self.rom.data, r, c)

    def orig(self, r, c):
        return self.get_in(self.rom.original, r, c)

    def text(self, r, c, raw):
        return str(raw)


class App(tk.Tk):
    def __init__(self, path=None):
        super().__init__()
        self.title("CBR250R map editor")
        self.geometry("1480x820")
        self.rom: Rom | None = None
        self.compare: bytes | None = None
        self.compare_name = ""
        self.view = None
        self.sel = None            # (r0, c0, r1, c1)
        self.anchor = None
        self.opt_vars = {}
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
        left = ttk.Frame(main, padding=4, width=310)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)

        ttk.Label(left, text="Tables  (~ likely, ? guess)", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.tlist = tk.Listbox(left, height=9, exportselection=False, font=("Segoe UI", 10))
        self.tlist.pack(fill="x")
        self.tlist.bind("<<ListboxSelect>>", lambda e: self.pick_table())

        op = ttk.LabelFrame(left, text="Off-road options (image only, not the ECU)", padding=4)
        op.pack(fill="x", pady=6)
        self.opt_frame = op
        ttk.Label(op, text="(open an image)").pack(anchor="w")

        ex = ttk.LabelFrame(left, text="Explorer (read only)", padding=4)
        ex.pack(fill="x", pady=4)
        self.ex_addr, self.ex_w, self.ex_n, self.ex_t = (tk.StringVar(value=v) for v in ("A000", "22", "29", "u8"))
        for i, (lab, var) in enumerate((("address", self.ex_addr), ("width", self.ex_w), ("rows", self.ex_n))):
            ttk.Label(ex, text=lab).grid(row=0, column=2 * i, sticky="w")
            ttk.Entry(ex, textvariable=var, width=6).grid(row=0, column=2 * i + 1, padx=2)
        ttk.Combobox(ex, textvariable=self.ex_t, values=["u8", "u16be"], width=6, state="readonly").grid(
            row=1, column=0, columnspan=2, pady=3, sticky="w")
        ttk.Button(ex, text="Show", command=self.show_raw).grid(row=1, column=2, columnspan=2, sticky="w")

        ttk.Label(left, text="Evidence", font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(6, 0))
        self.evidence = tk.Text(left, height=10, wrap="word", font=("Segoe UI", 9), relief="flat", bg="#f4f4f4")
        self.evidence.pack(fill="both", expand=True)

        right = ttk.Frame(main)
        right.pack(side="left", fill="both", expand=True)
        top = ttk.Frame(right)
        top.pack(fill="x")
        self.heading = ttk.Label(top, text="", font=("Segoe UI", 11, "bold"), padding=(4, 4))
        self.heading.pack(side="left")
        self.collabel = ttk.Label(top, text="columns:")
        self.colvar = tk.StringVar()
        self.colbox = ttk.Combobox(top, textvariable=self.colvar, state="readonly", width=8)
        self.colbox.bind("<<ComboboxSelected>>", lambda e: self.pick_cols())
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
        self.sel_label = ttk.Label(edit, text="", width=58)
        self.sel_label.pack(side="left")
        ttk.Label(edit, text="value").pack(side="left")
        self.value = tk.StringVar()
        ent = ttk.Entry(edit, textvariable=self.value, width=8)
        ent.pack(side="left", padx=3)
        ent.bind("<Return>", lambda e: self.set_value())
        ttk.Button(edit, text="Set", command=self.set_value).pack(side="left")
        for pct in (-5, -1, 1, 5):
            ttk.Button(edit, text=f"{pct:+d}%", width=5, command=lambda p=pct: self.scale(p)).pack(side="left", padx=1)
        for st in (-1, 1):
            ttk.Button(edit, text=("-" if st < 0 else "+") + "step", width=6,
                       command=lambda s=st: self.step(s)).pack(side="left", padx=1)
        ttk.Button(edit, text="Revert cells", command=self.revert).pack(side="left", padx=8)
        self.hover = ttk.Label(right, text="", padding=(4, 2), foreground="#444")
        self.hover.pack(anchor="w")

        self.bind_all("<Control-z>", lambda e: self.do_undo())
        self.bind("<KeyPress-plus>", lambda e: self.scale(1))
        self.bind("<KeyPress-equal>", lambda e: self.scale(1))
        self.bind("<KeyPress-minus>", lambda e: self.scale(-1))

    # ---------------------------------------------------------------- files
    def ask_open(self):
        start = os.path.join(PROJECT, "backups")
        p = filedialog.askopenfilename(initialdir=start if os.path.isdir(start) else PROJECT,
                                       filetypes=[("ECU image", "*.bin"), ("All", "*.*")])
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

    def ask_compare(self):
        if not self.rom:
            return
        p = filedialog.askopenfilename(initialdir=os.path.dirname(self.rom.path),
                                       filetypes=[("ECU image", "*.bin"), ("All", "*.*")])
        if p:
            self.set_compare(p)

    def set_compare(self, p):
        data = open(p, "rb").read()
        if len(data) != len(self.rom.data):
            messagebox.showerror("Compare", f"different size ({len(data)} vs {len(self.rom.data)} bytes)")
            return
        self.compare, self.compare_name = data, os.path.basename(p)
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

    # ---------------------------------------------------------------- off-road options
    def _options(self):
        for w in self.opt_frame.winfo_children():
            w.destroy()
        self.opt_vars = {}
        for o in self.rom.defn.get("options", []):
            status = o.get("status")
            if status == "found":
                var = tk.BooleanVar(value=self._opt_on(o))
                self.opt_vars[o["id"]] = var
                ttk.Checkbutton(self.opt_frame, text=o["name"], variable=var,
                                command=lambda opt=o, v=var: self.toggle_option(opt, v)).pack(anchor="w")
            else:
                mark = "✓ " if status == "no ECU change" else "✗ "
                ttk.Label(self.opt_frame, text=mark + o["name"]).pack(anchor="w")
            pre = "not found in the ROM yet: " if status == "not found" else ""
            ttk.Label(self.opt_frame, text="   " + pre + o.get("note", ""), foreground="#777", wraplength=285,
                      font=("Segoe UI", 8)).pack(anchor="w")

    def _opt_on(self, o):
        try:
            return self.rom.option_on(o)
        except RomError:
            return False

    def refresh_options(self):
        if not self.rom:
            return
        for o in self.rom.defn.get("options", []):
            if o["id"] in self.opt_vars:
                self.opt_vars[o["id"]].set(self._opt_on(o))

    def toggle_option(self, o, var, ask=True):
        on = var.get()
        if on and ask and not messagebox.askokcancel(o["name"], o.get("note", "") + "\n\nThis only changes the image "
                                                     "in the editor. Nothing goes to the ECU. Go ahead?"):
            var.set(False)
            return
        try:
            self.rom.set_option(o, on)
        except RomError as e:
            messagebox.showerror(o["name"], str(e))
        self.refresh_options()
        self.redraw()

    # ---------------------------------------------------------------- views
    def pick_table(self):
        if not self.rom or not self.tlist.curselection():
            return
        t = self.rom.tables[self.tlist.curselection()[0]]
        self.view = TableView(t)
        self.sel = None
        self._colchooser(t)
        self._evidence(t)
        self.redraw()

    def _evidence(self, t):
        txt = (f"{t.name}\naddress {t.addr:04X}-{t.end() - 1:04X}, {t.nrows} x {t.ncols}\nconfidence: "
               f"{t.confidence}\n\n{t.evidence}")
        if t.kind == "map":
            for lab, ax in (("rows", t.rows_axis), ("columns", t.cols_axis)):
                txt += f"\n\n{lab}: {ax.unit} ({ax.d.get('confidence')}): {ax.d.get('evidence', '')}"
        self.evidence.delete("1.0", "end")
        self.evidence.insert("end", txt)

    def _colchooser(self, t):
        if t is not None and len(t.col_choices) > 1:
            self.colbox.config(values=t.col_choices)
            self.colvar.set(t.cols_axis.key)
            self.collabel.pack(side="left", padx=(16, 2))
            self.colbox.pack(side="left")
        else:
            self.collabel.pack_forget()
            self.colbox.pack_forget()

    def pick_cols(self):
        if isinstance(self.view, TableView):
            self.view.t.set_cols(self.colvar.get())
            self.view.refresh_labels()
            self._evidence(self.view.t)
            self.redraw()

    def show_raw(self):
        if not self.rom:
            return
        try:
            v = RawView(self.rom, num("0x" + self.ex_addr.get().strip().lower().replace("0x", "")),
                        int(self.ex_w.get()), int(self.ex_n.get()), self.ex_t.get())
        except ValueError:
            messagebox.showerror("Explorer", "address in hex, width and rows as numbers")
            return
        self.view, self.sel = v, None
        self.tlist.selection_clear(0, "end")
        self._colchooser(None)
        self.redraw()

    def redraw(self):
        c = self.canvas
        c.delete("all")
        v = self.view
        if not v or not self.rom:
            return
        w = v.cell_w
        changed = len(self.rom.changed_bytes())
        self.info.config(text=f"{os.path.basename(self.rom.path)}   ECM ID {self.rom.ecmid()} ({self.rom.part_number()})"
                              f"   checksum {'OK' if self.rom.sum_ok(self.rom.original) else 'BAD in file'}"
                              f"   {changed} bytes changed"
                              + (f"   comparing with {self.compare_name}" if self.compare else ""))
        self.heading.config(text=f"{v.title}    rows: {v.row_unit}    columns: {v.col_unit}")
        c.create_text(HEAD_W / 2, HEAD_H / 2, text=f"{v.row_unit} \\ col", font=("Segoe UI", 8))
        for j, lab in enumerate(v.col_labels):
            x = HEAD_W + j * w
            c.create_rectangle(x, 0, x + w, HEAD_H, fill="#e8e8e8", outline="#ccc")
            c.create_text(x + w / 2, HEAD_H / 2, text=lab, font=("Segoe UI", 8), width=w - 2)
        for i, lab in enumerate(v.row_labels):
            y = HEAD_H + i * CELL_H
            c.create_rectangle(0, y, HEAD_W, y + CELL_H, fill="#e8e8e8", outline="#ccc")
            c.create_text(HEAD_W - 4, y + CELL_H / 2, text=lab, anchor="e", font=("Segoe UI", 8))
            for j in range(v.ncols):
                x = HEAD_W + j * w
                val, org = v.get(i, j), v.orig(i, j)
                lo, hi = v.lohi[j]
                c.create_rectangle(x, y, x + w, y + CELL_H, fill=heat((val - lo) / max(1, hi - lo)), outline="#ffffff")
                bold = val != org
                c.create_text(x + w / 2, y + CELL_H / 2, text=v.text(i, j, val),
                              font=("Segoe UI", 8, "bold" if bold else "normal"))
                if bold:
                    c.create_rectangle(x + 1, y + 1, x + w - 1, y + CELL_H - 1, outline="black", width=2)
                if self.compare is not None and v.get_in(self.compare, i, j) != val:
                    c.create_polygon(x + w - 10, y, x + w, y, x + w, y + 10, fill="#8e24aa")
        if self.sel:
            r0, c0, r1, c1 = self.sel
            c.create_rectangle(HEAD_W + c0 * w, HEAD_H + r0 * CELL_H, HEAD_W + (c1 + 1) * w,
                               HEAD_H + (r1 + 1) * CELL_H, outline="#1565c0", width=3)
        c.configure(scrollregion=(0, 0, HEAD_W + v.ncols * w + 4, HEAD_H + v.nrows * CELL_H + 4))
        self._sel_text()

    # ---------------------------------------------------------------- mouse / selection
    def cell_at(self, e):
        x, y = self.canvas.canvasx(e.x), self.canvas.canvasy(e.y)
        if not self.view or x < HEAD_W or y < HEAD_H:
            return None
        r, c = int((y - HEAD_H) // CELL_H), int((x - HEAD_W) // self.view.cell_w)
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
            self.value.set(self.view.text(*rc, self.view.get(*rc)))
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
        r0, c0, r1, c1 = self.sel
        txt = (f"{len(cells)} cells, {v.row_unit} {v.row_labels[r0]}-{v.row_labels[r1]}, "
               f"{v.col_unit} {v.col_labels[c0]}-{v.col_labels[c1]}")
        if isinstance(v, TableView) and (v.t.kind == "map" or c0 == c1):
            vals = [v.t.value(r, c, v.get(r, c)) for r, c in cells]
            txt += f", average {sum(vals) / len(vals):.2f}"
        self.sel_label.config(text=txt)

    def on_hover(self, e):
        rc = self.cell_at(e)
        if not rc:
            return
        v = self.view
        r, c = rc
        val, org = v.get(r, c), v.orig(r, c)
        txt = (f"{v.row_unit} {v.row_labels[r]}, {v.col_unit} {v.col_labels[c]}: {v.text(r, c, val)}   "
               f"(stock {v.text(r, c, org)})   raw {val}   address {v.addr(r, c):04X}")
        if self.compare is not None:
            txt += f"   {self.compare_name}: {v.text(r, c, v.get_in(self.compare, r, c))}"
        self.hover.config(text=txt)

    # ---------------------------------------------------------------- editing
    def _edit(self, fn):
        """fn(table, row, col, current raw, stock raw) -> new raw, for every selected cell, as one undo step."""
        v = self.view
        if not v or not v.editable:
            messagebox.showinfo("Edit", "This view is read only (Explorer, or a table changed only through the "
                                        "off-road options).")
            return
        cells = self.cells()
        if not cells:
            return
        self.rom.begin()
        try:
            for r, c in cells:
                v.t.set(r, c, fn(v.t, r, c, v.get(r, c), v.orig(r, c)))
        except RomError as e:
            messagebox.showerror("Edit", str(e))
        self.rom.commit()
        self.refresh_options()
        self.redraw()

    def set_value(self):
        try:
            x = float(self.value.get())
        except ValueError:
            return
        self._edit(lambda t, r, c, cur, org: t.to_raw(r, c, x))

    def scale(self, pct):
        def f(t, r, c, cur, org):
            new = t.to_raw(r, c, t.value(r, c, cur) * (1 + pct / 100))
            return new if new != cur else cur + (1 if pct > 0 else -1)   # always at least one step
        self._edit(f)

    def step(self, st):
        self._edit(lambda t, r, c, cur, org: cur + st)

    def revert(self):
        self._edit(lambda t, r, c, cur, org: org)

    def do_undo(self):
        if self.rom and self.rom.undo_last():
            self.refresh_options()
            self.redraw()


if __name__ == "__main__":
    App(sys.argv[1] if len(sys.argv) > 1 else None).mainloop()
