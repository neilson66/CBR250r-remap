"""Tune 1 (fuel only), built from this bike's own backup. Works on files only.

  python make_tune1.py <backup flash.bin> [out.bin]       default out: tunes/tune1_fuel.bin

What it changes in Fuel A (0xA080) and Fuel B (0xA57C), from TUNE_PLAN.md step 4 and the logs behind it:
  1. Both maps: full throttle (last two columns), 2,750-4,500 and 6,000-9,500 rpm: +8 %.
     Logs: O2 0.04-0.20 V (lean) at >= 80 % throttle everywhere except 5,000-5,500 rpm (0.7-0.9 V, rich), so
     5,000 and 5,500 are left alone.
  2. Fuel B only: low-rpm part throttle (10-30 % columns), 2,000-4,500 rpm: +12 % (the plan says 10-15 %).
     (Fuel A looks like a MAP map, where those columns are cruise: see STEPS.)
     Logs: the engine is already at 99-100 kPa there with 10-15 % throttle, but the throttle-based map gives about
     a third less fuel than at full throttle for the same rpm and air: the bog (S0005 at 35.6, 96.6, 165.6 s).
  Not touched: anything below 10 % throttle (the O2 closed loop runs there; it stops above 30 %), ignition, and
  every other table. The wideband sets the real amounts later.
"""

from __future__ import annotations

import os
import sys

from rom import PROJECT, Rom

FUEL_TABLES = ("fuel_a", "fuel_b")

# (name, tables, rpm rows, throttle % columns, factor). Rows/columns are picked by axis value, so a definition
# change can't silently move them.
# The part-throttle step goes in Fuel B only. In the bike's backup B has the shape of a throttle map (fuel rises
# fast over the first 10-30 % then flattens; 3,000 rpm 16 % = 7.40 ms, x 0.71 = 5.3 ms, near the 4.3 ms logged in
# the bog), while A only makes sense on MAP (tiny at low load, a jump in the last column; read on throttle it would
# give 1.7 ms there), as the XDF says. In A, the 10-30 % columns would be about 46-69 kPa: cruise, not the bog.
# The last two columns are the full-load cells on either axis (and A and B agree there from 3,000 rpm up), so the
# full-throttle step goes in both.
BOTH, B_ONLY = ("fuel_a", "fuel_b"), ("fuel_b",)
STEPS = [
    ("full throttle, 2,750-4,500 rpm", BOTH, (2750, 4500), (75.0, 100.0), 1.08),
    ("full throttle, 6,000-9,500 rpm", BOTH, (6000, 9500), (75.0, 100.0), 1.08),
    ("part throttle 10-30 %, 2,000-4,500 rpm", B_ONLY, (2000, 4500), (10.0, 30.0), 1.12),
]


def pick(values, lo, hi):
    return [i for i, v in enumerate(values) if lo - 1e-6 <= v <= hi + 1e-6]


def build(src: str, out: str) -> dict:
    rom = Rom.load(src)
    rom.begin()
    touched = set()
    for tid in FUEL_TABLES:
        t = rom.table(tid)
        # The values as the editor shows them (2,749.6 rpm shows as 2750), so the ranges match what you see.
        rpm = [float(x) for x in t.rows_axis.labels()]
        tps = [float(x) for x in t.cols_axis.labels()]
        if t.cols_axis.key != "tps":
            raise SystemExit(f"{tid}: expected throttle columns, got {t.cols_axis.key}")
        for name, tables, (r0, r1), (c0, c1), k in STEPS:
            if tid not in tables:
                continue
            rows, cols = pick(rpm, r0, r1), pick(tps, c0, c1)
            for r in rows:
                for c in cols:
                    if (tid, r, c) in touched:
                        raise SystemExit(f"{tid} {rpm[r]:.0f} rpm {tps[c]:.1f} %: two steps change the same cell")
                    touched.add((tid, r, c))
                    t.set(r, c, t.orig(r, c) * k)
            print(f"{tid}: {name}: x{k:.2f} on {len(rows)} rpm x {len(cols)} throttle cells "
                  f"({rpm[rows[0]]:.0f}-{rpm[rows[-1]]:.0f} rpm, {tps[cols[0]]:.1f}-{tps[cols[-1]]:.1f} %)")
    rom.commit()
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    info = rom.save_as(out)
    print(f"saved {out}: {len(info['changes'])} cells changed, checksum byte {info['checksum_byte']}, "
          f"sha256 {info['sha256']}")
    print(f"from {src} (sha256 {info['from_sha256']})")
    return info


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    build(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else os.path.join(PROJECT, "tunes", "tune1_fuel.bin"))
