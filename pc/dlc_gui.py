"""Honda DLC scanner window: live data, raw bytes, CSV logging, fault codes, table dump.

    python dlc_gui.py            (pick the ESP32's COM port, or "demo")
"""

from __future__ import annotations

import csv
import json
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk

from honda_dlc import TABLE11, DlcError, Ecu, hexs, open_link


def list_ports():
    try:
        from serial.tools import list_ports as lp
        return [p.device for p in lp.comports()] + ["demo"]
    except ImportError:
        return ["demo"]


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Honda DLC Scanner")
        self.q: queue.Queue = queue.Queue()
        self.jobs: queue.Queue = queue.Queue()
        self.ecu = None
        self.link = None
        self.running = False
        self.log_writer = None
        self.log_file = None
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
        self.btn_log = ttk.Button(top, text="Start log", command=self.toggle_log, state="disabled")
        self.btn_log.pack(side="left", padx=4)
        self.btn_dtc = ttk.Button(top, text="Fault codes", command=lambda: self.jobs.put("dtc"), state="disabled")
        self.btn_dtc.pack(side="left", padx=4)
        self.btn_dump = ttk.Button(top, text="Dump tables", command=lambda: self.jobs.put("dump"), state="disabled")
        self.btn_dump.pack(side="left", padx=4)
        self.status = ttk.Label(top, text="not connected", foreground="grey")
        self.status.pack(side="left", padx=10)

        mid = ttk.Frame(root, padding=6)
        mid.pack(fill="both", expand=True)
        vals = ttk.LabelFrame(mid, text="Live data (table 0x11, layout unverified)", padding=6)
        vals.pack(side="left", fill="y")
        self.val_labels = {}
        for r, f in enumerate(TABLE11):
            ttk.Label(vals, text=f.name).grid(row=r, column=0, sticky="w")
            lbl = ttk.Label(vals, text="—", width=9, anchor="e", font=("Consolas", 12, "bold"))
            lbl.grid(row=r, column=1, sticky="e")
            ttk.Label(vals, text=f.unit).grid(row=r, column=2, sticky="w", padx=4)
            self.val_labels[f.name] = lbl

        rawf = ttk.LabelFrame(mid, text="Raw bytes (changed bytes highlighted)", padding=6)
        rawf.pack(side="left", fill="both", expand=True, padx=(6, 0))
        self.raw_cells = []
        for i in range(32):
            ttk.Label(rawf, text=f"{i:02d}", foreground="grey").grid(row=(i // 8) * 2, column=i % 8)
            c = tk.Label(rawf, text="--", width=4, font=("Consolas", 12), relief="groove")
            c.grid(row=(i // 8) * 2 + 1, column=i % 8, padx=1, pady=(0, 4))
            self.raw_cells.append(c)

        self.text = tk.Text(root, height=12, font=("Consolas", 10))
        self.text.pack(fill="both", expand=True, padx=6, pady=6)

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
        self.running = True
        threading.Thread(target=self.worker, args=(port,), daemon=True).start()

    def toggle_log(self):
        if self.log_writer:
            self.log_file.close()
            self.log_writer = self.log_file = None
            self.btn_log.configure(text="Start log")
            return
        name = time.strftime("live_%Y%m%d_%H%M%S.csv")
        self.log_file = open(name, "w", newline="", encoding="utf-8")
        self.log_writer = csv.writer(self.log_file)
        self.log_writer.writerow(["time"] + [f"{f.name} ({f.unit})" for f in TABLE11] + ["raw"])
        self.btn_log.configure(text="Stop log")
        self.say(f"logging to {name}")

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
                    for b in (self.btn_log, self.btn_dtc, self.btn_dump):
                        b.configure(state=state)
                    if colour != "green":
                        self.btn_connect.configure(text="Connect")
                elif kind == "live":
                    self.show_live(*payload)
        except queue.Empty:
            pass
        self.root.after(50, self.pump)

    def show_live(self, raw, vals):
        for name, (v, _) in vals.items():
            self.val_labels[name].configure(text=v if v is not None else "—")
        for i, c in enumerate(self.raw_cells):
            if i < len(raw):
                changed = i < len(self.prev_raw) and raw[i] != self.prev_raw[i]
                c.configure(text=f"{raw[i]:02X}", bg="#ffd27f" if changed else "white")
            else:
                c.configure(text="--", bg="#eeeeee")
        self.prev_raw = raw
        if self.log_writer:
            self.log_writer.writerow([f"{time.time():.2f}"] + [vals[f.name][0] for f in TABLE11] + [hexs(raw)])

    # ---- worker thread (all K-line traffic happens here)
    def worker(self, port):
        put = self.q.put
        try:
            put(("status", ("connecting…", "orange")))
            self.link = open_link(port)
            self.ecu = Ecu(self.link)
            put(("msg", "init reply: " + hexs(self.ecu.connect())))
            put(("status", (f"connected ({port})", "green")))
            errors = 0
            while self.running:
                try:
                    job = self.jobs.get_nowait()
                except queue.Empty:
                    job = None
                try:
                    if job == "dtc":
                        self.do_dtc()
                    elif job == "dump":
                        self.do_dump()
                    raw, vals = self.ecu.live()
                    put(("live", (raw, vals)))
                    errors = 0
                except DlcError as e:
                    errors += 1
                    put(("msg", f"error: {e}"))
                    if errors >= 5:
                        put(("msg", "lost the ECU, re-initialising"))
                        self.ecu.connect()
                        errors = 0
                time.sleep(0.05)
        except Exception as e:  # noqa: BLE001 - show anything to the user
            put(("msg", f"connection failed: {e}"))
        finally:
            if self.link:
                self.link.close()
                self.link = None
            put(("status", ("not connected", "grey")))
            self.running = False

    def do_dtc(self):
        put = self.q.put
        tables = self.ecu.fault_tables()
        for name, rows in tables.items():
            for i, payload in rows:
                put(("msg", f"{name} {i:02X}: {hexs(payload)}"))
            put(("msg", f"{name} fault codes (best guess): {', '.join(Ecu.fault_codes(rows)) or 'none'}"))

    def do_dump(self):
        put = self.q.put
        put(("msg", "dumping tables 00-FF (about a minute)…"))
        found = self.ecu.dump()
        for t, d in found.items():
            put(("msg", f"table {t:02X} ({len(d)}): {hexs(d)}"))
        name = time.strftime("dump_%Y%m%d_%H%M%S.json")
        with open(name, "w") as fh:
            json.dump({f"{t:02X}": hexs(d) for t, d in found.items()}, fh, indent=1)
        put(("msg", f"saved {name}"))

    def close(self):
        self.running = False
        if self.log_file:
            self.log_file.close()
        self.root.after(200, self.root.destroy)


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
