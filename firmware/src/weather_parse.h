#pragma once
// Open-Meteo JSON -> wxb::WeatherB, alert derivation, request URL and the wx_status line.
// Pure (ArduinoJson only, no Arduino), covered by test/native/test_weather.cpp.

#include <stddef.h>

#include "weather_model.h"
#include "weather_policy.h"

namespace wxb {

// "https://api.open-meteo.com/v1/forecast?latitude=..&longitude=..&timezone=auto&..." into `out`.
// False if it does not fit.
bool requestUrl(double lat, double lon, char* out, size_t n);

// Parse a forecast response (timezone=auto: all times are local). `place` goes into out.place.
// On failure returns false and `out` is untouched. status = WX_OK, alert derived.
bool parseOpenMeteo(const char* json, size_t len, const char* place, WeatherB& out);

// Short English alert for the page, "" when none. Priority: thunder, snow, heavy rain, high temp, low temp.
// code = current WMO code, dayCode = today's (worst) code, precip = today's max probability %.
void deriveAlert(int code, int dayCode, int precip, int tmax, int tmin, char* out, size_t n);

// Day of week 0=Sunday..6 for a Gregorian date (m 1..12).
int dayOfWeek(int y, int m, int d);

// `WX` payload (without the "WX " prefix and newline): {"type":"status",...}. `last`/`err` "" or null = null.
// False if it does not fit.
bool statusJson(const wxp::Cfg& c, bool weatherMode, const char* last, const char* err, char* out, size_t n);

}  // namespace wxb
