# CBR250R tune plan: where we are (2026-11-26)

Handoff note so the work can carry on from the laptop. Read this, then CLAUDE.md and README.md.

## The bike now
- Stock intake and exhaust (airbox, PAIR and EVAP still fitted). Stock ECU.
- Charging fixed: new regulator/rectifier (the old shunt one melted its wiring). Battery is lithium (LiFePO4):
  keep charging at or below 14.6 V. ECU-measured voltage above 5,000 rpm is about 13.2 V; still to check with a
  meter at the battery (5,000 rpm, headlight on: should be 14.0-14.6 V).
- Fuel pump is new. Parts waiting to be fitted: injector, MAP sensor (sensor unit), idle air valve (IACV).
  After the sensor unit goes on: throttle valve fully closed position reset, workshop manual 5-50.
- Symptoms: stalls at warm idle (S0010 at 331 s: died dead from a steady 1,390 rpm), bog when the throttle is
  opened part way at low rpm, lean at full throttle.
- No fault codes stored (S0005).

## Logging (HondaDLC scanner, C6 board, github neilson66/HondaDLC)
- Firmware flashed 2026-11-26: O2 sensor card + trend (table 0x20 byte 0 x 5/256 V, read about 4 times a
  second), MARK button, Clear SD logs, SD log names with date/time (`cbr250r_YYYYMMDD_HHMM_Sxxxx_live.csv`).
- The O2 reading is confirmed as the stock narrowband sensor: 0 V on fuel cut, switching at cruise, ~0.9 V rich.
  It only says richer or leaner than 14.7:1, not how much.
- The ECU has no road speed (table 0x11 byte 13 is always 0; table 0x61 mirrors 0x11).
- Ride logs are in `logs/rides/`. Analyse one with `python pc/ride_analysis.py logs/rides/<file>_live.csv`
  (needs pandas and numpy).

| Log | What it is |
|---|---|
| 20261122_1812_warmup_phone.csv | Stationary warm-up after the regulator fix (phone CSV, no O2) |
| 20261124_S0010 | Ride, O2 once a second; warm idle stall at 331 s |
| 20261124_S0014 | Short ride, O2 once a second |
| 20261126_1049_S0005 | **Baseline.** 4th-gear pull 41-52 s (3,000 -> 8,230 rpm), then normal riding, O2 4/s |

## What the logs show
1. **Lean at full throttle almost everywhere.** O2 0.04-0.20 V at >= 80 % throttle from 3,000 to 9,000 rpm,
   except 5,000-5,500 rpm (0.7-0.9 V, rich).
2. **It is the tune, not the fuel supply.** Logged injector time at full throttle is the same on every ride
   (6.6 ms at 3-4k, 7.4 at 5k, 8.2-8.4 at 6-9k), and it is equally lean at 3,000 rpm as at 8,000 (three times the
   fuel flow), so the pump keeps up. The rich patch at 5,000 is where the pull showed a dip in pulling force:
   less air there, same fuel.
3. **The bog is the throttle-based fuel map.** At ~3,000 rpm the engine reaches 99-100 kPa (full load) with only
   10-15 % throttle, but the ECU fuels by throttle: 4.3 ms at 15 % versus 6.6 ms at 100 % for the same rpm and
   air, about a third less fuel. The O2 drops to ~0.1 V and the rpm stalls for 0.3-1 s (S0005 at 35.6, 96.6,
   165.6, 175.0 s).
