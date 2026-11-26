"""Read a HondaDLC scanner SD log (live.csv) and show what matters for tuning.

    python pc/ride_analysis.py logs/rides/20261126_1049_S0005_live.csv

Prints: full-throttle injector time and O2 by rpm, each full-throttle pull with its rpm climb rate (in one gear
that is the pulling force; force x rpm = relative power), and the throttle openings from low rpm (the bog).
Decoding: table 0x11 as in HondaDLC TABLE11; O2 = table 0x20 byte 0 x 5/256 V (FF = sensor still cold).
"""
import sys

import numpy as np
import pandas as pd


def h(x):
    return bytes.fromhex(x) if isinstance(x, str) and x.strip() else None


def load(path):
    d = pd.read_csv(path, dtype=str)
    r = d.raw_11.map(h)
    o = pd.DataFrame({
        "t": d.elapsed_s.astype(float), "mark": d.mark,
        "rpm": r.map(lambda b: b[0] * 256 + b[1]), "tps": r.map(lambda b: b[3] / 1.6),
        "ect": r.map(lambda b: b[5] - 40), "air": r.map(lambda b: b[7] - 40), "kpa": r.map(lambda b: b[9]),
        "bat": r.map(lambda b: b[12] / 10), "inj": r.map(lambda b: (b[14] * 256 + b[15]) / 250),
        "adv": r.map(lambda b: b[16] / 2 - 64)})
    o["o2"] = d.raw_20.map(lambda x: h(x)[0] * 5 / 256 if h(x) and h(x)[0] != 0xFF else np.nan) \
        if "raw_20" in d else np.nan
    return o


def full_throttle(o):
    w = o[o.tps >= 80]
    w = w.assign(band=(w.rpm // 1000 * 1000))
    g = w.groupby("band").agg(inj_ms=("inj", "mean"), o2_V=("o2", "mean"), o2_reads=("o2", "count"))
    print("Full throttle (>= 80 %) by rpm: injector time and O2 (0.1 V lean, 0.8-0.9 V rich)")
    print(g[g.index >= 2000].round(2).to_string(), "\n")


def pulls(o):
    w = (o.tps >= 80).values
    n, i, k = len(o), 0, 0
    print("Full-throttle pulls (rpm rate is only comparable between pulls in the same gear)")
    rows = []
    while i < n:
        if not w[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and w[j + 1] and o.rpm[j + 1] >= o.rpm[j] - 150:
            j += 1
        s = o.loc[i:j]
        if s.t.iloc[-1] - s.t.iloc[0] >= 1.0 and s.rpm.iloc[-1] - s.rpm.iloc[0] > 800:
            k += 1
            dt = s.t.iloc[-1] - s.t.iloc[0]
            print("  pull %d: %.1f-%.1f s, %d -> %d rpm in %.1f s, O2 mean %.2f V"
                  % (k, s.t.iloc[0], s.t.iloc[-1], s.rpm.iloc[0], s.rpm.iloc[-1], dt, s.o2.mean()))
            for a in range(i, j + 1):
                q = s[(s.t >= o.t[a] - 0.3) & (s.t <= o.t[a] + 0.3)]
                if len(q) >= 4 and q.t.max() - q.t.min() > 0.4:
                    rows.append((k, o.rpm[a] // 500 * 500, np.polyfit(q.t, q.rpm, 1)[0]))
        i = j + 1
    if rows:
        r = pd.DataFrame(rows, columns=["pull", "band", "rate"])
        p = r.pivot_table(index="band", columns="pull", values="rate", aggfunc="mean")
        print("\nrpm climb rate (rpm/s) per 500-rpm band; relative power = band x rate")
        print(p.round(0).to_string(), "\n")


def tip_ins(o):
    idx = o.index[(o.tps >= 5) & (o.tps.shift(1) < 3) & (o.rpm.shift(1) < 3500) & (o.rpm.shift(1) > 900)]
    print("Throttle openings from below 3,500 rpm: rpm change over the next 0.5 s, manifold pressure, O2")
    for i in idx:
        s = o[(o.t >= o.t[i]) & (o.t <= o.t[i] + 0.5)]
        print("  %6.1f s  %5d rpm  -> %3.0f %% throttle  rpm %+5d (lowest %+5d)  kPa max %3d  O2 after %s"
              % (o.t[i], o.rpm[i - 1], s.tps.max(), s.rpm.iloc[-1] - o.rpm[i - 1], s.rpm.min() - o.rpm[i - 1],
                 s.kpa.max(), " ".join("%.2f" % v for v in o.o2[i:i + 12].dropna().values[:3])))


if __name__ == "__main__":
    o = load(sys.argv[1])
    print("%s: %.0f s, coolant %d-%d C, air %d C, marks %s\n"
          % (sys.argv[1], o.t.max(), o.ect.min(), o.ect.max(), o.air.median(), o.mark.dropna().tolist()))
    full_throttle(o)
    pulls(o)
    tip_ins(o)
