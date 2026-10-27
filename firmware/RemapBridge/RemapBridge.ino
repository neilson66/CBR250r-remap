// CBR250R-Remap: USB <-> Honda K-line bridge for the laptop (ESP32-C3 on COM15; C6/S3/S2 also build).
//
// The laptop (pc/remap.py) does all the talking; this board only turns line commands into K-line bytes.
// No WiFi, no SD, no polling of its own: nothing happens on the K-line unless the laptop asks.
//
// SAFETY: write/erase/recover packets are refused here (ALLOW_WRITE = false). See isLocked(). Stage 1 of the
// project is read-only; only change ALLOW_WRITE after a verified backup and a tested recovery path (README).
//
// USB serial commands (115200, line based)
//   P                 -> "OK HONDA-REMAP 1"
//   I                 -> "OK INFO chip=... tx=.. rx=.. write=LOCKED"
//   W                 -> wake-up pulse + init handshake, "OK <reply hex>" or "ERR ..."
//   T <hex bytes>     -> send packet (checksum added), read the reply (length in byte 1): "R <reply hex>"
//   Q <n> <hex bytes> -> like T, but the reply's length byte is at position n (3 for the 82 82 00 flash reads)
//   B <hex bytes>     -> send bytes as given (no checksum added, no reply read)
// Bench tests (no ECU needed, only the interface and 12 V on its pull-up):
//   E                 -> echo test: send 55 AA 00 FF and check each byte comes back
//   L                 -> read the K-line level: "OK LEVEL HIGH" (idle, ~12 V) or "OK LEVEL LOW"
//   D 0 / D 1         -> hold the K-line low (check with a multimeter) / release it (L works while held)
//
// A frame is <type...> <length> <payload...> <checksum>: length counts every byte of the frame and all bytes sum
// to 0 (mod 256). Most frames have a 1-byte type (length at byte 1); the flash-read frames 82 82 00 have 3.
//
// Build: arduino-cli, esp32 core 3.x, USB CDC On Boot: Enabled.
//   C3: esp32:esp32:esp32c3:CDCOnBoot=cdc       C6: esp32:esp32:esp32c6:CDCOnBoot=cdc
//   S3: esp32:esp32:esp32s3:USBMode=hwcdc,CDCOnBoot=cdc,FlashSize=16M

#include <Arduino.h>

// ---- wiring (same two-transistor interface as the HondaDLC scanner, see README) ----
#if CONFIG_IDF_TARGET_ESP32C3
// ESP32-C3 (e.g. "C3 SuperMini"). GPIO18/19 are its USB pins, 9 is BOOT, so use 4/5.
const char *CHIP = "ESP32-C3";
const int PIN_KTX = 4;             // to the TX transistor (base resistor)
const int PIN_KRX = 5;             // from the RX transistor collector
const int PIN_LED = 8;             // on-board LED (lit when low on the SuperMini)
const bool LED_ON = LOW;
#elif CONFIG_IDF_TARGET_ESP32C6
// Waveshare ESP32-C6-LCD-1.47: 12/13 USB, 9 BOOT, 4-7 SD/LCD, so 2/3.
const char *CHIP = "ESP32-C6";
const int PIN_KTX = 2;
const int PIN_KRX = 3;
const int PIN_LED = -1;
const bool LED_ON = HIGH;
#elif CONFIG_IDF_TARGET_ESP32S3
const char *CHIP = "ESP32-S3";
const int PIN_KTX = 1;
const int PIN_KRX = 2;
const int PIN_LED = -1;
const bool LED_ON = HIGH;
#else
const char *CHIP = "ESP32-S2";
const int PIN_KTX = 17;
const int PIN_KRX = 18;
const int PIN_LED = 15;
const bool LED_ON = HIGH;
#endif
const bool KLINE_INVERTED = true;  // the two-transistor interface inverts both directions

const bool ALLOW_WRITE = false;    // Stage 1: read only. Do not change without the checklist in README.

