#include "protocol.h"

#include <ArduinoJson.h>
#include <math.h>
#include <stdio.h>
#include <string.h>

using namespace dash;

// ---------------------------------------------------------------------
// v1 field conversion: anything missing / null / unparsable -> "--" or -1
// ---------------------------------------------------------------------
static String compact(double n) {
  const char* units[] = {"", "K", "M", "B", "T"};
  int u = 0;
  while (fabs(n) >= 1000 && u < 4) {
    n /= 1000;
    u++;
  }
  return String(n, (u == 0 || n >= 100) ? 0 : 1) + units[u];
}

// Pre-formatted string as-is, numbers compacted (145900000 -> 145.9M).
static String fmtText(JsonVariantConst v) {
  if (v.is<const char*>()) {
    String s = v.as<const char*>();
    s.trim();
    return s.length() ? s : String("--");
  }
  if (v.is<double>()) return compact(v.as<double>());
  return "--";
}

static String fmtCost(JsonVariantConst v) {
  if (v.is<double>()) return "$" + String(v.as<double>(), 2);
  String s = fmtText(v);
  return (s == "--" || s.startsWith("$")) ? s : "$" + s;
}

static String fmtMinutes(JsonVariantConst v) {
  if (!v.is<double>()) return fmtText(v);
  long m = lround(v.as<double>());
  if (m < 0) return "--";
  if (m < 60) return String(m) + "m";
  char buf[16];
  snprintf(buf, sizeof(buf), "%ldh%02ldm", m / 60, m % 60);
  return buf;
}

static String fmtTemp(JsonVariantConst v) {
  if (v.is<double>()) return String(lround(v.as<double>())) + "\xC2\xB0" "C";  // °C
  return fmtText(v);
}

static int toPct(JsonVariantConst v) {
  double d;
  if (v.is<double>()) {
    d = v.as<double>();
  } else if (v.is<const char*>()) {
    const char* s = v.as<const char*>();
    char* end;
    d = strtod(s, &end);
    if (end == s) return -1;
  } else {
    return -1;
  }
  if (!isfinite(d)) return -1;  // "nan" / "inf" parse as numbers; lround() of them is undefined
  return constrain((int)lround(d), 0, 100);
}

static void parseStats(JsonObjectConst o, Stats& s) {
  s.time = fmtText(o["time"]);
  s.cpu = toPct(o["cpu"]);
  s.mem = toPct(o["mem"]);
  s.disk = toPct(o["disk"]);
  s.netUp = fmtText(o["net_up"]);
  s.netDown = fmtText(o["net_down"]);
  s.hasGpu = !o["gpu"].isNull() || !o["gpu_temp"].isNull();
  s.gpu = toPct(o["gpu"]);
  s.gpuTemp = fmtTemp(o["gpu_temp"]);

  JsonObjectConst tok = o["tok"];  // null object if missing -> all "--"
  s.blockTokens = fmtText(tok["block_tokens"]);
  s.blockCost = fmtCost(tok["block_cost"]);
  s.burn = fmtText(tok["burn_tpm"]);
  s.remain = fmtMinutes(tok["remain_min"]);
  s.todayTokens = fmtText(tok["today_tokens"]);
  s.todayCost = fmtCost(tok["today_cost"]);
}

// ---------------------------------------------------------------------
// v2 blocks. Each parser fills a private copy and only returns true when the
// whole message was valid, so a bad message never half-updates the view.
// ---------------------------------------------------------------------
static bool fail(HandleResult& r, const char* code, const char* field = nullptr) {
  if (field)
    snprintf(r.err, sizeof(r.err), "%s %s", code, field);
  else
    snprintf(r.err, sizeof(r.err), "%s", code);
  return false;
}

static void getStr(JsonVariantConst v, char* dst, size_t cap) {
  setStr(dst, cap, v.is<const char*>() ? v.as<const char*>() : "");
}

// 0..100, anything else (null, missing, text) -> -1.
static int8_t getPct8(JsonVariantConst v) {
  if (!v.is<double>()) return -1;
  return (int8_t)constrain((int)lround(v.as<double>()), 0, 100);
}

