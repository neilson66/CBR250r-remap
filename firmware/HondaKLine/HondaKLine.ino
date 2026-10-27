// Honda PGM-FI K-line scanner for ESP32 (main board: Waveshare ESP32-C6-LCD-1.47).
//
// Two ways to use it:
//  1. Phone (standalone): the board runs its own WiFi hotspot "CBR250R-DLC" (password "hondacbr250"). Open
//     http://192.168.4.1 on the phone: live values, trend, raw bytes, fault codes, clear codes, table dump, marks
//     and log downloads. The board polls the ECU itself and logs every session to the microSD card (/S0001/...).
//  2. PC (USB bridge): pc/dlc_gui.py or pc/honda_dlc.py send line commands over USB. As soon as a USB command
//     arrives the board stops its own polling and hands the K-line to the PC (until 8 s after the last command).
//
// USB serial commands (115200, line based)
//   P                 -> "OK HONDA-KLINE 1"
//   W                 -> wake-up pulse + init handshake, "OK <reply hex>" or "ERR ..."
//   T <hex bytes>     -> send packet (checksum added here), "R <reply hex>" or "ERR ..."
//   B <hex bytes>     -> send packet as given (no checksum added, no reply expected)
// Bench tests (no ECU needed, only the interface and 12 V on its pull-up):
//   E                 -> echo test: send 55 AA 00 FF and check each byte comes back
//   L                 -> read the K-line level: "OK LEVEL HIGH" (idle, ~12 V) or "OK LEVEL LOW"
//   D 0 / D 1         -> hold the K-line low (check with a multimeter) / release it (L works while held)
//
// A reply frame is: <type> <length> <payload...> <checksum>, where length counts every byte of the frame and all
// bytes sum to 0 (mod 256). Decoding happens on the phone page (page.h) and on the PC (honda_dlc.py).
//
// Build: Arduino IDE / arduino-cli, esp32 core 3.x, USB CDC On Boot: Enabled.
//   C6: esp32:esp32:esp32c6:CDCOnBoot=cdc   S3: esp32:esp32:esp32s3:USBMode=hwcdc,CDCOnBoot=cdc,FlashSize=16M

#include <Arduino.h>
#include <SD.h>
#include <SPI.h>
#include <WebServer.h>
#include <WiFi.h>

#include "page.h"

// ---- wiring (see README) ----
#if CONFIG_IDF_TARGET_ESP32C3
// ESP32-C3 (e.g. "C3 SuperMini"). GPIO18/19 are its USB pins, so use 4/5.
const int PIN_KTX = 4;             // to the TX transistor (base resistor)
const int PIN_KRX = 5;             // from the RX transistor collector
const int PIN_LED = 8;             // on-board LED (lit when low on the SuperMini; just a busy blink here)
#elif CONFIG_IDF_TARGET_ESP32C6
// ESP32-C6 (Waveshare ESP32-C6-LCD-1.47). GPIO12/13 are its USB pins, 9 is BOOT and 4-7 go to the SD card and
// LCD, so use 2/3 on the top header. Its LED is an RGB pixel, so no busy LED here.
const int PIN_KTX = 2;
const int PIN_KRX = 3;
const int PIN_LED = -1;
// microSD (SPI, shared with the LCD; Waveshare wiki): SCLK 7, MISO 5, MOSI 6, CS 4. LCD CS 14 is held high.
#define HAVE_SD 1
const int SD_SCK = 7, SD_MISO = 5, SD_MOSI = 6, SD_CS = 4, LCD_CS = 14;
#elif CONFIG_IDF_TARGET_ESP32S3
// ESP32-S3 (Waveshare ESP32-S3-LCD-1.47, the test board; LCD removed). GPIO1/2 are on the 3V3/GND header and
// not used by the microSD slot or USB. Its LED is an RGB pixel, so no busy LED here. (SD not wired up here.)
const int PIN_KTX = 1;
const int PIN_KRX = 2;
const int PIN_LED = -1;
#else
// ESP32-S2 mini (LOLIN)
const int PIN_KTX = 17;
const int PIN_KRX = 18;
const int PIN_LED = 15;
#endif
const bool KLINE_INVERTED = true;  // the two-transistor interface inverts both directions

const char *AP_SSID = "CBR250R-DLC";
const char *AP_PASS = "hondacbr250";  // at least 8 characters

