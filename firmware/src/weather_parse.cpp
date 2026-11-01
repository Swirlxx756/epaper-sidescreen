#include "weather_parse.h"

#include <ArduinoJson.h>
#include <math.h>
#include <stdio.h>
#include <string.h>

namespace wxb {

static const char* const WEEKDAY[7] = {"SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"};

bool requestUrl(double lat, double lon, char* out, size_t n) {
  int w = snprintf(out, n,
                   "https://api.open-meteo.com/v1/forecast?latitude=%.4f&longitude=%.4f&timezone=auto"
                   "&forecast_days=4&forecast_hours=12"
                   "&current=temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m,is_day"
                   "&hourly=temperature_2m,precipitation_probability"
                   "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset",
                   lat, lon);
  return w > 0 && (size_t)w < n;
}

int dayOfWeek(int y, int m, int d) {
  static const int t[] = {0, 3, 2, 5, 0, 3, 5, 1, 4, 6, 2, 4};
  if (m < 1 || m > 12) return 0;  // callers check; never index outside t[]
  if (m < 3) y -= 1;
  return (((y + y / 4 - y / 100 + y / 400 + t[m - 1] + d) % 7) + 7) % 7;  // C++ % can be negative
}

static bool isThunder(int c) { return c >= 95 && c <= 99; }
static bool isSnow(int c) { return (c >= 71 && c <= 77) || c == 85 || c == 86; }
static bool isHeavyRain(int c) { return c == 65 || c == 67 || c == 82; }

void deriveAlert(int code, int dayCode, int precip, int tmax, int tmin, char* out, size_t n) {
  out[0] = '\0';
  if (isThunder(code) || isThunder(dayCode)) {
    snprintf(out, n, "THUNDERSTORM");
  } else if (isSnow(code) || isSnow(dayCode)) {
    snprintf(out, n, "SNOW");
  } else if (precip >= 80) {
    snprintf(out, n, "HEAVY RAIN %d%%", precip > 100 ? 100 : precip);
  } else if (isHeavyRain(code) || isHeavyRain(dayCode)) {
    snprintf(out, n, "HEAVY RAIN");
  } else if (tmax >= 35) {
    snprintf(out, n, "HIGH TEMP %d\xC2\xB0" "C", tmax);
  } else if (tmin <= -5) {
    snprintf(out, n, "LOW TEMP %d\xC2\xB0" "C", tmin);
  }
}

static int clampI(double v, int lo, int hi) {
  long r = lround(v);
  return r < lo ? lo : (r > hi ? hi : (int)r);
}

// "YYYY-MM-DDTHH:MM" (>= 16 chars, digits in place).
static bool isIsoMinute(const char* s) {
  if (!s || strlen(s) < 16) return false;
  static const int digits[] = {0, 1, 2, 3, 5, 6, 8, 9, 11, 12, 14, 15};
  for (int i : digits)
    if (s[i] < '0' || s[i] > '9') return false;
  return s[4] == '-' && s[7] == '-' && s[10] == 'T' && s[13] == ':';
}
static int num2(const char* s) { return (s[0] - '0') * 10 + (s[1] - '0'); }

static double numOr(JsonVariantConst v, double dflt) { return v.is<double>() ? v.as<double>() : dflt; }

bool parseOpenMeteo(const char* json, size_t len, const char* place, WeatherB& out) {
  JsonDocument filter;
  JsonObject fc = filter["current"].to<JsonObject>();
  for (const char* k : {"time", "temperature_2m", "apparent_temperature", "relative_humidity_2m", "weather_code",
                        "wind_speed_10m", "is_day"})
    fc[k] = true;
  JsonObject fh = filter["hourly"].to<JsonObject>();
  for (const char* k : {"time", "temperature_2m", "precipitation_probability"}) fh[k] = true;
  JsonObject fd = filter["daily"].to<JsonObject>();
  for (const char* k : {"time", "weather_code", "temperature_2m_max", "temperature_2m_min",
                        "precipitation_probability_max", "sunrise", "sunset"})
    fd[k] = true;

  JsonDocument doc;
  if (deserializeJson(doc, json, len, DeserializationOption::Filter(filter))) return false;

  JsonObjectConst cur = doc["current"];
  JsonObjectConst hr = doc["hourly"];
  JsonObjectConst dy = doc["daily"];
  const char* ctime = cur["time"] | "";
  if (!isIsoMinute(ctime)) return false;
  if (!cur["temperature_2m"].is<double>() || !cur["weather_code"].is<double>()) return false;
  JsonArrayConst hTime = hr["time"], hTemp = hr["temperature_2m"], hPrec = hr["precipitation_probability"];
  JsonArrayConst dTime = dy["time"], dCode = dy["weather_code"], dMax = dy["temperature_2m_max"],
                 dMin = dy["temperature_2m_min"], dPrec = dy["precipitation_probability_max"],
                 dRise = dy["sunrise"], dSet = dy["sunset"];
  if (hTime.size() < 1 || hTemp.size() < 1 || dTime.size() < 4 || dCode.size() < 4 || dMax.size() < 4 ||
      dMin.size() < 4)
    return false;
  const char* h0 = hTime[0] | "";
  if (!isIsoMinute(h0)) return false;

  WeatherB w;
  memset(&w, 0, sizeof w);
  size_t pn = strlen(place);
  if (pn >= sizeof w.place) pn = sizeof w.place - 1;
  memcpy(w.place, place, pn);

  double t = cur["temperature_2m"].as<double>();
  w.temp = (int8_t)clampI(t, -127, 127);
  w.apparent = (int8_t)clampI(numOr(cur["apparent_temperature"], t), -127, 127);
  w.code = (uint8_t)clampI(cur["weather_code"].as<double>(), 0, 255);
  w.humidity = (uint8_t)clampI(numOr(cur["relative_humidity_2m"], 0), 0, 100);
  w.wind = (uint8_t)clampI(numOr(cur["wind_speed_10m"], 0), 0, 255);
  w.is_day = numOr(cur["is_day"], 1) >= 0.5;

  w.today_max = (int8_t)clampI(numOr(dMax[0], 0), -127, 127);
  w.today_min = (int8_t)clampI(numOr(dMin[0], 0), -127, 127);
  w.today_precip = (uint8_t)clampI(numOr(dPrec[0], 0), 0, 100);
  const char* sr = dRise[0] | "";
  const char* ss = dSet[0] | "";
  if (isIsoMinute(sr)) memcpy(w.sunrise, sr + 11, 5);
  else strcpy(w.sunrise, "--:--");
  if (isIsoMinute(ss)) memcpy(w.sunset, ss + 11, 5);
  else strcpy(w.sunset, "--:--");

  w.hour0 = num2(h0 + 11);
  int nh = (int)hTemp.size();
  if (nh > 12) nh = 12;
  for (int i = 0; i < 12; i++) {
    int j = i < nh ? i : nh - 1;  // short answer: repeat the last hour
    w.hourly[i].temp = (int8_t)clampI(numOr(hTemp[j], 0), -127, 127);
    w.hourly[i].precip = (uint8_t)clampI(numOr(hPrec[j], 0), 0, 100);
  }

  for (int i = 0; i < 3; i++) {  // tomorrow and the two days after
    const char* dt = dTime[i + 1] | "";
    if (strlen(dt) < 10 || dt[4] != '-' || dt[7] != '-') return false;
    static const int dateDigits[] = {0, 1, 2, 3, 5, 6, 8, 9};
    for (int k : dateDigits)
      if (dt[k] < '0' || dt[k] > '9') return false;  // num2() would turn a letter into a bogus weekday
    int y = num2(dt) * 100 + num2(dt + 2), mo = num2(dt + 5), da = num2(dt + 8);
    if (mo < 1 || mo > 12) return false;
    strcpy(w.days[i].weekday, WEEKDAY[dayOfWeek(y, mo, da)]);
    w.days[i].code = (uint8_t)clampI(numOr(dCode[i + 1], 0), 0, 255);
    w.days[i].max = (int8_t)clampI(numOr(dMax[i + 1], 0), -127, 127);
    w.days[i].min = (int8_t)clampI(numOr(dMin[i + 1], 0), -127, 127);
    w.days[i].precip = (uint8_t)clampI(numOr(dPrec[i + 1], 0), 0, 100);
  }

  snprintf(w.updated_at, sizeof w.updated_at, "%.5s %.5s", ctime + 5, ctime + 11);
  w.status = WX_OK;
  deriveAlert(w.code, (int)numOr(dCode[0], w.code), w.today_precip, w.today_max, w.today_min, w.alert,
              sizeof w.alert);
  out = w;
  return true;
}

bool statusJson(const wxp::Cfg& c, bool weatherMode, const char* last, const char* err, char* out, size_t n) {
  JsonDocument d;
  d["type"] = "status";
  d["on"] = c.on;
  d["place"] = c.hasLoc ? c.place : "";
  if (c.hasLoc) {
    d["lat"] = c.lat;
    d["lon"] = c.lon;
  } else {
    d["lat"] = nullptr;
    d["lon"] = nullptr;
  }
  d["after_min"] = c.afterMin;
  d["mode"] = weatherMode ? "weather" : "dash";
  if (last && last[0]) d["last"] = last;
  else d["last"] = nullptr;
  if (err && err[0]) d["err"] = err;
  else d["err"] = nullptr;
  size_t w = serializeJson(d, out, n);
  return w > 0 && w < n;
}

}  // namespace wxb
