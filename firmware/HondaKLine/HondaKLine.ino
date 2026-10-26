// Honda PGM-FI K-line bridge for the ESP32-S2 mini (LOLIN S2 Mini).
//
// USB serial (to the PC, 115200, line based)       K-line side (UART1, 10400 8N1)
//   P                 -> "OK HONDA-KLINE 1"
//   W                 -> wake-up pulse + init handshake, "OK <reply hex>" or "ERR ..."
//   T <hex bytes>     -> send packet (checksum added here), "R <reply hex>" or "ERR ..."
//   B <hex bytes>     -> send packet as given (no checksum added, no reply expected)
// Bench tests (no ECU needed, only the interface and 12 V on its pull-up):
//   E                 -> echo test: send 55 AA 00 FF and check each byte comes back
//   L                 -> read the K-line level: "OK LEVEL HIGH" (idle, ~12 V) or "OK LEVEL LOW"
//   D 0 / D 1         -> hold the K-line low (check with a multimeter) / release it
//
// The PC does all decoding. This firmware only handles the timing: the wake-up pulse, half-duplex echo removal
// and reading one reply frame. A reply frame is: <type> <length> <payload...> <checksum>, where length counts
// every byte of the frame and all bytes sum to 0 (mod 256).
//
// Build: Arduino IDE, board "LOLIN S2 Mini" (esp32 core 2.x or 3.x), Tools > USB CDC On Boot: Enabled.

#include <Arduino.h>

// ---- wiring (see README) ----
#if CONFIG_IDF_TARGET_ESP32C3
// ESP32-C3 (e.g. "C3 SuperMini"). GPIO18/19 are its USB pins, so use 4/5.
const int PIN_KTX = 4;             // to the TX transistor (base resistor)
const int PIN_KRX = 5;             // from the RX transistor collector
const int PIN_LED = 8;             // on-board LED (lit when low on the SuperMini; just a busy blink here)
#else
// ESP32-S2 mini (LOLIN)
const int PIN_KTX = 17;
const int PIN_KRX = 18;
const int PIN_LED = 15;
#endif
const bool KLINE_INVERTED = true;  // the two-transistor interface inverts both directions

const uint32_t KLINE_BAUD = 10400;
const uint32_t WAKE_LOW_MS = 70;
const uint32_t WAKE_HIGH_MS = 130;
const uint32_t REPLY_TIMEOUT_MS = 300;
const uint32_t BYTE_TIMEOUT_MS = 30;

HardwareSerial &K = Serial1;

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

static void printHex(const uint8_t *b, size_t n) {
  for (size_t i = 0; i < n; i++) {
    if (i) Serial.print(' ');
    if (b[i] < 0x10) Serial.print('0');
    Serial.print(b[i], HEX);
  }
}

// Read one byte from the K-line within timeout, -1 if none.
static int readByte(uint32_t timeoutMs) {
  uint32_t t0 = millis();
  while (millis() - t0 < timeoutMs) {
    if (K.available()) return K.read();
    delayMicroseconds(100);
  }
  return -1;
}

// Send a packet and swallow our own echo (the K-line is one wire, so every byte we send comes back).
// Returns false with an error printed if the echo is wrong (usually a wiring problem).
static bool sendPacket(const uint8_t *b, size_t n) {
  drainRx();
  K.write(b, n);
  K.flush();
  for (size_t i = 0; i < n; i++) {
    int c = readByte(BYTE_TIMEOUT_MS + 5);
    if (c < 0) {
      Serial.printf("ERR NO_ECHO at byte %u (check TX/RX wiring and 12V on the DLC)\n", (unsigned)i);
      return false;
    }
    if ((uint8_t)c != b[i]) {
      Serial.printf("ERR BAD_ECHO byte %u sent %02X got %02X\n", (unsigned)i, b[i], c);
      return false;
    }
  }
  return true;
}

// Read one reply frame into buf. Returns its length, or a negative error code.
static int readFrame(uint8_t *buf, size_t cap) {
  int c0 = readByte(REPLY_TIMEOUT_MS);
  if (c0 < 0) return -1;  // timeout
  int c1 = readByte(BYTE_TIMEOUT_MS);
  if (c1 < 0) return -2;  // truncated
  size_t len = (size_t)c1;
  if (len < 3 || len > cap) return -3;  // nonsense length
  buf[0] = (uint8_t)c0;
  buf[1] = (uint8_t)c1;
  for (size_t i = 2; i < len; i++) {
    int c = readByte(BYTE_TIMEOUT_MS);
    if (c < 0) return -2;
    buf[i] = (uint8_t)c;
  }
  uint8_t s = 0;
  for (size_t i = 0; i < len; i++) s += buf[i];
  if (s != 0) return -4;  // checksum
  return (int)len;
}

