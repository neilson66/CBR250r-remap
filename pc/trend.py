"""Trend chart: pick sensors as pens, scrolling live or from a saved session log (logs/<time>/live.csv).

Each pen has its own scale (fixed range, or auto-scaled to what is on screen). Hover to read every pen at that
moment; marks show as red lines. Drag with the left mouse button to pan back in time (pauses), mouse wheel to zoom.
"""

from __future__ import annotations

import bisect
import csv
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk

from honda_dlc import TABLE11

LOG_ROOT = Path(__file__).resolve().parent.parent / "logs"

COLOURS = ["#d32f2f", "#1976d2", "#388e3c", "#f57c00", "#7b1fa2", "#00838f", "#c2185b",
           "#5d4037", "#455a64", "#afb42b", "#0288d1", "#e64a19", "#512da8"]

# Fixed pen ranges (min, max) used unless auto-scale is on.
RANGES = {
    "RPM": (0, 11000), "Throttle sensor": (0, 5), "Throttle": (0, 100), "Coolant sensor": (0, 5),
    "Coolant temp": (-20, 120), "Intake air sensor": (0, 5), "Intake air temp": (-20, 60), "MAP sensor": (0, 5),
    "Manifold pressure": (0, 110), "Battery": (10, 15.5), "Speed": (0, 160), "Injector time": (0, 10),
    "Ignition advance": (-10, 50),
}
DEFAULT_PENS = {"RPM", "Throttle", "Coolant temp", "Manifold pressure"}
WINDOWS = {"30 s": 30, "1 min": 60, "2 min": 120, "5 min": 300, "10 min": 600, "30 min": 1800, "All": None}
MAX_LIVE = 36000  # samples kept live (~1 hour at 10/s)


def to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class Store:
    def __init__(self):
        self.t: list[float] = []
        self.data: dict[str, list] = {f.name: [] for f in TABLE11}
        self.marks: list[tuple[float, str]] = []

    def add(self, t, values: dict, mark: str = ""):
        self.t.append(t)
        for f in TABLE11:
            self.data[f.name].append(values.get(f.name))
        if mark:
            self.marks.append((t, mark))
        if len(self.t) > MAX_LIVE:
            cut = len(self.t) - MAX_LIVE
            del self.t[:cut]
            for v in self.data.values():
                del v[:cut]


