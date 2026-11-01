#pragma once
// Two-plane frame buffer for black/white + red/yellow panels, using the
// vendor Paint convention: bit 1 = white, bit 0 = ink, MSB = leftmost pixel.
// A pixel is white (both 1), black (black plane 0) or colour (ry plane 0);
// never both, so the two planes always agree on the background.

#include <stdint.h>
#include <string.h>

enum TriColor : uint8_t { TRI_WHITE = 0, TRI_BLACK = 1, TRI_COLOR = 2 };

// GFX 16-bit colour -> plane colour. 0xFFFF white, 0x0000 black, anything
// else (GxEPD_RED / GxEPD_YELLOW ...) is the third colour.
inline TriColor triFromGfx(uint16_t c) {
  if (c == 0xFFFF) return TRI_WHITE;
  if (c == 0x0000) return TRI_BLACK;
  return TRI_COLOR;
}

template <int W, int H>
struct TriPlanes {
  static const int BYTES = W / 8 * H;
  uint8_t black[BYTES];
  uint8_t ry[BYTES];

  void fill(TriColor c) {
    memset(black, c == TRI_BLACK ? 0x00 : 0xFF, BYTES);
    memset(ry, c == TRI_COLOR ? 0x00 : 0xFF, BYTES);
  }

  void set(int x, int y, TriColor c) {
    if (x < 0 || y < 0 || x >= W || y >= H) return;
    int i = (x + y * W) / 8;
    uint8_t m = 0x80 >> (x & 7);
    black[i] = c == TRI_BLACK ? (black[i] & ~m) : (black[i] | m);
    ry[i] = c == TRI_COLOR ? (ry[i] & ~m) : (ry[i] | m);
  }

  TriColor get(int x, int y) const {
    int i = (x + y * W) / 8;
    uint8_t m = 0x80 >> (x & 7);
    if (!(ry[i] & m)) return TRI_COLOR;
    return (black[i] & m) ? TRI_WHITE : TRI_BLACK;
  }
};