const uint32_t KLINE_BAUD = 10400;
const uint32_t WAKE_LOW_MS = 70;
const uint32_t WAKE_HIGH_MS = 130;
const uint32_t REPLY_TIMEOUT_MS = 300;
const uint32_t BYTE_TIMEOUT_MS = 30;

HardwareSerial &K = Serial1;

// =====================================================================================================  K-line

enum { K_TIMEOUT = -1, K_TRUNC = -2, K_BADLEN = -3, K_CHECKSUM = -4, K_NOECHO = -5, K_BADECHO = -6 };

static const char *kerrName(int e) {
  switch (e) {
    case K_TIMEOUT: return "TIMEOUT";
    case K_TRUNC: return "TRUNCATED";
    case K_BADLEN: return "BAD_LENGTH";
    case K_CHECKSUM: return "CHECKSUM";
    case K_NOECHO: return "NO_ECHO";
    case K_BADECHO: return "BAD_ECHO";
    default: return "UNKNOWN";
  }
}

static int echoErrByte = 0, echoErrSent = 0, echoErrGot = 0;

static uint8_t checksum(const uint8_t *b, size_t n) {
  uint8_t s = 0;
  for (size_t i = 0; i < n; i++) s += b[i];
  return (uint8_t)(0x100 - s);
}

static void klineBegin() {
  K.begin(KLINE_BAUD, SERIAL_8N1, PIN_KRX, PIN_KTX, KLINE_INVERTED);
}

static void drainRx() {
  while (K.available()) K.read();
}

static String hexStr(const uint8_t *b, size_t n) {
  static const char *H = "0123456789ABCDEF";
  String s;
  s.reserve(n * 3);
  for (size_t i = 0; i < n; i++) {
    if (i) s += ' ';
    s += H[b[i] >> 4];
    s += H[b[i] & 15];
  }
  return s;
}

// Read one byte from the K-line within timeout, -1 if none. Tight loop: nothing else runs on this board.
static int readByte(uint32_t timeoutMs) {
  uint32_t t0 = millis();
  for (;;) {
    if (K.available()) return K.read();
    if (millis() - t0 >= timeoutMs) return -1;
  }
}

// Send bytes and swallow our own echo (the K-line is one wire, so every byte we send comes back).
static int sendPacket(const uint8_t *b, size_t n) {
  drainRx();
  K.write(b, n);
  K.flush();
  for (size_t i = 0; i < n; i++) {
    int c = readByte(BYTE_TIMEOUT_MS + 5);
    if (c < 0) { echoErrByte = i; return K_NOECHO; }
    if ((uint8_t)c != b[i]) { echoErrByte = i; echoErrSent = b[i]; echoErrGot = c; return K_BADECHO; }
  }
  return 0;
}

// Read one reply frame whose length byte is at position lenPos. Returns its length, or a negative error code.
static int readFrame(uint8_t *buf, size_t cap, size_t lenPos = 1) {
  int c = readByte(REPLY_TIMEOUT_MS);
  if (c < 0) return K_TIMEOUT;
  buf[0] = (uint8_t)c;
  for (size_t i = 1; i <= lenPos; i++) {
    c = readByte(BYTE_TIMEOUT_MS);
    if (c < 0) return K_TRUNC;
    buf[i] = (uint8_t)c;
  }
  size_t len = buf[lenPos];
  if (len < lenPos + 2 || len > cap) return K_BADLEN;
  for (size_t i = lenPos + 1; i < len; i++) {
    c = readByte(BYTE_TIMEOUT_MS);
    if (c < 0) return K_TRUNC;
    buf[i] = (uint8_t)c;
  }
  uint8_t s = 0;
  for (size_t i = 0; i < len; i++) s += buf[i];
  if (s != 0) return K_CHECKSUM;
  return (int)len;
}

