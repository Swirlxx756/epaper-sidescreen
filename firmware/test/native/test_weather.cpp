// Host-side tests for the offline weather feature: Open-Meteo JSON -> WeatherB, alert derivation, the
// request URL, wx_cfg / wx_status protocol messages, the wx_status line and the mode / fetch rules.
// No hardware, no network. Build + run (from firmware/, after `pio run -e waveshare_esp32_42tri` has
// fetched the libraries; Windows: g++ from MSYS2 in C:/msys64/ucrt64/bin):
//   g++ -std=c++17 -Wall -Wextra -DARDUINO=100 -DARDUINOJSON_ENABLE_ARDUINO_STRING=0 -DARDUINOJSON_ENABLE_ARDUINO_STREAM=0 -DARDUINOJSON_ENABLE_ARDUINO_PRINT=0 -DARDUINOJSON_ENABLE_PROGMEM=0 -Itest/host_shim -Isrc -I.pio/libdeps/waveshare_esp32_42tri/ArduinoJson/src test/native/test_weather.cpp src/weather_parse.cpp src/protocol.cpp -o test_weather && ./test_weather
// wx_open_meteo.inc is a real answer of api.open-meteo.com (Shanghai, 2026-11-02), unchanged.

#include <math.h>
#include <stdio.h>
#include <string.h>

#include <string>

#include "protocol.h"
#include "weather_model.h"
#include "weather_parse.h"
#include "weather_policy.h"

