# CBR250R-Remap: notes for Claude

Read, back up and later remap the stock Keihin ECU of Neil's **2012 Honda CBR250R (MC41)** over its 4-pin red DLC
(Honda K-line), with a dedicated ESP32-C3 as a USB ↔ K-line bridge and Python on the laptop. README.md has the
research, safety rules, plan, wiring and commands.

Neil's tuning goal (2026-10-27): pod filter for better response and power, and he wants the emissions gear off.
Agreed approach: the fuelling must be corrected for the pod filter (the bike already reads lean). Neil says the bike
is for **off-road use**, so removing the emissions gear is in scope: PAIR (block-off plates + solenoid), cat (exhaust),
O2 closed loop (disable in the map once found, rather than unplugging the sensor and living with DTC 21-1), EVAP if
fitted. Tune with a wideband O2 sensor logged alongside the ECU data. Neil already has a wideband (make/model and
output type not yet known: ask). Plan: its 0-5 V output → divider → a free ADC1 pin (C3: GPIO0/1/3; GPIO4/5 are the
K-line), controller signal ground to the ESP32 GND, logged with rpm/TPS/MAP.

Forked from the HondaDLC scanner (`Documents\HondaDLC`, github neilson66/HondaDLC). **Never edit or push HondaDLC
from here.** Origin: https://github.com/neilson66/CBR250r-remap (Neil creates repos by hand; no gh CLI on this PC).

## Safety rules (always)
- **Stage 1 is read only.** Never send erase/write/recovery packets (`7B`, `7D`, `7E`, `72 .. 00 F1`, `27` other
  than `27 .. E0`) to a real ECU. The firmware refuses them (`ALLOW_WRITE = false` in `RemapBridge.ino`); don't
  change that, add a bypass, or send them with `B`, until Neil explicitly asks and every condition below holds.
- Writing needs: a verified backup (two identical reads, checksum OK, SHA-256, copied off the laptop), a recovery
  path proven on a **spare** ECU, and a bench supply ≥ 2.5 A (or battery on a charger), engine off. First write is
  the unchanged stock image, on the spare.
- Never guess map addresses: every table in a definition needs evidence (stock-image diffs, code that reads it,
  published definition) recorded next to it.
- Ask before flashing a board Neil hasn't named. He named the **ESP32-C3 on COM15** for the remap bridge.

## Layout
- `firmware/RemapBridge/RemapBridge.ino`: USB-only bridge, 115200 line commands: `P` → `OK HONDA-REMAP 1`, `I` info
  (`write=LOCKED`), `W` wake+init, `T <hex>` send with checksum + read reply (length at byte 1), `Q <n> <hex>` same
  with the reply length at byte n (3 for `82 82 00` flash reads), `B <hex>` raw, `E` echo, `L` level, `D 0/1`.
  Pins per chip via `CONFIG_IDF_TARGET_*` (C3 TX 4 / RX 5, C6 2/3, S3 1/2). FQBN C3: `esp32:esp32:esp32c3:CDCOnBoot=cdc`.
  arduino-cli: `C:\Program Files\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe`.
  Flashed to the C3 on COM15 on 2026-10-27 (answers P and I; not yet run against the ECU).
- `pc/honda_dlc.py`: shared K-line base from the scanner (`SerialLink` accepts both bridges; `query(pkt, len_pos)`),
  `Ecu`, `TABLE11`, `DTC_NAMES`, `check`.
- `pc/map_editor.py` (Tkinter, files only), `pc/rom.py` (Rom/Table/Axis, edits only inside defined tables, save_as
  fixes the checksum and refuses backups/, reference/ and the opened file), `pc/definitions/KYJA203.json` (axes,
  tables, off-road options with status "not found"; matched by the Keihin code at 0xDFF0).
- `pc/remap.py`: `check | id | backup --yes | verify <bin> | compare <a> <b>`, `demo` port = pretend ECU with a
  flash image. Backups to `backups/<time>_backup/` (git-ignored).
- `reference/stock_bins/38770-KYJ-922.bin`, `-971.bin`: from Adeen117/HondaECU `bins/CBR250R_KYJ_2010-2014`
  (git-ignored, Honda's code: don't commit or publish).
- Python: `C:\Users\neil6\Documents\KinectScanner\.venv\Scripts\python.exe` (has pyserial).

