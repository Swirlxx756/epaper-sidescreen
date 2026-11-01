#include "weather_page_b.h"

#include <math.h>
#include <stdio.h>
#include <string.h>

#include "dither.h"

namespace wxb {
namespace {

constexpr uint16_t BLACK = 0x0000;
constexpr uint16_t WHITE = 0xFFFF;
#define DEG "\xC2\xB0"

// Same font family as dashboard_v2 (Helvetica text, Logisoso for the big number).
const uint8_t* const F_R8 = u8g2_font_helvR08_tf;
const uint8_t* const F_B8 = u8g2_font_helvB08_tf;
const uint8_t* const F_R10 = u8g2_font_helvR10_tf;
const uint8_t* const F_B10 = u8g2_font_helvB10_tf;
const uint8_t* const F_B12 = u8g2_font_helvB12_tf;
const uint8_t* const F_BIG = u8g2_font_logisoso38_tn;
const uint8_t* const F_CJK = u8g2_font_wqy14_t_chinese3;  // place name (same font as variant A)

// Layout: identical column grid to dashboard_v2.
constexpr int WIDTH = 400, HEIGHT = 300;
constexpr int COL_L0 = 6, COL_L1 = 193, COL_R0 = 206, COL_R1 = 393, COL_W = 188, VSEP_X = 199;
constexpr int TITLE_Y = 15;
constexpr int BIG_TOP = 20, BIG_H = 38, ROW3 = 71, SEP_L1 = 77;
constexpr int TODAY_TITLE = 90, TODAY_FIRST = 108, TODAY_PITCH = 20;
constexpr int RANGE_SEP = 217, RANGE_TITLE = 233, RANGE_NOW = 250, RANGE_BAR_Y = 262, RANGE_BAR_H = 8;
constexpr int SEP_L2 = 281, STATUS_BASE = 297;
// right column
constexpr int T_TOP = 35, T_BOT = 56;  // temperature line band
constexpr int CAP_Y = 83, BAR_BASE = 124, BAR_MAXH = 36, HOUR_Y = 135, SEP_R1 = 141;
constexpr int DAYS_TITLE = 154, DAY_C0 = 184, DAY_PITCH = 41;
constexpr int RAIN_ALERT_PCT = 60;
constexpr int RAIN_EMPTY_PCT = 15;  // peak below this: show the intentional "no rain" state

enum Kind { K_CLEAR, K_PARTLY, K_CLOUD, K_FOG, K_RAIN, K_SNOW, K_THUNDER };

Kind kindOf(int code) {
  if (code == 0) return K_CLEAR;
  if (code <= 2) return K_PARTLY;
  if (code == 3) return K_CLOUD;
  if (code == 45 || code == 48) return K_FOG;
  if ((code >= 51 && code <= 67) || (code >= 80 && code <= 82)) return K_RAIN;
  if ((code >= 71 && code <= 77) || code == 85 || code == 86) return K_SNOW;
  if (code >= 95) return K_THUNDER;
  return K_CLOUD;
}

const char* condText(int code) {
  switch (code) {
    case 0: return "CLEAR";
    case 1: return "MOSTLY CLEAR";
    case 2: return "PARTLY CLOUDY";
    case 3: return "OVERCAST";
    case 45: case 48: return "FOG";
    case 51: case 53: case 55: return "DRIZZLE";
    case 56: case 57: case 66: case 67: return "FREEZING RAIN";
    case 61: return "LIGHT RAIN";
    case 63: return "RAIN";
    case 65: return "HEAVY RAIN";
    case 71: case 73: case 75: case 77: return "SNOW";
    case 80: case 81: return "SHOWERS";
    case 82: return "HEAVY SHOWERS";
    case 85: case 86: return "SNOW SHOWERS";
    case 95: return "THUNDERSTORM";
    case 96: case 99: return "STORM + HAIL";
    default: return "CLOUDY";
  }
}

// ---- Icons (GFX primitives), black only.
void sun(Adafruit_GFX& g, int cx, int cy, int r, int ray0, int ray1, int t) {
  g.fillCircle(cx, cy, r, BLACK);
  for (int i = 0; i < 8; i++) {  // rays: chain of t x t dots along the ray
    float a = i * 3.14159265f / 4;
    float c = cosf(a), s = sinf(a);
    for (int d = ray0; d <= ray1; d++) g.fillRect((int)lroundf(cx + c * d) - t / 2, (int)lroundf(cy + s * d) - t / 2, t, t, BLACK);
  }
}

void moon(Adafruit_GFX& g, int cx, int cy, int r) {
  g.fillCircle(cx, cy, r, BLACK);
  g.fillCircle(cx + (r * 5) / 10, cy - (r * 4) / 10, (r * 85) / 100, WHITE);
}

void star(Adafruit_GFX& g, int x, int y, int r) {
  g.drawFastHLine(x - r, y, 2 * r + 1, BLACK);
  g.drawFastVLine(x, y - r, 2 * r + 1, BLACK);
}

// Outlined cloud, width w. Two passes: black union, then the white interior (shrunk by t).
void cloud(Adafruit_GFX& g, int cx, int cy, int w, int t) {
  float u = w / 10.0f;
  struct C { float x, y, r; } c[3] = {{-2.8f, 0.6f, 2.2f}, {-0.4f, -0.9f, 3.0f}, {2.6f, 0.3f, 2.4f}};
  int rx0 = cx + (int)lroundf(-2.8f * u), rx1 = cx + (int)lroundf(2.6f * u);
  int ry0 = cy + (int)lroundf(0.6f * u), ry1 = cy + (int)lroundf(2.8f * u);
  for (int i = 0; i < 3; i++)
    g.fillCircle(cx + (int)lroundf(c[i].x * u), cy + (int)lroundf(c[i].y * u), (int)lroundf(c[i].r * u), BLACK);
  g.fillRect(rx0, ry0, rx1 - rx0 + 1, ry1 - ry0 + 1, BLACK);
  for (int i = 0; i < 3; i++)
    g.fillCircle(cx + (int)lroundf(c[i].x * u), cy + (int)lroundf(c[i].y * u), (int)lroundf(c[i].r * u) - t, WHITE);
  g.fillRect(rx0, ry0, rx1 - rx0 + 1, ry1 - ry0 + 1 - t, WHITE);
}

void icon(Adafruit_GFX& g, int cx, int cy, int S, int code, bool day) {
  Kind k = kindOf(code);
  int t = S >= 40 ? 2 : 1;
  float f = S / 10.0f;
  auto P = [&](float v) { return (int)lroundf(v * f); };
  switch (k) {
    case K_CLEAR:
      if (day) {
        sun(g, cx, cy, P(2.2f), P(3.1f), P(4.6f), t);
      } else {
        moon(g, cx, cy, P(3.0f));
        if (S >= 40) {
          star(g, cx + P(3.2f), cy - P(3.0f), 3);
          star(g, cx - P(3.8f), cy + P(2.6f), 2);
        }
      }
      break;
    case K_PARTLY:
      if (day) sun(g, cx - P(1.6f), cy - P(1.6f), P(1.5f), P(2.1f), P(3.1f), t);
      else moon(g, cx - P(1.4f), cy - P(1.6f), P(2.0f));
      cloud(g, cx + P(0.5f), cy + P(1.1f), P(7.2f), t);
      break;
    case K_CLOUD:
      cloud(g, cx, cy - P(0.3f), P(9.0f), t);
      break;
    case K_FOG:
      cloud(g, cx, cy - P(2.0f), P(7.5f), t);
      for (int i = 0; i < 3; i++) {
        int y = cy + P(1.9f) + i * P(1.1f);
        int hw = P(i == 1 ? 3.6f : 3.0f);
        for (int k2 = 0; k2 < t; k2++) g.drawFastHLine(cx - hw + (i == 1 ? 0 : P(0.6f)), y + k2, 2 * hw, BLACK);
      }
      break;
    case K_RAIN:
    case K_SNOW:
    case K_THUNDER:
      cloud(g, cx, cy - P(1.7f), P(8.0f), t);
      if (k == K_RAIN) {
        for (int i = -1; i <= 1; i++)
          for (int k2 = 0; k2 < t; k2++)
            g.drawLine(cx + P(2.0f * i) + k2 + P(0.4f), cy + P(1.7f), cx + P(2.0f * i) - P(0.7f) + k2 + P(0.4f),
                       cy + P(3.7f), BLACK);
      } else if (k == K_SNOW) {
        for (int i = -1; i <= 1; i++) {
          int x = cx + P(2.2f * i), y = cy + P(i == 0 ? 3.0f : 2.4f);
          int r = S >= 40 ? 3 : 2;
          star(g, x, y, r);
          if (S >= 40) {
            g.drawLine(x - 2, y - 2, x + 2, y + 2, BLACK);
            g.drawLine(x - 2, y + 2, x + 2, y - 2, BLACK);
          }
        }
      } else {  // lightning bolt
        int bx = cx + P(0.2f), by = cy + P(1.0f);
        g.fillTriangle(bx + P(1.0f), by, bx - P(1.0f), by + P(2.2f), bx + P(0.4f), by + P(2.2f), BLACK);
        g.fillTriangle(bx + P(1.2f), by + P(1.2f), bx - P(0.6f), by + P(4.0f), bx - P(0.5f), by + P(1.2f), BLACK);
      }
      break;
  }
}

// ---- Text helpers
struct Painter {
  Adafruit_GFX& g;
  U8G2_FOR_ADAFRUIT_GFX& u;
  uint16_t accent;

