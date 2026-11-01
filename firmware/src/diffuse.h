#pragma once
// Floyd-Steinberg error diffusion into the three-colour palette white / red /
// black, for gradients and smooth fills. Pure logic (no Arduino, no GFX), so
// it is tested on the host, see test/native/test_dash.cpp.
//
// Colour model. A target is described by two numbers in Q8 (256 = 1.0):
//   l = lightness (0 black .. 256 white)
//   r = redness   (0 .. 256)
// A pixel painted with area fractions fw / fr / fk of white / red / black has
//   l = fw + fr * RED_LIGHT / 256,  r = fr.
// The palette points in (l, r) space are white (256, 0), red (RED_LIGHT, 256)
// and black (0, 0). Every pixel takes the nearest palette point of
// (target + carried error); the difference is spread to the neighbours with
// the Floyd-Steinberg weights 7/16, 3/16, 5/16, 1/16. Rows alternate scan
// direction (serpentine), which hides the diagonal worms of a plain left to
// right scan.
//
// Memory: only two rows of error (two channels each) are kept, on the stack:
// 4 * (MAXW + 2) int16 = about 1.6 KB. Wider rectangles are done in strips.
//
// allowBlack = false leaves only white and red (heat cells, area under the trend);
// it is ignored when red is not available (a B/W screen needs black to make grey).
//
// Black / white screens: allowRed = false makes red unavailable; the target
// is reduced to its lightness (red counts as grey RED_LIGHT) and only white
// and black are used.

#include <stdint.h>
#include <string.h>

namespace diffuse {

enum Color : uint8_t { WHITE = 0, RED = 1, BLACK = 2 };

constexpr int ONE = 256;
constexpr int RED_LIGHT = 77;  // lightness of the red ink, about 0.30
constexpr int MAXW = 200;      // widest strip handled in one pass

struct Px {
  int16_t l = ONE;
  int16_t r = 0;
  bool skip = false;  // leave this pixel alone (no drawing, no error carried)
};

// From area fractions (Q8, fw + fr + fk = 256).
inline Px fractions(int fw, int fr, int fk) {
  (void)fk;
  Px p;
  p.l = (int16_t)(fw + fr * RED_LIGHT / ONE);
  p.r = (int16_t)fr;
  return p;
}

// Point t (Q8, 0..256) on the gradient white -> red -> black:
// t = 0 white, t = 128 red, t = 256 black.
inline Px gradient(int t) {
  if (t < 0) t = 0;
  if (t > ONE) t = ONE;
  if (t <= ONE / 2) return fractions(ONE - 2 * t, 2 * t, 0);
  return fractions(0, 2 * (ONE - t), 2 * t - ONE);
}

// White -> red only (s = 0 white .. 256 full red): for the area under the trend.
inline Px whiteToRed(int s) {
  if (s < 0) s = 0;
  if (s > ONE) s = ONE;
  return fractions(ONE - s, s, 0);
}

inline Px skipPx() {
  Px p;
  p.skip = true;
  return p;
}

namespace detail {
inline int clampi(int v, int lo, int hi) { return v < lo ? lo : (v > hi ? hi : v); }
}  // namespace detail

// Dither the w x h rectangle at (x0, y0). target(x, y) -> Px (absolute
// coordinates); put(x, y, Color) is called once for each pixel that is not
// skipped. Nothing outside the rectangle is touched.
template <class TargetFn, class PutFn>
void rect(int x0, int y0, int w, int h, bool allowRed, TargetFn target, PutFn put, bool allowBlack = true) {
  if (w <= 0 || h <= 0) return;
  if (w > MAXW) {  // strips: the error does not carry across a strip edge
    for (int sx = 0; sx < w; sx += MAXW) rect(x0 + sx, y0, (w - sx > MAXW) ? MAXW : (w - sx), h, allowRed, target, put, allowBlack);
    return;
  }
  // err[row][channel][x + 1]; row 0 = current, row 1 = next.
  int16_t err[2][2][MAXW + 2];
  memset(err, 0, sizeof(err));
  int cur = 0;
  const bool useBlack = allowBlack || !allowRed;

  for (int y = 0; y < h; y++) {
    const int nxt = cur ^ 1;
    const bool rtl = (y & 1) != 0;
    const int dir = rtl ? -1 : 1;
    for (int i = 0; i < w; i++) {
      const int x = rtl ? (w - 1 - i) : i;
      const int xi = x + 1;
      Px t = target(x0 + x, y0 + y);
      if (t.skip) {
        err[cur][0][xi] = err[cur][1][xi] = 0;
        continue;
      }
      int l = t.l, r = t.r;
      if (!allowRed) {
        // Lightness only: the redness of the target counted as grey already.
        r = 0;
      }
      int vl = detail::clampi(l + err[cur][0][xi], 0, ONE);
      int vr = allowRed ? detail::clampi(r + err[cur][1][xi], 0, ONE) : 0;

      // Nearest palette point (squared distance in (l, r)).
      int dW = (vl - ONE) * (vl - ONE) + vr * vr;
      int dK = vl * vl + vr * vr;
      Color c = WHITE;
      int best = dW;
      if (useBlack && dK < dW) {
        c = BLACK;
        best = dK;
      }
      if (allowRed) {
        int dR = (vl - RED_LIGHT) * (vl - RED_LIGHT) + (vr - ONE) * (vr - ONE);
        if (dR < best) c = RED;
      }
      int pl = c == WHITE ? ONE : (c == RED ? RED_LIGHT : 0);
      int pr = c == RED ? ONE : 0;
      put(x0 + x, y0 + y, c);

      int el = vl - pl, er = vr - pr;
      for (int ch = 0; ch < 2; ch++) {
        int e = ch == 0 ? el : er;
        if (e == 0) continue;
        int xf = xi + dir, xb = xi - dir;
        err[cur][ch][xf] = (int16_t)(err[cur][ch][xf] + e * 7 / 16);
        err[nxt][ch][xb] = (int16_t)(err[nxt][ch][xb] + e * 3 / 16);
        err[nxt][ch][xi] = (int16_t)(err[nxt][ch][xi] + e * 5 / 16);
        err[nxt][ch][xf] = (int16_t)(err[nxt][ch][xf] + e / 16);
      }
    }
    memset(err[cur][0], 0, sizeof(int16_t) * (w + 2));
    memset(err[cur][1], 0, sizeof(int16_t) * (w + 2));
    cur = nxt;
  }
}

}  // namespace diffuse
