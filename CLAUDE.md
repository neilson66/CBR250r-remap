# Honda DLC Scanner — notes for Claude

Reads live data and fault codes from Neil's **2012 Honda CBR250R (MC41)** through its 4-pin red DLC (Honda K-line).
Goal right now: **get it talking on the bike, then work out why the bike is running badly** from the logs.
See README.md for parts, wiring and bring-up. GitHub: neilson66/HondaDLC.

## Layout
- `firmware/HondaKLine/HondaKLine.ino`: ESP32 USB-serial ↔ K-line bridge. It handles only timing (wake-up pulse,
  echo removal, one reply frame). Line commands at 115200: `P` ping, `W` wake+init, `T <hex>` send with checksum
  and read the reply, `B <hex>` send raw, bench tests `E` echo, `L` line level, `D 0`/`D 1` hold low/release
  (`L` works while held). Pins are chosen per chip with `CONFIG_IDF_TARGET_*`.
- `pc/honda_dlc.py`: protocol, `SerialLink`, `SimLink` (demo ECU), `Ecu` (live, fault tables, clear, dump),
  table 0x11 field layout (`TABLE11`), CLI: `python honda_dlc.py <COMx|demo> check|live|dump|dtc|raw <hex>`.
- `pc/dlc_gui.py`: one-page window (sensors with big live values + trend + raw bytes + messages). Every connection
  logs to `logs/<date_time>/` (`live.csv`, `faults.txt`, `info.txt`, `dump.json`). Mark button/space bar.
  Clear codes button. `pc/trend.py`: the trend chart (pens, own scales, hover, pan/zoom, Open log).
- `docs/wiring.svg` (+ `wiring.png` rendered from it with headless Edge).

## Hardware
- Interface: two 2N3904s (E-B-C, flat face towards you). Q1: TX → 1k → base, collector on K-line. Q2: K-line →
  47k/10k divider → base, collector → RX with 10k pull-up to 3V3. K-line pull-up: Bl/Bu 12 V → 1N4148 → 1k.
  Both stages invert, so the firmware uses UART inversion (`KLINE_INVERTED`).
- DLC wire colours (traced to ECM 33P on the wiring diagram): **Y = K-line (ECM 30)**, **Bl/Bu = 12 V key-on
  (ECM 1, IGP)**, **G = ground**, **Bu = SCS (ECM 15)**, not connected.
- Main board: **Waveshare ESP32-C6-LCD-1.47**, TX GPIO2, RX GPIO3 (avoid 12/13 USB, 9 BOOT, 4-7 SD/LCD).
  Spares, also flashed: Waveshare ESP32-S3-LCD-1.47 with its LCD removed (TX 1, RX 2), ESP32-C3 (TX 4, RX 5).
  COM port numbers differ per PC.
- Flashing: Arduino IDE / arduino-cli, esp32 core 3.x. FQBNs: `esp32:esp32:esp32c6:CDCOnBoot=cdc`,
  `esp32:esp32:esp32s3:USBMode=hwcdc,CDCOnBoot=cdc,FlashSize=16M`, `esp32:esp32:esp32c3:CDCOnBoot=cdc`.
  After flashing the S3/C6 the chip can stay in the bootloader (no reply to `P`): reset it with
  `esptool --port COMx --before no-reset --after watchdog-reset read-mac`, or unplug and replug.

## Protocol (community-documented, NOT yet confirmed on this bike)
10400 8N1. Wake-up: K-line low 70 ms, high 130 ms. `FE 04 FF FF` (no reply), then `72 05 00 F0 99` → `02 04 00 FA`.
Read a table: `72 05 71 <t> <cs>` → `02 <len> 71 <t> <data> <cs>`. Byte 1 is the total length, and all bytes sum
to 0 mod 256. Faults: `72 05 74 <n>` current / `73 <n>` past (layout a guess). Clear: `72 05 60 03` (unverified).

## Status / unverified
- 2026-10-27: it talks on the bike (C6, COM6 on the new PC). The first fault was the interface GND not joined to the
  ESP32/DLC G. Wake-up, init, table 0x11 and the fault tables all answer. Key on, engine off, table 0x11 read
  `00 00 19 00 96 40 98 40 93 64 FF FF 7D 00 00 00 80 55 28 6E`, which decodes sensibly with `TABLE11` (0 rpm,
  TPS 0.49 V, coolant/air 24 °C, MAP 100 kPa, battery 12.5 V); still confirm throttle/rpm bytes move. Bytes 17-19 unknown.
- Fault rows: byte 0 unknown, then (main, sub) pairs. Current: 54-2 (bank angle sensor is physically missing).
  Past: 1-2, 8-1, 9-2, 12-1, 54-1, 54-2.
- First log (logs/20261027_103451, cold, on the stand): stalls whenever the throttle closes, starts only with
  throttle open, battery 12.4-13.0 V running (not charging). Steady at 12 % throttle is fine (2400 ± 80 rpm).
- Candidate decodes from that log (unconfirmed): table 0x11 bytes 10-11 always FF; byte 17 and bytes 18-19 (16-bit)
  fall smoothly as the coolant warms and are higher with the engine stopped (looks like an idle-air / warm-up
  command). Table 0x20 byte 0 is FF for the first ~30 s of running, then 0x02-0x23 (looks like the O2 sensor; the
  steady reading 0x04-0x06 would be lean if the scale is 5 V/256), byte 1 always 0x80 (trim at 1.0?). Table D0 bytes
  11-12 = crank period (rpm ≈ 2,460,000 / value). Table D1 byte 4: 00 stopped, 01 running, 05 on throttle openings.
- `check` steps: 1 bridge, 2 line idles high, 3 hold-low seen, 4 echo, 5 ECU init, 6 table 0x11. On the bench
  (9 V battery for the 12 V, no ECU) 1-4 should pass.
- The bike "runs badly": ask Neil for the symptoms; diagnose from the session logs (marks, fault codes, sensor
  plausibility: temperatures, throttle zero, MAP vs rpm, battery, injector/advance).

## Workshop manual
Neil's copy: `D:\workshopmanual-cbr250r_2011-29032019-1427.pdf` (499 scanned pages, no text: render pages with
PyMuPDF and read the images). PDF page ≈ manual page + 111 for section 5 early pages, + 110 from about 5-50.
- 5-3 specs: idle 1,400 ± 100 rpm, fuel pressure at idle 294 kPa, pump flow 69.2 cm³ / 10 s, injector 11-13 Ω,
  throttle body GQ9JA. 5-5 symptom table. 5-13 erase DTCs with the SCS connector. 5-14 DTC index (codes are
  main-sub as decoded). 5-15 sensor unit (MAP + TP + IAT, one 5P connector). 5-50 throttle valve fully closed
  position reset (needed after a sensor unit / throttle body change). 17-7 charging test: warm, high beam,
  5,000 rpm, battery V < charging V < 15.5 V.
- Parts ordered (2026-10-27): throttle body, fuel pump with regulator, IACV, injector. Bank angle sensor is missing.

## Plan after it talks (agreed)
Always-on unit on the bike: C6 powered by a 12 V → USB-C adaptor fed from the key-switched Bl/Bu (1 A fuse), ESP32
logs every ride to its microSD, phone connects to the ESP32's own WiFi hotspot and opens a web page (live values,
trend, fault codes, log download). No flashing/tuning of the ECU: read only, apart from clearing codes.

## Neil's preferences
- Plain, practical explanations; he builds the hardware himself (Jaycar parts).
- Ask before flashing a board he hasn't named for it. Commit with "Co-Authored-By" lines; he creates GitHub repos by hand.