class TrendView(ttk.Frame):
    def __init__(self, master):
        super().__init__(master)
        self.live = Store()
        self.log: Store | None = None
        self.paused = tk.BooleanVar(value=False)
        self.auto = tk.BooleanVar(value=False)
        self.window = tk.StringVar(value="1 min")
        self.view_end: float | None = None   # right edge when paused / panned
        self.cursor_x: int | None = None
        self.drag_x: int | None = None
        self.dirty = True

        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 4))
        ttk.Label(bar, text="Window").pack(side="left")
        cb = ttk.Combobox(bar, textvariable=self.window, values=list(WINDOWS), width=7, state="readonly")
        cb.pack(side="left", padx=4)
        cb.bind("<<ComboboxSelected>>", lambda e: self.touch())
        ttk.Checkbutton(bar, text="Pause", variable=self.paused, command=self.on_pause).pack(side="left", padx=6)
        ttk.Checkbutton(bar, text="Auto-scale pens", variable=self.auto, command=self.touch).pack(side="left")
        ttk.Button(bar, text="Open log…", command=self.open_log).pack(side="left", padx=(12, 4))
        self.btn_live = ttk.Button(bar, text="Back to live", command=self.back_to_live, state="disabled")
        self.btn_live.pack(side="left")
        self.source = ttk.Label(bar, text="live", foreground="grey")
        self.source.pack(side="left", padx=10)

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        pens = ttk.Frame(body)
        pens.pack(side="left", fill="y", padx=(0, 6))
        self.pen_vars: dict[str, tk.BooleanVar] = {}
        self.pen_values: dict[str, ttk.Label] = {}
        self.colour = {}
        for i, f in enumerate(TABLE11):
            self.colour[f.name] = COLOURS[i % len(COLOURS)]
            var = tk.BooleanVar(value=f.name in DEFAULT_PENS)
            self.pen_vars[f.name] = var
            tk.Label(pens, bg=self.colour[f.name], width=2).grid(row=i, column=0, padx=(0, 4), pady=1)
            ttk.Checkbutton(pens, text=f.name, variable=var, command=self.touch).grid(row=i, column=1, sticky="w")
            lbl = ttk.Label(pens, text="—", width=8, anchor="e", font=("Consolas", 10, "bold"))
            lbl.grid(row=i, column=2, sticky="e")
            ttk.Label(pens, text=f.unit, foreground="grey").grid(row=i, column=3, sticky="w", padx=(3, 0))
            self.pen_values[f.name] = lbl
        self.cursor_time = ttk.Label(pens, text="", foreground="grey")
        self.cursor_time.grid(row=len(TABLE11), column=0, columnspan=4, sticky="w", pady=(6, 0))

        self.canvas = tk.Canvas(body, bg="white", highlightthickness=1, highlightbackground="#ccc")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: self.touch())
        self.canvas.bind("<Motion>", self.on_motion)
        self.canvas.bind("<Leave>", self.on_leave)
        self.canvas.bind("<ButtonPress-1>", lambda e: setattr(self, "drag_x", e.x))
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", lambda e: setattr(self, "drag_x", None))
        self.canvas.bind("<MouseWheel>", self.on_wheel)
        self.after(200, self.tick)

    # ---- data in
    def add_sample(self, t, vals: dict, mark: str = ""):
        self.live.add(t, {k: to_float(v[0]) for k, v in vals.items()}, mark)
        if self.log is None:
            self.dirty = True

    def clear_live(self):
        self.live = Store()
        self.touch()

    def open_log(self):
        path = filedialog.askopenfilename(title="Open session log", initialdir=LOG_ROOT,
                                          filetypes=[("Session log", "live.csv"), ("CSV", "*.csv")])
        if path:
            self.load_csv(path)

    def load_csv(self, path):
        s = Store()
        with open(path, newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                t = to_float(row.get("elapsed_s"))
                if t is None:
                    continue
                s.add(t, {f.name: to_float(row.get(f"{f.name} ({f.unit})")) for f in TABLE11}, row.get("mark", ""))
        self.log = s
        self.window.set("All")
        self.view_end = None
        self.source.configure(text=f"log: {path}")
        self.btn_live.configure(state="normal")
        self.touch()

    def back_to_live(self):
        self.log = None
        self.view_end = None
        self.paused.set(False)
        self.source.configure(text="live")
        self.btn_live.configure(state="disabled")
        self.touch()

    # ---- interaction
    def touch(self):
        self.dirty = True

    def store(self) -> Store:
        return self.log if self.log is not None else self.live

    def on_pause(self):
        s = self.store()
        self.view_end = (s.t[-1] if s.t else None) if self.paused.get() else None
        self.touch()

    def on_motion(self, e):
        self.cursor_x = e.x
        self.touch()

    def on_leave(self, _e):
        self.cursor_x = None
        self.touch()

    def on_drag(self, e):
        if self.drag_x is None or not hasattr(self, "_span"):
            return
        t0, t1, x0, x1 = self._span
        dt = (e.x - self.drag_x) * (t1 - t0) / max(1, x1 - x0)
        self.drag_x = e.x
        if self.log is None:
            self.paused.set(True)
        self.view_end = t1 - dt
        self.touch()

    def on_wheel(self, e):
        names = list(WINDOWS)
        i = names.index(self.window.get())
        i = max(0, min(len(names) - 1, i + (-1 if e.delta > 0 else 1)))
        self.window.set(names[i])
        self.touch()

    def tick(self):
        if self.dirty:
            self.dirty = False
            self.draw()
        self.after(200, self.tick)

    # ---- drawing
    def visible_range(self, s: Store):
        if not s.t:
            return None
        win = WINDOWS[self.window.get()]
        end = self.view_end if (self.view_end is not None and (self.paused.get() or self.log is not None)) else s.t[-1]
        if win is None:
            return s.t[0], max(s.t[-1], s.t[0] + 1)
        end = min(max(end, s.t[0] + win), s.t[-1]) if s.t[-1] - s.t[0] > win else s.t[0] + win
        return end - win, end

    def draw(self):
        c = self.canvas
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        x0, x1, y0, y1 = 56, w - 12, 10, h - 26
        if x1 - x0 < 50 or y1 - y0 < 50:
            return
        s = self.store()
        rng = self.visible_range(s)
        pens = [f for f in TABLE11 if self.pen_vars[f.name].get()]
        if rng is None:
            c.create_text(w / 2, h / 2, text="No data yet. Connect (or choose demo) or open a log.", fill="grey")
            return
        t0, t1 = rng
        self._span = (t0, t1, x0, x1)
        i0 = max(0, bisect.bisect_left(s.t, t0) - 1)
        i1 = min(len(s.t), bisect.bisect_right(s.t, t1) + 1)
        tx = lambda t: x0 + (t - t0) * (x1 - x0) / (t1 - t0)

        # grid and time axis
        for k in range(6):
            y = y0 + k * (y1 - y0) / 5
            c.create_line(x0, y, x1, y, fill="#eee")
        step = next((st for st in (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600)
                     if (t1 - t0) / st <= 10), 7200)
        t = (int(t0 // step) + 1) * step
        while t <= t1:
            x = tx(t)
            c.create_line(x, y0, x, y1, fill="#eee")
            c.create_text(x, y1 + 12, text=f"{int(t // 60)}:{int(t % 60):02d}", fill="#666", font=("Segoe UI", 8))
            t += step
        c.create_rectangle(x0, y0, x1, y1, outline="#bbb")

        # pens
        scales = {}
        stride = max(1, (i1 - i0) // max(1, (x1 - x0) * 2))
        for f in pens:
            vals = s.data[f.name]
            lo, hi = RANGES.get(f.name, (0, 100))
            if self.auto.get():
                seen = [v for v in vals[i0:i1] if v is not None]
                if seen:
                    lo, hi = min(seen), max(seen)
                    pad = (hi - lo) * 0.1 or max(1.0, abs(hi) * 0.05)
                    lo, hi = lo - pad, hi + pad
            scales[f.name] = (lo, hi)
            pts = []
            for i in range(i0, i1, stride):
                v = vals[i]
                if v is None:
                    if len(pts) >= 4:
                        c.create_line(*pts, fill=self.colour[f.name], width=2)
                    pts = []
                    continue
                y = y1 - (min(max(v, lo), hi) - lo) * (y1 - y0) / (hi - lo)
                pts += [tx(s.t[i]), y]
            if len(pts) >= 4:
                c.create_line(*pts, fill=self.colour[f.name], width=2)

        # left axis labelled in the first pen's units
        if pens:
            f = pens[0]
            lo, hi = scales[f.name]
            for k in range(6):
                v = hi - k * (hi - lo) / 5
                c.create_text(x0 - 4, y0 + k * (y1 - y0) / 5, text=f"{v:.0f}" if abs(hi - lo) >= 50 else f"{v:.3g}", anchor="e",
                              fill=self.colour[f.name], font=("Segoe UI", 8))

        # marks
        for t, m in s.marks:
            if t0 <= t <= t1:
                x = tx(t)
                c.create_line(x, y0, x, y1, fill="#e53935", dash=(4, 3))
                c.create_text(x + 3, y0 + 2, text=m, anchor="nw", fill="#e53935", font=("Segoe UI", 8, "bold"))

        # cursor read-out (or latest values)
        idx = len(s.t) - 1 if s.t else None
        if self.cursor_x is not None and x0 <= self.cursor_x <= x1:
            tc = t0 + (self.cursor_x - x0) * (t1 - t0) / (x1 - x0)
            j = bisect.bisect_left(s.t, tc)
            idx = min(range(max(0, j - 1), min(len(s.t), j + 1)), key=lambda k: abs(s.t[k] - tc))
            x = tx(s.t[idx])
            c.create_line(x, y0, x, y1, fill="#333")
            self.cursor_time.configure(text=f"at {int(s.t[idx] // 60)}:{s.t[idx] % 60:04.1f}")
        else:
            self.cursor_time.configure(text="latest")
        for f in TABLE11:
            v = s.data[f.name][idx] if idx is not None else None
            self.pen_values[f.name].configure(text="—" if v is None else f.fmt.format(v))
            if idx is not None and v is not None and f.name in scales and self.cursor_x is not None:
                lo, hi = scales[f.name]
                y = y1 - (min(max(v, lo), hi) - lo) * (y1 - y0) / (hi - lo)
                c.create_oval(tx(s.t[idx]) - 3, y - 3, tx(s.t[idx]) + 3, y + 3, fill=self.colour[f.name], outline="")