// st: missing = ok.
static bool parseSt(JsonObjectConst o, St& st, HandleResult& r) {
  JsonVariantConst v = o["st"];
  st = ST_OK;
  if (v.isNull()) return true;
  const char* s = v.is<const char*>() ? v.as<const char*>() : "";
  if (strcmp(s, "ok") == 0) st = ST_OK;
  else if (strcmp(s, "stale") == 0) st = ST_STALE;
  else if (strcmp(s, "na") == 0) st = ST_NA;
  else if (strcmp(s, "off") == 0) st = ST_OFF;
  else return fail(r, "bad_field", "st");
  return true;
}

static bool parseDash(JsonObjectConst o, Top& t, HandleResult& r) {
  t = Top();
  t.got = true;
  if (!parseSt(o, t.st, r)) return false;
  JsonVariantConst ev = o["every"];  // refresh interval in seconds (also on st:"na"); capped at 1 h
  t.everyS = ev.is<double>() ? (uint16_t)constrain((int)lround(ev.as<double>()), 0, 3600) : 0;
  if (t.st == ST_NA) return true;  // stored empty: nothing stale is shown
  getStr(o["time"], t.time, sizeof(t.time));
  // Messages without main / alt (older hosts) leave these empty -> shown as "--".
  getStr(o["main"]["l"], t.main.label, sizeof(t.main.label));
  getStr(o["main"]["tok"], t.main.tok, sizeof(t.main.tok));
  getStr(o["main"]["cost"], t.main.cost, sizeof(t.main.cost));
  getStr(o["alt"]["l"], t.alt.label, sizeof(t.alt.label));
  getStr(o["alt"]["tok"], t.alt.tok, sizeof(t.alt.tok));
  return true;
}

static bool parseQuota(JsonObjectConst o, Quota& q, HandleResult& r) {
  q = Quota();
  q.got = true;
  if (!parseSt(o, q.st, r)) return false;
  if (q.st == ST_NA) return true;
  JsonVariantConst pv = o["p"];
  if (pv.isNull()) return fail(r, "missing_field", "p");
  if (!pv.is<JsonArrayConst>()) return fail(r, "bad_field", "p");
  for (JsonVariantConst e : pv.as<JsonArrayConst>()) {
    if (q.n >= MAX_PROVIDERS) break;  // extra providers are ignored
    if (!e.is<JsonObjectConst>()) return fail(r, "bad_field", "p");
    JsonObjectConst po = e.as<JsonObjectConst>();
    QProvider& p = q.p[q.n++];
    if (!parseSt(po, p.st, r)) return false;
    getStr(po["n"], p.name, sizeof(p.name));
    getStr(po["plan"], p.plan, sizeof(p.plan));
    if (p.st == ST_NA) continue;
    getStr(po["bal"], p.bal, sizeof(p.bal));
    JsonVariantConst tv = po["txt"];
    if (!tv.isNull() && !tv.is<JsonArrayConst>()) return fail(r, "bad_field", "txt");
    for (JsonVariantConst se : tv.as<JsonArrayConst>()) {
      if (p.nSegs >= MAX_SEGS) break;
      if (!se.is<JsonObjectConst>()) return fail(r, "bad_field", "txt");
      QSeg& sg = p.segs[p.nSegs++];
      getStr(se["s"], sg.s, sizeof(sg.s));
      sg.warn = se["w"].is<double>() ? se["w"].as<double>() != 0 : (se["w"].is<bool>() && se["w"].as<bool>());
    }
    JsonVariantConst iv = po["i"];
    if (!iv.isNull() && !iv.is<JsonArrayConst>()) return fail(r, "bad_field", "i");
    for (JsonVariantConst ie : iv.as<JsonArrayConst>()) {
      if (p.nItems >= MAX_ITEMS) break;
      if (!ie.is<JsonObjectConst>()) return fail(r, "bad_field", "i");
      JsonObjectConst io = ie.as<JsonObjectConst>();
      QItem& it = p.items[p.nItems++];
      getStr(io["l"], it.label, sizeof(it.label));
      it.left = getPct8(io["left"]);
      getStr(io["rst"], it.reset, sizeof(it.reset));
    }
  }
  return true;
}

