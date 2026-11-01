#pragma once
// Offline-weather rules that need no hardware (host-testable): the stored configuration and its
// validation, when the board switches to the weather page, and when to fetch.

#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "utf8.h"

#define WX_PLACE_MAX 23            // bytes of UTF-8 (+ terminator in storage)
#define WX_AFTER_MIN_DEFAULT 10    // minutes offline before the weather page
#define WX_REFETCH_MS 1800000UL    // refetch while in weather mode: 30 min
#define WX_RETRY_MS 300000UL       // after a failed fetch: 5 min
#define WX_FRESH_MS 2700000UL      // data older than this is "stale" (1.5 refetch periods)
#define WX_TASK_MAX_MS 90000UL     // a fetch task still running after this long is given up (watchdog)

namespace wxp {

struct Cfg {
  bool on = false;
  bool hasLoc = false;
  char place[WX_PLACE_MAX + 1] = "";
  double lat = 0, lon = 0;
  uint16_t afterMin = WX_AFTER_MIN_DEFAULT;
};

inline bool validLat(double v) { return v >= -90.0 && v <= 90.0; }
inline bool validLon(double v) { return v >= -180.0 && v <= 180.0; }
inline bool validAfter(long v) { return v >= 1 && v <= 240; }
// 1..23 bytes of valid UTF-8, no control characters, no quote / backslash (it is echoed inside JSON).
inline bool validPlace(const char* s, size_t n) {
  if (n < 1 || n > WX_PLACE_MAX) return false;
  for (size_t i = 0; i < n; i++) {
    unsigned char c = (unsigned char)s[i];
    if (c < 0x20 || c == 0x7F || c == '"' || c == '\\') return false;
  }
  return validUtf8(s, n);
}

// The fetch task started at `startMs` has been running too long (it is then reported as failed "timeout").
inline bool fetchOverdue(uint32_t startMs, uint32_t now) { return now - startMs >= WX_TASK_MAX_MS; }

// Stored form of Cfg: one NVS blob, so a power cut cannot leave half of a new config (the old code wrote
// four keys one by one). Layout (little endian, WX_BLOB_SIZE bytes):
//   0 version(1)  1 on  2 hasLoc  3 reserved(0)  4..5 afterMin  6..29 place (24, NUL padded)  30..37 lat  38..45 lon
#define WX_BLOB_SIZE 46
#define WX_BLOB_VERSION 1
inline void encodeCfg(const Cfg& c, uint8_t out[WX_BLOB_SIZE]) {
  memset(out, 0, WX_BLOB_SIZE);
  out[0] = WX_BLOB_VERSION;
  out[1] = c.on ? 1 : 0;
  out[2] = c.hasLoc ? 1 : 0;
  out[4] = (uint8_t)(c.afterMin & 0xFF);
  out[5] = (uint8_t)(c.afterMin >> 8);
  memcpy(out + 6, c.place, WX_PLACE_MAX + 1);
  memcpy(out + 30, &c.lat, 8);
  memcpy(out + 38, &c.lon, 8);
}
// False (c untouched) for a wrong size / version or values that fail the same checks as the protocol
// (a corrupt blob must not become a location). A blob without a location keeps the default place / lat / lon.
inline bool decodeCfg(const uint8_t* b, size_t n, Cfg& c) {
  if (n != WX_BLOB_SIZE || b[0] != WX_BLOB_VERSION || b[1] > 1 || b[2] > 1) return false;
  Cfg t;
  t.on = b[1] != 0;
  uint16_t am = (uint16_t)(b[4] | (b[5] << 8));
  if (!validAfter(am)) return false;
  t.afterMin = am;
  if (b[2]) {
    char place[WX_PLACE_MAX + 1];
    memcpy(place, b + 6, sizeof(place));
    if (place[WX_PLACE_MAX] != '\0') return false;
    double lat, lon;
    memcpy(&lat, b + 30, 8);
    memcpy(&lon, b + 38, 8);
    if (!validPlace(place, strlen(place)) || !validLat(lat) || !validLon(lon)) return false;
    t.hasLoc = true;
    memcpy(t.place, place, sizeof(place));
    t.lat = lat;
    t.lon = lon;
  }
  c = t;
  return true;
}

// Should the weather page be wanted now? lastMsgMs: millis() of the last valid host message (0 = none since
// boot). `active`: it was wanted on the previous call (once in, it stays until a host message arrives or the
// feature is switched off; a lost Wi-Fi does not drop it).
inline bool wantWeather(const Cfg& c, bool active, uint32_t lastMsgMs, uint32_t now, bool wifiUp,
                        uint32_t offlineTimeoutMs) {
  if (!c.on) return false;
  uint32_t need = (uint32_t)c.afterMin * 60000UL;
  if (need < offlineTimeoutMs) need = offlineTimeoutMs;
  uint32_t offlineFor = now - lastMsgMs;  // lastMsgMs == 0: since boot
  if (offlineFor < need) return false;
  if (active) return true;
  return !c.hasLoc || wifiUp;
}

// State of the last fetches, for the fetch timing and the page status.
struct Fetch {
  bool haveData = false;
  uint32_t okMs = 0;     // millis() of the last good fetch
  bool lastFailed = false;
  uint32_t tryMs = 0;    // millis() of the last attempt start
  bool everTried = false;
};

inline bool fetchDue(const Fetch& f, uint32_t now) {
  if (!f.everTried) return true;
  uint32_t gap = f.lastFailed ? WX_RETRY_MS : WX_REFETCH_MS;
  return now - f.tryMs >= gap;
}

inline bool dataFresh(const Fetch& f, uint32_t now) { return f.haveData && now - f.okMs < WX_FRESH_MS; }

// Show the weather page only when there is something to show: the "no city" page, or data that is
// fresh, or a failed refetch / no Wi-Fi (then stale data is better than the dashboard's OFFLINE).
inline bool showWeather(const Cfg& c, const Fetch& f, bool want, bool wifiUp, uint32_t now) {
  if (!want) return false;
  if (!c.hasLoc) return true;
  if (!f.haveData) return false;
  return dataFresh(f, now) || f.lastFailed || !wifiUp;
}

}  // namespace wxp
