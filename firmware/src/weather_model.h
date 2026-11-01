#pragma once
// Weather page data model (no drawing, no Arduino): shared by the page (weather_page_b), the
// Open-Meteo parser (weather_parse) and the view model (model.h).

#include <stddef.h>
#include <stdint.h>
#include <string.h>

namespace wxb {

enum Status : uint8_t { WX_OK = 0, WX_STALE, WX_NO_LOCATION, WX_NO_WIFI };

struct Hour {
  int8_t temp;     // C
  uint8_t precip;  // probability %
};
struct Day {
  char weekday[4];  // "MON"
  uint8_t code;     // WMO
  int8_t max, min;
  uint8_t precip;   // %
};

struct WeatherB {
  char place[32];       // UTF-8; drawn with u8g2_font_wqy14_t_chinese3 (covers common Chinese city names)
  int8_t temp, apparent;
  uint8_t code;         // WMO weather_code
  uint8_t humidity;     // %
  uint8_t wind;         // km/h
  bool is_day;
  int8_t today_max, today_min;
  uint8_t today_precip; // max probability today, %
  char sunrise[6], sunset[6];  // "HH:MM"
  int hour0;            // clock hour (0..23) of hourly[0]
  Hour hourly[12];
  Day days[3];
  char updated_at[12];  // "MM-DD HH:MM"
  Status status;
  char alert[28];       // "" = none; shown in red
};

// FNV-1a over every field the page draws (content signature: equal = identical page).
inline uint64_t signature(const WeatherB& w) {
  uint64_t h = 1469598103934665603ULL;
  auto mix = [&h](const void* p, size_t n) {
    const unsigned char* b = (const unsigned char*)p;
    for (size_t i = 0; i < n; i++) h = (h ^ b[i]) * 1099511628211ULL;
  };
  auto str = [&](const char* s) { mix(s, strlen(s) + 1); };
  str(w.place);
  str(w.sunrise);
  str(w.sunset);
  str(w.updated_at);
  str(w.alert);
  const int32_t v[] = {w.temp, w.apparent, w.code, w.humidity, w.wind, w.is_day, w.today_max, w.today_min,
                       w.today_precip, w.hour0, w.status};
  mix(v, sizeof(v));
  for (const Hour& x : w.hourly) {
    const int32_t t[] = {x.temp, x.precip};
    mix(t, sizeof(t));
  }
  for (const Day& d : w.days) {
    str(d.weekday);
    const int32_t t[] = {d.code, d.max, d.min, d.precip};
    mix(t, sizeof(t));
  }
  return h;
}

}  // namespace wxb