## Protocol
Diagnostics (confirmed on this bike via HondaDLC): 10400 8N1, wake 70 ms low / 130 ms high, `FE 04 FF FF`, then
`72 05 00 F0 99` → `02 04 00 FA`; tables `72 05 71 <t>`; clear codes `72 05 60 03` works. All frames sum to 0.
Flash (from HondaECU source, **not tried on this bike**):
- read: after a key off → on cycle, wake/init, `27 0B E0 48 65 6C 6C 6F 48 6F`, `27 0B E0 77 41 72 65 59 6F 75`
  ("HelloHowAreYou"), then `82 82 00 09 <bank> <lo> <hi> <n> <cs>` → `92 92 .. <len at byte 3> <n bytes> <cs>`.
- write/recover sequences are in README; the firmware blocks them.
- Image: 0xE000 bytes (56 KB), checksum byte 0xDFEF makes the sum 0, ECM ID at 0xC3EE (= first 5 bytes of table
  0x00), Keihin code at 0xDFF0 (`KYJA203`). Known ECM IDs: `01 00 B8 05 02` = KYJ-922, `01 00 B8 0F 01` = KYJ-971.
- 922 image SHA-256 `dcf710d07cf9f8bb4c4c900d502b20fc615583dd610d5fe53ed44a428e1c0224`, sum OK. 971 image sum 0xF2
  (bad checksum: modified or bad read?).
- 922 vs 971 differ at 0x47F2-3, 0x49A2-3, 0x6A85-89, 0x6BCA-B, 0x6F2A-B, 0x6F34-5, 0x6F55, 0xA001-03,
  0xAD40-0xADA0 (five 9-byte rows, stride 22: a 2-D table, 922 = 3F/40, 971 = 39/3A), 0xC3F1-2 (ECM ID),
  0xC528-0xC545 (8 × 4 bytes: `00 33 71 C7` vs `03 FE 71 C7` …), 0xDFEF. Candidate calibration areas, meaning unknown.
- Vectors at 0x0000 are 16-bit big-endian (reset 0x0082): looks like a Renesas H8/300H Tiny with 56 KB flash
  (H8/3687?). Unconfirmed until the chip marking is read.

## Maps (2026-10-27, in pc/definitions/KYJA203.json)
Two sources that agree on every address: our own search (stock images + HondaDLC logs) and cbrlocal's TunerPro XDF
(`reference/CBR250R_cbrlocal_testing.xdf`, from Neil, git-ignored). rpm axis 0xA004 (29 u16, × 0.27466), TPS axis
0xA03E (22 u16, ÷ 10.23 %), MAP axis 0xA06A (22 u8, × 3.8 mbar). Fuel A 0xA080 / B 0xA57C (29×22 u16, × 0.0008 ms),
ignition in gear 0xAA78 / neutral 0xACF6 (29×22 u8, × 0.355 − 15.71 °). XDF only: rev limiter 0xB582, O2 closed-loop
throttle limits 0xB5FC (lower, 3.5 %) / 0xB61E (upper, 30 %) as 8 × (rpm, TPS), fault code list 0xC5DC (32 × main/sub,
no PAIR code). Logs (rpm ≥ 1800, bike on the stand in neutral): every map fits throttle better than MAP (fuel 0.80 vs
0.41-0.45, ignition 0.93-0.96 vs 0.79), so the XDF's MAP axis for fuel A is doubtful. Ignition degree scale and fuel
ms scale come from the XDF only: check idle timing against the manual. Off-road options (O2 closed loop off, O2/
heater DTCs, bank angle DTCs) are implemented in the editor from the XDF; none tested on an ECU.

