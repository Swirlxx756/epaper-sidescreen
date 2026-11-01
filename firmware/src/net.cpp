#include "net.h"

#if ENABLE_WIFI

#include <ArduinoJson.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <lwip/sockets.h>
#include <lwip/tcp.h>

#include <algorithm>
#include <vector>

#include "protocol.h"
#include "utf8.h"

namespace net {

static Handlers hd;
static bool begun = false;

// Station state machine.
static char ssid_[PROTO_SSID_MAX + 1];
static char pass_[PROTO_PASS_MAX + 1];
static bool haveCreds = false;
static State st = WST_OFF;
static bool retrying = false;  // silent auto-retry after "failed": the public state stays "failed"
static uint32_t attemptMs = 0;
static uint32_t failedMs = 0;

// What was last reported through onStatus.
static State repState = WST_OFF;
static IPAddress repIp;

// Sockets.
static WiFiServer server(WIFI_TCP_PORT);
static WiFiUDP udp;
static WiFiClient cli;
static bool listening = false;
static char lineBuf[MAX_LINE_LEN + 1];
static size_t lineLen = 0;
static bool lineTooLong = false;

static State publicState() { return retrying ? WST_FAILED : st; }
State state() { return publicState(); }

// The client is gone for good (peer closed, or a write failed): same bookkeeping as a replaced client.
static void dropFailedClient() {
  if (cli) cli.stop();
  lineLen = 0;
  lineTooLong = false;
  if (hd.onClientGone) hd.onClientGone();
}

// Print sink for the TCP client. Bytes are collected until the newline and then leave in ONE write(): a reply
// printed as text + "CR LF" used to be two TCP segments, which a host reading until the first newline could see
// split. A failed or short write means the connection is dead (or hopelessly backed up): close it.
class TcpOut : public Print {
 public:
  size_t write(uint8_t c) override { return write(&c, 1); }
  size_t write(const uint8_t* b, size_t n) override {
    for (size_t i = 0; i < n;) {
      if (len_ == sizeof(buf_)) flushNow();  // longer than any reply: send what we have
      size_t take = std::min(n - i, sizeof(buf_) - len_);
      memcpy(buf_ + len_, b + i, take);
      len_ += take;
      i += take;
    }
    if (len_ > 0 && buf_[len_ - 1] == 10) flushNow();
    return n;
  }
  void discard() { len_ = 0; }