// Send pkt (checksum added) and read the reply frame.
static int query(const uint8_t *pkt, size_t n, uint8_t *out, size_t cap, size_t lenPos = 1) {
  uint8_t p[256];
  if (n > sizeof p - 1) return K_BADLEN;
  memcpy(p, pkt, n);
  p[n] = checksum(p, n);
  int r = sendPacket(p, n + 1);
  if (r < 0) return r;
  return readFrame(out, cap, lenPos);
}

// Wake-up pulse, broadcast and init. Returns the init reply length (02 04 00 FA expected) or an error.
static int wakeInit(uint8_t *out, size_t cap) {
  K.end();
  pinMode(PIN_KTX, OUTPUT);
  digitalWrite(PIN_KTX, KLINE_INVERTED ? HIGH : LOW);   // K-line low
  delay(WAKE_LOW_MS);
  digitalWrite(PIN_KTX, KLINE_INVERTED ? LOW : HIGH);   // K-line high
  delay(WAKE_HIGH_MS);
  klineBegin();
  delay(2);                                             // let the UART re-attach settle, drop any glitch byte
  drainRx();
  static const uint8_t wake[] = {0xFE, 0x04, 0xFF, 0xFF};  // no reply expected
  int r = sendPacket(wake, sizeof wake);
  if (r < 0) return r;
  delay(20);
  drainRx();
  static const uint8_t init[] = {0x72, 0x05, 0x00, 0xF0, 0x99};
  r = sendPacket(init, sizeof init);
  if (r < 0) return r;
  return readFrame(out, cap);
}

// Packets that could change the ECU's flash or put it into its write/recovery modes (from HondaECU's source):
//   7B .. (recover init), 7D .. (write init), 7E .. (erase / write / finish), 72 .. 00 F1 (enhanced diag, used
//   before recovery), 27 with anything other than E0 (E0 = the read security-access password, see README).
static bool isLocked(const uint8_t *p, size_t n) {
  if (ALLOW_WRITE || n == 0) return false;
  if (p[0] == 0x7B || p[0] == 0x7D || p[0] == 0x7E) return true;
  if (p[0] == 0x72 && n >= 4 && p[2] == 0x00 && p[3] == 0xF1) return true;
  if (p[0] == 0x27 && !(n >= 3 && p[2] == 0xE0)) return true;
  return false;
}

// ========================================================================================================  USB

static bool held = false;  // K-line held low by "D 0"

static void driveLine(bool low) {
  K.end();
  pinMode(PIN_KTX, OUTPUT);
  digitalWrite(PIN_KTX, (low == KLINE_INVERTED) ? HIGH : LOW);
}

static void release() {
  driveLine(false);
  klineBegin();
  held = false;
}

static void printErr(int r) {
  if (r == K_NOECHO) Serial.printf("ERR NO_ECHO at byte %d (check TX/RX wiring and 12V on the DLC)\n", echoErrByte);
  else if (r == K_BADECHO) Serial.printf("ERR BAD_ECHO byte %d sent %02X got %02X\n", echoErrByte, echoErrSent, echoErrGot);
  else Serial.printf("ERR %s\n", kerrName(r));
}

// Parse "AA BB CC" (spaces optional) into out. Returns count, or -1 on bad input.
static int parseHex(const String &s, uint8_t *out, size_t cap) {
  size_t n = 0;
  int nib = -1;
  for (size_t i = 0; i < s.length(); i++) {
    char ch = s[i];
    int v;
    if (ch >= '0' && ch <= '9') v = ch - '0';
    else if (ch >= 'a' && ch <= 'f') v = ch - 'a' + 10;
    else if (ch >= 'A' && ch <= 'F') v = ch - 'A' + 10;
    else if (ch == ' ' || ch == ',') continue;
    else return -1;
    if (nib < 0) nib = v;
    else {
      if (n >= cap) return -1;
      out[n++] = (uint8_t)((nib << 4) | v);
      nib = -1;
    }
  }
  return nib < 0 ? (int)n : -1;
}

