# CBR250R-Remap (2011-13 Honda CBR250R, MC41)

Read, back up and (much later, carefully) remap the stock Keihin ECU of Neil's 2012 CBR250R through its 4-pin red
DLC, with a dedicated ESP32 as a USB ↔ K-line bridge and Python on the laptop.

```
Bike DLC ──K-line── 2-transistor interface ── ESP32-C3 (RemapBridge) ──USB── laptop (pc/remap.py)
```

Built on the HondaDLC scanner (github neilson66/HondaDLC), which already talks to this bike's ECU. That project
stays as it is; the shared K-line code is in `pc/honda_dlc.py` here.

## Safety rules (read before touching the ECU)
1. **Read only until there is a verified backup.** Stage 1 never erases or writes. The bridge firmware refuses
   write/erase/recovery packets (`ALLOW_WRITE = false`), so a typo can't send one.
2. **A backup counts only when** the flash has been read twice, both reads are identical, the Honda checksum is good
   (all 57,344 bytes sum to 0 mod 256), and the file is saved with its SHA-256 in two places (USB stick + cloud).
3. **No write before:** a verified backup, a tested recovery path (see Stage 4), and a stable supply: a bench
   12-13.8 V supply of at least 2.5 A on the ECU (HondaECU's README: at 2 A transfers are slow with CRC errors),
   or the bike battery on a charger. Never with the engine running, never on this bike's battery as it is now (it
   isn't charging).
4. **Never invent map addresses.** A table is only edited once its address, size and scaling are known from
   evidence (diffs between stock images, a published definition, or the code that reads it).
5. **Fix the mechanical faults first** (stalling on closed throttle, no charging, parts on order). A remap can't
   fix a bad IACV, throttle body, fuel pump or stator, and tuning around faults gives a bad map.

## Research (2026-10-27)
Sources: open-source HondaECU (RyanHope, now MCU Innovations; forks
[Adeen117/HondaECU](https://github.com/Adeen117/HondaECU), [roccofz1/HondaECU](https://github.com/roccofz1/HondaECU),
[WiseNoodle/honda-moto-ecu-suite](https://github.com/WiseNoodle/honda-moto-ecu-suite)), the
[cbr250.net ECU tuning FAQ](https://www.cbr250.net/threads/read-first-the-ecu-tuning-faq.69649/) and
[DIY ECU tuning tutorial](https://www.cbr250.net/threads/diy-ecu-tuning-tutorial.69670/) (only their search
summaries could be read: the forum blocks automated reading), and two stock MC41 images in the HondaECU repo.

**Confirmed (from source code / files, not yet on this bike):**
- HondaECU lists the CBR250R 2012, 38770-KYJ-971: **56 KB ROM, checksum byte at 0xDFEF**. Its `ecmids.py` lists
  38770-KYJ-922 with ECM ID `01 00 B8 05 02` at image address 0xC3EE and the Keihin code at 0xDFF0.
- The repo has two stock images, `38770-KYJ-922.bin` and `38770-KYJ-971.bin` (57,344 bytes each, both
  `KYJA203` at 0xDFF0). The 922 image sums to 0 mod 256 (checksum good); the 971 image does not (sum 0xF2), so it
  may be a modified or bad read. SHA-256 of 922: `dcf710d0…44a428e1c0224` (full value in CLAUDE.md).
- The image starts with a table of 16-bit big-endian vectors (reset → 0x0082), which fits a Renesas H8/300H
  Tiny-type MCU with 56 KB flash (e.g. H8/3687). **The exact MCU is a guess**: read the chip markings when the ECU is
  open.
- The two images differ in only 16 places, which shows where calibration lives: 0x47F2, 0x49A2, 0x6A85-89,
  0x6BCA, 0x6F2A-55, 0xA001-03, **0xAD40-0xADA0 (five rows of 9 bytes, 22 bytes apart: a 2-D table)**,
  0xC3F1-F2 (ECM ID), **0xC528-0xC545 (eight 4-byte entries)** and 0xDFEF (checksum).
- HondaECU's K-line flash protocol (same framing and checksum as our diagnostic packets):
  - read (stock ECU, "security access"): after key-on, wake + `72 05 00 F0`, then `27 0B E0 "HelloHo"` and
    `27 0B E0 "wAreYou"`, then `82 82 00 09 <bank> <addr lo> <addr hi> <n>` returns n bytes (8-12 per request).
  - write: `7D 01 01 01..03`, `7D 01 02 50 47 4D`, `7D 01 03 2D 46 49` (write init), then `7E 01 ..` erase steps
    with a ~11 s wait and polling `7E 01 05` until 00, then 128-byte blocks `7E 01 06 <start> <128 bytes> <end>
    <cs1> <cs2>`, then `7E 01 09/0A/0C/0D` to finish.
  - recovery (an ECU left half-written): `7B 00 ..` init, `72 00 F1`, `27 00 01 00`, then the same erase/write.
- The cbr250.net FAQ says the MC41 ECUs ending 922 and 971 can be read and written over the red 4-pin DLC with
  HondaECU 3.3.0 (the last free version) + TunerPro, and that the paid HondaECU (MCU Innovations) costs about
  US$150. The tutorial: "Security Access", cycle the key off and on, the fuel pump keeps running and the ECU is in
  programming mode; read, check the checksum, save; then Write with "fix checksum".

**Not confirmed / unknown:**
- Whether Neil's ECU is a 922, 971 or another KYJ part number (Stage 1 reads its ECM ID).
- Whether the read sequence above works on this ECU (nobody here has tried it).
- **No map definition (XDF) for the MC41 was found.** The free XDFs in HondaECU are for other models; the
  `cbr250rr-ecu-binary-definition` XDF on GitHub is for the CBR250RR MC51 (38770-K64), a different ECU. Maps will
  have to be found by us (Stage 3).
- Whether the ECU has a boot-mode pin (for a bench read/rescue through the MCU's own bootloader). Only matters if
  K-line recovery fails.

**Other routes** (no flashing of the stock ECU): plug-in ECUs (aRacer RC SuperX lists the CBR250R 2011-16),
piggy-backs (PowerTRONIC lists the CBR250R 2011-17), the HRC kit ECU 38770-KYJ-R11, or the paid HondaECU /
Woolich tools (Woolich support for the MC41 not checked).

## Plan
1. **Stage 1a, identify (read only):** `remap.py COM15 check`, then `id`: ECM ID from table 0x00, compare with
   the known IDs. Note the ECU label (38770-KYJ-…) on the bike.
2. **Stage 1b, backup (read only):** `remap.py COM15 backup --yes`: security access, read 0x0000-0xDFFF twice,
   compare, checksum, `flash.bin` + SHA-256. Copy it off the laptop. Compare it with the stock images.
3. **Stage 2, bench rig:** spare ECU (a used KYJ ECU is cheap) on the bench with a 12 V 3 A supply, so tests and
   any first write happen on a spare, not the bike's ECU. Bench harness: ECM 33P pin 1 IGP (+12 V switched),
   ground, pin 30 K-line (pin numbers from our wiring trace; check the ECU's other power/ground pins in the manual).
4. **Stage 3, maps (offline):** a viewer on the laptop: hex + table view, the known calibration areas above,
   compare images, heat-map tables, and a definition file we build up (address, size, axes, scaling, with the
   evidence for each). Then a TunerPro XDF of what is known.
5. **Stage 4, recovery path (on the spare ECU):** prove a full write of the *unchanged* stock image and the
   recovery sequence on the spare ECU. Only then a small, reversible change.
6. **Stage 5, write to the bike's ECU:** only with Neil's say-so, every safety rule above met, unchanged
   image first.

## Hardware: the remap bridge
Same two-transistor interface as the scanner (2 × 2N3904, 1k/1k/47k/10k/10k, 1N4148), on an **ESP32-C3** (COM15
on this PC) powered from the laptop's USB. No WiFi, no SD, no phone page: the laptop does everything.

| Board | TX (to R2/Q1) | RX (from Q2/R5) | Avoid |
|---|---|---|---|
| **ESP32-C3** (remap bridge) | **GPIO4** | **GPIO5** | 18/19 (USB), 9 (BOOT) |
| Waveshare ESP32-C6-LCD-1.47 | GPIO2 | GPIO3 | 12/13, 9, 4-7 |
| Waveshare ESP32-S3-LCD-1.47 | GPIO1 | GPIO2 | |

![Wiring diagram](docs/wiring.svg)

```
DLC 12V ──|>|── 1k ──┬──────────────── DLC K-line
         1N4148      │
                     ├── C  Q1  E ── GND
   ESP TX ────── 1k ─┘ B (Q1 base)
                     │
                     └── 47k ── B  Q2  E ── GND
                               B ── 10k ── GND
                               C ──┬── ESP RX
                                   └── 10k ── 3V3
DLC GND ── ESP GND
```

DLC wires: **Y = K-line (ECM 30)**, **Bl/Bu = 12 V key-on (ECM 1)**, **G = ground**, Bu = SCS (not used). Join the
interface ground, the ESP32 GND and the DLC G (the scanner's first fault was a missing ground).

Extras for the remap work:
- **Key-on sense:** the backup needs a key off → on cycle. The pull-up is fed from key-on 12 V, so the bridge's `L`
  command already sees the key state (HIGH = on). Nothing extra to build.
- **Bench supply:** 12-13.8 V, ≥ 2.5 A, for a bench ECU and for any write. A 10 A inline fuse.
- **Later, optional:** a 3.3 V-safe connection to the ECU MCU's mode/reset pins for a boot-mode rescue, only if a
  K-line recovery ever fails. Not needed for reading.
- A long USB cable or a laptop on a stool: the laptop must stay powered and connected for the whole read (~3-5 min
  per pass at 10400 baud).

## Software
- `firmware/RemapBridge/RemapBridge.ino`: USB line commands → K-line (`P`, `I`, `W`, `T`, `Q`, `B`, `E`, `L`,
  `D 0/1`). Write/erase/recovery packets refused. Flash with
  `arduino-cli compile --fqbn esp32:esp32:esp32c3:CDCOnBoot=cdc firmware/RemapBridge` then
  `arduino-cli upload --fqbn esp32:esp32:esp32c3:CDCOnBoot=cdc -p COM15 firmware/RemapBridge`.
- `pc/honda_dlc.py`: shared K-line base (framing, checksum, `SerialLink`, `Ecu`, table 0x11 layout, fault codes).
- `pc/remap.py`: `check`, `id`, `backup --yes`, `verify <bin>`, `compare <a> <b>`; `demo` instead of the port runs
  against a pretend ECU. Backups go to `backups/<time>_backup/` (`read1.bin`, `read2.bin`, `flash.bin`,
  `flash.bin.sha256`, `info.json`), not committed to git.
- `reference/stock_bins/`: the two stock images from HondaECU (local only, not committed).

```
pip install pyserial
python pc/remap.py COM15 check
python pc/remap.py COM15 id
```