static bool parseModels(JsonObjectConst o, Models& m, HandleResult& r) {
  m = Models();
  m.got = true;
  if (!parseSt(o, m.st, r)) return false;
  if (m.st == ST_NA) return true;
  getStr(o["scope"], m.scope, sizeof(m.scope));
  JsonVariantConst mv = o["m"];
  if (mv.isNull()) return fail(r, "missing_field", "m");
  if (!mv.is<JsonArrayConst>()) return fail(r, "bad_field", "m");
  for (JsonVariantConst e : mv.as<JsonArrayConst>()) {
    if (m.n >= MAX_MODELS) break;
    if (!e.is<JsonArrayConst>()) return fail(r, "bad_field", "m");
    JsonArrayConst row = e.as<JsonArrayConst>();
    ModelRow& mr = m.m[m.n++];
    JsonVariantConst name = row[0];
    setModelName(mr.name, sizeof(mr.name), name.is<const char*>() ? name.as<const char*>() : "");
    getStr(row[1], mr.tok, sizeof(mr.tok));
    mr.pct = getPct8(row[2]);
  }
  return true;
}

static bool parseHeat(JsonObjectConst o, Heat& h, HandleResult& r) {
  h = Heat();
  h.got = true;
  if (!parseSt(o, h.st, r)) return false;
  // `view`: "cal" = calendar, anything else / missing = heat grid; also kept on st:"na" (title follows it).
  JsonVariantConst vw = o["view"];
  h.calendar = vw.is<const char*>() && strcmp(vw.as<const char*>(), "cal") == 0;
  if (h.st == ST_NA) return true;

  JsonVariantConst dv = o["d"], tv = o["t"];
  if (dv.isNull() && tv.isNull()) return fail(r, "missing_field", "d");
  // d / t length decides the layout (1..HEAT_MAX days; the last char is today).
  // Longer strings (e.g. an old 182-day host): keep only the last HEAT_MAX.
  size_t dLen = 0, tLen = 0;
  const char* ds = nullptr;
  const char* ts = nullptr;
  if (!dv.isNull()) {
    ds = dv.is<const char*>() ? dv.as<const char*>() : nullptr;
    if (!ds || (dLen = strlen(ds)) == 0) return fail(r, "bad_field", "d");
  }
  if (!tv.isNull()) {
    ts = tv.is<const char*>() ? tv.as<const char*>() : nullptr;
    if (!ts || (tLen = strlen(ts)) == 0) return fail(r, "bad_field", "t");
  }
  if (ds && ts && dLen != tLen) return fail(r, "bad_field", "t");  // same days in both
  size_t len = ds ? dLen : tLen;
  size_t skip = len > (size_t)HEAT_MAX ? len - HEAT_MAX : 0;
  h.n = (uint8_t)(len - skip);
  if (ds) {
    for (size_t i = 0; i < len; i++) {
      uint8_t c = decodeHeat(ds[i]);
      if (c == BAD_CHAR) return fail(r, "bad_field", "d");
      if (i >= skip) h.days[i - skip] = c;
    }
    h.hasDays = true;
  }
  if (ts) {
    for (size_t i = 0; i < len; i++) {
      uint8_t c = decodeTrend(ts[i]);
      if (c == BAD_CHAR) return fail(r, "bad_field", "t");
      if (i >= skip) h.trend[i - skip] = c;
    }
    h.hasTrend = true;
  }
  h.start = addDays(parseYmd(o["start"].is<const char*>() ? o["start"].as<const char*>() : ""), (int)skip);
  // `rows` (older hosts) is ignored: the calendar is always 7 columns, one row per week.
  getStr(o["peak"], h.peak, sizeof(h.peak));
  JsonVariantConst av = o["active"];
  if (av.isNull()) av = o["active30"];  // old name
  h.active = av.is<double>() ? (int16_t)constrain((int)lround(av.as<double>()), 0, 999) : -1;
  return true;
}

static bool parseMouse(JsonObjectConst o, Mouse& m, HandleResult& r) {
  m = Mouse();
  m.got = true;
  if (!parseSt(o, m.st, r)) return false;
  if (m.st == ST_NA || m.st == ST_OFF) return true;  // no battery value to show
  m.pct = getPct8(o["pct"]);
  m.chg = o["chg"].is<bool>() && o["chg"].as<bool>();
  getStr(o["at"], m.at, sizeof(m.at));
  return true;
}