static void reportFrameError(int r) {
  switch (r) {
    case -1: Serial.println("ERR TIMEOUT"); break;
    case -2: Serial.println("ERR TRUNCATED"); break;
    case -3: Serial.println("ERR BAD_LENGTH"); break;
    case -4: Serial.println("ERR CHECKSUM"); break;
    default: Serial.println("ERR UNKNOWN"); break;
  }
}

static void wakeUp() {
  // Take the TX pin away from the UART and drive the K-line directly.
  K.end();
  pinMode(PIN_KTX, OUTPUT);
  // K-line low = transistor on = pin HIGH when the interface inverts.
  digitalWrite(PIN_KTX, KLINE_INVERTED ? HIGH : LOW);
  delay(WAKE_LOW_MS);
  digitalWrite(PIN_KTX, KLINE_INVERTED ? LOW : HIGH);
  delay(WAKE_HIGH_MS);
  klineBegin();
  drainRx();

  // "Wake up" broadcast: no reply expected.
  static const uint8_t wake[] = {0xFE, 0x04, 0xFF, 0xFF};
  if (!sendPacket(wake, sizeof wake)) return;
  delay(20);
  drainRx();

  // Init: 72 05 00 F0 99 -> expected 02 04 00 FA
  static const uint8_t init[] = {0x72, 0x05, 0x00, 0xF0, 0x99};
  if (!sendPacket(init, sizeof init)) return;
  uint8_t buf[64];
  int r = readFrame(buf, sizeof buf);
  if (r < 0) { reportFrameError(r); return; }
  Serial.print("OK ");
  printHex(buf, r);
  Serial.println();
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
    else if (ch == ' ' || ch == ',') { continue; }
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

static void handleLine(String line) {
  line.trim();
  if (line.length() == 0) return;
  char cmd = toupper(line[0]);
  String rest = line.substring(1);
  rest.trim();
  digitalWrite(PIN_LED, HIGH);
  if (held && cmd != 'D') release();

  if (cmd == 'D') {
    if (rest == "0") { driveLine(true); held = true; Serial.println("OK HOLDING LOW"); }
    else { release(); Serial.println("OK RELEASED"); }
  } else if (cmd == 'L') {
    K.end();
    pinMode(PIN_KRX, INPUT);
    delay(2);
    bool pinHigh = digitalRead(PIN_KRX);
    klineBegin();
    bool lineHigh = KLINE_INVERTED ? !pinHigh : pinHigh;
    Serial.println(lineHigh ? "OK LEVEL HIGH" : "OK LEVEL LOW");
  } else if (cmd == 'E') {
    static const uint8_t pat[] = {0x55, 0xAA, 0x00, 0xFF};
    if (sendPacket(pat, sizeof pat)) Serial.println("OK ECHO");
  } else if (cmd == 'P') {
    Serial.println("OK HONDA-KLINE 1");
  } else if (cmd == 'W') {
    wakeUp();
  } else if (cmd == 'T' || cmd == 'B') {
    uint8_t pkt[64];
    int n = parseHex(rest, pkt, sizeof pkt - 1);
    if (n <= 0) {
      Serial.println("ERR BAD_HEX");
    } else {
      if (cmd == 'T') pkt[n] = checksum(pkt, n), n++;
      if (sendPacket(pkt, n)) {
        if (cmd == 'B') {
          Serial.println("OK");
        } else {
          uint8_t buf[256];
          int r = readFrame(buf, sizeof buf);
          if (r < 0) reportFrameError(r);
          else { Serial.print("R "); printHex(buf, r); Serial.println(); }
        }
      }
    }
  } else {
    Serial.println("ERR UNKNOWN_COMMAND");
  }
  digitalWrite(PIN_LED, LOW);
}

void setup() {
  pinMode(PIN_LED, OUTPUT);
  Serial.begin(115200);
  klineBegin();
}

void loop() {
  static String line;
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      handleLine(line);
      line = "";
    } else if (line.length() < 200) {
      line += c;
    }
  }
}