 private:
  void flushNow() {
    size_t n = len_;
    len_ = 0;
    if (!cli || !cli.connected()) return;
    if (cli.write(buf_, n) != n) dropFailedClient();
  }
  uint8_t buf_[384];
  size_t len_ = 0;
};
static TcpOut tcpOut;

Print& client() { return tcpOut; }

// Would a write() go through without waiting? (lwip: send buffer above its low-water mark.)
static bool socketWritable() {
  int fd = cli.fd();
  if (fd < 0) return false;
  fd_set w;
  FD_ZERO(&w);
  FD_SET(fd, &w);
  struct timeval tv = {0, 0};
  return select(fd + 1, nullptr, &w, nullptr, &tv) > 0;
}

// DIAG lines are optional: when the socket is backed up (host stopped reading) they are dropped rather than
// stalling the loop in write().
void sendLine(const char* line) {
  if (!cli || !cli.connected() || !socketWritable()) return;
  tcpOut.println(line);
}

void dropClient() {
  tcpOut.discard();
  if (cli) cli.stop();
}

// Dead peers (host crashed / Wi-Fi gone) otherwise hold the single client slot until a write fails.
static void enableKeepAlive() {
  int fd = cli.fd();
  if (fd < 0) return;
  int on = 1, idle = 30, intvl = 10, cnt = 3;
  setsockopt(fd, SOL_SOCKET, SO_KEEPALIVE, &on, sizeof(on));
  setsockopt(fd, IPPROTO_TCP, TCP_KEEPIDLE, &idle, sizeof(idle));
  setsockopt(fd, IPPROTO_TCP, TCP_KEEPINTVL, &intvl, sizeof(intvl));
  setsockopt(fd, IPPROTO_TCP, TCP_KEEPCNT, &cnt, sizeof(cnt));
}

static const char* stateName(State s) {
  switch (s) {
    case WST_CONNECTING: return "connecting";
    case WST_CONNECTED: return "connected";
    case WST_FAILED: return "failed";
    default: return "off";
  }
}

// Last 3 bytes of the base MAC as lowercase hex (efuse MAC: byte 0 is the lowest).
static void deviceId(char* out /* 7 */) {
  uint64_t m = ESP.getEfuseMac();
  snprintf(out, 7, "%02x%02x%02x", (unsigned)((m >> 24) & 0xFF), (unsigned)((m >> 32) & 0xFF),
           (unsigned)((m >> 40) & 0xFF));
}

// ---------------------------------------------------------------------
// Station
// ---------------------------------------------------------------------
static void startAttempt() {
  WiFi.persistent(false);  // credentials live in our NVS namespace only
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(false);  // this module does the retrying
  WiFi.setSleep(false);          // USB powered: lower latency beats power saving
  WiFi.setHostname(FW_NAME);
  WiFi.begin(ssid_, pass_[0] ? pass_ : nullptr);
  attemptMs = millis();
  st = WST_CONNECTING;
}

void begin(const Handlers& h) {
  hd = h;
  begun = true;
  WiFi.persistent(false);
  repState = publicState();
}

void connect(const char* ssid, const char* pass) {
  if (!begun) return;
  strlcpy(ssid_, ssid, sizeof(ssid_));
  strlcpy(pass_, pass, sizeof(pass_));
  haveCreds = true;
  retrying = false;
  startAttempt();
}

static void closeSockets() {
  tcpOut.discard();
  if (cli) {
    cli.stop();
    if (hd.onClientGone) hd.onClientGone();
  }
  lineLen = 0;
  lineTooLong = false;
  if (listening) {
    server.end();
    udp.stop();
    listening = false;
  }
}

void disconnect() {
  haveCreds = false;
  ssid_[0] = pass_[0] = '\0';
  retrying = false;
  st = WST_OFF;
  if (!begun) return;
  closeSockets();
  WiFi.disconnect(true, false);
  WiFi.mode(WIFI_OFF);
}

static void startListening() {
  if (listening) return;
  server.begin();
  server.setNoDelay(true);
  udp.begin(WIFI_UDP_PORT);
  listening = true;
}

static void pollStation(uint32_t now) {
  if (!haveCreds) return;
  wl_status_t ws = WiFi.status();
  switch (st) {
    case WST_CONNECTING:
      if (ws == WL_CONNECTED) {
        st = WST_CONNECTED;
        retrying = false;
        startListening();
      } else if (now - attemptMs >= WIFI_CONNECT_MS ||
                 // A stale status from the previous attempt must not fail this one at once.
                 (now - attemptMs >= 2000 && (ws == WL_NO_SSID_AVAIL || ws == WL_CONNECT_FAILED))) {
        st = WST_FAILED;
        retrying = false;
        failedMs = now;
        WiFi.disconnect(false, false);
      }
      break;
    case WST_CONNECTED:
      if (ws != WL_CONNECTED) startAttempt();  // link dropped: reconnect (reported as "connecting")
      break;
    case WST_FAILED:
      if (now - failedMs >= WIFI_RETRY_MS) {
        retrying = true;  // keep reporting "failed" while this attempt runs
        startAttempt();
      }
      break;
    default:
      break;
  }
}

// ---------------------------------------------------------------------
// Status / scan JSON
// ---------------------------------------------------------------------
void markReported() {
  repState = publicState();
  repIp = (st == WST_CONNECTED) ? WiFi.localIP() : IPAddress();
}

void printStatus(Print& out) {
  JsonDocument d;
  d["type"] = "status";
  d["st"] = stateName(publicState());
  if (haveCreds) d["ssid"] = (const char*)ssid_;
  if (st == WST_CONNECTED && !retrying) {
    d["ip"] = WiFi.localIP().toString();
    d["rssi"] = WiFi.RSSI();
    d["port"] = WIFI_TCP_PORT;
  }
  char id[7];
  deviceId(id);
  d["id"] = (const char*)id;
  String s;
  serializeJson(d, s);  // password is not in the document
  out.print("WIFI ");
  out.println(s);
}

// SSIDs are arbitrary bytes; only valid UTF-8 (utf8.h) can go into JSON (and be typed back).
void printPair(Print& out, const char* token) {
  JsonDocument d;
  d["type"] = "pair";
  d["token"] = token;
  char id[7];
  deviceId(id);
  d["id"] = (const char*)id;
  String s;
  serializeJson(d, s);
  out.print("WIFI ");
  out.println(s);
}

struct Found {
  String ssid;
  int rssi;
  bool sec;
};

bool printScan(Print& out) {
  bool wasOff = WiFi.getMode() == WIFI_MODE_NULL;
  if (wasOff) WiFi.mode(WIFI_STA);
  // A connection attempt in progress makes scanNetworks() fail: pause it.
  if (haveCreds && st == WST_CONNECTING) WiFi.disconnect(false, false);

  int n = WiFi.scanNetworks();  // blocking; hidden networks are skipped
  std::vector<Found> found;
  for (int i = 0; i < n; i++) {
    String name = WiFi.SSID(i);
    if (name.length() == 0 || name.length() > PROTO_SSID_MAX || !validUtf8(name.c_str(), name.length())) continue;
    int rssi = WiFi.RSSI(i);
    bool sec = WiFi.encryptionType(i) != WIFI_AUTH_OPEN;
    bool dup = false;
    for (Found& f : found) {
      if (f.ssid == name) {  // same name, several APs: keep the strongest
        dup = true;
        if (rssi > f.rssi) {
          f.rssi = rssi;
          f.sec = sec;
        }
        break;
      }
    }
    if (!dup) found.push_back({name, rssi, sec});
  }
  WiFi.scanDelete();

  // Put the station back the way it was.
  if (haveCreds && st == WST_CONNECTING) {
    startAttempt();
  } else if (!haveCreds && wasOff) {
    WiFi.mode(WIFI_OFF);
  }
  if (n < 0) return false;

  std::sort(found.begin(), found.end(), [](const Found& a, const Found& b) { return a.rssi > b.rssi; });
  if (found.size() > 20) found.resize(20);

  JsonDocument d;
  d["type"] = "scan";
  JsonArray arr = d["nets"].to<JsonArray>();
  for (const Found& f : found) {
    JsonObject o = arr.add<JsonObject>();
    o["ssid"] = f.ssid;
    o["rssi"] = f.rssi;
    o["sec"] = f.sec;
  }
  String s;
  serializeJson(d, s);
  out.print("WIFI ");
  out.println(s);
  return true;
}

// ---------------------------------------------------------------------
// TCP line server (one client) and UDP discovery
// ---------------------------------------------------------------------
static void pollTcp() {
  WiFiClient c = server.accept();
  if (c) {
    if (cli && cli.connected() && hd.keepClient && hd.keepClient()) {
      c.stop();  // an upgrade / image transfer is running on the current client: the newcomer waits outside
    } else {
      if (cli) {
        cli.stop();  // the newest client wins; the old one gets nothing
        tcpOut.discard();
        if (hd.onClientGone) hd.onClientGone();
      }
      cli = c;
      cli.setNoDelay(true);
      enableKeepAlive();
      lineLen = 0;
      lineTooLong = false;
      tcpOut.println(hd.ready);
      if (hd.onClient) hd.onClient();
    }
  }
  if (!cli) return;
  if (!cli.connected() && cli.available() <= 0) {
    cli.stop();
    if (hd.onClientGone) hd.onClientGone();
    return;
  }
  // Same framing as the serial side: '\r' ignored, 1024-byte limit, rest of an oversized line dropped.
  while (cli && cli.available() > 0) {
    size_t raw = hd.rawWant ? hd.rawWant() : 0;
    if (raw > 0) {  // firmware image bytes: no framing
      static uint8_t chunk[1460];
      int n = cli.read(chunk, raw < sizeof(chunk) ? raw : sizeof(chunk));
      if (n <= 0) break;
      hd.onRaw(chunk, (size_t)n);
      continue;
    }
    int ch = cli.read();
    if (ch < 0) break;
    if (ch == '\r') continue;
    if (ch == '\n') {
      lineBuf[lineLen] = '\0';
      if (hd.onLine) hd.onLine(lineBuf, lineLen, lineTooLong);
      lineLen = 0;
      lineTooLong = false;
      continue;
    }
    if (lineTooLong) continue;
    if (lineLen >= MAX_LINE_LEN) {
      lineTooLong = true;
      lineLen = 0;
      continue;
    }
    lineBuf[lineLen++] = (char)ch;
  }
}

static void pollUdp() {
  for (int guard = 0; guard < 4; guard++) {
    int n = udp.parsePacket();
    if (n <= 0) return;
    char buf[96];
    if (n >= (int)sizeof(buf)) {  // not a discover packet
      udp.flush();
      continue;
    }
    int got = udp.read((uint8_t*)buf, sizeof(buf) - 1);
    if (got <= 0) continue;
    buf[got] = '\0';
    JsonDocument q;
    if (deserializeJson(q, buf, got)) continue;
    if (q["v"].as<int>() != 2 || !q["type"].is<const char*>() || strcmp(q["type"].as<const char*>(), "discover") != 0) continue;

    JsonDocument d;
    d["v"] = 2;
    d["type"] = "here";
    d["name"] = FW_NAME;
    char id[7];
    deviceId(id);
    d["id"] = (const char*)id;
    d["ip"] = WiFi.localIP().toString();
    d["port"] = WIFI_TCP_PORT;
    d["fw"] = FW_VERSION;
    d["busy"] = cli && cli.connected();
    String s;
    serializeJson(d, s);
    s += '\n';
    udp.beginPacket(udp.remoteIP(), udp.remotePort());  // unicast back to the asker
    udp.write((const uint8_t*)s.c_str(), s.length());
    udp.endPacket();
  }
}

void poll() {
  if (!begun) return;
  pollStation(millis());
  if (listening) {
    pollTcp();
    pollUdp();
  }
  // Report only what the host can see: public state or IP.
  IPAddress ip = (st == WST_CONNECTED) ? WiFi.localIP() : IPAddress();
  State pub = publicState();
  if (pub != repState || ip != repIp) {
    repState = pub;
    repIp = ip;
    if (hd.onStatus) hd.onStatus();
  }
}

}  // namespace net
#endif
