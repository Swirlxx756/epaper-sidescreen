// Offline preview of weather page variant B: renders the six scenarios to PPM (scale 3 by default).
// Build: bash test/preview/build_weather_b.sh   (from firmware/)
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <string>

#include "tri_planes.h"
#include "weather_page_b.h"

static const uint16_t RED = 0xF800;

class PlaneCanvas : public Adafruit_GFX {
 public:
  PlaneCanvas() : Adafruit_GFX(400, 300) {}
  TriPlanes<400, 300> planes;
  void drawPixel(int16_t x, int16_t y, uint16_t color) override { planes.set(x, y, triFromGfx(color)); }
  void fillScreen(uint16_t color) override { planes.fill(triFromGfx(color)); }
};

static bool writePpm(const PlaneCanvas& c, FILE* f, int scale) {
  fprintf(f, "P6\n%d %d\n255\n", 400 * scale, 300 * scale);
  for (int y = 0; y < 300 * scale; y++)
    for (int x = 0; x < 400 * scale; x++) {
      TriColor t = c.planes.get(x / scale, y / scale);
      unsigned char px[3] = {255, 255, 255};
      if (t == TRI_BLACK) px[0] = px[1] = px[2] = 0;
      if (t == TRI_COLOR) { px[0] = 200; px[1] = 20; px[2] = 20; }
      fwrite(px, 1, 3, f);
    }
  return !ferror(f);
}

using namespace wxb;

static void setDays(WeatherB& w, const Day (&d)[3]) { for (int i = 0; i < 3; i++) w.days[i] = d[i]; }
static void setHours(WeatherB& w, int hour0, const int8_t (&t)[12], const uint8_t (&p)[12]) {
  w.hour0 = hour0;
  for (int i = 0; i < 12; i++) w.hourly[i] = Hour{t[i], p[i]};
}

static WeatherB base() {
  WeatherB w;
  memset(&w, 0, sizeof w);
  strcpy(w.place, "上海");  // UTF-8 source file
  strcpy(w.sunrise, "06:12");
  strcpy(w.sunset, "17:08");
  strcpy(w.updated_at, "10-31 08:00");
  w.status = WX_OK;
  w.is_day = true;
  return w;
}

int main(int argc, char** argv) {
  std::string outDir = argc > 1 ? argv[1] : "test/preview/out";
  int scale = argc > 2 ? atoi(argv[2]) : 3;
  if (scale < 1) scale = 1;

  struct Sc { const char* name; WeatherB w; } sc[6];

  { // sunny
    WeatherB w = base();
    w.temp = 26; w.apparent = 27; w.code = 0; w.humidity = 48; w.wind = 11;
    w.today_max = 28; w.today_min = 19; w.today_precip = 5;
    setHours(w, 9, {26, 27, 27, 28, 28, 28, 27, 26, 25, 24, 22, 21}, {0, 0, 0, 5, 5, 5, 0, 0, 0, 0, 5, 5});
    setDays(w, {{"TUE", 1, 29, 20, 10}, {"WED", 2, 27, 19, 30}, {"THU", 3, 24, 18, 45}});
    sc[0] = {"wx_sunny", w};
  }
  { // rain
    WeatherB w = base();
    w.temp = 18; w.apparent = 17; w.code = 65; w.humidity = 92; w.wind = 24;
    w.today_max = 20; w.today_min = 15; w.today_precip = 90;
    setHours(w, 14, {18, 18, 17, 17, 17, 16, 16, 16, 16, 17, 17, 18}, {90, 95, 80, 70, 60, 45, 30, 20, 15, 10, 10, 20});
    setDays(w, {{"TUE", 63, 19, 14, 80}, {"WED", 80, 21, 15, 55}, {"THU", 2, 24, 16, 15}});
    strcpy(w.alert, "HEAVY RAIN 90%");
    sc[1] = {"wx_rain", w};
  }
  { // hot
    WeatherB w = base();
    w.temp = 36; w.apparent = 40; w.code = 1; w.humidity = 55; w.wind = 9;
    w.today_max = 37; w.today_min = 27; w.today_precip = 10;
    setHours(w, 12, {36, 37, 37, 36, 35, 34, 32, 31, 30, 29, 29, 28}, {0, 0, 5, 5, 10, 10, 10, 5, 0, 0, 0, 0});
    setDays(w, {{"TUE", 0, 37, 28, 5}, {"WED", 95, 33, 26, 70}, {"THU", 3, 31, 25, 35}});
    strcpy(w.alert, "HIGH TEMP 36" "\xC2\xB0" "C");
    sc[2] = {"wx_hot", w};
  }
  { // night
    WeatherB w = base();
    w.temp = 12; w.apparent = 10; w.code = 0; w.is_day = false; w.humidity = 70; w.wind = 6;
    w.today_max = 19; w.today_min = 9; w.today_precip = 0;
    setHours(w, 22, {12, 12, 11, 11, 10, 10, 9, 9, 10, 12, 14, 16}, {0, 0, 0, 0, 0, 0, 0, 5, 5, 5, 0, 0});
    setDays(w, {{"TUE", 1, 20, 9, 0}, {"WED", 45, 18, 10, 10}, {"THU", 61, 16, 11, 60}});
    sc[3] = {"wx_night", w};
  }
  { // stale
    WeatherB w = base();
    w.status = WX_STALE;
    w.temp = 22; w.apparent = 22; w.code = 2; w.humidity = 60; w.wind = 14;
    w.today_max = 24; w.today_min = 16; w.today_precip = 20;
    setHours(w, 8, {22, 23, 24, 24, 23, 22, 21, 20, 19, 18, 18, 17}, {5, 5, 10, 15, 20, 20, 15, 10, 5, 5, 0, 0});
    setDays(w, {{"MON", 3, 23, 15, 25}, {"TUE", 61, 20, 14, 65}, {"WED", 71, 8, 1, 40}});
    strcpy(w.updated_at, "10-30 22:00");
    sc[4] = {"wx_stale", w};
  }
  { // no location
    WeatherB w = base();
    w.status = WX_NO_LOCATION;
    w.place[0] = 0;
    sc[5] = {"wx_noloc", w};
  }

  int fail = 0;
  for (auto& s : sc) {
    PlaneCanvas canvas;
    U8G2_FOR_ADAFRUIT_GFX u8g2;
    canvas.fillScreen(0xFFFF);
    u8g2.begin(canvas);
    u8g2.setFontMode(1);
    u8g2.setFontDirection(0);
    wxb::draw(canvas, u8g2, s.w, RED);
    std::string path = outDir + "/b_" + s.name + ".ppm";
    FILE* f = fopen(path.c_str(), "wb");
    bool ok = f && writePpm(canvas, f, scale);
    if (f) ok = fclose(f) == 0 && ok;
    if (!ok) { fprintf(stderr, "cannot write %s\n", path.c_str()); fail++; }
    else printf("wrote %s\n", path.c_str());
  }
  return fail ? 1 : 0;
}