const uint32_t KLINE_BAUD = 10400;
const uint32_t WAKE_LOW_MS = 70;
const uint32_t WAKE_HIGH_MS = 130;
const uint32_t REPLY_TIMEOUT_MS = 300;
const uint32_t BYTE_TIMEOUT_MS = 30;
const uint32_t BRIDGE_HOLD_MS = 8000;   // USB bridge keeps the K-line this long after the last PC command
const int EXTRA_EVERY = 10;             // read the extra tables every N live samples
// Tables other than 0x11 that may hold O2 sensor, fuel trim and status flags (same list as the PC app).
const uint8_t EXTRA_CANDIDATES[] = {0x10, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17, 0x20, 0x21, 0x60, 0x61, 0xD0, 0xD1};

HardwareSerial &K = Serial1;
WebServer server(80);

// =====================================================================================================  K-line

enum { K_TIMEOUT = -1, K_TRUNC = -2, K_BADLEN = -3, K_CHECKSUM = -4, K_NOECHO = -5, K_BADECHO = -6,
       K_WRONG = -7 };

static const char *kerrName(int e) {
  switch (e) {
    case K_TIMEOUT: return "TIMEOUT";
    case K_TRUNC: return "TRUNCATED";
    case K_BADLEN: return "BAD_LENGTH";
    case K_CHECKSUM: return "CHECKSUM";
    case K_NOECHO: return "NO_ECHO";
    case K_BADECHO: return "BAD_ECHO";
    case K_WRONG: return "WRONG_REPLY";
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

// Read one byte from the K-line within timeout, -1 if none. Sleeps a tick between polls so WiFi keeps running.
static int readByte(uint32_t timeoutMs) {
  uint32_t t0 = millis();
  for (;;) {
    if (K.available()) return K.read();
    if (millis() - t0 >= timeoutMs) return -1;
    vTaskDelay(1);
  }
}

// Send a packet and swallow our own echo (the K-line is one wire, so every byte we send comes back).
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

// Read one reply frame into buf. Returns its length, or a negative error code.
static int readFrame(uint8_t *buf, size_t cap) {
  int c0 = readByte(REPLY_TIMEOUT_MS);
  if (c0 < 0) return K_TIMEOUT;
  int c1 = readByte(BYTE_TIMEOUT_MS);
  if (c1 < 0) return K_TRUNC;
  size_t len = (size_t)c1;
  if (len < 3 || len > cap) return K_BADLEN;
  buf[0] = (uint8_t)c0;
  buf[1] = (uint8_t)c1;
  for (size_t i = 2; i < len; i++) {
    int c = readByte(BYTE_TIMEOUT_MS);
    if (c < 0) return K_TRUNC;
    buf[i] = (uint8_t)c;
  }
  uint8_t s = 0;
  for (size_t i = 0; i < len; i++) s += buf[i];
  if (s != 0) return K_CHECKSUM;
  return (int)len;
}

// Send pkt (checksum added) and read the reply frame.
static int query(const uint8_t *pkt, size_t n, uint8_t *out, size_t cap) {
  uint8_t p[64];
  if (n > sizeof p - 1) return K_BADLEN;
  memcpy(p, pkt, n);
  p[n] = checksum(p, n);
  int r = sendPacket(p, n + 1);
  if (r < 0) return r;
  return readFrame(out, cap);
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

// Read table t; copies its data bytes to out and returns their count, or an error.
static int readTable(uint8_t t, uint8_t *out, size_t cap) {
  uint8_t req[] = {0x72, 0x05, 0x71, t};
  uint8_t f[64];
  int r = query(req, sizeof req, f, sizeof f);
  if (r < 0) return r;
  if (r < 5 || f[2] != 0x71 || f[3] != t) return K_WRONG;
  int n = r - 5;
  if ((size_t)n > cap) n = cap;
  memcpy(out, f + 4, n);
  return n;
}

// ===================================================================================  shared state (locked)

SemaphoreHandle_t mtx;    // everything in this section
SemaphoreHandle_t sdMtx;  // the SD card (ECU task writes, web server reads)
#define LOCK() xSemaphoreTake(mtx, portMAX_DELAY)
#define UNLOCK() xSemaphoreGive(mtx)

struct Sample {
  uint32_t seq, t;  // t = ms since the session started
  uint8_t len;
  uint8_t d[24];
  char mark[12];
};
const int RING_N = 2000;  // ~3 minutes at 10/s; the phone keeps its own longer history
Sample *ring = nullptr;
uint32_t lastSeq = 0;

struct Msg { uint32_t id; char text[110]; };
const int MSG_N = 40;
Msg msgs[MSG_N];
uint32_t lastMsgId = 0;

char statusText[80] = "starting";
char statusKind[12] = "idle";  // idle, waking, ok, bridge, busy
char sessName[8] = "";
bool sdOk = false;

const int MAX_EXTRAS = 16;
int nExtras = 0;
uint8_t exTable[MAX_EXTRAS], exLen[MAX_EXTRAS], exData[MAX_EXTRAS][24];

uint8_t fRows[2][12][24], fLen[2][12], fIdx[2][12];
int fN[2] = {0, 0};
uint32_t faultsSeq = 0;

bool reqFaults = false, reqClear = false, reqDump = false;
char pendingMark[12] = "";
int markCount = 0;
String dumpJson = "{}";

bool timeKnown = false;
int64_t phoneOffsetMs = 0;  // phone epoch ms - millis()
int tzMin = 0;              // phone getTimezoneOffset()
String pendingInfo;         // text for the session's info.txt (written by the ECU task)

static void msgLocked(const String &s) {
  Msg &m = msgs[++lastMsgId % MSG_N];
  m.id = lastMsgId;
  strlcpy(m.text, s.c_str(), sizeof m.text);
}

static void msg(const String &s) {
  LOCK();
  msgLocked(s);
  UNLOCK();
}

static void setStatus(const char *kind, const String &text) {
  LOCK();
  strlcpy(statusKind, kind, sizeof statusKind);
  strlcpy(statusText, text.c_str(), sizeof statusText);
  UNLOCK();
}

static String clockStr(uint32_t ms) {  // local time of millis() ms, or "" if the phone hasn't set the clock
  if (!timeKnown) return "";
  int64_t local = (int64_t)ms + phoneOffsetMs - (int64_t)tzMin * 60000;
  time_t secs = (time_t)(local / 1000);
  struct tm tmv;
  gmtime_r(&secs, &tmv);
  char b[16];
  snprintf(b, sizeof b, "%02d:%02d:%02d.%d", tmv.tm_hour, tmv.tm_min, tmv.tm_sec, (int)((local % 1000) / 100));
  return b;
}

// ==========================================================================================  SD session logs

int nextSessNum = 1;

#ifdef HAVE_SD
static void sdInit() {
  pinMode(LCD_CS, OUTPUT);
  digitalWrite(LCD_CS, HIGH);  // keep the LCD off the shared SPI bus
  SPI.begin(SD_SCK, SD_MISO, SD_MOSI, SD_CS);
  if (!SD.begin(SD_CS, SPI, 20000000)) {
    msg("no microSD card (sessions are not logged on the board)");
    return;
  }
  sdOk = true;
  File root = SD.open("/");
  for (File f = root.openNextFile(); f; f = root.openNextFile()) {
    const char *nm = f.name();
    if (nm[0] == '/') nm++;
    if (f.isDirectory() && nm[0] == 'S' && strlen(nm) == 5) {
      int n = atoi(nm + 1);
      if (n >= nextSessNum) nextSessNum = n + 1;
    }
    f.close();
  }
  root.close();
  msg("microSD ready, " + String((uint32_t)(SD.cardSize() / (1024 * 1024))) + " MB, next session S" +
      String(nextSessNum));
}
#else
static void sdInit() { msg("no microSD on this board"); }
#endif

static void sdAppend(const String &path, const String &text) {
  if (!sdOk) return;
  if (xSemaphoreTake(sdMtx, pdMS_TO_TICKS(3000)) != pdTRUE) return;
  File f = SD.open(path, FILE_APPEND);
  if (f) {
    f.print(text);
    f.close();
  }
  xSemaphoreGive(sdMtx);
}

// ======================================================================================  ECU task (auto mode)

struct Session {
  bool active = false;
  char name[8];
  uint32_t t0 = 0;
  File csv;
  String wbuf;
  uint32_t lastFlush = 0;
  uint8_t extras[MAX_EXTRAS];
  int n = 0;
  int errors = 0;
} S;

static String dir() { return String("/") + S.name; }

static void flushCsv(bool force) {
  if (!sdOk || !S.csv || S.wbuf.length() == 0) return;
  if (xSemaphoreTake(sdMtx, force ? pdMS_TO_TICKS(3000) : 0) != pdTRUE) {
    if (S.wbuf.length() > 32768) { S.wbuf = ""; msg("SD busy too long, some log rows dropped"); }
    return;
  }
  S.csv.print(S.wbuf);
  S.csv.flush();
  xSemaphoreGive(sdMtx);
  S.wbuf = "";
  S.lastFlush = millis();
}

// Fault codes from the CBR250R workshop manual DTC index (5-14); same as DTC_NAMES in pc/honda_dlc.py.
static const char *dtcName(int main, int sub) {
  static const struct { uint8_t m, s; const char *n; } T[] = {
      {1, 1, "MAP sensor low voltage"}, {1, 2, "MAP sensor high voltage"},
      {7, 1, "coolant temp sensor low voltage"}, {7, 2, "coolant temp sensor high voltage"},
      {8, 1, "throttle sensor low voltage"}, {8, 2, "throttle sensor high voltage"},
      {9, 1, "intake air temp sensor low voltage"}, {9, 2, "intake air temp sensor high voltage"},
      {12, 1, "fuel injector circuit"}, {21, 1, "O2 sensor"}, {29, 1, "IACV circuit"}, {33, 2, "ECM EEPROM"},
      {54, 1, "bank angle sensor low voltage"}, {54, 2, "bank angle sensor high voltage"}};
  for (auto &e : T)
    if (e.m == main && e.s == sub) return e.n;
  return nullptr;
}

static String faultText() {  // the fault rows as text, like the PC app's faults.txt
  String s;
  LOCK();
  const char *names[2] = {"current", "past"};
  for (int k = 0; k < 2; k++) {
    String codes;
    for (int i = 0; i < fN[k]; i++) {
      char h[8];
      snprintf(h, sizeof h, "%02X", fIdx[k][i]);
      s += String(names[k]) + " " + h + ": " + hexStr(fRows[k][i], fLen[k][i]) + "\n";
      for (int j = 1; j + 1 < fLen[k][i]; j += 2) {
        int m = fRows[k][i][j], sb = fRows[k][i][j + 1];
        if (!m) continue;
        const char *nm = dtcName(m, sb);
        codes += (codes.length() ? ", " : "") + String(m) + "-" + String(sb) + (nm ? String(" (") + nm + ")" : "");
      }
    }
    s += String(names[k]) + " fault codes: " + (codes.length() ? codes : "none") + "\n";
  }
  UNLOCK();
  return s;
}

static void readFaults() {
  const uint8_t cmds[2] = {0x74, 0x73};  // current, past
  for (int k = 0; k < 2; k++) {
    int n = 0;
    uint8_t rows[12][24], lens[12], idx[12];
    for (int i = 1; i <= 0x0C; i++) {
      uint8_t req[] = {0x72, 0x05, cmds[k], (uint8_t)i};
      uint8_t f[64];
      int r = query(req, sizeof req, f, sizeof f);
      if (r < 5) break;
      int len = min(r - 5, 24);
      memcpy(rows[n], f + 4, len);
      lens[n] = len;
      idx[n] = i;
      n++;
    }
    LOCK();
    memcpy(fRows[k], rows, sizeof rows);
    memcpy(fLen[k], lens, sizeof lens);
    memcpy(fIdx[k], idx, sizeof idx);
    fN[k] = n;
    faultsSeq++;
    UNLOCK();
  }
  String t = faultText();
  msg("fault codes read");
  if (S.active) sdAppend(dir() + "/faults.txt", "--- " + clockStr(millis()) + " +" + String((millis() - S.t0) / 1000) + " s\n" + t + "\n");
}

static void clearFaults() {
  msg("saving the current fault codes before clearing…");
  readFaults();
  uint8_t req[] = {0x72, 0x05, 0x60, 0x03};
  uint8_t f[64];
  int r = query(req, sizeof req, f, sizeof f);
  String line = r > 0 ? "clear command sent, ECU replied " + hexStr(f, r) : String("clear command: no valid reply (") + kerrName(r) + ")";
  msg(line);
  if (S.active) sdAppend(dir() + "/faults.txt", "CLEAR: " + line + "\n");
  delay(500);
  readFaults();
}

static void doDump() {
  setStatus("busy", "dumping tables 00-FF (about a minute)…");
  msg("dumping tables 00-FF (about a minute)…");
  String j = "{";
  int found = 0;
  for (int t = 0; t < 0x100; t++) {
    uint8_t d[64];
    int n = readTable((uint8_t)t, d, sizeof d);
    if (n < 0) continue;
    char k[8];
    snprintf(k, sizeof k, "%02X", t);
    j += String(found ? "," : "") + "\n \"" + k + "\": \"" + hexStr(d, n) + "\"";
    found++;
  }
  j += "\n}\n";
  LOCK();
  dumpJson = j;
  UNLOCK();
  if (S.active) sdAppend(dir() + "/dump.json", j);
  msg("dump done: " + String(found) + " tables answered" + (S.active && sdOk ? ", saved as " + dir() + "/dump.json" : ""));
}

static void sessionEnd() {
  if (!S.active) return;
  flushCsv(true);
  if (S.csv) {
    xSemaphoreTake(sdMtx, pdMS_TO_TICKS(3000));
    S.csv.close();
    xSemaphoreGive(sdMtx);
  }
  msg(String("session ") + S.name + " closed");
  S.active = false;
  LOCK();
  sessName[0] = 0;
  UNLOCK();
}

static void sessionStart(const uint8_t *init, int n) {
  snprintf(S.name, sizeof S.name, "S%04d", nextSessNum++);
  S.t0 = millis();
  S.n = 0;
  S.errors = 0;
  S.wbuf = "";
  LOCK();
  strlcpy(sessName, S.name, sizeof sessName);
  lastSeq++;  // new session: the phone starts a fresh history when the session name changes
  UNLOCK();
  msg(String("ECU answered: ") + hexStr(init, n) + ", session " + S.name);
  if (sdOk) {
    xSemaphoreTake(sdMtx, pdMS_TO_TICKS(3000));
    SD.mkdir(dir());
    xSemaphoreGive(sdMtx);
  }
  String info = String("session ") + S.name + "\ninit reply " + hexStr(init, n) + "\nboard uptime " +
                String(millis() / 1000) + " s\n";
  if (timeKnown) info += "started " + clockStr(S.t0) + " (phone clock)\n";
  setStatus("busy", "reading fault codes…");
  S.active = true;  // so readFaults() logs to this session
  readFaults();
  // Probe the extra tables once; the ones that answer are logged about once a second.
  int ne = 0;
  info += "extra tables:\n";
  for (uint8_t t : EXTRA_CANDIDATES) {
    uint8_t d[64];
    int r = readTable(t, d, sizeof d);
    if (r < 0) continue;
    if (ne < MAX_EXTRAS) S.extras[ne++] = t;
    char h[24];
    snprintf(h, sizeof h, "table %02X (%d): ", t, r);
    info += h + hexStr(d, r) + "\n";
  }
  LOCK();
  nExtras = ne;
  for (int i = 0; i < ne; i++) { exTable[i] = S.extras[i]; exLen[i] = 0; }
  UNLOCK();
  sdAppend(dir() + "/info.txt", info);
  if (sdOk) {
    xSemaphoreTake(sdMtx, pdMS_TO_TICKS(3000));
    S.csv = SD.open(dir() + "/live.csv", FILE_WRITE);
    xSemaphoreGive(sdMtx);
    String hdr = "time,elapsed_s,mark,raw_11";
    for (int i = 0; i < ne; i++) {
      char h[10];
      snprintf(h, sizeof h, ",raw_%02X", S.extras[i]);
      hdr += h;
    }
    S.wbuf = hdr + "\n";
  }
  setStatus("ok", String("connected — session ") + S.name + (sdOk ? ", logging to SD" : ", no SD"));
}

static void autoStep() {
  static uint32_t nextTry = 0;
  if (!S.active) {
    if ((int32_t)(millis() - nextTry) < 0) { vTaskDelay(pdMS_TO_TICKS(20)); return; }
    setStatus("waking", "waking the ECU…");
    uint8_t buf[32];
    int r = wakeInit(buf, sizeof buf);
    if (r == 4 && buf[0] == 0x02 && buf[1] == 0x04 && buf[2] == 0x00 && buf[3] == 0xFA) {
      sessionStart(buf, r);
    } else {
      setStatus("idle", r == K_NOECHO ? "no K-line echo — check the interface and that the key is on"
                                      : "no ECU answer — key on, kill switch RUN? (retrying)");
      nextTry = millis() + 3000;
    }
    return;
  }

  // requests from the phone
  bool f, c, d;
  String info;
  LOCK();
  f = reqFaults; c = reqClear; d = reqDump;
  reqFaults = reqClear = reqDump = false;
  info = pendingInfo;
  pendingInfo = "";
  UNLOCK();
  if (info.length()) sdAppend(dir() + "/info.txt", info);
  if (c) clearFaults();
  else if (f) readFaults();
  if (d) doDump();
  if (c || d) setStatus("ok", String("connected — session ") + S.name + (sdOk ? ", logging to SD" : ", no SD"));

  uint8_t data[32];
  int n = readTable(0x11, data, sizeof data);
  if (n < 0) {
    if (++S.errors >= 5) {
      msg(String("lost the ECU (") + kerrName(n) + ")");
      sessionEnd();
      setStatus("idle", "lost the ECU — retrying");
      nextTry = millis() + 1000;
    }
    return;
  }
  S.errors = 0;

  String exCols;
  if (S.n % EXTRA_EVERY == 0) {
    for (int i = 0; i < nExtras; i++) {
      uint8_t d2[64];
      int r = readTable(S.extras[i], d2, sizeof d2);
      if (r >= 0) {
        LOCK();
        exLen[i] = min(r, 24);
        memcpy(exData[i], d2, exLen[i]);
        UNLOCK();
      }
      exCols += "," + (r >= 0 ? hexStr(d2, r) : String(""));
    }
  } else {
    for (int i = 0; i < nExtras; i++) exCols += ",";
  }
  S.n++;

  uint32_t now = millis();
  char mark[12];
  LOCK();
  strlcpy(mark, pendingMark, sizeof mark);
  pendingMark[0] = 0;
  Sample &s = ring[++lastSeq % RING_N];
  s.seq = lastSeq;
  s.t = now - S.t0;
  s.len = min(n, 24);
  memcpy(s.d, data, s.len);
  strlcpy(s.mark, mark, sizeof s.mark);
  UNLOCK();

  if (sdOk && S.csv) {
    char el[16];
    snprintf(el, sizeof el, "%.1f", (now - S.t0) / 1000.0);
    S.wbuf += clockStr(now) + "," + el + "," + mark + "," + hexStr(data, n) + exCols + "\n";
    if (now - S.lastFlush > 1000 || S.wbuf.length() > 4096) flushCsv(false);
  }
  vTaskDelay(pdMS_TO_TICKS(40));
}

// ===================================================================================  USB bridge (PC mode)

static bool held = false;  // K-line held low by "D 0"
static uint32_t lastUsbMs = 0;
static bool usbSeen = false;

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
  if (line == "I" || line == "i") {  // status report; doesn't take the K-line from the board's own polling
    LOCK();
    Serial.printf("OK INFO status=[%s] %s session=%s sd=%d samples=%u wifi=%s %s\n", statusKind, statusText,
                  sessName, sdOk, (unsigned)lastSeq, AP_SSID, WiFi.softAPIP().toString().c_str());
    uint32_t m0 = lastMsgId >= 10 ? lastMsgId - 9 : 1;
    for (uint32_t q = m0; q <= lastMsgId; q++)
      if (msgs[q % MSG_N].id == q) Serial.printf("  msg %s\n", msgs[q % MSG_N].text);
    UNLOCK();
    return;
  }
  if (!usbSeen || millis() - lastUsbMs > BRIDGE_HOLD_MS) {
    sessionEnd();
    msg("laptop connected over USB: the K-line is handed to the PC app");
    setStatus("bridge", "laptop connected (USB bridge) — the PC app has the K-line");
  }
  usbSeen = true;
  lastUsbMs = millis();
  char cmd = toupper(line[0]);
  String rest = line.substring(1);
  rest.trim();
  if (PIN_LED >= 0) digitalWrite(PIN_LED, HIGH);
  if (held && cmd != 'D' && cmd != 'L') release();  // L may read the line while D 0 holds it low

  if (cmd == 'D') {
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
  } else if (cmd == 'P') {
    Serial.println("OK HONDA-KLINE 1");
  } else if (cmd == 'W') {
    uint8_t buf[64];
    int r = wakeInit(buf, sizeof buf);
    if (r < 0) printErr(r);
    else Serial.println("OK " + hexStr(buf, r));
  } else if (cmd == 'T' || cmd == 'B') {
    uint8_t pkt[64];
    int n = parseHex(rest, pkt, sizeof pkt - 1);
    if (n <= 0) {
      Serial.println("ERR BAD_HEX");
    } else if (cmd == 'B') {
      int r = sendPacket(pkt, n);
      if (r < 0) printErr(r);
      else Serial.println("OK");
    } else {
      uint8_t buf[256];
      int r = query(pkt, n, buf, sizeof buf);
      if (r < 0) printErr(r);
      else Serial.println("R " + hexStr(buf, r));
    }
  } else {
    Serial.println("ERR UNKNOWN_COMMAND");
  }
  if (PIN_LED >= 0) digitalWrite(PIN_LED, LOW);
}

static void ecuTask(void *) {
  String line;
  for (;;) {
    while (Serial.available()) {
      char c = (char)Serial.read();
      if (c == '\n' || c == '\r') {
        handleLine(line);
        line = "";
      } else if (line.length() < 200) {
        line += c;
      }
    }
    if (usbSeen && millis() - lastUsbMs < BRIDGE_HOLD_MS) {
      vTaskDelay(pdMS_TO_TICKS(2));
      continue;
    }
    if (usbSeen) {  // the PC went quiet: take the K-line back
      usbSeen = false;
      if (held) release();
      msg("USB bridge idle: the board is polling the ECU itself again");
    }
    autoStep();
  }
}

// =========================================================================================  web server

static String jsonEsc(const char *s) {
  String o;
  for (; *s; s++) {
    if (*s == '"' || *s == '\\') { o += '\\'; o += *s; }
    else if ((uint8_t)*s < 0x20) o += ' ';
    else o += *s;
  }
  return o;
}

static void apiLive() {
  uint32_t since = server.arg("since").toInt();
  uint32_t mSince = server.arg("m").toInt();
  String j;
  j.reserve(16000);
  LOCK();
  j += "{\"st\":\"" + jsonEsc(statusText) + "\",\"kind\":\"" + statusKind + "\",\"sess\":\"" + sessName +
       "\",\"sd\":" + (sdOk ? "true" : "false") + ",\"seq\":" + lastSeq + ",\"fseq\":" + faultsSeq +
       ",\"time\":" + (timeKnown ? "true" : "false") + ",\"s\":[";
  uint32_t first = lastSeq >= RING_N ? lastSeq - RING_N + 1 : 1;
  if (since + 1 > first) first = since + 1;
  if (lastSeq >= 400 && first < lastSeq - 399) first = lastSeq - 399;  // at most 400 per reply
  bool comma = false;
  for (uint32_t q = first; q <= lastSeq; q++) {
    Sample &s = ring[q % RING_N];
    if (s.seq != q) continue;
    j += String(comma ? "," : "") + "[" + s.seq + "," + s.t + ",\"" + hexStr(s.d, s.len) + "\",\"" + s.mark + "\"]";
    comma = true;
  }
  j += "],\"ex\":{";
  for (int i = 0; i < nExtras; i++) {
    char k[4];
    snprintf(k, sizeof k, "%02X", exTable[i]);
    j += String(i ? "," : "") + "\"" + k + "\":\"" + hexStr(exData[i], exLen[i]) + "\"";
  }
  j += "},\"msgs\":[";
  comma = false;
  uint32_t m0 = lastMsgId >= MSG_N ? lastMsgId - MSG_N + 1 : 1;
  if (mSince + 1 > m0) m0 = mSince + 1;
  for (uint32_t q = m0; q <= lastMsgId; q++) {
    Msg &m = msgs[q % MSG_N];
    if (m.id != q) continue;
    j += String(comma ? "," : "") + "[" + m.id + ",\"" + jsonEsc(m.text) + "\"]";
    comma = true;
  }
  j += "]}";
  UNLOCK();
  server.send(200, "application/json", j);
}

static void apiFaults() {
  String j = "{";
  LOCK();
  j += "\"seq\":" + String(faultsSeq);
  const char *names[2] = {"current", "past"};
  for (int k = 0; k < 2; k++) {
    j += String(",\"") + names[k] + "\":[";
    for (int i = 0; i < fN[k]; i++)
      j += String(i ? "," : "") + "[" + fIdx[k][i] + ",\"" + hexStr(fRows[k][i], fLen[k][i]) + "\"]";
    j += "]";
  }
  UNLOCK();
  j += "}";
  server.send(200, "application/json", j);
}

static void apiMark() {
  LOCK();
  String m = "M" + String(++markCount);
  if (pendingMark[0]) strlcat(pendingMark, " ", sizeof pendingMark);
  strlcat(pendingMark, m.c_str(), sizeof pendingMark);
  msgLocked("mark " + m + (timeKnown ? " at " + clockStr(millis()) : ""));
  UNLOCK();
  server.send(200, "application/json", "{\"mark\":\"" + m + "\"}");
}

static void apiCmd() {
  String c = server.arg("c");
  LOCK();
  bool active = S.active;
  if (active) {
    if (c == "faults") reqFaults = true;
    else if (c == "clear") reqClear = true;
    else if (c == "dump") reqDump = true;
  }
  UNLOCK();
  server.send(200, "application/json", active ? "{\"ok\":true}" : "{\"ok\":false,\"why\":\"not connected to the ECU\"}");
}

static void apiTime() {
  int64_t ms = atoll(server.arg("ms").c_str());
  int tz = server.arg("tz").toInt();
  if (ms > 1600000000000LL) {
    LOCK();
    bool first = !timeKnown;
    phoneOffsetMs = ms - (int64_t)millis();
    tzMin = tz;
    timeKnown = true;
    if (first && S.active) pendingInfo += "phone clock " + clockStr(millis()) + " at +" + String((millis() - S.t0) / 1000) + " s\n";
    UNLOCK();
  }
  server.send(200, "application/json", "{\"ok\":true}");
}

static void apiLogs() {
  String j = "[";
  if (sdOk && xSemaphoreTake(sdMtx, pdMS_TO_TICKS(2000)) == pdTRUE) {
    File root = SD.open("/");
    bool c1 = false;
    for (File d = root.openNextFile(); d; d = root.openNextFile()) {
      String dn = d.name();
      if (dn.startsWith("/")) dn = dn.substring(1);
      if (d.isDirectory() && dn.length() == 5 && dn[0] == 'S') {
        j += String(c1 ? "," : "") + "{\"s\":\"" + dn + "\",\"f\":[";
        bool c2 = false;
        for (File f = d.openNextFile(); f; f = d.openNextFile()) {
          String fn = f.name();
          int sl = fn.lastIndexOf('/');
          if (sl >= 0) fn = fn.substring(sl + 1);
          j += String(c2 ? "," : "") + "[\"" + fn + "\"," + String((uint32_t)f.size()) + "]";
          c2 = true;
          f.close();
        }
        j += "]}";
        c1 = true;
      }
      d.close();
    }
    root.close();
    xSemaphoreGive(sdMtx);
  }
  j += "]";
  server.send(200, "application/json", j);
}

static void apiDownload() {
  String p = server.arg("f");
  if (!sdOk || !p.startsWith("/S") || p.indexOf("..") >= 0) { server.send(404, "text/plain", "not found"); return; }
  if (xSemaphoreTake(sdMtx, pdMS_TO_TICKS(3000)) != pdTRUE) { server.send(503, "text/plain", "SD busy"); return; }
  File f = SD.open(p);
  if (!f || f.isDirectory()) {
    if (f) f.close();
    xSemaphoreGive(sdMtx);
    server.send(404, "text/plain", "not found");
    return;
  }
  String name = p.substring(1);
  name.replace("/", "_");
  server.sendHeader("Content-Disposition", "attachment; filename=\"" + name + "\"");
  server.streamFile(f, p.endsWith(".csv") ? "text/csv" : p.endsWith(".json") ? "application/json" : "text/plain");
  f.close();
  xSemaphoreGive(sdMtx);
}

static void apiDump() {
  LOCK();
  String j = dumpJson;
  UNLOCK();
  server.send(200, "application/json", j);
}

// =======================================================================================  setup / loop

void setup() {
  if (PIN_LED >= 0) pinMode(PIN_LED, OUTPUT);
  Serial.begin(115200);
  mtx = xSemaphoreCreateMutex();
  sdMtx = xSemaphoreCreateMutex();
  ring = (Sample *)calloc(RING_N, sizeof(Sample));
  klineBegin();
  sdInit();

  WiFi.mode(WIFI_AP);
  WiFi.softAP(AP_SSID, AP_PASS);
  msg(String("WiFi hotspot ") + AP_SSID + ", open http://" + WiFi.softAPIP().toString());

  server.on("/", [] {
    server.sendHeader("Cache-Control", "no-cache");
    server.send_P(200, "text/html", PAGE_HTML);
  });
  server.on("/api/live", apiLive);
  server.on("/api/faults", apiFaults);
  server.on("/api/mark", apiMark);
  server.on("/api/cmd", apiCmd);
  server.on("/api/time", apiTime);
  server.on("/api/logs", apiLogs);
  server.on("/api/dump", apiDump);
  server.on("/dl", apiDownload);
  server.onNotFound([] { server.send(404, "text/plain", "not found"); });
  server.begin();

  xTaskCreate(ecuTask, "ecu", 12288, nullptr, 2, nullptr);
}

void loop() {
  server.handleClient();
  delay(2);
}