## Status
- **2026-11-26: ECU read and backed up** (RemapBridge on the HondaDLC C6, COM6 on the Zenbook; scanner firmware
  flashed back afterwards). `backups/20261126_115509_backup/flash.bin`, two identical reads, sum OK, SHA-256
  `2f91f93e96fe240f7577582ee5206a90d3ce9ebc85df993acef6f7b77811762a`. ECM ID `01 00 B8 01 01` (new), Keihin code
  `KYJA203`, checksum byte D5. vs KYJ-971 it differs only at 0xA003 (7C vs 3C), 0xAD40-0xADA0 (five rows inside the
  neutral ignition map: 3F/40 like the 922, 971 has 39/3A), ECM ID and checksum. Fuel A/B and in-gear ignition are
  identical to both stock images, so `KYJA203.json` applies. **This is the image every tune starts from.**
  Learned doing it: the K-line reads high even with the key off, so `backup` now detects the key cycle by the ECU
  stopping/starting to answer the wake-up (`--keyed` skips the wait); the security access (`27 …`) only gets a
  reply right after key-on (`07 08 49 7F 61 6D 7F DC`, then `07 08 46 69 6E 2E 7F 27`); ~340 B/s, 169 s per read;
  the fuel pump runs non-stop while the ECU is in read mode. Still to do: copy the backup folder off the laptop
  (USB stick + cloud); confirm the pump primes normally on the next key-on.
- **2026-11-26: tune 1 built (file only, not written to any ECU):** `python pc/make_tune1.py <backup flash.bin>` →
  `tunes/tune1_fuel.bin` (+ `.changes.json`; `tunes/*.bin` git-ignored, Honda's code). SHA-256 `5cf79a28…3e84`,
  120 cells. Full throttle (last 2 columns) 2,750-4,500 and 6,000-9,500 rpm +8 % in A and B; 10-30 % throttle at
  2,000-4,500 rpm +12 % in **B only**. Why B only: in this backup A and B share only 20 of 638 cells; B has the
  shape of a throttle map (fits the 4.3 ms logged in the bog after the 0.71 factor), A only makes sense on MAP
  (as the XDF says), so A's 10-30 % columns would be ~46-69 kPa cruise. Their last columns agree from 3,000 rpm up.
  When the ECU uses A vs B is still unknown: if the bog doesn't improve, that's the first suspect.
- **2026-11-26: spare ECU for write/recovery tests:** Neil is buying one ($60) from a **CB190R / CBF190** (Honda
  China, wiring diagram "CBF190WH-G (CH)"), physically identical to his. Bench only, never on the CBR. Same 33P ECM,
  and the pins we use are the same numbers as the CBR250R's: A-1 IGP 12 V key-on (Br/Bu on the 190), A-30 K-line
  (Y), A-15 SCS (Bu). Grounds A-9 PG1, A-10 PG2 (G), A-2 LG (G/Bl); A-4 SG is the sensor ground output. No
  battery-backup pin, no HISS shown. Bench: 12 V (fused) to A-1, ground to A-9/A-10/A-2, K-line to A-30, the
  interface's 12 V pull-up from the same supply. Connector face numbering isn't in the diagram: use the moulded
  numbers and check PG1-PG2 continuity. Not yet known: same chip / image size / read password. Read it first.
  Pictures on Neil's other PC: `Pictures\190ecm1.png`, `190ecm2.png` (not in git: Honda's manual).
- **2026-11-26: read `TUNE_PLAN.md` first.** Charging fixed, ride logs in `logs/rides/` (analyse with
  `pc/ride_analysis.py`), stock map confirmed against logged injector time, lean at full throttle and a low-rpm
  part-throttle bog both traced to the stock fuel map. Next: fit injector/MAP/IACV, then read the ECU with
  RemapBridge flashed onto the HondaDLC C6 board, then tune 1 from that backup.
- 2026-10-27: project started, research done, bridge firmware flashed on the C3 (COM15), `remap.py` tested only
  against the demo ECU and the stock images. Next: on the bike, `check`, then `id` (which ECU is it?), then, with
  Neil, `backup --yes` (engine off, battery OK or on a charger).
- The bike also has mechanical faults to fix first (from HondaDLC logs): stalls when the throttle closes, battery
  not charging (12.4-12.9 V at 3,500-6,800 rpm), DTC 54-2 (bank angle sensor missing). Parts on order: throttle
  body, fuel pump with regulator, IACV, injector. Workshop manual: `D:\workshopmanual-cbr250r_2011-29032019-1427.pdf`
  (scanned; idle 1,400 ± 100 rpm, 5-50 throttle position reset after a throttle body change, 17-7 charging test).
- Open questions: MCU type; does the read work on a stock ECU; any boot-mode pin; where fuel/ignition maps are.

## Neil's preferences
- Plain, practical explanations; he builds the hardware himself (Jaycar parts).
- Commit messages end with a Co-Authored-By line; he creates GitHub repos by hand.
