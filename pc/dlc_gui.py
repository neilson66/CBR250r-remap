"""Honda DLC scanner window: live data, raw bytes, automatic session logging, marks, fault codes, table dump.

    python dlc_gui.py            (pick the ESP32's COM port, or "demo")

Every connection writes a session folder logs/<date_time>/ with:
  info.txt    port, init reply, which extra tables answered and their first values
  faults.txt  fault-code tables read at connect (and again whenever "Fault codes" is pressed)
  live.csv    ~10 rows/s: decoded table 0x11, its raw bytes, any marks, and every ~1 s the raw extra tables
  dump.json   if "Dump tables" was pressed
Press "Mark" (or the space bar) the moment the engine stumbles, so the moment can be found in the log.
"""

from __future__ import annotations

import csv
import json
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk

from honda_dlc import TABLE11, DlcError, Ecu, hexs, open_link
from trend import LOG_ROOT, TrendView
# Tables other than 0x11 that may hold O2 sensor, fuel trim and status flags on Honda ECUs. The ones that answer
# are logged raw about once a second so they can be decoded later.
EXTRA_CANDIDATES = [0x10, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17, 0x20, 0x21, 0x60, 0x61, 0xD0, 0xD1]
EXTRA_EVERY = 10  # read the extra tables every N live samples


def list_ports():
    try:
        from serial.tools import list_ports as lp
        return [p.device for p in lp.comports()] + ["demo"]
    except ImportError:
        return ["demo"]


