#pragma once
// Three-colour calibration frame (diagnostic build only, EPD_TRI_TEST_PATTERN).
// Three byte-aligned vertical bands so every band maps to whole bytes of
// both planes and a screen description can be checked against the bytes:
//   band 1  x   8..135  black          (black plane 0x00, ry plane 0xFF)
//   band 2  x 136..263  third colour   (black plane 0xFF, ry plane 0x00)
//   band 3  x 264..391  white, framed  (inside the frame both planes 0xFF)
// all bands y 40..219. ui.cpp adds black text labels outside the bands.

#include "tri_planes.h"

namespace tricalib {

const int BAND_TOP = 40, BAND_BOTTOM = 220;  // [top, bottom)
const int BAND_W = 128;
const int BAND1_X = 8, BAND2_X = BAND1_X + BAND_W, BAND3_X = BAND2_X + BAND_W;
const int FRAME = 2;  // black frame width around band 3

template <int W, int H>
void fillRect(TriPlanes<W, H>& p, int x, int y, int w, int h, TriColor c) {
  for (int j = y; j < y + h; j++)
    for (int i = x; i < x + w; i++) p.set(i, j, c);
}

template <int W, int H>
void draw(TriPlanes<W, H>& p) {
  const int h = BAND_BOTTOM - BAND_TOP;
  p.fill(TRI_WHITE);
  fillRect(p, BAND1_X, BAND_TOP, BAND_W, h, TRI_BLACK);
  fillRect(p, BAND2_X, BAND_TOP, BAND_W, h, TRI_COLOR);
  fillRect(p, BAND3_X, BAND_TOP, BAND_W, FRAME, TRI_BLACK);
  fillRect(p, BAND3_X, BAND_BOTTOM - FRAME, BAND_W, FRAME, TRI_BLACK);
  fillRect(p, BAND3_X, BAND_TOP, FRAME, h, TRI_BLACK);
  fillRect(p, BAND3_X + BAND_W - FRAME, BAND_TOP, FRAME, h, TRI_BLACK);
}

}  // namespace tricalib
