#pragma once
// 4x4 Bayer ordered dither for the black/white plane. Pure logic (no
// Arduino), so it can be tested on the host, see test/native/test_dash.cpp.
//
// The pattern is indexed by absolute screen coordinates, so two areas drawn
// with the same level line up without a seam. Levels 0..5 give
// 0 / 12.5 / 25 / 50 / 75 / 100 % black.

#include <stdint.h>

namespace dither {

constexpr int LEVELS = 6;

// Black pixels per 16 for each level.
constexpr uint8_t kInk[LEVELS] = {0, 2, 4, 8, 12, 16};

// Classic 4x4 Bayer thresholds 0..15.
constexpr uint8_t kBayer[4][4] = {
    {0, 8, 2, 10},
    {12, 4, 14, 6},
    {3, 11, 1, 9},
    {15, 7, 13, 5},
};

// True when the pixel at absolute (x, y) is ink at `level`. Levels outside
// 0..5 are clamped. Negative coordinates are fine.
inline bool isInk(int level, int x, int y) {
  if (level <= 0) return false;
  if (level >= LEVELS - 1) return true;
  return kBayer[y & 3][x & 3] < kInk[level];
}

// Fill a rectangle with the dither pattern. Gfx needs
// drawPixel(int16_t, int16_t, uint16_t) (Adafruit_GFX does). Only ink pixels
// are drawn; the white pixels are left as they are (the screen is cleared to
// white before drawing).
template <class Gfx>
void fillRect(Gfx& g, int x, int y, int w, int h, int level, uint16_t ink) {
  if (level <= 0) return;
  for (int j = 0; j < h; j++)
    for (int i = 0; i < w; i++)
      if (isInk(level, x + i, y + j)) g.drawPixel(x + i, y + j, ink);
}

// 1 px horizontal / vertical line at 50 % (every other dot), used as the
// separators between blocks.
template <class Gfx>
void hLine(Gfx& g, int x, int y, int w, uint16_t ink) {
  fillRect(g, x, y, w, 1, 3, ink);
}
template <class Gfx>
void vLine(Gfx& g, int x, int y, int h, uint16_t ink) {
  fillRect(g, x, y, 1, h, 3, ink);
}

// Dashed 1 px line of length L, every other dot, counted from both ends towards
// the middle: the first and the last dot are always on, so a dashed rectangle
// has black corners whatever its size. (An even length gets two neighbouring
// dots in the middle.)
inline bool dashOn(int i, int L) {
  if (i < 0 || i >= L) return false;
  int d = i < L - 1 - i ? i : L - 1 - i;  // distance from the nearer end
  return (d & 1) == 0;
}

// 1 px outline of w x h drawn with dashOn on every edge.
template <class Gfx>
void dashedRect(Gfx& g, int x, int y, int w, int h, uint16_t ink) {
  for (int i = 0; i < w; i++)
    if (dashOn(i, w)) {
      g.drawPixel(x + i, y, ink);
      g.drawPixel(x + i, y + h - 1, ink);
    }
  for (int j = 0; j < h; j++)
    if (dashOn(j, h)) {
      g.drawPixel(x, y + j, ink);
      g.drawPixel(x + w - 1, y + j, ink);
    }
}

}  // namespace dither