// ---------------------------------------------------------------------
// Device commands (v2). Only parsed and validated here; the secret never
// goes into an error text.
// ---------------------------------------------------------------------
// A JSON string of 0..maxLen bytes (no embedded NUL) copied into dst
// (capacity maxLen + 1). `required`: null / missing is an error.
static bool getWifiStr(JsonVariantConst v, const char* field, size_t minLen, size_t maxLen, bool required,
                       char* dst, HandleResult& r) {
  dst[0] = '\0';
  if (v.isNull()) return required ? fail(r, "missing_field", field) : true;
  if (!v.is<const char*>()) return fail(r, "bad_field", field);
  JsonString js = v.as<JsonString>();
  size_t n = js.size();
  if (n < minLen || n > maxLen || memchr(js.c_str(), '\0', n) != nullptr) return fail(r, "bad_field", field);
  memcpy(dst, js.c_str(), n);
  dst[n] = '\0';
  return true;
}

static bool parseWifiSet(JsonObjectConst o, HandleResult& r) {
  if (getWifiStr(o["ssid"], "ssid", 1, PROTO_SSID_MAX, true, r.ssid, r) &&
      getWifiStr(o["pass"], "pass", 0, PROTO_PASS_MAX, false, r.pass, r))
    return true;
  r.ssid[0] = r.pass[0] = '\0';  // nothing half-parsed is handed back
  return false;
}

static bool parseOtaBegin(JsonObjectConst o, HandleResult& r) {
  JsonVariantConst s = o["size"];
  if (s.isNull()) return fail(r, "missing_field", "size");
  if (!s.is<uint32_t>() || s.as<uint32_t>() == 0 || s.as<uint32_t>() > PROTO_OTA_SIZE_MAX)
    return fail(r, "bad_field", "size");
  char md5[33];
  if (!getWifiStr(o["md5"], "md5", 32, 32, true, md5, r)) return false;
  for (int i = 0; i < 32; i++) {
    char c = md5[i];
    if (c >= 'A' && c <= 'F') c = (char)(c - 'A' + 'a');
    if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) return fail(r, "bad_field", "md5");
    md5[i] = c;
  }
  char token[PROTO_TOKEN_MAX + 1];
  if (!getWifiStr(o["token"], "token", 1, PROTO_TOKEN_MAX, true, token, r)) return false;
  r.otaSize = s.as<uint32_t>();
  memcpy(r.md5, md5, sizeof(r.md5));
  memcpy(r.token, token, sizeof(r.token));
  return true;
}

static bool parseWxCfg(JsonObjectConst o, HandleResult& r) {
  JsonVariantConst on = o["on"];
  if (on.isNull()) return fail(r, "missing_field", "on");
  if (!on.is<bool>()) return fail(r, "bad_field", "on");
  bool wxOn = on.as<bool>();
  bool setLoc = false;
  char place[WX_PLACE_MAX + 1] = "";
  double lat = 0, lon = 0;
  uint16_t after = 0;
  if (wxOn) {
    JsonVariantConst pl = o["place"], la = o["lat"], lo = o["lon"], am = o["after_min"];
    if (!pl.isNull() || !la.isNull() || !lo.isNull()) {
      if (pl.isNull()) return fail(r, "missing_field", "place");
      if (la.isNull()) return fail(r, "missing_field", "lat");
      if (lo.isNull()) return fail(r, "missing_field", "lon");
      if (!pl.is<const char*>()) return fail(r, "bad_field", "place");
      JsonString js = pl.as<JsonString>();
      if (!wxp::validPlace(js.c_str(), js.size())) return fail(r, "bad_field", "place");
      if (!la.is<double>() || !wxp::validLat(la.as<double>())) return fail(r, "bad_field", "lat");
      if (!lo.is<double>() || !wxp::validLon(lo.as<double>())) return fail(r, "bad_field", "lon");
      memcpy(place, js.c_str(), js.size());
      place[js.size()] = '\0';
      // ArduinoJson may keep the number as a float (31.23 -> 31.2299995): round to 1e-4 degree (11 m,
      // all the forecast URL uses) so the stored / echoed value is the one the host sent.
      lat = round(la.as<double>() * 1e4) / 1e4;
      lon = round(lo.as<double>() * 1e4) / 1e4;
      setLoc = true;
    }
    if (!am.isNull()) {
      if (!am.is<int>() || !wxp::validAfter(am.as<int>())) return fail(r, "bad_field", "after_min");
      after = (uint16_t)am.as<int>();
    }
  }
  r.wxOn = wxOn;
  r.wxSetLoc = setLoc;
  memcpy(r.wxPlace, place, sizeof(place));
  r.wxLat = lat;
  r.wxLon = lon;
  r.wxAfterMin = after;
  return true;
}

