# CBR250R-Remap: notes for Claude

Read, back up and later remap the stock Keihin ECU of Neil's **2012 Honda CBR250R (MC41)** over its 4-pin red DLC
(Honda K-line), with a dedicated ESP32-C3 as a USB ↔ K-line bridge and Python on the laptop. README.md has the
research, safety rules, plan, wiring and commands.

Neil's tuning goal (2026-10-27): pod filter for better response and power, and he wants the emissions gear off.
Agreed approach: the fuelling must be corrected for the pod filter (the bike already reads lean). Removing or
disabling emissions equipment (O2 closed loop, PAIR valve, cat, EVAP) is only legal for an off-road/track bike, not
on a registered road bike, so ask how the bike will be used before working on that part.

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

## Status
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
