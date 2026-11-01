#include "settings.h"

#if ENABLE_WIFI

#include <Preferences.h>
#include <string.h>

#include "snap_flush_policy.h"

namespace settings {

static const char* NS_WIFI = "wifi";
static const char* NS_SNAP = "snap";
static const char* NS_OTA = "ota";
static const char* NS_WX = "wx";
static const char* NS_PAGE = "page";
static pagep::Mode pageM = pagep::PG_AUTO;
static wxp::Cfg wx;  // RAM copy
// Index = Kind (KIND_NONE unused).
static const char* const SNAP_KEY[KIND_COUNT] = {nullptr, "dash", "quota", "models", "heat", "mouse"};

static char lines[KIND_COUNT][PROTO_MAX_LINE_LEN + 1];  // RAM copy, "" = none
static bool lineDirty[KIND_COUNT];
static SnapFlushPolicy policy(SNAP_FLUSH_MIN_MS);

void begin(uint32_t now) {
  bool any = false;
  Preferences p;
  if (p.begin(NS_SNAP, true)) {  // read-only; fails on a never-written namespace
    for (int k = 1; k < KIND_COUNT; k++) {
      if (!p.isKey(SNAP_KEY[k])) continue;
      size_t n = p.getString(SNAP_KEY[k], lines[k], sizeof(lines[k]));
      lines[k][n < sizeof(lines[k]) ? n : sizeof(lines[k]) - 1] = '\0';
      if (n > 0) any = true;
    }
    p.end();
  }
  policy.begin(now, !any);

  pageM = pagep::PG_AUTO;
  if (p.begin(NS_PAGE, true)) {
    uint8_t v = p.getUChar("p", 0);
    if (pagep::validStored(v)) pageM = (pagep::Mode)v;
    p.end();
  }

  wx = wxp::Cfg();
  if (p.begin(NS_WX, true)) {
    uint8_t blob[WX_BLOB_SIZE];
    // 1.7.0+: one blob "cfg". Before: separate keys (on / after / place / lat / lon), read when there is no blob.
    if (p.isKey("cfg")) {
      if (p.getBytesLength("cfg") != WX_BLOB_SIZE || p.getBytes("cfg", blob, sizeof(blob)) != WX_BLOB_SIZE ||
          !wxp::decodeCfg(blob, sizeof(blob), wx))
        wx = wxp::Cfg();  // corrupt blob: defaults, not a half-read config
    } else {
      char place[WX_PLACE_MAX + 1] = "";
      if (p.isKey("place")) p.getString("place", place, sizeof(place));
      double lat = p.getDouble("lat", 1000), lon = p.getDouble("lon", 1000);
      wx.on = p.getUChar("on", 0) != 0;
      uint16_t am = p.getUShort("after", WX_AFTER_MIN_DEFAULT);
      if (wxp::validAfter(am)) wx.afterMin = am;
      if (wxp::validPlace(place, strlen(place)) && wxp::validLat(lat) && wxp::validLon(lon)) {
        wx.hasLoc = true;
        strlcpy(wx.place, place, sizeof(wx.place));
        wx.lat = lat;
        wx.lon = lon;
      }
    }
    p.end();
  }
}

pagep::Mode pageMode() { return pageM; }

bool pageSave(pagep::Mode m) {
  Preferences p;
  if (!p.begin(NS_PAGE, false)) return false;
  bool ok = p.putUChar("p", (uint8_t)m) == 1;
  p.end();
  if (ok) pageM = m;
  return ok;
}

const wxp::Cfg& wxCfg() { return wx; }

bool wxSave(const wxp::Cfg& c) {
  Preferences p;
  if (!p.begin(NS_WX, false)) return false;
  uint8_t blob[WX_BLOB_SIZE];
  wxp::encodeCfg(c, blob);
  bool ok = p.putBytes("cfg", blob, sizeof(blob)) == sizeof(blob);  // one key = one atomic NVS write
  if (ok) {
    // The keys of 1.5.0 / 1.6.0 are dead weight now (the blob wins anyway).
    for (const char* k : {"on", "after", "place", "lat", "lon"})
      if (p.isKey(k)) p.remove(k);
  }
  p.end();
  if (ok) wx = c;
  return ok;
}

bool loadWifi(char* ssid, char* pass) {
  ssid[0] = pass[0] = '\0';
  Preferences p;
  if (!p.begin(NS_WIFI, true)) return false;
  size_t n = p.isKey("ssid") ? p.getString("ssid", ssid, PROTO_SSID_MAX + 1) : 0;
  if (n > 0 && p.isKey("pass")) p.getString("pass", pass, PROTO_PASS_MAX + 1);
  p.end();
  if (n == 0) ssid[0] = pass[0] = '\0';
  return n > 0;
}

bool saveWifi(const char* ssid, const char* pass) {
  Preferences p;
  if (!p.begin(NS_WIFI, false)) return false;
  bool ok = p.putString("ssid", ssid) == strlen(ssid);
  // Open network: no "pass" key (putString returns 0 for "" too, so it cannot signal an error).
  if (strlen(pass) == 0)
    p.remove("pass");
  else
    ok = ok && p.putString("pass", pass) == strlen(pass);
  p.end();
  return ok;
}

void clearWifi() {
  Preferences p;
  if (p.begin(NS_WIFI, false)) {
    p.clear();
    p.end();
  }
}

bool otaToken(char* out, bool create) {
  out[0] = '\0';
  Preferences p;
  if (p.begin(NS_OTA, true)) {  // read-only; fails on a never-written namespace
    size_t n = p.isKey("token") ? p.getString("token", out, 17) : 0;
    p.end();
    if (n == 17 && out[0]) return true;  // getString counts the terminator
    out[0] = '\0';
  }
  if (!create) return false;
  char tok[17];
  for (int i = 0; i < 2; i++) snprintf(tok + i * 8, 9, "%08lx", (unsigned long)esp_random());
  if (!p.begin(NS_OTA, false)) return false;
  bool ok = p.putString("token", tok) == 16;
  p.end();
  if (ok) memcpy(out, tok, sizeof(tok));
  return ok;
}

const char* snapLine(Kind k) { return (k > KIND_NONE && k < KIND_COUNT) ? lines[k] : ""; }

void snapPut(Kind k, const char* line, size_t len) {
  if (k <= KIND_NONE || k >= KIND_COUNT || len == 0 || len > PROTO_MAX_LINE_LEN) return;
  if (strlen(lines[k]) == len && memcmp(lines[k], line, len) == 0) return;  // unchanged
  memcpy(lines[k], line, len);
  lines[k][len] = '\0';
  lineDirty[k] = true;
  policy.markDirty();
}

void service(uint32_t now) {
  if (!policy.due(now)) return;
  bool failed = false;
  Preferences p;
  if (!p.begin(NS_SNAP, false)) {
    failed = true;
  } else {
    for (int k = 1; k < KIND_COUNT; k++) {
      if (!lineDirty[k]) continue;
      size_t n = strlen(lines[k]);
      if (p.putString(SNAP_KEY[k], lines[k]) == n)
        lineDirty[k] = false;
      else
        failed = true;
    }
    p.end();
  }
  policy.noteFlushed(now, failed);  // failed: retried after the next gap
}

void resetAll(uint32_t now) {
  clearWifi();
  Preferences p;
  for (const char* ns : {NS_SNAP, NS_OTA, NS_WX, NS_PAGE}) {
    if (p.begin(ns, false)) {
      p.clear();
      p.end();
    }
  }
  wx = wxp::Cfg();
  pageM = pagep::PG_AUTO;
  memset(lines, 0, sizeof(lines));
  memset(lineDirty, 0, sizeof(lineDirty));
  policy.restart(now);
}

}  // namespace settings
#endif
