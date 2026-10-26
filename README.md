# Honda DLC Scanner (CBR250R 2011-13, MC41)

Reads live engine data and fault codes from the 4-pin red Honda DLC under the seat. It uses an ESP32 board (main: Waveshare ESP32-C6-LCD-1.47; also ESP32-S3, C3 or S2 mini) as a
USB-to-K-line bridge and a Python app on the PC.

```
Bike DLC ──K-line── 2-transistor interface ── ESP32-C6 ──USB── PC (dlc_gui.py / honda_dlc.py)
```

## Parts

| Part | Notes |
|---|---|
| ESP32 board: Waveshare ESP32-C6-LCD-1.47 (main), ESP32-S3, C3 or S2 mini | the bridge; powered over USB-C from the PC, or from a 12 V→USB-C adaptor on the bike |
| 2 × NPN transistor | 2N3904, BC547 or 2N2222 |
| 1 × 1 kΩ | Q1 base resistor |
| 1 × 1 kΩ (½ W is fine) | K-line pull-up to 12 V |
| 1 × 47 kΩ, 1 × 10 kΩ | Q2 base divider |
| 1 × 10 kΩ | RX pull-up to 3.3 V |
| 1 × 1N4148 or 1N4007 diode | reverse protection on the 12 V feed |
| Way to plug into the DLC | a "Honda 4-pin DLC" adapter / breakout lead, or female Dupont/spade terminals pushed into the plug |
| Small stripboard/breadboard, wire, USB-C cable | |
| Multimeter | to identify the DLC pins before connecting |

Option: replace the transistors with an **L9637D** or **MC33290** K-line chip. If you do, its RX output must be
brought down to 3.3 V, and set `KLINE_INVERTED = false` in the firmware.

## Wiring

![Wiring diagram](docs/wiring.svg)

Pins (the firmware picks them from the chip):

| Board | TX (to R2/Q1) | RX (from Q2/R5) | Avoid |
|---|---|---|---|
| **Waveshare ESP32-C6-LCD-1.47** (main) | **GPIO2** | **GPIO3** | 12/13 (USB), 9 (BOOT), 4-7 (SD/LCD) |
| Waveshare ESP32-S3-LCD-1.47 | GPIO1 | GPIO2 | |
| ESP32-C3 | GPIO4 | GPIO5 | 18/19 (USB) |
| ESP32-S2 mini | GPIO17 | GPIO18 | |

**Powering it on the bike** with a 12 V→USB-C adaptor: feed the adaptor from the key-switched Bl/Bu wire (fused,
1 A) so it can't flatten the battery. Still wire the DLC ground (G) to the board GND. Unplug the adaptor when you
plug in the laptop (one USB-C port).

Text version:

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

Both stages invert, and the firmware turns on the UART's own inversion to undo it. Do **not** connect the bike's
12 V to the ESP32. Only the pull-up uses it.

**CBR250R DLC wire colours** (from the wiring diagram, traced to the ECM 33P plug):

| Wire | Signal | ECM pin |
|---|---|---|
| **Y** yellow | K-line | 30 (K-LINE) |
| **Bl/Bu** black/blue | 12 V, key on | 1 (IGP) |
| **G** green | ground | |
| **Bu** light blue | SCS: not used here (shorting it to GND makes the dash blink stored codes) | 15 (SCS) |

Check with a multimeter before connecting, key on: Bl/Bu to G should read about 12 V, and Y to G should be close to
battery voltage (the ECU pulls it up). If Y reads 0 V, stop.

## Bring-up (do these in order)
1. **Board only:** flash the firmware, then type `P` in the Serial Monitor (115200). Expect `OK HONDA-KLINE 1`.
2. **Bench, no bike:** build the interface and put 9-12 V on the pull-up (a 9 V battery or a bench supply in place of
   the DLC 12 V pin, its negative to GND). Then run `python pc/honda_dlc.py COM5 check`. Steps 2 (line idles high)
   and 3 (echo) should pass. Step 4 fails without an ECU, which is expected.
   - Manual checks: `D 0` should read about 0-1 V on the K-line with a multimeter, `D 1` should read about 12 V,
     and `L` reports the level.
3. **On the bike:** key on, kill switch at RUN. Connect to the DLC and run `check` again. All five steps should
   pass. If init fails, swap nothing else first: check that the K-line is on the right DLC pin.
4. Then `dlc_gui.py` → Connect. Blip the throttle and watch which raw bytes change.

## Using the app
One full-screen page: live values on the left, trend in the middle, raw bytes and messages along the bottom.
- **Sensors**: big live values; tick any sensors to plot them together as pens, each on its own scale (or tick Auto-scale). Hover to
  read every pen at that moment; drag to pan back, mouse wheel to zoom; **Open log…** replays a saved session.
- Every connection is logged to `logs/<date_time>/` (`live.csv`, `faults.txt`, `info.txt`). Press **Mark** or the
  space bar when the engine misbehaves; marks show as red lines on the trend.
- **Clear codes**: saves the current fault codes to the log, sends the community Honda clear command (`72 05 60 03`,
  not yet confirmed on the MC41) and re-reads the codes. If they don't clear, use the service-manual SCS method.

## Firmware
Arduino IDE → install the "esp32" boards package → board **ESP32C3 Dev Module** with *USB CDC On Boot: Enabled* (or **LOLIN S2 Mini**, where that is the default). Open
`firmware/HondaKLine/HondaKLine.ino` and upload. (If the upload doesn't start, hold button 0, tap RST, and release.)
Check it in the Serial Monitor at 115200: typing `P` should answer `OK HONDA-KLINE 1`.

## PC app
```
pip install pyserial
python pc/dlc_gui.py
```
Choose the COM port, turn the key on (engine stop switch at RUN) and click Connect. Choose `demo` to try it
without the bike. Command-line version: `python pc/honda_dlc.py COM5 live|dump|dtc|raw <hex>`.

## Status / unverified
- The protocol (wake-up, `72 05 00 F0` init, `72 05 71 <table>` reads, checksum) is the community-documented Honda
  K-line protocol. It hasn't been run on this bike yet.
- The table 0x11 field layout and the fault-code decoding are best guesses taken from other Honda models. Use the
  raw-byte view (changed bytes light up) and "Dump tables" to confirm them. For example, blip the throttle and see
  which byte moves.
