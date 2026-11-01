#include "weather_net.h"

#if ENABLE_WIFI

#include <Arduino.h>
#include <HTTPClient.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <time.h>

#include <atomic>

#include "weather_parse.h"

namespace wxnet {

// TLS: the server certificate chain is verified against ISRG Root X1 (Let's Encrypt's root, valid until
// 2035-06-04; api.open-meteo.com chains to it, checked with `openssl s_client -CAfile` on 2026-11-02).
// If Let's Encrypt or the host change roots, fetches fail with err "tls" and this PEM must be replaced.
// Certificate validity needs the clock, hence the SNTP wait in the task.
static const char ISRG_ROOT_X1[] =
    "-----BEGIN CERTIFICATE-----\n"
    "MIIFazCCA1OgAwIBAgIRAIIQz7DSQONZRGPgu2OCiwAwDQYJKoZIhvcNAQELBQAw\n"
    "TzELMAkGA1UEBhMCVVMxKTAnBgNVBAoTIEludGVybmV0IFNlY3VyaXR5IFJlc2Vh\n"
    "cmNoIEdyb3VwMRUwEwYDVQQDEwxJU1JHIFJvb3QgWDEwHhcNMTUwNjA0MTEwNDM4\n"
    "WhcNMzUwNjA0MTEwNDM4WjBPMQswCQYDVQQGEwJVUzEpMCcGA1UEChMgSW50ZXJu\n"
    "ZXQgU2VjdXJpdHkgUmVzZWFyY2ggR3JvdXAxFTATBgNVBAMTDElTUkcgUm9vdCBY\n"
    "MTCCAiIwDQYJKoZIhvcNAQEBBQADggIPADCCAgoCggIBAK3oJHP0FDfzm54rVygc\n"
    "h77ct984kIxuPOZXoHj3dcKi/vVqbvYATyjb3miGbESTtrFj/RQSa78f0uoxmyF+\n"
    "0TM8ukj13Xnfs7j/EvEhmkvBioZxaUpmZmyPfjxwv60pIgbz5MDmgK7iS4+3mX6U\n"
    "A5/TR5d8mUgjU+g4rk8Kb4Mu0UlXjIB0ttov0DiNewNwIRt18jA8+o+u3dpjq+sW\n"
    "T8KOEUt+zwvo/7V3LvSye0rgTBIlDHCNAymg4VMk7BPZ7hm/ELNKjD+Jo2FR3qyH\n"
    "B5T0Y3HsLuJvW5iB4YlcNHlsdu87kGJ55tukmi8mxdAQ4Q7e2RCOFvu396j3x+UC\n"
    "B5iPNgiV5+I3lg02dZ77DnKxHZu8A/lJBdiB3QW0KtZB6awBdpUKD9jf1b0SHzUv\n"
    "KBds0pjBqAlkd25HN7rOrFleaJ1/ctaJxQZBKT5ZPt0m9STJEadao0xAH0ahmbWn\n"
    "OlFuhjuefXKnEgV4We0+UXgVCwOPjdAvBbI+e0ocS3MFEvzG6uBQE3xDk3SzynTn\n"
    "jh8BCNAw1FtxNrQHusEwMFxIt4I7mKZ9YIqioymCzLq9gwQbooMDQaHWBfEbwrbw\n"
    "qHyGO0aoSCqI3Haadr8faqU9GY/rOPNk3sgrDQoo//fb4hVC1CLQJ13hef4Y53CI\n"
    "rU7m2Ys6xt0nUW7/vGT1M0NPAgMBAAGjQjBAMA4GA1UdDwEB/wQEAwIBBjAPBgNV\n"
    "HRMBAf8EBTADAQH/MB0GA1UdDgQWBBR5tFnme7bl5AFzgAiIyBpY9umbbjANBgkq\n"
    "hkiG9w0BAQsFAAOCAgEAVR9YqbyyqFDQDLHYGmkgJykIrGF1XIpu+ILlaS/V9lZL\n"
    "ubhzEFnTIZd+50xx+7LSYK05qAvqFyFWhfFQDlnrzuBZ6brJFe+GnY+EgPbk6ZGQ\n"
    "3BebYhtF8GaV0nxvwuo77x/Py9auJ/GpsMiu/X1+mvoiBOv/2X/qkSsisRcOj/KK\n"
    "NFtY2PwByVS5uCbMiogziUwthDyC3+6WVwW6LLv3xLfHTjuCvjHIInNzktHCgKQ5\n"
    "ORAzI4JMPJ+GslWYHb4phowim57iaztXOoJwTdwJx4nLCgdNbOhdjsnvzqvHu7Ur\n"
    "TkXWStAmzOVyyghqpZXjFaH3pO3JLF+l+/+sKAIuvtd7u+Nxe5AW0wdeRlN8NwdC\n"
    "jNPElpzVmbUq4JUagEiuTDkHzsxHpFKVK7q4+63SM1N95R1NbdWhscdCb+ZAJzVc\n"
    "oyi3B43njTOQ5yOf+1CceWxG1bQVs5ZufpsMljq4Ui0/1lvh+wjChP4kqKOJ2qxq\n"
    "4RgqsahDYVvTH9w7jXbyLeiNdd8XM2w9U/t7y0Ff/9yi0GE44Za4rF2LN9d11TPA\n"
    "mRGunUHBcnWEvgJBQl9nJEiU0Zsnvgc/ubhPgXRR4Xq37Z0j4r7g1SgEEzwxA57d\n"
    "emyPxgcYxn/eR44/KJ4EBs+lVDR3veyJm+kXQ99b21/+jh5Xos1AnX5iItreGCc=\n"
    "-----END CERTIFICATE-----\n";

static const uint32_t SNTP_WAIT_MS = 15000;
static const size_t BODY_MAX = 12 * 1024;  // a real answer is ~1.7 KB
static const size_t MIN_HEAP = 70 * 1024;  // TLS handshake needs ~40 KB contiguous-ish

// States: IDLE -> RUNNING (start) -> FINISHING (whoever finishes first: the task, or the watchdog) -> DONE (take
// -> IDLE). FINISHING makes "task finishes" and "watchdog gives up" mutually exclusive.
enum : uint8_t { IDLE = 0, RUNNING, FINISHING, DONE };
static std::atomic<uint8_t> state{IDLE};
static std::atomic<bool> alive{false};  // the task exists (it may have been given up by the watchdog but still run)
static uint32_t startMs;                // millis() of start(), for the watchdog
static bool resOk;
static wxb::WeatherB resData;           // the published result (written only in FINISHING)
static wxb::WeatherB taskData;          // the task's own parse target (one task at a time)
static char resErr[24];
static uint32_t resStackFree;           // minimum free stack of the task, bytes
static double argLat, argLon;
static char argPlace[WX_PLACE_MAX + 1];

static void finish(bool ok, const char* err, uint32_t stackFree) {
  uint8_t expected = RUNNING;
  if (!state.compare_exchange_strong(expected, FINISHING)) return;  // given up by the watchdog: nobody wants this
  resOk = ok;
  if (ok) resData = taskData;
  strlcpy(resErr, err, sizeof(resErr));
  resStackFree = stackFree;
  state.store(DONE);
}

static bool clockSet() { return time(nullptr) > 1700000000L; }  // 2023-11

static const uint32_t BODY_IDLE_MS = 10000;   // no byte for this long
static const uint32_t BODY_TOTAL_MS = 30000;  // whole body

// Streams the HTTP body into `body`, never holding more than BODY_MAX: a server (or a captive portal) that sends
// a lot, or without a Content-Length, can no longer fill the heap the way HTTPClient::getString() did.
// `expected` = Content-Length or -1. Returns nullptr when the body is complete, else the error reason.
static const char* readBody(HTTPClient& http, int expected, String& body) {
  WiFiClient* s = http.getStreamPtr();
  if (!s) return "empty";
  body.reserve(expected > 0 ? (size_t)expected : 2048);
  uint8_t buf[512];
  const uint32_t t0 = millis();
  uint32_t lastData = t0;
  for (;;) {
    if (expected >= 0 && (int)body.length() >= expected) return nullptr;
    int avail = s->available();
    if (avail > 0) {
      size_t want = (size_t)avail < sizeof(buf) ? (size_t)avail : sizeof(buf);
      if (expected >= 0 && want > (size_t)expected - body.length()) want = (size_t)expected - body.length();
      int got = s->read(buf, want);
      if (got > 0) {
        if (body.length() + (size_t)got > BODY_MAX) return "too_big";  // abort: the rest is never read
        if (!body.concat((const char*)buf, (unsigned int)got)) return "low_mem";
        lastData = millis();
        continue;
      }
    }
    if (!s->connected() && s->available() <= 0) return expected >= 0 ? "short" : nullptr;  // end of the stream
    uint32_t now = millis();
    if (now - lastData > BODY_IDLE_MS || now - t0 > BODY_TOTAL_MS) return "timeout";
    vTaskDelay(pdMS_TO_TICKS(10));
  }
}

// The fetch. True after filling taskData, else false with the reason in `err`.
static bool run(char* err, size_t errN) {
  if (WiFi.status() != WL_CONNECTED) return strlcpy(err, "no_wifi", errN), false;
  if (!clockSet()) {
    configTime(0, 0, "pool.ntp.org", "time.cloudflare.com", "ntp.aliyun.com");
    uint32_t t0 = millis();
    while (!clockSet() && millis() - t0 < SNTP_WAIT_MS) vTaskDelay(pdMS_TO_TICKS(200));
  }
  if (!clockSet()) return strlcpy(err, "sntp", errN), false;
  if (ESP.getMaxAllocHeap() < MIN_HEAP / 2 || ESP.getFreeHeap() < MIN_HEAP) return strlcpy(err, "low_mem", errN), false;

  static char url[640];
  if (!wxb::requestUrl(argLat, argLon, url, sizeof(url))) return strlcpy(err, "url", errN), false;

  String body;
  err[0] = 0;
  {
    WiFiClientSecure client;
    client.setCACert(ISRG_ROOT_X1);
    client.setTimeout(10);  // seconds
    HTTPClient http;
    http.setConnectTimeout(10000);
    http.setTimeout(10000);
    http.useHTTP10(true);  // no chunked encoding: the body is plain JSON
    if (!http.begin(client, url)) {
      strlcpy(err, "tls", errN);
    } else {
      int code = http.GET();
      if (code == HTTP_CODE_OK) {
        int n = http.getSize();  // -1 = unknown
        if (n > (int)BODY_MAX) {
          strlcpy(err, "too_big", errN);
        } else if (const char* e = readBody(http, n, body)) {
          strlcpy(err, e, errN);
        }
      } else if (code < 0) {
        strlcpy(err, "tls", errN);  // connect / handshake / certificate / timeout
      } else {
        snprintf(err, errN, "http_%d", code);
      }
      http.end();
    }
  }
  if (err[0]) return false;
  if (body.length() == 0) return strlcpy(err, "empty", errN), false;
  if (!wxb::parseOpenMeteo(body.c_str(), body.length(), argPlace, taskData)) return strlcpy(err, "parse", errN), false;
  return true;
}

static void task(void*) {
  char err[24] = "";
  bool ok = run(err, sizeof(err));
  // uxTaskGetStackHighWaterMark() is in bytes on ESP-IDF: the least stack that was ever free.
  finish(ok, ok ? "" : err, (uint32_t)uxTaskGetStackHighWaterMark(nullptr));
  alive.store(false);
  vTaskDelete(nullptr);
}

bool start(double lat, double lon, const char* place) {
  if (alive.load()) return false;  // an earlier task is still unwinding (the watchdog gave up on it)
  uint8_t expected = IDLE;
  if (!state.compare_exchange_strong(expected, RUNNING)) return false;  // running, or a result not taken yet
  argLat = lat;
  argLon = lon;
  strlcpy(argPlace, place, sizeof(argPlace));
  startMs = millis();
  alive.store(true);
  if (xTaskCreate(task, "wxfetch", 16384, nullptr, 1, nullptr) != pdPASS) {
    alive.store(false);
    finish(false, "task", 0);
  }
  return true;
}

bool busy() { return state.load() == RUNNING; }

bool watchdog(uint32_t now) {
  if (state.load() != RUNNING || !wxp::fetchOverdue(startMs, now)) return false;
  uint8_t expected = RUNNING;
  if (!state.compare_exchange_strong(expected, FINISHING)) return false;  // the task finished just now
  resOk = false;
  strlcpy(resErr, "timeout", sizeof(resErr));
  resStackFree = 0;
  state.store(DONE);
  return true;
}

uint32_t stackFreeBytes() { return resStackFree; }

bool take(bool& ok, wxb::WeatherB& out, char* err, size_t errN) {
  if (state.load() != DONE) return false;
  ok = resOk;
  if (ok) out = resData;
  strlcpy(err, resErr, errN);
  state.store(IDLE);
  return true;
}

}  // namespace wxnet
#endif