static int failures = 0, checks = 0;
#define CHECK(cond)                                            \
  do {                                                         \
    checks++;                                                  \
    if (!(cond)) {                                             \
      failures++;                                              \
      printf("  FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
    }                                                          \
  } while (0)
#define CHECK_STR(a, b)                                                         \
  do {                                                                          \
    checks++;                                                                   \
    if (strcmp((a), (b)) != 0) {                                                \
      failures++;                                                               \
      printf("  FAIL %s:%d: \"%s\" != \"%s\"\n", __FILE__, __LINE__, (a), (b)); \
    }                                                                           \
  } while (0)

static const char* FIXTURE =
#include "wx_open_meteo.inc"
    ;

static std::string replaceAll(std::string s, const std::string& a, const std::string& b) {
  size_t p = 0;
  while ((p = s.find(a, p)) != std::string::npos) {
    s.replace(p, a.size(), b);
    p += b.size();
  }
  return s;
}

static bool parse(const std::string& json, wxb::WeatherB& w, const char* place = "\xE4\xB8\x8A\xE6\xB5\xB7") {
  return wxb::parseOpenMeteo(json.c_str(), json.size(), place, w);
}

static void test_parse_fixture() {
  printf("parse real Open-Meteo answer\n");
  wxb::WeatherB w;
  memset(&w, 0x5A, sizeof w);
  CHECK(parse(FIXTURE, w));
  CHECK_STR(w.place, "\xE4\xB8\x8A\xE6\xB5\xB7");
  CHECK(w.temp == 15 && w.apparent == 14);
  CHECK(w.code == 51 && w.humidity == 96 && w.wind == 11 && !w.is_day);
  CHECK(w.today_max == 20 && w.today_min == 13 && w.today_precip == 100);
  CHECK_STR(w.sunrise, "06:10");
  CHECK_STR(w.sunset, "17:04");
  CHECK(w.hour0 == 1);
  CHECK(w.hourly[0].temp == 15 && w.hourly[0].precip == 100);
  CHECK(w.hourly[2].precip == 82 && w.hourly[11].temp == 19 && w.hourly[11].precip == 0);
  CHECK_STR(w.days[0].weekday, "TUE");
  CHECK_STR(w.days[1].weekday, "WED");
  CHECK_STR(w.days[2].weekday, "THU");
  CHECK(w.days[0].code == 3 && w.days[0].max == 20 && w.days[0].min == 13 && w.days[0].precip == 29);
  CHECK(w.days[1].code == 81 && w.days[1].max == 20 && w.days[1].min == 16 && w.days[1].precip == 100);
  CHECK(w.days[2].code == 51 && w.days[2].max == 19 && w.days[2].min == 13 && w.days[2].precip == 55);
  CHECK_STR(w.updated_at, "11-02 01:15");
  CHECK(w.status == wxb::WX_OK);
  CHECK_STR(w.alert, "HEAVY RAIN 100%");

  // Same input, same signature; any drawn field changes it.
  wxb::WeatherB w2;
  CHECK(parse(FIXTURE, w2));
  CHECK(wxb::signature(w) == wxb::signature(w2));
  w2.hourly[7].temp++;
  CHECK(wxb::signature(w) != wxb::signature(w2));
  w2 = w;
  w2.status = wxb::WX_STALE;
  CHECK(wxb::signature(w) != wxb::signature(w2));
  w2 = w;
  strcpy(w2.updated_at, "11-02 01:30");
  CHECK(wxb::signature(w) != wxb::signature(w2));
}

static void test_parse_edge() {
  printf("parse edge cases\n");
  wxb::WeatherB w, keep;
  memset(&keep, 0x11, sizeof keep);

  // Failures leave the output untouched.
  w = keep;
  CHECK(!parse("", w) && memcmp(&w, &keep, sizeof w) == 0);
  CHECK(!parse("not json", w));
  CHECK(!parse("{}", w));
  CHECK(!parse("{\"error\":true,\"reason\":\"Latitude must be in range of -90 to 90\"}", w));
  CHECK(!parse(replaceAll(FIXTURE, "\"current\":{", "\"currentX\":{"), w));
  CHECK(!parse(replaceAll(FIXTURE, "2026-11-02T01:15", "garbage"), w));
  CHECK(!parse(replaceAll(FIXTURE, "\"temperature_2m\":14.6,\"apparent", "\"temperature_2m\":null,\"apparent"), w));
  CHECK(!parse(replaceAll(FIXTURE, "\"2026-11-05\"", ""), w));  // only 3 daily entries -> invalid json shape
  CHECK(memcmp(&w, &keep, sizeof w) == 0);
  // Truncated body (connection dropped).
  std::string cut = FIXTURE;
  cut.resize(cut.size() / 2);
  CHECK(!parse(cut, w));
  CHECK(memcmp(&w, &keep, sizeof w) == 0);

  // null precipitation, short hourly list, negative / large numbers, long place name.
  std::string j = FIXTURE;
  j = replaceAll(j, "\"precipitation_probability\":[100,100,82,54,31,17,7,0,0,0,0,0]",
                 "\"precipitation_probability\":[null,50]");
  j = replaceAll(j, "\"temperature_2m\":[14.6,14.5,14.6,14.5,14.5,14.2,14.5,15.8,17.0,17.9,18.7,19.3]",
                 "\"temperature_2m\":[-0.4,-7.6]");
  j = replaceAll(j, "\"wind_speed_10m\":10.6", "\"wind_speed_10m\":300.0");
  CHECK(parse(j, w, "A very long place name that does not fit in the buffer"));
  CHECK(w.hourly[0].precip == 0 && w.hourly[1].precip == 50 && w.hourly[11].precip == 50);  // repeats the last hour
  CHECK(w.hourly[0].temp == 0 && w.hourly[1].temp == -8 && w.hourly[11].temp == -8);
  CHECK(w.wind == 255);
  CHECK(strlen(w.place) == sizeof(w.place) - 1);

  // A day boundary: the 12h window may start at 23 and the clock hour wraps in the page.
  j = replaceAll(FIXTURE, "\"hourly\":{\"time\":[\"2026-11-02T01:00\"", "\"hourly\":{\"time\":[\"2026-11-02T23:00\"");
  CHECK(parse(j, w) && w.hour0 == 23);

  // Cold, hot.
  j = replaceAll(FIXTURE, "\"temperature_2m_max\":[19.7,", "\"temperature_2m_max\":[36.2,");
  j = replaceAll(j, "\"precipitation_probability_max\":[100,", "\"precipitation_probability_max\":[10,");
  j = replaceAll(j, "\"weather_code\":[55,", "\"weather_code\":[1,");
  j = replaceAll(j, "\"weather_code\":51,", "\"weather_code\":1,");
  CHECK(parse(j, w));
  CHECK_STR(w.alert, "HIGH TEMP 36\xC2\xB0" "C");
}

static void test_alert() {
  printf("alert derivation\n");
  char a[28];
  wxb::deriveAlert(1, 1, 10, 25, 15, a, sizeof a);
  CHECK_STR(a, "");
  wxb::deriveAlert(3, 61, 79, 34, -4, a, sizeof a);  // every threshold just missed
  CHECK_STR(a, "");
  wxb::deriveAlert(1, 1, 80, 25, 15, a, sizeof a);
  CHECK_STR(a, "HEAVY RAIN 80%");
  wxb::deriveAlert(65, 65, 30, 25, 15, a, sizeof a);
  CHECK_STR(a, "HEAVY RAIN");
  wxb::deriveAlert(1, 67, 30, 25, 15, a, sizeof a);
  CHECK_STR(a, "HEAVY RAIN");
  wxb::deriveAlert(82, 1, 30, 25, 15, a, sizeof a);
  CHECK_STR(a, "HEAVY RAIN");
  wxb::deriveAlert(95, 95, 90, 40, 30, a, sizeof a);  // thunder wins over rain and heat
  CHECK_STR(a, "THUNDERSTORM");
  wxb::deriveAlert(2, 99, 10, 25, 15, a, sizeof a);
  CHECK_STR(a, "THUNDERSTORM");
  for (int c : {71, 73, 75, 77, 85, 86}) {
    wxb::deriveAlert(c, c, 90, 0, -10, a, sizeof a);  // snow wins over "precip >= 80" and cold
    CHECK_STR(a, "SNOW");
  }
  wxb::deriveAlert(78, 78, 10, 0, 0, a, sizeof a);  // 78 is not snow
  CHECK_STR(a, "");
  wxb::deriveAlert(1, 1, 10, 35, 20, a, sizeof a);
  CHECK_STR(a, "HIGH TEMP 35\xC2\xB0" "C");
  wxb::deriveAlert(1, 1, 10, 10, -5, a, sizeof a);
  CHECK_STR(a, "LOW TEMP -5\xC2\xB0" "C");
  wxb::deriveAlert(1, 1, 10, 36, -6, a, sizeof a);  // hot before cold
  CHECK_STR(a, "HIGH TEMP 36\xC2\xB0" "C");
  wxb::deriveAlert(1, 1, 250, 10, 5, a, sizeof a);
  CHECK_STR(a, "HEAVY RAIN 100%");
  // Fits the page: <= 27 bytes even for the longest.
  wxb::deriveAlert(1, 1, 10, -128, -128, a, sizeof a);
  CHECK(strlen(a) < sizeof a);
}

static void test_url_dow() {
  printf("request URL, day of week\n");
  char url[640];
  CHECK(wxb::requestUrl(31.23, 121.47, url, sizeof url));
  CHECK(strncmp(url, "https://api.open-meteo.com/v1/forecast?latitude=31.2300&longitude=121.4700&timezone=auto", 87) == 0);
  CHECK(strstr(url, "&forecast_days=4&forecast_hours=12") != nullptr);
  CHECK(strstr(url, "&current=temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m,is_day") != nullptr);
  CHECK(strstr(url, "&hourly=temperature_2m,precipitation_probability") != nullptr);
  CHECK(strstr(url, "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset") != nullptr);
  CHECK(wxb::requestUrl(-33.8688, -151.2093, url, sizeof url) && strstr(url, "latitude=-33.8688&longitude=-151.2093") != nullptr);
  CHECK(!wxb::requestUrl(1, 2, url, 40));
  CHECK(wxb::dayOfWeek(2026, 11, 2) == 1);   // Monday
  CHECK(wxb::dayOfWeek(2024, 2, 29) == 4);   // Thursday
  CHECK(wxb::dayOfWeek(2000, 1, 1) == 6);    // Saturday
  CHECK(wxb::dayOfWeek(2026, 12, 31) == 4);
  // Never negative, never out of range, for any input the parser could hand over.
  for (int y : {-5, 0, 1, 2026})
    for (int m = 0; m <= 13; m++)
      for (int d : {0, 1, 31, 99}) {
        int w = wxb::dayOfWeek(y, m, d);
        CHECK(w >= 0 && w <= 6);
      }
  CHECK(wxb::dayOfWeek(-1, 1, 1) >= 0 && wxb::dayOfWeek(0, 1, -40) >= 0);  // negative sums used to give -1..-6
}

static void test_daily_date_digits() {
  printf("daily dates must be digits\n");
  wxb::WeatherB w;
  std::string j = replaceAll(FIXTURE, "\"2026-11-03\"", "\"20x6-11-03\"");
  CHECK(j != FIXTURE);
  CHECK(!parse(j, w));
  j = replaceAll(FIXTURE, "\"2026-11-04\"", "\"2026-1a-04\"");
  CHECK(!parse(j, w));
  j = replaceAll(FIXTURE, "\"2026-11-05\"", "\"2026-11-0z\"");
  CHECK(!parse(j, w));
  CHECK(parse(FIXTURE, w));
}

static void test_utf8_place_blob() {
  printf("UTF-8 place check, config blob\n");
  // validUtf8
  CHECK(validUtf8("", 0) && validUtf8("abc", 3) && validUtf8("\xE4\xB8\x8A\xE6\xB5\xB7", 6));
  CHECK(validUtf8("\xC2\xA2", 2) && validUtf8("\xF0\x9F\x98\x80", 4) && validUtf8("\xF4\x8F\xBF\xBF", 4));
  CHECK(!validUtf8("\x80", 1));                  // stray continuation
  CHECK(!validUtf8("\xE4\xB8", 2));             // truncated
  CHECK(!validUtf8("\xE4\xB8" "a", 3));         // bad continuation
  CHECK(!validUtf8("\xC0\x80", 2) && !validUtf8("\xC1\xBF", 2));  // overlong
  CHECK(!validUtf8("\xE0\x80\x80", 3) && !validUtf8("\xF0\x80\x80\x80", 4));
  CHECK(!validUtf8("\xED\xA0\x80", 3));        // surrogate
  CHECK(!validUtf8("\xF4\x90\x80\x80", 4) && !validUtf8("\xF5\x80\x80\x80", 4) && !validUtf8("\xFF", 1));
  // validPlace: the protocol / storage check
  using namespace wxp;
  CHECK(validPlace("\xE4\xB8\x8A\xE6\xB5\xB7", 6));
  CHECK(!validPlace("\xE4\xB8", 2) && !validPlace("ab\xFF", 3) && !validPlace("\xC0\xAF", 2));
  // ... and through the protocol
  ViewState v;
  const char* badLine = "{\"v\":2,\"type\":\"wx_cfg\",\"on\":true,\"place\":\"\xE4\xB8\",\"lat\":31.2,\"lon\":121.4}";
  HandleResult r = handleLine(v, badLine, strlen(badLine));
  CHECK(!r.ok && strstr(r.err, "bad_field") != nullptr);

  // Blob round trip.
  Cfg c;
  c.on = true;
  c.hasLoc = true;
  strcpy(c.place, "\xE4\xB8\x8A\xE6\xB5\xB7");
  c.lat = 31.23;
  c.lon = 121.47;
  c.afterMin = 45;
  uint8_t blob[WX_BLOB_SIZE];
  encodeCfg(c, blob);
  Cfg d;
  CHECK(decodeCfg(blob, sizeof blob, d));
  CHECK(d.on && d.hasLoc && d.afterMin == 45 && d.lat == 31.23 && d.lon == 121.47 && strcmp(d.place, c.place) == 0);
  Cfg none;  // no location, off, default minutes
  encodeCfg(none, blob);
  Cfg e;
  e.on = true;
  CHECK(decodeCfg(blob, sizeof blob, e));
  CHECK(!e.on && !e.hasLoc && e.afterMin == WX_AFTER_MIN_DEFAULT);
  // Corrupt blobs are refused and leave the target alone.
  encodeCfg(c, blob);
  Cfg keep = d;
  CHECK(!decodeCfg(blob, sizeof blob - 1, d));
  uint8_t bad[WX_BLOB_SIZE];
  memcpy(bad, blob, sizeof bad); bad[0] = 2;  CHECK(!decodeCfg(bad, sizeof bad, d));   // version
  memcpy(bad, blob, sizeof bad); bad[1] = 7;  CHECK(!decodeCfg(bad, sizeof bad, d));   // flag
  memcpy(bad, blob, sizeof bad); bad[4] = 0; bad[5] = 0; CHECK(!decodeCfg(bad, sizeof bad, d));  // after_min 0
  memcpy(bad, blob, sizeof bad); bad[4] = 0xFF; bad[5] = 0xFF; CHECK(!decodeCfg(bad, sizeof bad, d));
  memcpy(bad, blob, sizeof bad); bad[6 + WX_PLACE_MAX] = 'x'; CHECK(!decodeCfg(bad, sizeof bad, d));  // not terminated
  memcpy(bad, blob, sizeof bad); bad[6] = '"'; CHECK(!decodeCfg(bad, sizeof bad, d));  // quote in place
  memcpy(bad, blob, sizeof bad); bad[7] = 0xFF; CHECK(!decodeCfg(bad, sizeof bad, d));  // bad UTF-8
  memcpy(bad, blob, sizeof bad); memset(bad + 30, 0xFF, 8); CHECK(!decodeCfg(bad, sizeof bad, d));  // lat NaN
  double big = 91.0;
  memcpy(bad, blob, sizeof bad); memcpy(bad + 30, &big, 8); CHECK(!decodeCfg(bad, sizeof bad, d));
  CHECK(d.afterMin == keep.afterMin && strcmp(d.place, keep.place) == 0 && d.lat == keep.lat);
  // Watchdog helper.
  CHECK(!fetchOverdue(1000, 1000 + WX_TASK_MAX_MS - 1) && fetchOverdue(1000, 1000 + WX_TASK_MAX_MS));
  CHECK(fetchOverdue(0xFFFFFF00u, 0xFFFFFF00u + (uint32_t)WX_TASK_MAX_MS));  // millis() wrap
}

static HandleResult feed(ViewState& v, const std::string& line) { return handleLine(v, line.c_str(), line.size()); }

static void test_wx_cfg() {
  printf("wx_cfg / wx_status messages\n");
  ViewState v;
  HandleResult r = feed(v, "{\"v\":2,\"type\":\"wx_cfg\",\"on\":true,\"place\":\"\xE4\xB8\x8A\xE6\xB5\xB7\",\"lat\":31.23,\"lon\":121.47,\"after_min\":10}");
  CHECK(r.ok && r.cmd == CMD_WX_CFG && r.wxOn && r.wxSetLoc && r.wxAfterMin == 10);
  CHECK_STR(r.wxPlace, "\xE4\xB8\x8A\xE6\xB5\xB7");
  CHECK(r.wxLat == 31.23);
  CHECK(r.wxLon == 121.47);
  CHECK(r.kind == KIND_NONE && !v.hasData);

  r = feed(v, "{\"v\":2,\"type\":\"wx_cfg\",\"on\":false}");  // disable, keeps the location
  CHECK(r.ok && r.cmd == CMD_WX_CFG && !r.wxOn && !r.wxSetLoc && r.wxAfterMin == 0);
  r = feed(v, "{\"v\":2,\"type\":\"wx_cfg\",\"on\":false,\"place\":\"x\",\"lat\":999}");  // ignored when off
  CHECK(r.ok && !r.wxSetLoc);
  r = feed(v, "{\"v\":2,\"type\":\"wx_cfg\",\"on\":true}");  // enable with the stored location
  CHECK(r.ok && r.wxOn && !r.wxSetLoc && r.wxAfterMin == 0);
  r = feed(v, "{\"v\":2,\"type\":\"wx_cfg\",\"on\":true,\"after_min\":240}");
  CHECK(r.ok && r.wxAfterMin == 240);
  r = feed(v, "{\"v\":2,\"type\":\"wx_cfg\",\"on\":true,\"place\":\"NYC\",\"lat\":-90,\"lon\":-180}");  // bounds
  CHECK(r.ok && r.wxSetLoc && r.wxLat == -90 && r.wxLon == -180);
  r = feed(v, "{\"v\":2,\"type\":\"wx_cfg\",\"on\":true,\"place\":\"NYC\",\"lat\":90,\"lon\":180,\"after_min\":1}");
  CHECK(r.ok && r.wxLat == 90 && r.wxLon == 180 && r.wxAfterMin == 1);
  // 23 bytes ok, 24 not (UTF-8 bytes, not characters).
  r = feed(v, "{\"v\":2,\"type\":\"wx_cfg\",\"on\":true,\"place\":\"" + std::string(23, 'a') + "\",\"lat\":1,\"lon\":1}");
  CHECK(r.ok);
  r = feed(v, "{\"v\":2,\"type\":\"wx_cfg\",\"on\":true,\"place\":\"" + std::string(24, 'a') + "\",\"lat\":1,\"lon\":1}");
  CHECK(!r.ok && strcmp(r.err, "bad_field place") == 0 && r.cmd == CMD_NONE);
  r = feed(v, "{\"v\":2,\"type\":\"wx_cfg\",\"on\":true,\"place\":\"\xE4\xB8\x8A\xE6\xB5\xB7\xE4\xB8\x8A\xE6\xB5\xB7\xE4\xB8\x8A\xE6\xB5\xB7\xE4\xB8\x8A\xE6\xB5\xB7\",\"lat\":1,\"lon\":1}");  // 24 bytes
  CHECK(!r.ok && strcmp(r.err, "bad_field place") == 0);

  struct Bad { const char* line; const char* err; } bad[] = {
      {"{\"v\":2,\"type\":\"wx_cfg\"}", "missing_field on"},
      {"{\"v\":2,\"type\":\"wx_cfg\",\"on\":1}", "bad_field on"},
      {"{\"v\":2,\"type\":\"wx_cfg\",\"on\":\"yes\"}", "bad_field on"},
      {"{\"v\":1,\"type\":\"wx_cfg\",\"on\":true}", "bad_version"},
      {"{\"type\":\"wx_cfg\",\"on\":true}", "bad_version"},
      {"{\"v\":1,\"type\":\"wx_status\"}", "bad_version"},
      {"{\"v\":2,\"type\":\"wx_cfg\",\"on\":true,\"place\":\"A\",\"lat\":91,\"lon\":0}", "bad_field lat"},
      {"{\"v\":2,\"type\":\"wx_cfg\",\"on\":true,\"place\":\"A\",\"lat\":-90.5,\"lon\":0}", "bad_field lat"},
      {"{\"v\":2,\"type\":\"wx_cfg\",\"on\":true,\"place\":\"A\",\"lat\":\"1\",\"lon\":0}", "bad_field lat"},
      {"{\"v\":2,\"type\":\"wx_cfg\",\"on\":true,\"place\":\"A\",\"lat\":0,\"lon\":180.1}", "bad_field lon"},
      {"{\"v\":2,\"type\":\"wx_cfg\",\"on\":true,\"place\":\"A\",\"lat\":0,\"lon\":-181}", "bad_field lon"},
      {"{\"v\":2,\"type\":\"wx_cfg\",\"on\":true,\"place\":\"\",\"lat\":0,\"lon\":0}", "bad_field place"},
      {"{\"v\":2,\"type\":\"wx_cfg\",\"on\":true,\"place\":5,\"lat\":0,\"lon\":0}", "bad_field place"},
      {"{\"v\":2,\"type\":\"wx_cfg\",\"on\":true,\"place\":\"a\\\"b\",\"lat\":0,\"lon\":0}", "bad_field place"},
      {"{\"v\":2,\"type\":\"wx_cfg\",\"on\":true,\"place\":\"a\\nb\",\"lat\":0,\"lon\":0}", "bad_field place"},
      {"{\"v\":2,\"type\":\"wx_cfg\",\"on\":true,\"place\":\"A\",\"lat\":0}", "missing_field lon"},
      {"{\"v\":2,\"type\":\"wx_cfg\",\"on\":true,\"lat\":0,\"lon\":0}", "missing_field place"},
      {"{\"v\":2,\"type\":\"wx_cfg\",\"on\":true,\"lon\":0,\"place\":\"A\"}", "missing_field lat"},
      {"{\"v\":2,\"type\":\"wx_cfg\",\"on\":true,\"after_min\":0}", "bad_field after_min"},
      {"{\"v\":2,\"type\":\"wx_cfg\",\"on\":true,\"after_min\":241}", "bad_field after_min"},
      {"{\"v\":2,\"type\":\"wx_cfg\",\"on\":true,\"after_min\":\"10\"}", "bad_field after_min"},
      {"{\"v\":2,\"type\":\"wx_cfg\",\"on\":true,\"after_min\":10.5}", "bad_field after_min"},
  };
  for (const Bad& b : bad) {
    r = feed(v, b.line);
    if (r.ok || strcmp(r.err, b.err) != 0) printf("  line: %s\n   got: ok=%d err=%s\n", b.line, r.ok, r.err);
    CHECK(!r.ok && strcmp(r.err, b.err) == 0 && r.cmd == CMD_NONE);
  }

  r = feed(v, "{\"v\":2,\"type\":\"wx_status\"}");
  CHECK(r.ok && r.cmd == CMD_WX_STATUS);
  CHECK(!v.hasData && !v.weather);  // commands never touch the view
}

static void test_status_json() {
  printf("wx_status line\n");
  wxp::Cfg c;
  char b[256];
  CHECK(wxb::statusJson(c, false, "", "", b, sizeof b));
  CHECK_STR(b, "{\"type\":\"status\",\"on\":false,\"place\":\"\",\"lat\":null,\"lon\":null,\"after_min\":10,\"mode\":\"dash\",\"last\":null,\"err\":null}");
  c.on = true;
  c.hasLoc = true;
  strcpy(c.place, "\xE4\xB8\x8A\xE6\xB5\xB7");
  c.lat = 31.23;
  c.lon = 121.47;
  c.afterMin = 15;
  CHECK(wxb::statusJson(c, true, "11-02 01:15", "tls", b, sizeof b));
  CHECK_STR(b, "{\"type\":\"status\",\"on\":true,\"place\":\"\xE4\xB8\x8A\xE6\xB5\xB7\",\"lat\":31.23,\"lon\":121.47,\"after_min\":15,\"mode\":\"weather\",\"last\":\"11-02 01:15\",\"err\":\"tls\"}");
  CHECK(!wxb::statusJson(c, true, "11-02 01:15", "tls", b, 20));
  // The longest place still fits the firmware's 200-byte buffer.
  memset(c.place, 'x', WX_PLACE_MAX);
  c.place[WX_PLACE_MAX] = '\0';
  c.lat = -33.868820123;
  c.lon = -151.209295999;
  CHECK(wxb::statusJson(c, true, "11-02 01:15", "http_503", b, 200));
}

static void test_policy() {
  printf("mode and fetch rules\n");
  using namespace wxp;
  const uint32_t TO = 120000UL, MIN = 60000UL;
  Cfg c;
  c.on = true;
  c.hasLoc = true;
  strcpy(c.place, "X");
  c.afterMin = 10;

  // Not before after_min, never when off.
  CHECK(!wantWeather(c, false, 1000, 1000 + 10 * MIN - 1, true, TO));
  CHECK(wantWeather(c, false, 1000, 1000 + 10 * MIN, true, TO));
  Cfg off = c;
  off.on = false;
  CHECK(!wantWeather(off, false, 1000, 1000 + 20 * MIN, true, TO));
  CHECK(!wantWeather(off, true, 1000, 1000 + 20 * MIN, true, TO));  // switched off while shown: back to dashboard
  // Small after_min cannot beat the host-offline timeout.
  Cfg one = c;
  one.afterMin = 1;
  CHECK(!wantWeather(one, false, 5000, 5000 + TO - 1, true, TO));
  CHECK(wantWeather(one, false, 5000, 5000 + TO, true, TO));
  // No message since boot: counted from boot.
  CHECK(!wantWeather(c, false, 0, 10 * MIN - 1, true, TO));
  CHECK(wantWeather(c, false, 0, 10 * MIN, true, TO));
  // Wi-Fi needed to enter, not to stay.
  CHECK(!wantWeather(c, false, 1000, 1000 + 11 * MIN, false, TO));
  CHECK(wantWeather(c, true, 1000, 1000 + 11 * MIN, false, TO));
  // No location: the "no city" page does not need Wi-Fi.
  Cfg noloc = c;
  noloc.hasLoc = false;
  CHECK(wantWeather(noloc, false, 1000, 1000 + 11 * MIN, false, TO));
  // A host message ends it at once.
  CHECK(!wantWeather(c, true, 1000 + 11 * MIN, 1000 + 11 * MIN + 5, true, TO));
  // millis() wrap.
  CHECK(wantWeather(c, false, 0xFFFFFFFFUL - 5 * MIN, 5 * MIN + 1, true, TO));
  CHECK(!wantWeather(c, false, 0xFFFFFFFFUL - 5 * MIN + 10, 5 * MIN - 1, true, TO));

  // Fetch timing: at once, then every 30 min, after a failure every 5 min.
  Fetch f;
  CHECK(fetchDue(f, 12345));
  f.everTried = true;
  f.tryMs = 100000;
  f.haveData = true;
  f.okMs = 100000;
  CHECK(!fetchDue(f, 100000 + WX_REFETCH_MS - 1));
  CHECK(fetchDue(f, 100000 + WX_REFETCH_MS));
  f.lastFailed = true;
  CHECK(!fetchDue(f, 100000 + WX_RETRY_MS - 1));
  CHECK(fetchDue(f, 100000 + WX_RETRY_MS));
  CHECK(WX_RETRY_MS < WX_REFETCH_MS && WX_FRESH_MS > WX_REFETCH_MS);

  // What is shown.
  Fetch none;
  CHECK(!showWeather(c, none, false, true, 0));                       // not wanted
  CHECK(!showWeather(c, none, true, true, 0));                        // nothing to show yet: stay on the dashboard
  CHECK(showWeather(noloc, none, true, true, 0));                     // "no city" page
  Fetch ok;
  ok.haveData = true;
  ok.okMs = 1000;
  ok.everTried = true;
  ok.tryMs = 1000;
  CHECK(showWeather(c, ok, true, true, 1000 + 60000));                // fresh data
  CHECK(!showWeather(c, ok, true, true, 1000 + WX_FRESH_MS));         // old data, refetch pending, Wi-Fi up: wait
  Fetch failed = ok;
  failed.lastFailed = true;
  CHECK(showWeather(c, failed, true, true, 1000 + WX_FRESH_MS));      // refetch failed: show the stale data
  CHECK(showWeather(c, ok, true, false, 1000 + WX_FRESH_MS));         // Wi-Fi down: show the stale data
  CHECK(!showWeather(c, ok, false, true, 1000 + 60000));

  // Validators.
  CHECK(validLat(90) && validLat(-90) && !validLat(90.001) && !validLat(-90.001));
  CHECK(validLon(180) && validLon(-180) && !validLon(180.001));
  CHECK(validAfter(1) && validAfter(240) && !validAfter(0) && !validAfter(241));
  CHECK(validPlace("a", 1) && !validPlace("", 0) && !validPlace("a\"b", 3) && !validPlace("a\\b", 3));
  CHECK(!validPlace("a\tb", 3) && !validPlace("ab", WX_PLACE_MAX + 1));
}

static void test_view_signature() {
  printf("view signature in weather mode\n");
  ViewState v;
  String a = v.signature();  // boot
  v.weather = true;
  memset(&v.wx, 0, sizeof v.wx);
  v.wx.status = wxb::WX_NO_LOCATION;
  String b = v.signature();
  CHECK(!(a == b));
  CHECK(b == v.signature());
  v.wx.status = wxb::WX_STALE;
  CHECK(!(b == v.signature()));
  // Dashboard data arriving while the weather page is shown does not change the weather page.
  String c = v.signature();
  feed(v, "{\"v\":2,\"type\":\"dash\",\"st\":\"na\"}");
  CHECK(c == v.signature());
  v.weather = false;
  CHECK(!(c == v.signature()));
}

int main() {
  test_parse_fixture();
  test_parse_edge();
  test_alert();
  test_url_dow();
  test_daily_date_digits();
  test_utf8_place_blob();
  test_wx_cfg();
  test_status_json();
  test_policy();
  test_view_signature();
  printf("\n%d checks, %d failed\n", checks, failures);
  return failures ? 1 : 0;
}