class Session:
    """One connection's log folder."""

    def __init__(self, port: str, init_reply: bytes):
        self.dir = LOG_ROOT / time.strftime("%Y%m%d_%H%M%S")
        self.dir.mkdir(parents=True, exist_ok=True)
        self.t0 = time.time()
        self.extras: list[int] = []
        self.csv_file = None
        self.writer = None
        self.info(f"port {port}\ninit reply {hexs(init_reply)}\nstarted {time.ctime(self.t0)}\n")

    def info(self, text: str):
        with open(self.dir / "info.txt", "a", encoding="utf-8") as fh:
            fh.write(text)

    def faults(self, text: str):
        with open(self.dir / "faults.txt", "a", encoding="utf-8") as fh:
            fh.write(f"--- {time.strftime('%H:%M:%S')}\n{text}\n")

    def start_csv(self):
        self.csv_file = open(self.dir / "live.csv", "w", newline="", encoding="utf-8-sig")
        self.writer = csv.writer(self.csv_file)
        self.writer.writerow(["time", "elapsed_s", "mark"] + [f"{f.name} ({f.unit})" for f in TABLE11]
                             + ["raw_11"] + [f"raw_{t:02X}" for t in self.extras])

    def row(self, mark: str, vals: dict, raw11: bytes, extra: dict):
        now = time.time()
        self.writer.writerow([time.strftime("%H:%M:%S", time.localtime(now)) + f".{int(now * 10) % 10}",
                              f"{now - self.t0:.1f}", mark]
                             + [vals[f.name][0] for f in TABLE11] + [hexs(raw11)]
                             + [hexs(extra[t]) if t in extra else "" for t in self.extras])

    def close(self):
        if self.csv_file:
            self.csv_file.close()
            self.csv_file = None


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Honda DLC Scanner")
        self.q: queue.Queue = queue.Queue()
        self.jobs: queue.Queue = queue.Queue()
        self.marks: queue.Queue = queue.Queue()
        self.mark_count = 0
        self.ecu = None
        self.link = None
        self.session: Session | None = None
        self.running = False
        self.prev_raw = b""

        top = ttk.Frame(root, padding=6)
        top.pack(fill="x")
        ttk.Label(top, text="Port").pack(side="left")
        self.port = ttk.Combobox(top, values=list_ports(), width=10)
        self.port.pack(side="left", padx=4)
        if self.port["values"]:
            self.port.current(0)
        ttk.Button(top, text="↻", width=3, command=lambda: self.port.configure(values=list_ports())).pack(side="left")
        self.btn_connect = ttk.Button(top, text="Connect", command=self.connect)
        self.btn_connect.pack(side="left", padx=4)
        self.btn_mark = ttk.Button(top, text="Mark (space)", command=self.mark, state="disabled")
        self.btn_mark.pack(side="left", padx=4)
        self.btn_dtc = ttk.Button(top, text="Fault codes", command=lambda: self.jobs.put("dtc"), state="disabled")
        self.btn_dtc.pack(side="left", padx=4)
        self.btn_clear = ttk.Button(top, text="Clear codes", command=self.ask_clear, state="disabled")
        self.btn_clear.pack(side="left", padx=4)
        self.btn_dump = ttk.Button(top, text="Dump tables", command=lambda: self.jobs.put("dump"), state="disabled")
        self.btn_dump.pack(side="left", padx=4)
        self.status = ttk.Label(top, text="not connected", foreground="grey")
        self.status.pack(side="left", padx=10)

        # One page: sensors + trend fill the middle, raw bytes and messages along the bottom.
        bottom = ttk.Frame(root, padding=(6, 0, 6, 6))
        bottom.pack(side="bottom", fill="x")
        rawf = ttk.LabelFrame(bottom, text="Raw bytes, table 0x11 (changed bytes highlighted)", padding=6)
        rawf.pack(side="left", fill="y")
        self.raw_cells = []
        for i in range(32):
            ttk.Label(rawf, text=f"{i:02d}", foreground="grey").grid(row=(i // 16) * 2, column=i % 16)
            c = tk.Label(rawf, text="--", width=3, font=("Consolas", 12), relief="groove")
            c.grid(row=(i // 16) * 2 + 1, column=i % 16, padx=1, pady=(0, 4))
            self.raw_cells.append(c)
        msgf = ttk.LabelFrame(bottom, text="Messages", padding=4)
        msgf.pack(side="left", fill="both", expand=True, padx=(8, 0))
        self.text = tk.Text(msgf, height=7, font=("Consolas", 10))
        self.text.pack(fill="both", expand=True)

        self.trend = TrendView(root)
        self.trend.configure(padding=6)
        self.trend.pack(fill="both", expand=True)

        root.bind("<space>", lambda e: self.mark())
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(50, self.pump)

    # ---- UI thread
    def say(self, msg):
        self.text.insert("end", msg + "\n")
        self.text.see("end")

    def connect(self):
        if self.running:
            self.running = False
            return
        port = self.port.get()
        self.btn_connect.configure(text="Disconnect")
        self.trend.clear_live()
        self.running = True
        threading.Thread(target=self.worker, args=(port,), daemon=True).start()

    def ask_clear(self):
        from tkinter import messagebox
        if messagebox.askyesno(
                "Clear fault codes",
                "Erase the fault codes stored in the ECU?\n\n"
                "The current codes are read and saved to this session's faults.txt first. "
                "Engine off, key on is best.\n\n"
                "This uses the community Honda clear command, not yet confirmed on the CBR250R; "
                "the codes are re-read afterwards to show whether it worked."):
            self.jobs.put("clear")

    def mark(self):
        if not self.running:
            return
        self.mark_count += 1
        self.marks.put(f"M{self.mark_count}")
        self.say(f"mark M{self.mark_count} at {time.strftime('%H:%M:%S')}")

    def pump(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "msg":
                    self.say(payload)
                elif kind == "status":
                    text, colour = payload
                    self.status.configure(text=text, foreground=colour)
                    state = "normal" if colour == "green" else "disabled"
                    for b in (self.btn_mark, self.btn_dtc, self.btn_clear, self.btn_dump):
                        b.configure(state=state)
                    if colour == "grey":
                        self.btn_connect.configure(text="Connect")
                elif kind == "live":
                    self.show_live(*payload)
        except queue.Empty:
            pass
        self.root.after(50, self.pump)

    def show_live(self, raw, vals, elapsed, mark):
        self.trend.add_sample(elapsed, vals, mark)
        for i, c in enumerate(self.raw_cells):
            if i < len(raw):
                changed = i < len(self.prev_raw) and raw[i] != self.prev_raw[i]
                c.configure(text=f"{raw[i]:02X}", bg="#ffd27f" if changed else "white")
            else:
                c.configure(text="--", bg="#eeeeee")
        self.prev_raw = raw

    # ---- worker thread (all K-line traffic and log writing happen here)
    def worker(self, port):
        put = self.q.put
        try:
            put(("status", ("connecting…", "orange")))
            self.link = open_link(port)
            self.ecu = Ecu(self.link)
            init = self.ecu.connect()
            put(("msg", "init reply: " + hexs(init)))
            self.session = s = Session(port, init)
            put(("msg", f"logging to {s.dir}"))
            self.do_dtc()
            self.probe_extras()
            s.start_csv()
            put(("status", (f"connected ({port}) — logging", "green")))
            errors = 0
            n = 0
            while self.running:
                try:
                    job = self.jobs.get_nowait()
                except queue.Empty:
                    job = None
                try:
                    if job == "dtc":
                        self.do_dtc()
                    elif job == "clear":
                        self.do_clear()
                    elif job == "dump":
                        self.do_dump()
                    raw, vals = self.ecu.live()
                    extra = {}
                    if n % EXTRA_EVERY == 0:
                        for t in s.extras:
                            try:
                                extra[t] = self.ecu.read_table(t)
                            except DlcError:
                                pass
                    marks = []
                    while not self.marks.empty():
                        marks.append(self.marks.get_nowait())
                    s.row(" ".join(marks), vals, raw, extra)
                    put(("live", (raw, vals, time.time() - s.t0, " ".join(marks))))
                    n += 1
                    errors = 0
                except DlcError as e:
                    errors += 1
                    put(("msg", f"error: {e}"))
                    if errors >= 5:
                        put(("msg", "lost the ECU, re-initialising"))
                        s.info(f"{time.strftime('%H:%M:%S')} lost the ECU, re-initialising\n")
                        self.ecu.connect()
                        errors = 0
                time.sleep(0.05)
        except Exception as e:  # noqa: BLE001 - show anything to the user
            put(("msg", f"connection failed: {e}"))
        finally:
            if self.session:
                self.session.close()
                put(("msg", f"log saved in {self.session.dir}"))
                self.session = None
            if self.link:
                self.link.close()
                self.link = None
            put(("status", ("not connected", "grey")))
            self.running = False

    def probe_extras(self):
        s = self.session
        lines = []
        for t in EXTRA_CANDIDATES:
            try:
                d = self.ecu.read_table(t)
            except DlcError:
                continue
            s.extras.append(t)
            lines.append(f"table {t:02X} ({len(d)}): {hexs(d)}")
        s.info("extra tables:\n" + ("\n".join(lines) or "none") + "\n")
        self.q.put(("msg", "extra tables logged: " + (", ".join(f"{t:02X}" for t in s.extras) or "none")))

    def do_dtc(self):
        put = self.q.put
        tables = self.ecu.fault_tables()
        lines = []
        for name, rows in tables.items():
            for i, payload in rows:
                lines.append(f"{name} {i:02X}: {hexs(payload)}")
            lines.append(f"{name} fault codes (best guess): {', '.join(Ecu.fault_codes(rows)) or 'none'}")
        for line in lines:
            put(("msg", line))
        if self.session:
            self.session.faults("\n".join(lines))

    def do_clear(self):
        put = self.q.put
        put(("msg", "saving the current fault codes before clearing…"))
        self.do_dtc()
        try:
            reply = self.ecu.clear_faults()
            put(("msg", f"clear command sent, ECU replied {hexs(reply)}"))
            self.session.faults(f"CLEAR sent, reply {hexs(reply)}")
        except DlcError as e:
            put(("msg", f"clear command: no valid reply ({e}). Codes may not be cleared; re-reading…"))
            self.session.faults(f"CLEAR sent, no valid reply ({e})")
        time.sleep(0.5)
        put(("msg", "fault codes after clearing:"))
        self.do_dtc()

    def do_dump(self):
        put = self.q.put
        put(("msg", "dumping tables 00-FF (about a minute)…"))
        found = self.ecu.dump()
        for t, d in found.items():
            put(("msg", f"table {t:02X} ({len(d)}): {hexs(d)}"))
        path = self.session.dir / "dump.json"
        with open(path, "w") as fh:
            json.dump({f"{t:02X}": hexs(d) for t, d in found.items()}, fh, indent=1)
        put(("msg", f"saved {path}"))

    def close(self):
        self.running = False
        self.root.after(300, self.root.destroy)


if __name__ == "__main__":
    root = tk.Tk()
    root.geometry("1920x1080")
    root.minsize(1280, 760)
    try:
        root.state("zoomed")  # maximised on Windows
    except tk.TclError:
        pass
    App(root)
    root.mainloop()