static void handleLine(String line) {
  line.trim();
  if (line.length() == 0) return;
  char cmd = toupper(line[0]);
  String rest = line.substring(1);
  rest.trim();
  if (PIN_LED >= 0) digitalWrite(PIN_LED, LED_ON);
  if (held && cmd != 'D' && cmd != 'L') release();  // L may read the line while D 0 holds it low

  if (cmd == 'P') {
    Serial.println("OK HONDA-REMAP 1");
  } else if (cmd == 'I') {
    Serial.printf("OK INFO chip=%s tx=%d rx=%d baud=%u write=%s\n", CHIP, PIN_KTX, PIN_KRX, (unsigned)KLINE_BAUD,
                  ALLOW_WRITE ? "ALLOWED" : "LOCKED");
  } else if (cmd == 'D') {
    if (rest == "0") { driveLine(true); held = true; Serial.println("OK HOLDING LOW"); }
    else { release(); Serial.println("OK RELEASED"); }
  } else if (cmd == 'L') {
    K.end();
    pinMode(PIN_KRX, INPUT);
    delay(2);
    bool pinHigh = digitalRead(PIN_KRX);
    if (held) {
      pinMode(PIN_KTX, OUTPUT);  // K.end() released the TX pin; keep holding the line low
      digitalWrite(PIN_KTX, KLINE_INVERTED ? HIGH : LOW);
    } else {
      klineBegin();
    }
    bool lineHigh = KLINE_INVERTED ? !pinHigh : pinHigh;
    Serial.println(lineHigh ? "OK LEVEL HIGH" : "OK LEVEL LOW");
  } else if (cmd == 'E') {
    static const uint8_t pat[] = {0x55, 0xAA, 0x00, 0xFF};
    int r = sendPacket(pat, sizeof pat);
    if (r < 0) printErr(r);
    else Serial.println("OK ECHO");
  } else if (cmd == 'W') {
    uint8_t buf[64];
    int r = wakeInit(buf, sizeof buf);
    if (r < 0) printErr(r);
    else Serial.println("OK " + hexStr(buf, r));
  } else if (cmd == 'T' || cmd == 'B' || cmd == 'Q') {
    size_t lenPos = 1;
    if (cmd == 'Q') {  // "Q <n> <hex>"
      int sp = rest.indexOf(' ');
      lenPos = (size_t)rest.substring(0, sp < 0 ? rest.length() : sp).toInt();
      rest = sp < 0 ? "" : rest.substring(sp + 1);
    }
    uint8_t pkt[256];
    int n = parseHex(rest, pkt, sizeof pkt - 1);
    if (lenPos < 1 || lenPos > 4) {
      Serial.println("ERR BAD_LENPOS");
    } else if (n <= 0) {
      Serial.println("ERR BAD_HEX");
    } else if (isLocked(pkt, n)) {
      Serial.println("ERR LOCKED (write/erase/recover commands are disabled in this firmware)");
    } else if (cmd == 'B') {
      int r = sendPacket(pkt, n);
      if (r < 0) printErr(r);
      else Serial.println("OK");
    } else {
      uint8_t buf[256];
      int r = query(pkt, n, buf, sizeof buf, lenPos);
      if (r < 0) printErr(r);
      else Serial.println("R " + hexStr(buf, r));
    }
  } else {
    Serial.println("ERR UNKNOWN_COMMAND");
  }
  if (PIN_LED >= 0) digitalWrite(PIN_LED, !LED_ON);
}

void setup() {
  Serial.begin(115200);
  if (PIN_LED >= 0) { pinMode(PIN_LED, OUTPUT); digitalWrite(PIN_LED, !LED_ON); }
  release();  // K-line idle (high)
}

void loop() {
  static String line;
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      handleLine(line);
      line = "";
    } else if (line.length() < 600) {
      line += c;
    }
  }
}