4. **The map is confirmed.** The 100 % column of Fuel A (0xA080, also Fuel B's last column) has the same shape
   as the logged injector time, ratio logged/map ~0.71 at every rpm (3,000: 9.39 -> 6.60; 6,000: 11.41 -> 8.21;
   8,000: 11.64 -> 8.27), including the step up between 5,000 and 6,000 rpm. The 922 and 971 stock images have
   identical full-throttle columns.

Baseline 4th-gear pull (S0005, 41-52 s). Relative power = rpm x rpm climb rate, not horsepower, no drag
correction:

| rpm | 3,000 | 4,000 | 4,500 | 5,000 | 5,500 | 6,000 | 6,500 | 7,000 | 8,000 |
|---|---|---|---|---|---|---|---|---|---|
| climb rate, rpm/s | 454 | 444 | 537 | 411 | 556 | 542 | 503 | 469 | 366 |
| relative power, % | 41 | 54 | 74 | 63 | 93 | 99 | 100 | 100 | 89 |
| O2, V | 0.15 | 0.12 | 0.21 | 0.86 | 0.78 | 0.17 | 0.06 | 0.12 | 0.07 |

Only one clean pull in one direction, so treat single bands (the dip at 5,000) as uncertain.

## Next steps, in order
1. **Fit the parts** (injector, MAP sensor unit, IACV), do the 5-50 throttle reset, clear codes, warm idle until
   the fan cycles. Ride and log: does the warm idle still stall?
2. **Better baseline** (same day as the tune test if possible): 2-3 full-throttle pulls each way on the same
   stretch, same gear (4th), 3,000 -> about 8,000 rpm, MARK before each. Note weather and fuel load.
3. **Read the ECU** (read only):
   - Flash `firmware/RemapBridge` onto the scanner's C6 board (pins TX 2 / RX 3 already supported). FQBN
     `esp32:esp32:esp32c6:CDCOnBoot=cdc`. It refuses erase/write packets (`ALLOW_WRITE = false`).
   - Power: with the key on, don't feed the board from both the bike's 12 V converter and the laptop USB.
     Disconnect the board's 12 V feed and run it from the laptop; the K-line interface takes its 12 V from the DLC.
   - Engine off, key on, battery charged: `python pc/remap.py COMx check`, then `id`, then `backup --yes`.
   - Check the backup (two identical reads, sum 0 mod 256, SHA-256), copy it off the laptop.
   - Flash the HondaDLC scanner firmware back onto the C6: the ready-built files and the esptool command are in
     that repo's `firmware/HondaKLine/prebuilt_c6/README.md` (no compiling needed), or rebuild `firmware/HondaKLine`.
4. **Build tune 1 from the backup** (never from the reference images), in `pc/map_editor.py`, both Fuel A and B:
   - Full throttle (76 % and 100 % columns), 2,750-4,500 and 6,000-9,500 rpm: +8 %. Leave 5,000-5,500.
   - Low-rpm part throttle (10-30 % columns), about 2,000-4,500 rpm, where the engine is already at full load:
     +10-15 %.
   - Leave the cruise cells (< 8 % throttle) alone: the O2 closed loop runs there (it stops above 30 % throttle).
   - Goal of tune 1: O2 at those points comes up to ~0.8 V, and the bog and the lean pulls improve. The wideband
     then sets the real amount (target 12.5-13:1 at full throttle; probably +15-20 % where leanest).
5. **Before any write:** a tested recovery path (README stage 4) and a stable supply (bench 12-13.8 V >= 2.5 A or
   the battery on a charger). Engine never running.
6. **Wideband:** model still unknown. If its controller has a narrowband simulation output, it can take the stock
   O2 sensor's place and keep the ECU's closed loop working; otherwise it needs its own bung in the header.
7. **Compare** each tune with the baseline: same pulls, `pc/ride_analysis.py`, power by rpm band and O2.

Ignition timing comes later, one careful A/B road test at a time (no knock sensor).

## Also open (2026-11-26)
- **Fan:** Neil saw 99 C on the dash at idle with the fan off. No log has gone above 90 C, so it isn't known
  whether the ECU switched it. The fan-on temperature isn't in the manual (ECU-controlled, probably ~100-105 C)
  and isn't in any map definition yet. Test: 10 A fan fuse; bridge the relay socket (fuse, wiring, motor); relay
  test manual 6-18 (coil Bl/W-Br/R, contacts Bl/W-Bu); then idle from warm, logging, MARK when the fan starts or at
  ~105 C with no fan (then switch off). Compare the scanner's coolant temp with the dash. ECU fan output is pin 19
  (FANC, Br/R) switching the relay coil to ground.
- **Wideband:** model still unknown. Neil is making a tailpipe sniffer (go 40-50 cm deep, sensor tip pointing up,
  outlet open). Trust full throttle and steady cruise; idle, low rpm and closed throttle read lean (air drawn in,
  PAIR air); allow ~0.2-0.5 s delay. Plan: log it over a second UART on the scanner's C6 (wideband TX -> a free GPIO
  RX, shared ground; level shifting depends on the model), AFR column in live.csv, AFR card on the phone page.

## Laptop setup
- Clone github.com/neilson66/CBR250r-remap and github.com/neilson66/HondaDLC.
- `pip install pyserial pandas numpy`.
- Arduino CLI with the esp32 core 3.x (the Zenbook used arduino-cli 1.5.1 from winget).
- Not in git, copy by hand if wanted: `reference/` (stock images 922/971 and cbrlocal's XDF). The tune itself must
  start from this bike's own backup.