static bool parseImgId(JsonObjectConst o, HandleResult& r) {
  JsonVariantConst id = o["id"];
  if (id.isNull()) return fail(r, "missing_field", "id");
  if (!id.is<const char*>() || strcmp(id.as<const char*>(), "todo") != 0) return fail(r, "bad_field", "id");
  return true;
}

static bool parsePage(JsonObjectConst o, HandleResult& r) {
  JsonVariantConst p = o["p"];
  if (p.isNull()) return fail(r, "missing_field", "p");
  if (!p.is<const char*>() || !pagep::parseMode(p.as<const char*>(), r.pageMode)) return fail(r, "bad_field", "p");
  return true;
}

static bool parseImg(JsonObjectConst o, const char* type, HandleResult& r) {
  if (!parseImgId(o, r)) return false;
  if (strcmp(type, "img_end") == 0) return true;
  if (strcmp(type, "img_begin") == 0) {
    JsonVariantConst sz = o["size"], crc = o["crc"];
    if (sz.isNull()) return fail(r, "missing_field", "size");
    if (!sz.is<uint32_t>() || sz.as<uint32_t>() == 0) return fail(r, "bad_field", "size");
    if (sz.as<uint32_t>() > IMG_MAX_COMPRESSED) return fail(r, "too_big");
    if (crc.isNull()) return fail(r, "missing_field", "crc");
    if (!crc.is<uint32_t>()) return fail(r, "bad_field", "crc");
    r.imgSize = sz.as<uint32_t>();
    r.imgCrc = crc.as<uint32_t>();
    return true;
  }
  static uint8_t chunk[IMG_CHUNK_MAX];  // handed to the caller through r.imgData
  JsonVariantConst off = o["off"], d = o["d"];
  if (off.isNull()) return fail(r, "missing_field", "off");
  if (!off.is<uint32_t>()) return fail(r, "bad_field", "off");
  if (d.isNull()) return fail(r, "missing_field", "d");
  if (!d.is<const char*>()) return fail(r, "bad_field", "d");
  JsonString js = d.as<JsonString>();
  int n = imgrx::b64Decode(js.c_str(), js.size(), chunk, sizeof(chunk));
  if (n < 1) return fail(r, "bad_field", "d");
  r.imgOff = off.as<uint32_t>();
  r.imgData = chunk;
  r.imgLen = (uint16_t)n;
  return true;
}

// ---------------------------------------------------------------------
// Entry points
// ---------------------------------------------------------------------
HandleResult tooLongResult() {
  HandleResult r;
  fail(r, "too_long");
  return r;
}

