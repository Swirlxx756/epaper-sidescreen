#pragma once
// Shared by preview.cpp (example screens) and render.cpp (the host GUI's live preview): an Adafruit_GFX
// target backed by the two colour planes, the same drawing call as the board, and PPM output.
// Both programs compile the REAL src/dashboard_v2.cpp and src/protocol.cpp, so a preview cannot drift
// from what the firmware draws.

#include <stdio.h>

#include "dashboard_v2.h"
#include "protocol.h"
#include "tri_planes.h"

static const uint16_t GXEPD_RED = 0xF800;  // GxEPD_RED; any non-black / non-white colour lands on the colour plane

// Same job as TriCanvas in ui.cpp: Adafruit_GFX target backed by the two planes.
class PlaneCanvas : public Adafruit_GFX {
 public:
  PlaneCanvas() : Adafruit_GFX(400, 300) {}
  TriPlanes<400, 300> planes;
  void drawPixel(int16_t x, int16_t y, uint16_t color) override { planes.set(x, y, triFromGfx(color)); }
  void fillScreen(uint16_t color) override { planes.fill(triFromGfx(color)); }
};

// The dashboard v2 screen as ui.cpp draws it. tri = false: B/W panel (accent = black).
inline void drawView(PlaneCanvas& canvas, const ViewState& v, bool tri) {
  U8G2_FOR_ADAFRUIT_GFX u8g2;
  canvas.fillScreen(0xFFFF);
  u8g2.begin(canvas);
  u8g2.setFontMode(1);  // transparent background, like ui.cpp
  u8g2.setFontDirection(0);
  dashv2::draw(canvas, u8g2, v.d, v.online, dashv2::Style{tri ? GXEPD_RED : (uint16_t)0x0000, tri});
}

// Binary PPM (P6), each panel pixel as a scale x scale block: white, black or an e-paper red (not pure #f00).
inline bool writePpm(const PlaneCanvas& c, FILE* f, int scale) {
  fprintf(f, "P6\n%d %d\n255\n", 400 * scale, 300 * scale);
  for (int y = 0; y < 300 * scale; y++)
    for (int x = 0; x < 400 * scale; x++) {
      TriColor t = c.planes.get(x / scale, y / scale);
      unsigned char px[3] = {255, 255, 255};
      if (t == TRI_BLACK) px[0] = px[1] = px[2] = 0;
      if (t == TRI_COLOR) {
        px[0] = 200;
        px[1] = 20;
        px[2] = 20;
      }
      fwrite(px, 1, 3, f);
    }
  return !ferror(f);
}