  int asc(const uint8_t* f) {
    u.setFont(f);
    return u.getFontAscent();
  }
  int width(const uint8_t* f, const char* s) {
    u.setFont(f);
    return u.getUTF8Width(s);
  }
  void text(const uint8_t* f, int x, int y, const char* s, uint16_t fg = BLACK) {
    u.setFont(f);
    u.setFontMode(1);
    u.setForegroundColor(fg);
    u.drawUTF8(x, y, s);
  }
  void right(const uint8_t* f, int xr, int y, const char* s, uint16_t fg = BLACK) { text(f, xr - width(f, s), y, s, fg); }
  void center(const uint8_t* f, int xm, int y, const char* s, uint16_t fg = BLACK) { text(f, xm - width(f, s) / 2, y, s, fg); }

  // Inverted little label starting at x; returns its right edge.
  int tag(int x, int base, const char* s) {
    int a = asc(F_B8), bw = width(F_B8, s) + 5;
    g.fillRect(x, base - a - 1, bw, a + 3, BLACK);
    text(F_B8, x + 2, base, s, WHITE);
    return x + bw;
  }
};

void connectionOffline(Painter& p, int cx, int cy) {
  for (int d = 0; d < 2; d++) {
    p.g.drawLine(cx - 5 + d, cy - 5, cx + 4 + d, cy + 4, p.accent);
    p.g.drawLine(cx - 5 + d, cy + 4, cx + 4 + d, cy - 5, p.accent);
  }
}

void drop(Adafruit_GFX& g, int x, int y, uint16_t c) {  // small raindrop, ~9 px tall, centred on x
  g.fillCircle(x, y + 2, 3, c);
  g.fillTriangle(x, y - 5, x - 3, y + 1, x + 3, y + 1, c);
}

void pct(char* buf, size_t n, int v) { snprintf(buf, n, "%d%%", v); }
void deg(char* buf, size_t n, int v) { snprintf(buf, n, "%d" DEG, v); }

// Calm filler for the lower-left area when there is no alert: today's low..high bar with the
// current temperature marked (bar filled from the low up to "now").
void drawRange(Painter& p, const WeatherB& w) {
  dither::hLine(p.g, COL_L0, RANGE_SEP, COL_W, BLACK);
  p.text(F_B8, COL_L0, RANGE_TITLE, "TODAY RANGE");
  int lo = w.today_min, hi = w.today_max;
  int cur = w.temp;
  if (cur < lo) lo = cur;
  if (cur > hi) hi = cur;
  if (hi == lo) hi = lo + 1;
  char lb[8], hb[8], nb[16], d[8];
  deg(lb, sizeof lb, lo);
  deg(hb, sizeof hb, hi);
  const int bx0 = COL_L0 + p.width(F_B10, lb) + 8, bx1 = COL_L1 - p.width(F_B10, hb) - 8;
  const int by = RANGE_BAR_Y, bh = RANGE_BAR_H;
  p.g.drawRect(bx0, by, bx1 - bx0 + 1, bh, BLACK);
  int mx = bx0 + (int)lroundf((float)(cur - lo) * (bx1 - bx0) / (hi - lo));
  p.g.fillRect(bx0, by, mx - bx0 + 1, bh, BLACK);
  p.g.fillTriangle(mx - 4, by - 7, mx + 4, by - 7, mx, by - 2, BLACK);  // marker pointing down at "now"
  deg(d, sizeof d, cur);
  snprintf(nb, sizeof nb, "NOW %s", d);
  int nw = p.width(F_B8, nb), nx = mx - nw / 2;
  if (nx < COL_L0) nx = COL_L0;
  if (nx + nw > COL_L1) nx = COL_L1 - nw;
  p.text(F_B8, nx, RANGE_NOW, nb);
  p.text(F_B10, COL_L0, by + bh, lb);
  p.right(F_B10, COL_L1, by + bh, hb);
}

// ---- Left column
void drawNow(Painter& p, const WeatherB& w) {
  // Place name in the CJK font (ASCII letters are upper-cased to match the other titles).
  char place[32];
  snprintf(place, sizeof place, "%s", w.place);
  for (char* c = place; *c; c++)
    if (*c >= 'a' && *c <= 'z') *c -= 32;
  p.text(F_CJK, COL_L0, TITLE_Y + 1, place);
  int x = COL_L0 + p.width(F_CJK, place) + 6;
  if (w.status == WX_STALE) p.tag(x, TITLE_Y, "STALE");
  else if (w.status == WX_NO_WIFI) p.tag(x, TITLE_Y, "NO WIFI");

  char t[8];
  snprintf(t, sizeof t, "%d", w.temp);
  int base = BIG_TOP + (BIG_H + p.asc(F_BIG)) / 2;
  p.text(F_BIG, COL_L0, base, t);
  int tw = p.width(F_BIG, t);
  p.text(F_B12, COL_L0 + tw + 3, BIG_TOP + 13, DEG "C");
  icon(p.g, 156, 39, 50, w.code, w.is_day);

  int a = p.asc(F_B8);
  const char* cond = condText(w.code);
  int bw = p.width(F_B8, cond) + 6;
  p.g.fillRect(COL_L0, ROW3 - a - 2, bw, a + 4, BLACK);
  p.text(F_B8, COL_L0 + 3, ROW3, cond, WHITE);
  char fl[24], d[8];
  deg(d, sizeof d, w.apparent);
  snprintf(fl, sizeof fl, "FEELS %s", d);
  p.right(F_B8, COL_L1, ROW3, fl);

  p.text(F_B8, COL_L0, TODAY_TITLE, "TODAY");
  struct Row { const char* label; char val[24]; bool red; } rows[6];
  char hi[8], lo[8];
  deg(hi, sizeof hi, w.today_max);
  deg(lo, sizeof lo, w.today_min);
  rows[0] = {"HIGH / LOW", {}, false};
  snprintf(rows[0].val, 24, "%s / %s", hi, lo);
  rows[1] = {"RAIN CHANCE", {}, w.today_precip >= RAIN_ALERT_PCT};
  pct(rows[1].val, 24, w.today_precip);
  rows[2] = {"HUMIDITY", {}, false};
  pct(rows[2].val, 24, w.humidity);
  rows[3] = {"WIND", {}, false};
  snprintf(rows[3].val, 24, "%d km/h", w.wind);
  rows[4] = {"SUNRISE", {}, false};
  snprintf(rows[4].val, 24, "%s", w.sunrise);
  rows[5] = {"SUNSET", {}, false};
  snprintf(rows[5].val, 24, "%s", w.sunset);
  for (int i = 0; i < 6; i++) {
    int y = TODAY_FIRST + i * TODAY_PITCH;
    p.text(F_R8, COL_L0, y, rows[i].label);
    p.right(F_B10, COL_L1, y, rows[i].val, rows[i].red ? p.accent : BLACK);
  }

  drawRange(p, w);
  if (w.alert[0]) {
    // Quiet note on the range title row: small red dot + text, no box or warning sign.
    int tw = p.width(F_B8, w.alert);
    p.right(F_B8, COL_L1, RANGE_TITLE, w.alert, p.accent);
    p.g.fillCircle(COL_L1 - tw - 7, RANGE_TITLE - p.asc(F_B8) / 2, 2, p.accent);
  }
}

// ---- Right column
void drawNext12(Painter& p, const WeatherB& w) {
  p.text(F_B8, COL_R0, TITLE_Y, "NEXT 12H");
  p.right(F_R8, COL_R1, TITLE_Y, "TEMP " DEG "C / RAIN %");

  const int n = 12;
  const float x0 = COL_R0 + 8, x1 = COL_R1 - 8;
  auto X = [&](int i) { return (int)lroundf(x0 + (x1 - x0) * i / (n - 1)); };

  int mn = 127, mx = -128, imn = 0, imx = 0;
  for (int i = 0; i < n; i++) {
    if (w.hourly[i].temp < mn) { mn = w.hourly[i].temp; imn = i; }
    if (w.hourly[i].temp > mx) { mx = w.hourly[i].temp; imx = i; }
  }
  const int span = mx - mn < 3 ? 3 : mx - mn;  // do not stretch noise into a rollercoaster
  auto Y = [&](int v) { return (int)lroundf(T_BOT - (float)(v - mn) * (T_BOT - T_TOP) / span); };

  int px = X(0), py = Y(w.hourly[0].temp);
  for (int i = 1; i < n; i++) {
    int cx = X(i), cy = Y(w.hourly[i].temp);
    p.g.drawLine(px, py, cx, cy, BLACK);
    p.g.drawLine(px, py + 1, cx, cy + 1, BLACK);
    px = cx;
    py = cy;
  }
  for (int i = 0; i < n; i++) p.g.fillCircle(X(i), Y(w.hourly[i].temp), i == 0 ? 3 : 2, BLACK);
  char b[16];
  auto label = [&](int i, bool above) {
    deg(b, sizeof b, w.hourly[i].temp);
    int xm = X(i), tw = p.width(F_B8, b);
    if (xm - tw / 2 < COL_R0) xm = COL_R0 + tw / 2;
    if (xm + tw / 2 > COL_R1) xm = COL_R1 - tw / 2;
    p.center(F_B8, xm, above ? Y(w.hourly[i].temp) - 6 : Y(w.hourly[i].temp) + 14, b);
  };
  label(imx, true);
  if (imn != imx) label(imn, false);

  int peak = 0;
  for (int i = 0; i < n; i++)
    if (w.hourly[i].precip > peak) peak = w.hourly[i].precip;
  p.text(F_R8, COL_R0, CAP_Y, "RAIN CHANCE");
  char pk[16];
  snprintf(pk, sizeof pk, "MAX %d%%", peak);
  p.right(F_B8, COL_R1, CAP_Y, pk, peak >= RAIN_ALERT_PCT ? p.accent : BLACK);
  const bool dry = peak < RAIN_EMPTY_PCT;
  if (dry) {  // intentional empty state: dotted guide lines + caption instead of invisible bars
    for (int x = COL_R0; x < COL_R1; x += 4) {
      p.g.drawPixel(x, BAR_BASE - 4, BLACK);
      p.g.drawPixel(x, BAR_BASE - BAR_MAXH + 8, BLACK);
    }
    int cy = BAR_BASE - BAR_MAXH / 2 + 3, cw = p.width(F_B10, "NO RAIN") + 14;
    p.g.fillRect(COL_R0 + COL_W / 2 - cw / 2, cy - 12, cw, 17, WHITE);
    p.center(F_B10, COL_R0 + COL_W / 2, cy, "NO RAIN");
  }
  for (int i = 0; i < n && !dry; i++) {
    int v = w.hourly[i].precip;
    int h = (v * BAR_MAXH + 50) / 100;
    if (v > 0 && h < 2) h = 2;
    uint16_t c = v >= RAIN_ALERT_PCT ? p.accent : BLACK;
    if (h > 0) p.g.fillRect(X(i) - 4, BAR_BASE - h + 1, 9, h, c);
  }
  dither::hLine(p.g, COL_R0, BAR_BASE + 2, COL_W, BLACK);
  for (int i = 0; i < n; i += 3) {
    snprintf(b, sizeof b, "%02d", (w.hour0 + i) % 24);
    p.center(F_R8, X(i), HOUR_Y + 1, b);
  }
}

void drawDays(Painter& p, const WeatherB& w) {
  p.text(F_B8, COL_R0, DAYS_TITLE, "3 DAYS");
  for (int i = 0; i < 3; i++) {
    const Day& d = w.days[i];
    int cy = DAY_C0 + i * DAY_PITCH + 5;
    p.text(F_B12, COL_R0, cy + 5, d.weekday);
    icon(p.g, 266, cy, 30, d.code, true);
    char hi[8], lo[8];
    deg(hi, sizeof hi, d.max);
    deg(lo, sizeof lo, d.min);
    int tx = 292;
    p.text(F_B10, tx, cy + 4, hi);
    int hw = p.width(F_B10, hi);
    p.text(F_R10, tx + hw + 4, cy + 4, "/");
    p.text(F_R10, tx + hw + 14, cy + 4, lo);
    char r[8];
    pct(r, sizeof r, d.precip);
    uint16_t c = d.precip >= RAIN_ALERT_PCT ? p.accent : BLACK;
    p.right(F_B10, COL_R1, cy + 4, r, c);
    drop(p.g, COL_R1 - p.width(F_B10, r) - 9, cy - 1, c);
    if (i < 2) dither::hLine(p.g, COL_R0, DAY_C0 + i * DAY_PITCH + 25, COL_W, BLACK);
  }
}

void drawStatus(Painter& p, const WeatherB& w) {
  char s[48];
  if (w.status == WX_NO_LOCATION) snprintf(s, sizeof s, "OFFLINE \xC2\xB7 NO CITY SET");
  else snprintf(s, sizeof s, "OFFLINE \xC2\xB7 WEATHER %s", w.updated_at);
  p.text(F_B8, COL_L0, STATUS_BASE, s);
  connectionOffline(p, COL_L1 - 6, STATUS_BASE - 5);
}

void drawNoLocation(Painter& p) {
  p.text(F_B8, COL_L0, TITLE_Y, "WEATHER");
  dither::hLine(p.g, COL_L0, SEP_L2, COL_W, BLACK);
  int cx = WIDTH / 2, cy = 66;  // map pin
  p.g.fillCircle(cx, cy - 6, 15, BLACK);
  p.g.fillTriangle(cx - 13, cy + 2, cx + 13, cy + 2, cx, cy + 26, BLACK);
  p.g.fillCircle(cx, cy - 6, 6, WHITE);
  p.center(F_B12, cx, 148, "NO CITY SET");
  p.center(F_R10, cx, 173, "Set your city in the PC app");
  p.center(F_R10, cx, 190, "(Settings > Weather), then reconnect.");
  int fx = 50, fy = 124, fw = WIDTH - 100, fh = 78;  // dotted frame like the dashboard's NO DATA boxes
  for (int x = fx; x < fx + fw; x += 2) {
    p.g.drawPixel(x, fy, BLACK);
    p.g.drawPixel(x, fy + fh, BLACK);
  }
  for (int y = fy; y <= fy + fh; y += 2) {
    p.g.drawPixel(fx, y, BLACK);
    p.g.drawPixel(fx + fw - 1, y, BLACK);
  }
}

}  // namespace

void draw(Adafruit_GFX& g, U8G2_FOR_ADAFRUIT_GFX& u, const WeatherB& w, uint16_t accent) {
  Painter p{g, u, accent};
  if (w.status == WX_NO_LOCATION) {
    drawNoLocation(p);
    drawStatus(p, w);
    return;
  }
  dither::vLine(g, VSEP_X, 4, HEIGHT - 8, BLACK);
  dither::hLine(g, COL_L0, SEP_L1, COL_W, BLACK);
  dither::hLine(g, COL_L0, SEP_L2, COL_W, BLACK);
  dither::hLine(g, COL_R0, SEP_R1, COL_W, BLACK);

  drawNow(p, w);
  drawStatus(p, w);
  drawNext12(p, w);
  drawDays(p, w);
}

}  // namespace wxb