HandleResult handleLine(ViewState& view, const char* line, size_t len) {
  HandleResult r;
  if (len == 0) {  // ignore blank lines
    r.blank = true;
    return r;
  }
  if (len > PROTO_MAX_LINE_LEN) return tooLongResult();

  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, line, len);
  if (err) {
    snprintf(r.err, sizeof(r.err), "bad_json %s", err.c_str());
    return r;
  }
  if (!doc.is<JsonObject>()) {
    fail(r, "not_object");
    return r;
  }
  JsonObjectConst root = doc.as<JsonObjectConst>();

  // "v" missing is treated as 1 (old hosts). Only 1 and 2 exist.
  int ver = 1;
  JsonVariantConst vv = root["v"];
  if (!vv.isNull()) {
    if (!vv.is<int>() || (vv.as<int>() != 1 && vv.as<int>() != 2)) {
      fail(r, "bad_version");
      return r;
    }
    ver = vv.as<int>();
  }
  const char* type = root["type"].is<const char*>() ? root["type"].as<const char*>() : nullptr;
  if (!type) {
    fail(r, "missing_type");
    return r;
  }

  bool ok = true;
  if (strcmp(type, "ping") == 0) {
    // heartbeat only, either version
  } else if (strcmp(type, "full_refresh") == 0) {
    r.forceFull = true;
  } else if (strcmp(type, "stats") == 0) {
    if (ver != 1) return fail(r, "bad_version"), r;
    parseStats(root, view.s);
    view.hasData = true;
    view.v2 = false;  // back to the v1 screen
  } else if (strcmp(type, "dash") == 0 || strcmp(type, "quota") == 0 || strcmp(type, "models") == 0 ||
             strcmp(type, "heat") == 0 || strcmp(type, "mouse") == 0) {
    if (ver != 2) return fail(r, "bad_version"), r;
    if (strcmp(type, "dash") == 0) {
      Top t;
      if ((ok = parseDash(root, t, r))) view.d.top = t;
    } else if (strcmp(type, "quota") == 0) {
      Quota q;
      if ((ok = parseQuota(root, q, r))) view.d.quota = q;
    } else if (strcmp(type, "models") == 0) {
      Models m;
      if ((ok = parseModels(root, m, r))) view.d.models = m;
    } else if (strcmp(type, "heat") == 0) {
      Heat h;
      if ((ok = parseHeat(root, h, r))) view.d.heat = h;
    } else {
      Mouse m;
      if ((ok = parseMouse(root, m, r))) view.d.mouse = m;
    }
    if (!ok) return r;
    view.hasData = true;
    view.v2 = true;
    r.kind = strcmp(type, "dash") == 0     ? KIND_DASH
             : strcmp(type, "quota") == 0  ? KIND_QUOTA
             : strcmp(type, "models") == 0 ? KIND_MODELS
             : strcmp(type, "heat") == 0   ? KIND_HEAT
                                           : KIND_MOUSE;
  } else if (strcmp(type, "wifi_scan") == 0 || strcmp(type, "wifi_set") == 0 || strcmp(type, "wifi_status") == 0 ||
             strcmp(type, "wifi_clear") == 0 || strcmp(type, "reset_settings") == 0) {
    if (ver != 2) return fail(r, "bad_version"), r;
    if (strcmp(type, "wifi_set") == 0) {
      if (!parseWifiSet(root, r)) return r;  // `view` is never touched by commands
      r.cmd = CMD_WIFI_SET;
    } else {
      r.cmd = strcmp(type, "wifi_scan") == 0     ? CMD_WIFI_SCAN
              : strcmp(type, "wifi_status") == 0 ? CMD_WIFI_STATUS
              : strcmp(type, "wifi_clear") == 0  ? CMD_WIFI_CLEAR
                                                 : CMD_RESET_SETTINGS;
    }
  } else if (strcmp(type, "wx_cfg") == 0 || strcmp(type, "wx_status") == 0) {
    if (ver != 2) return fail(r, "bad_version"), r;
    if (strcmp(type, "wx_cfg") == 0) {
      if (!parseWxCfg(root, r)) return r;
      r.cmd = CMD_WX_CFG;
    } else {
      r.cmd = CMD_WX_STATUS;
    }
  } else if (strcmp(type, "page") == 0 || strcmp(type, "page_status") == 0 || strcmp(type, "refresh_now") == 0) {
    if (ver != 2) return fail(r, "bad_version"), r;
    if (strcmp(type, "page") == 0) {
      if (!parsePage(root, r)) return r;
      r.cmd = CMD_PAGE;
    } else {
      r.cmd = strcmp(type, "page_status") == 0 ? CMD_PAGE_STATUS : CMD_REFRESH_NOW;
    }
  } else if (strncmp(type, "img_", 4) == 0 &&
             (strcmp(type, "img_begin") == 0 || strcmp(type, "img_chunk") == 0 || strcmp(type, "img_end") == 0)) {
    if (ver != 2) return fail(r, "bad_version"), r;
    if (!parseImg(root, type, r)) return r;
    r.cmd = strcmp(type, "img_begin") == 0 ? CMD_IMG_BEGIN : strcmp(type, "img_chunk") == 0 ? CMD_IMG_CHUNK : CMD_IMG_END;
  } else if (strcmp(type, "pair") == 0 || strcmp(type, "ota_begin") == 0) {
    if (ver != 2) return fail(r, "bad_version"), r;
    if (strcmp(type, "ota_begin") == 0) {
      if (!parseOtaBegin(root, r)) return r;
      r.cmd = CMD_OTA_BEGIN;
    } else {
      r.cmd = CMD_PAIR;
    }
  } else {
    fail(r, "unknown_type");
    return r;
  }
  r.ok = true;
  return r;
}
