"""Pretend ESP32 board: serves the phone page (firmware/HondaKLine/page.h) with simulated ECU data, so the page can
be tried in a browser without the board or the bike.

    python mock_board.py [port]      then open http://localhost:8765
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from honda_dlc import SimLink, hexs

PAGE = Path(__file__).resolve().parent.parent / "firmware" / "HondaKLine" / "page.h"


def page_html() -> bytes:
    text = PAGE.read_text(encoding="utf-8")
    m = re.search(r'R"rawpage\((.*)\)rawpage"', text, re.S)
    return m.group(1).encode("utf-8")


class Board:
    def __init__(self):
        self.lock = threading.Lock()
        self.sim = SimLink()
        self.sim.wake()
        self.t0 = time.time()
        self.samples = []  # (seq, t_ms, hex, mark)
        self.seq = 0
        self.msgs = [(1, "mock board: simulated ECU"), (2, "ECU answered: 02 04 00 FA, session S0001")]
        self.mark = ""
        self.marks = 0
        self.fseq = 1
        self.faults = {"current": [[1, "00 36 02 00 00 00 00"]], "past": [[1, "01 01 02 08 01 09 02"],
                                                                         [2, "00 0C 01 36 01 36 02"]]}
        threading.Thread(target=self.run, daemon=True).start()

    def msg(self, text):
        self.msgs.append((self.msgs[-1][0] + 1, text))
        self.msgs = self.msgs[-40:]

    def run(self):
        while True:
            data = self.sim._table11()
            with self.lock:
                self.seq += 1
                self.samples.append((self.seq, int((time.time() - self.t0) * 1000), hexs(data), self.mark))
                self.mark = ""
                self.samples = self.samples[-2000:]
            time.sleep(0.1)

    def live(self, since, m):
        with self.lock:
            s = [list(x) for x in self.samples if x[0] > since][-400:]
            return {"st": "connected — session S0001, logging to SD (mock)", "kind": "ok", "sess": "S0001",
                    "sd": True, "seq": self.seq, "fseq": self.fseq, "time": True, "s": s,
                    "ex": {"20": "05 80", "D0": "00 00 00 00 00 00 00 00 00 00 00 04 1A 00", "D1": "00 00 00 00 01 00"},
                    "msgs": [list(x) for x in self.msgs if x[0] > m]}


BOARD = Board()


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/":
            self._send(200, page_html(), "text/html; charset=utf-8")
        elif u.path == "/api/live":
            self._send(200, BOARD.live(int(q.get("since", 0)), int(q.get("m", 0))))
        elif u.path == "/api/faults":
            self._send(200, {"seq": BOARD.fseq, **BOARD.faults})
        elif u.path == "/api/logs":
            self._send(200, [{"s": "S0001", "f": [["info.txt", 300], ["faults.txt", 900], ["live.csv", 52000]]}])
        elif u.path == "/dl":
            self._send(200, b"time,elapsed_s,mark,raw_11\n", "text/csv")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        with BOARD.lock:
            if u.path == "/api/mark":
                BOARD.marks += 1
                BOARD.mark = f"M{BOARD.marks}"
                BOARD.msg(f"mark M{BOARD.marks}")
                body = {"mark": BOARD.mark}
            elif u.path == "/api/cmd":
                c = q.get("c")
                if c == "clear":
                    BOARD.faults = {"current": [[1, "00 36 02 00 00 00 00"]], "past": []}
                    BOARD.msg("clear command sent, ECU replied 02 05 60 01 98")
                BOARD.fseq += 1
                BOARD.msg(f"{c} done (mock)")
                body = {"ok": True}
            else:
                body = {"ok": True}
        self._send(200, body)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    print(f"mock board on http://localhost:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
