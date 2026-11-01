#include "dashboard_v2.h"

#include <math.h>
#include <stdio.h>
#include <string.h>

#include "dither.h"

using namespace dash;

namespace dashv2 {
namespace {

constexpr uint16_t BLACK = 0x0000;
constexpr uint16_t WHITE = 0xFFFF;

// ---- Fonts: Helvetica for text and normal numbers, Logisoso for the big number
const uint8_t* const F_R8 = u8g2_font_helvR08_tf;
const uint8_t* const F_B8 = u8g2_font_helvB08_tf;
const uint8_t* const F_R10 = u8g2_font_helvR10_tf;
const uint8_t* const F_B10 = u8g2_font_helvB10_tf;
const uint8_t* const F_B12 = u8g2_font_helvB12_tf;
// Big number candidates, largest first; the first one that fits the column is used.
const uint8_t* const F_BIG[4] = {u8g2_font_logisoso28_tr, u8g2_font_logisoso24_tr, u8g2_font_logisoso20_tr,
                                 u8g2_font_helvB14_tf};

// ---- Layout (pixels, y = text baseline unless noted). No top bar.
constexpr int COL_L0 = 6, COL_L1 = 193;    // left column  (188 wide)
constexpr int COL_R0 = 206, COL_R1 = 393;  // right column (188 wide)
constexpr int COL_W = 188;
constexpr int VSEP_X = 199;

// Left: TOKENS -> QUOTA, time in the bottom-left corner
constexpr int TOK_ROW1 = 15, TOK_BAND_TOP = 20, TOK_BAND_H = 30, TOK_ROW3 = 65, SEP_L1 = 72;
// Bottom-left status row (below SEP_L2): time on the left, mouse battery + connection on the right.
constexpr int QUOTA_TITLE = 85, QUOTA_TOP = 90, QUOTA_BOTTOM = 278, SEP_L2 = 281, TIME_BASE = 297;

// Right: ACTIVITY (+ mouse + connection) -> TREND -> MODELS
constexpr int ACT_ROW = 15;
constexpr int HEAT_Y = 22, HEAT_H = 130, SEP_R1 = 155;
// Calendar view: weekday letters, then one row of 7 cells per week of the month (1 px white gap).
constexpr int CAL_CW = 26, CAL_CH = 23, CAL_X0 = COL_R0 + 3, CAL_HEAD = 30, CAL_Y0 = 34, CAL_Y1 = 152;
constexpr int TREND_TITLE = 167, CHART_X = COL_R0, CHART_W = COL_W, CHART_Y0 = 172, CHART_Y1 = 211, DATES_BASE = 224;
constexpr int SEP_R2 = 229;
// MODELS sits at the bottom (helvetica 8, 11 px rows) so the calendar and TREND get the height.
constexpr int MODELS_TITLE = 241, MODELS_FIRST = 253, MODELS_PITCH = 11;

const char* orDash(const char* s) { return (s && *s) ? s : "--"; }

// ---- Painter: text helpers bound to one canvas
struct Painter {
  Adafruit_GFX& g;
  U8G2_FOR_ADAFRUIT_GFX& u;
  Style style;

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
    u.setFontMode(1);  // transparent; u8g2 resets the mode on every setFont(), so set it here
    u.setForegroundColor(fg);
    u.drawUTF8(x, y, s);
  }
  void right(const uint8_t* f, int xr, int y, const char* s, uint16_t fg = BLACK) {
    text(f, xr - width(f, s), y, s, fg);
  }
  void center(const uint8_t* f, int xm, int y, const char* s, uint16_t fg = BLACK) {
    text(f, xm - width(f, s) / 2, y, s, fg);
  }

  // Small inverted label that ends at xRight; returns its left edge.
  int staleTag(int xRight, int base) {
    const char* s = "STALE";
    int a = asc(F_B8);
    int bw = width(F_B8, s) + 5;
    int bx = xRight - bw + 1;
    g.fillRect(bx, base - a - 1, bw, a + 3, BLACK);
    text(F_B8, bx + 2, base, s, WHITE);
    return bx;
  }

  // Block title: LABEL on the left; optional small note at the right edge;
  // STALE tag just left of the note (or at the right edge without a note).
  void title(int x, int xRight, int base, const char* label, const char* note, bool stale) {
    text(F_B8, x, base, label);
    int edge = xRight;
    if (note) {
      right(F_R8, xRight, base, note);
      edge = xRight - width(F_R8, note) - 5;
    }
    if (stale) staleTag(edge, base);
  }

  // Centered message in a frame, e.g. NO DATA.
  void placeholder(int x, int y, int w, int h, const char* msg, bool frame) {
    if (frame) g.drawRect(x, y, w, h, BLACK);
    center(F_B8, x + w / 2, y + h / 2 + asc(F_B8) / 2, msg);
  }
};

// Worst of two states for alert / stale decisions (na > stale > ok).
St worse(St a, St b) {
  auto rank = [](St s) { return s == ST_OK ? 0 : s == ST_STALE ? 1 : 2; };
  return rank(a) >= rank(b) ? a : b;
}

// ---------------------------------------------------------------------
// Top row of the right column: mouse battery + connection icon
// ---------------------------------------------------------------------

// Battery 20x10 outline + 2 px nub; fill follows pct (pct < 0: hollow).
void drawBattery(Painter& p, int x, int y, int pct, uint16_t ink) {
  p.g.drawRect(x, y, 20, 10, ink);
  p.g.fillRect(x + 20, y + 3, 2, 4, ink);
  if (pct <= 0) return;
  int fw = (16 * pct + 50) / 100;
  if (fw < 1) fw = 1;
  p.g.fillRect(x + 2, y + 2, fw, 6, ink);
}

// Mouse: battery icon + percent, ending at xRight. Low battery = accent icon;
// stale = percent in an inverted box; charging = CHG left of the icon;
// off / na = hollow battery and "--".
void drawMouseStatus(Painter& p, const Mouse& m, int xRight, int base) {
  bool off = m.got && m.st == ST_OFF;
  bool na = m.got && m.st == ST_NA;
  int pct = (m.got && !off && !na) ? m.pct : -1;
  bool alert = m.got && m.st == ST_OK && pct >= 0 && pct <= 15;
  bool stale = m.got && m.st == ST_STALE;

  char txt[8];
  if (pct >= 0) snprintf(txt, sizeof(txt), "%d%%", pct);
  else snprintf(txt, sizeof(txt), "--");
  int tw = p.width(F_B8, txt);
  int a = p.asc(F_B8);
  if (stale) {
    p.g.fillRect(xRight - tw - 2, base - a - 1, tw + 5, a + 3, BLACK);
    p.right(F_B8, xRight, base, txt, WHITE);
  } else {
    p.right(F_B8, xRight, base, txt);
  }
  int iconX = xRight - tw - 6 - 22;
  drawBattery(p, iconX, base - 9, pct, alert ? p.style.accent : BLACK);
  if (m.got && m.st == ST_OK && m.chg) p.right(F_B8, iconX - 5, base, "CHG");
}

// Connection icon centred on (cx, cy): online = solid dot in a ring; offline =
// accent cross (black on B/W) on white, 2 px strokes.
void drawConnection(Painter& p, bool online, int cx, int cy) {
  if (online) {
    p.g.fillCircle(cx, cy, 3, BLACK);
    p.g.drawCircle(cx, cy, 6, BLACK);
  } else {
    for (int d = 0; d < 2; d++) {  // two 1 px diagonals side by side = 2 px strokes
      p.g.drawLine(cx - 5 + d, cy - 5, cx + 4 + d, cy + 4, p.style.accent);
      p.g.drawLine(cx - 5 + d, cy + 4, cx + 4 + d, cy - 5, p.style.accent);
    }
  }
}

// ---------------------------------------------------------------------
// Left column
// ---------------------------------------------------------------------
void drawTokens(Painter& p, const Top& t) {
  bool have = t.got && t.st != ST_NA;
  // Row 1: TOKENS [STALE]                 cost
  p.text(F_B8, COL_L0, TOK_ROW1, "TOKENS");
  if (t.got && t.st == ST_STALE) p.staleTag(COL_L0 + p.width(F_B8, "TOKENS") + 5 + p.width(F_B8, "STALE") + 4, TOK_ROW1);
  p.right(F_B10, COL_L1, TOK_ROW1, have ? orDash(t.main.cost) : "--");

  // Row 2: the full number, in the largest font that fits the column.
  const char* tok = have ? orDash(t.main.tok) : "--";
  const uint8_t* font = F_BIG[3];
  for (const uint8_t* f : F_BIG)
    if (p.width(f, tok) <= COL_W) {
      font = f;
      break;
    }
  int base = TOK_BAND_TOP + (TOK_BAND_H + p.asc(font)) / 2;
  p.text(font, COL_L0, base, tok);

  // Row 3: [MAIN] badge on the left, "ALT 7.43B" on the right.
  if (have && t.main.label[0]) {
    int a = p.asc(F_B8);
    int bw = p.width(F_B8, t.main.label) + 6;
    p.g.fillRect(COL_L0, TOK_ROW3 - a - 2, bw, a + 4, BLACK);
    p.text(F_B8, COL_L0 + 3, TOK_ROW3, t.main.label, WHITE);
  }
  char alt[32];
  if (have && (t.alt.label[0] || t.alt.tok[0])) snprintf(alt, sizeof(alt), "%s %s", t.alt.label, t.alt.tok);
  else snprintf(alt, sizeof(alt), "--");
  p.right(F_B8, COL_L1, TOK_ROW3, alt);
}

// One quota bar: 1 px outline around the whole bar, remaining part solid, used
// part plain white (no dithering). On alert the whole bar (outline and
// remaining part) is accent. left < 0 -> outline only.
void drawBar(Painter& p, int x, int y, int w, int h, int left, bool alert) {
  // The outline is accent too on alert, so the bar is still visible at 0 %.
  p.g.drawRect(x, y, w, h, alert ? p.style.accent : BLACK);
  if (left < 0) return;
  int iw = w - 2, ih = h - 2;
  int rem = (iw * left + 50) / 100;  // rounded
  if (left > 0 && rem == 0) rem = 1;
  if (rem > 0) p.g.fillRect(x + 1, y + 1, rem, ih, alert ? p.style.accent : BLACK);
}

// Quota rows have fixed heights that fit the fonts; whatever does not fit in
// the block is dropped, never squeezed (a provider is shown only if its
// header row and at least its first item / text row fit).
void drawQuota(Painter& p, const Quota& q) {
  p.title(COL_L0, COL_L1, QUOTA_TITLE, "QUOTA", nullptr, q.got && q.st == ST_STALE);
  int areaH = QUOTA_BOTTOM - QUOTA_TOP;
  if (!q.got || q.st == ST_NA) {
    p.placeholder(COL_L0, QUOTA_TOP, COL_L1 - COL_L0 + 1, areaH, q.got ? "--" : "NO DATA", false);
    return;
  }
  const int HH = 14, IH = 12;  // header row (helvB10), item / text row (helvR08)
  const int BAR_H = 6;
  const int barX = COL_L0 + 28, barW = 66;
  const int pctR = barX + barW + 30, rstR = COL_L1;

  int y = QUOTA_TOP;
  for (int i = 0; i < q.n; i++) {
    const QProvider& pr = q.p[i];
    bool na = pr.st == ST_NA;
    int items = na ? 0 : pr.nItems;
    int segs = na ? 0 : pr.nSegs;
    // Header row needs room; a provider with body rows needs its first one too.
    if (y + HH + (items + segs > 0 ? IH : 0) > QUOTA_BOTTOM) break;
    St eff = worse(q.st, pr.st);

    // Header row: NAME plan [STALE]            balance
    int hb = y + HH - 2;
    p.text(F_B10, COL_L0, hb, orDash(pr.name));
    int x = COL_L0 + p.width(F_B10, orDash(pr.name)) + 5;
    if (pr.plan[0]) {
      p.text(F_R8, x, hb, pr.plan);
      x += p.width(F_R8, pr.plan) + 5;
    }
    if (pr.st == ST_STALE && q.st == ST_OK) p.staleTag(x + p.width(F_B8, "STALE") + 4, hb);
    if (pr.bal[0] && !na) p.right(F_B10, COL_L1, hb, pr.bal);
    else if (na || (items + segs == 0)) p.right(F_B10, COL_L1, hb, "--");
    y += HH;

    for (int r = 0; r < items; r++) {
      if (y + IH > QUOTA_BOTTOM) break;
      int b = y + IH - 2;
      const QItem& it = pr.items[r];
      p.text(F_R8, COL_L0, b, orDash(it.label));
      bool alert = it.left >= 0 && it.left <= 10 && eff == ST_OK;
      drawBar(p, barX, b - BAR_H + 1, barW, BAR_H, it.left, alert);
      char pct[8];
      if (it.left >= 0) snprintf(pct, sizeof(pct), "%d%%", it.left);
      else snprintf(pct, sizeof(pct), "--");
      p.right(F_B8, pctR, b, pct, alert ? p.style.accent : BLACK);  // alert: red percentage (black on B/W)
      if (it.reset[0]) p.right(F_R8, rstR, b, it.reset);
      y += IH;
    }

    // Text line: the first segment is left aligned, the last one is right aligned
    // to the column edge (a single segment stays left); a warn segment is inverted.
    if (segs > 0 && y + IH <= QUOTA_BOTTOM) {
      int b = y + IH - 2, a = p.asc(F_R8);
      // Warn segment: red text on a three-colour panel; on B/W (no red) inverted instead.
      const bool invert = !p.style.tri;
      auto drawSeg = [&](const QSeg& sg, int sx) {
        int w = p.width(F_R8, sg.s), pad = (sg.warn && invert) ? 3 : 0;
        if (sg.warn && invert) {
          p.g.fillRect(sx, b - a - 1, w + 2 * pad, a + 3, BLACK);
          p.text(F_R8, sx + pad, b, sg.s, WHITE);
        } else {
          p.text(F_R8, sx, b, sg.s, sg.warn ? p.style.accent : BLACK);
        }
        return sx + w + 2 * pad;
      };
      auto segW = [&](const QSeg& sg) { return p.width(F_R8, sg.s) + ((sg.warn && invert) ? 6 : 0); };
      int leftEnd = drawSeg(pr.segs[0], COL_L0);
      if (segs >= 2) {
        const QSeg& last = pr.segs[segs - 1];
        int lx = COL_L1 + 1 - segW(last);
        for (int k = 1; k + 1 < segs; k++) {  // middle segments (rare): after the first, if they fit
          int w = segW(pr.segs[k]);
          if (leftEnd + 8 + w + 8 > lx) break;
          leftEnd = drawSeg(pr.segs[k], leftEnd + 8);
        }
        if (lx >= leftEnd + 6) drawSeg(last, lx);
      }
      y += IH;
    }
    y += 4;  // gap between providers
  }
}

// ---------------------------------------------------------------------
// Right column
// ---------------------------------------------------------------------

// Heat calendar: 7 rows (Sunday..Saturday, no labels), one column per week, the
// oldest week on the left, today in the last column; the last 10 weeks are
// shown. 18 px square cells without outline or gap (180 x 126 in total). Each
// day is filled by heatInk (white -> red -> red + black); unknown and 0 stay
// white; today is marked by a small triangle to the
// right of the grid, pointing at its row.
void drawHeatGrid(Painter& p, const Heat& h) {
  const int firstRow = h.firstRow();
  const int x0 = COL_R0 + 1, y0 = HEAT_Y + 2;
  for (int i = 0; i < h.n; i++) {
    int week = h.row(i) - firstRow;
    if (week < 0) continue;  // older than the shown weeks
    int x = x0 + week * HEAT_CELL, y = y0 + h.col(i) * HEAT_CELL;
    uint8_t v = h.days[i];
    if (v != UNKNOWN && v > 0)  // unknown (no record yet) and 0 tokens both stay white
      for (int j = y; j < y + HEAT_CELL; j++)
        for (int k = x; k < x + HEAT_CELL; k++) {
          uint8_t c = heatInk(v, k, j);
          if (c) p.g.drawPixel(k, j, c == 1 ? p.style.accent : BLACK);
        }
    if (i == h.n - 1) {  // today: small black triangle right of the grid, pointing left
      int tx = x0 + HEAT_CAL_WEEKS * HEAT_CELL + 1, ty = y + HEAT_CELL / 2;
      p.g.fillTriangle(tx, ty, tx + 3, ty - 3, tx + 3, ty + 3, BLACK);
    }
  }
}

// "MM-DD HH:MM" -> the date part ("MM-DD") or the time part ("HH:MM"); no space: all of it is the time.
void splitTime(const char* s, char* date, char* time, size_t cap) {
  const char* sp = strchr(s, ' ');
  if (!sp) {
    date[0] = 0;
    snprintf(time, cap, "%s", s);
    return;
  }
  snprintf(date, cap, "%.*s", (int)(sp - s), s);
  snprintf(time, cap, "%s", sp + 1);
}

// ACTIVITY / CALENDAR [STALE] on the left; at the right edge, in the title's font,
// today's date (MM-DD), in both the heat and the calendar style.
void drawActivityRow(Painter& p, const Heat& h, const Top& t) {
  const char* label = h.calendar ? "CALENDAR" : "ACTIVITY";
  p.text(F_B8, COL_R0, ACT_ROW, label);
  if (h.got && h.st == ST_STALE)
    p.staleTag(COL_R0 + p.width(F_B8, label) + 5 + p.width(F_B8, "STALE") + 4, ACT_ROW);
  char date[12], time[12];
  splitTime((t.got && t.st != ST_NA) ? t.time : "", date, time, sizeof(date));
  if (date[0]) p.right(F_B8, COL_R1, ACT_ROW, date);
}

// Bottom-left corner, below SEP_L2: time (HH:MM) on the left; mouse battery and
// the connection icon at the right edge of the left column.
void drawStatusRow(Painter& p, const Top& t, const Mouse& m, bool online) {
  char date[12], time[12];
  splitTime((t.got && t.st != ST_NA) ? t.time : "", date, time, sizeof(time));
  p.text(F_B12, COL_L0, TIME_BASE, orDash(time));
  const int cx = COL_L1 - 6, cy = TIME_BASE - 5;
  drawConnection(p, online, cx, cy);
  drawMouseStatus(p, m, cx - 12, TIME_BASE - 1);
}

// Calendar view: the month of today (the last heat day) as a wall calendar,
// weekday letters on top (Sunday first, in the accent colour), one row per
// week, 4..6 rows; the rows share the block height (at most CAL_CH each).
// Cells use the heat grid's ramp (heatInk). Every day of the month gets its
// number (white halo, so it stays crisp on a dithered cell), Sundays in the
// accent colour, the rest black. The days of the previous month in the first
// row get their heat background but no number; days after today stay white.
// No mark for today (the date is in the title).
void drawCalendar(Painter& p, const Heat& h) {
  static const char* const kWd[7] = {"S", "M", "T", "W", "T", "F", "S"};
  for (int c = 0; c < 7; c++)
    p.center(F_R8, CAL_X0 + c * CAL_CW + CAL_CW / 2, CAL_HEAD, kWd[c], c == 0 ? p.style.accent : BLACK);
  const Ymd today = addDays(h.start, h.n - 1);
  if (!today.valid()) return;
  const Ymd first = addDays(today, 1 - today.d);
  int days = 28;
  while (days < 31 && addDays(first, days).d != 1) days++;
  const int lead = weekdaySun0(first), rows = (lead + days + 6) / 7;
  const int ch = (CAL_Y1 - CAL_Y0) / rows < CAL_CH ? (CAL_Y1 - CAL_Y0) / rows : CAL_CH;
  const int firstIdx = h.n - today.d;  // day index of the 1st of this month (may be < 0)
  for (int slot = 0; slot < lead + days; slot++) {
    int dm = slot - lead + 1, i = firstIdx + dm - 1;  // dm <= 0: previous month
    int x = CAL_X0 + (slot % 7) * CAL_CW, y = CAL_Y0 + (slot / 7) * ch, w = CAL_CW - 1, hh = ch - 1;
    uint8_t v = (i >= 0 && i < h.n) ? h.days[i] : UNKNOWN;
    if (v != UNKNOWN && v > 0)
      for (int j = y; j < y + hh; j++)
        for (int k = x; k < x + w; k++) {
          uint8_t c = heatInk(v, k, j);
          if (c) p.g.drawPixel(k, j, c == 1 ? p.style.accent : BLACK);
        }
    if (dm <= 0) continue;
    char num[4];
    snprintf(num, sizeof(num), "%d", dm);
    int nx = x + w / 2 - p.width(F_B8, num) / 2, ny = y + (hh + p.asc(F_B8)) / 2;
    for (int dy = -1; dy <= 1; dy++)
      for (int dx = -1; dx <= 1; dx++)
        if (dx || dy) p.text(F_B8, nx + dx, ny + dy, num, WHITE);
    p.text(F_B8, nx, ny, num, slot % 7 == 0 ? p.style.accent : BLACK);
  }
}

void drawHeat(Painter& p, const Heat& h) {
  bool live = h.got && h.st != ST_NA;
  if (!live || !h.hasDays || h.n == 0) {
    p.placeholder(COL_R0, HEAT_Y, COL_W, HEAT_H - 4, h.got && h.st == ST_NA ? "--" : "NO DATA", true);
    return;
  }
  if (h.calendar) drawCalendar(p, h);
  else drawHeatGrid(p, h);
}

// Trend: the days the calendar shows (unknown = 0) on a split time axis (the
// last 15 days take the right half, the earlier days the left half, see
// trendX), a Catmull-Rom curve through each day's real value, one knot per day:
// the slope at a day is the chord between its two neighbours, so the curve
// keeps flowing through a day instead of flattening at every one (a monotone
// spline levels off at each up/down day, which looks wavy on daily data). The
// small overshoot this allows is clipped to the chart (0 .. peak). The curve
// is sampled densely and stroked with a round brush of TREND_STROKE px, so a
// steep part is as thick as a flat one. Only the curve in the accent colour
// (black on B/W): no fill, no axes. Static buffers: the stack of the loop task
// is shared with the serial parser.
#ifndef TREND_STROKE
#define TREND_STROKE 1
#endif
float g_xs[HEAT_MAX], g_ys[HEAT_MAX], g_ms[HEAT_MAX];

// Catmull-Rom tangents for knots with uneven x: the slope of the chord through
// both neighbours; the two ends use their single segment.
void curveTangents(const float* x, const float* y, float* m, int n) {
  if (n < 2) {
    m[0] = 0;
    return;
  }
  m[0] = (y[1] - y[0]) / (x[1] - x[0]);
  m[n - 1] = (y[n - 1] - y[n - 2]) / (x[n - 1] - x[n - 2]);
  for (int k = 1; k + 1 < n; k++) m[k] = (y[k + 1] - y[k - 1]) / (x[k + 1] - x[k - 1]);
}

float hermite(const float* x, const float* y, const float* m, int k, float xv) {
  float h = x[k + 1] - x[k];
  float t = (xv - x[k]) / h, t2 = t * t, t3 = t2 * t;
  return (2 * t3 - 3 * t2 + 1) * y[k] + (t3 - 2 * t2 + t) * h * m[k] + (-2 * t3 + 3 * t2) * y[k + 1] +
         (t3 - t2) * h * m[k + 1];
}

// Round brush of TREND_STROKE px centred on (x, y): 1 = one pixel, 2 = 2 x 2,
// 3 = a plus (3 x 3 without the corners).
void stamp(Adafruit_GFX& g, int x, int y, uint16_t c) {
#if TREND_STROKE <= 1
  g.drawPixel(x, y, c);
#elif TREND_STROKE == 2
  g.fillRect(x, y, 2, 2, c);
#else
  g.drawFastHLine(x - 1, y, 3, c);
  g.drawFastVLine(x, y - 1, 3, c);
#endif
}

// Brush along the pixel line from (x0, y0) to (x1, y1) (Bresenham), the first point excluded.
void strokeTo(Adafruit_GFX& g, int x0, int y0, int x1, int y1, uint16_t c) {
  int dx = abs(x1 - x0), sx = x0 < x1 ? 1 : -1, dy = -abs(y1 - y0), sy = y0 < y1 ? 1 : -1, err = dx + dy;
  while (x0 != x1 || y0 != y1) {
    int e2 = 2 * err;
    if (e2 >= dy) {
      err += dy;
      x0 += sx;
    }
    if (e2 <= dx) {
      err += dx;
      y0 += sy;
    }
    stamp(g, x0, y0, c);
  }
}

void drawTrend(Painter& p, const Heat& h) {
  bool stale = h.got && h.st == ST_STALE;
  bool live = h.got && h.st != ST_NA;
  char note[20];
  const char* noteP = nullptr;
  if (live) {
    snprintf(note, sizeof(note), "peak %s", orDash(h.peak));
    noteP = note;
  }
  p.title(COL_R0, COL_R1, TREND_TITLE, "TREND", noteP, stale);

  const int n = h.n;
  int known = 0;
  if (live && h.hasTrend)
    for (int i = 0; i < n; i++)
      if (h.trend[i] != UNKNOWN) known++;
  const int ch = CHART_Y1 - CHART_Y0 + 1;
  if (known == 0) {
    p.placeholder(CHART_X, CHART_Y0, CHART_W, ch, h.got && h.st == ST_NA ? "--" : "NO DATA", true);
    return;
  }

  const uint16_t curveColor = p.style.tri ? p.style.accent : BLACK;
  // Value 0 sits 3 px above the bottom, 63 (= peak) 2 px below the top, so the brush stays inside.
  const float yTop = (float)(CHART_Y0 + 2), yBottom = (float)(CHART_Y1 - 3);
  auto py = [&](float v) { return yBottom - v * (yBottom - yTop) / 63.0f; };

  const int from = h.firstDay();
  const int m = trendValues(h.trend, from, n, g_ys);  // one value per shown day
  for (int k = 0; k < m; k++) {
    g_xs[k] = (float)CHART_X + trendX(k, m, CHART_W);
    g_ys[k] = py(g_ys[k]);
  }
  if (m >= 2) {
    curveTangents(g_xs, g_ys, g_ms, m);
    // Sample each segment at about 4 points per pixel of its length, round, and stroke from point to point.
    int px = (int)lroundf(g_xs[0]), pyi = (int)lroundf(g_ys[0]);
    stamp(p.g, px, pyi, curveColor);
    for (int k = 0; k + 1 < m; k++) {
      float dx = g_xs[k + 1] - g_xs[k], dy = fabsf(g_ys[k + 1] - g_ys[k]);
      int steps = 1 + (int)(4.0f * (dx > dy ? dx : dy));
      for (int i = 1; i <= steps; i++) {
        float xv = g_xs[k] + dx * (float)i / (float)steps;
        float yv = i == steps ? g_ys[k + 1] : hermite(g_xs, g_ys, g_ms, k, xv);
        if (yv < yTop) yv = yTop;  // overshoot past the peak or below 0 is clipped
        if (yv > yBottom) yv = yBottom;
        int nx = (int)lroundf(xv), ny = (int)lroundf(yv);
        if (nx != px || ny != pyi) strokeTo(p.g, px, pyi, nx, ny, curveColor);
        px = nx;
        pyi = ny;
      }
    }
  } else if (m == 1) {
    p.g.fillRect((int)(g_xs[0] + 0.5f) - 1, (int)(g_ys[0] + 0.5f) - 1, 3, 3, curveColor);
  }

  // Dates under the chart: first shown day, the middle (with a split axis: the
  // day where the right half starts, 15 days before today), last day.
  if (h.start.valid() && m > 0) {
    const int split = trendSplit(m);
    Ymd first = addDays(h.start, from), mid = addDays(h.start, from + (split ? split : (m - 1) / 2)),
        last = addDays(h.start, n - 1);
    char a[8], b[8], c[8];
    snprintf(a, sizeof(a), "%02d-%02d", first.m, first.d);
    snprintf(b, sizeof(b), "%02d-%02d", mid.m, mid.d);
    snprintf(c, sizeof(c), "%02d-%02d", last.m, last.d);
    p.text(F_R8, COL_R0, DATES_BASE, a);
    if (m > 2) p.center(F_R8, CHART_X + CHART_W / 2, DATES_BASE, b);
    p.right(F_R8, COL_R1, DATES_BASE, c);
  }
}

void drawModels(Painter& p, const Models& m) {
  p.title(COL_R0, COL_R1, MODELS_TITLE, "MODELS", nullptr, m.got && m.st == ST_STALE);  // no scope (TODAY / TOTAL) in the title
  if (!m.got || m.st == ST_NA) {
    p.placeholder(COL_R0, MODELS_TITLE + 6, COL_R1 - COL_R0 + 1, 50, m.got ? "--" : "NO DATA", false);
    return;
  }
  // Token column ends where the widest share ("100%") begins, plus a gap, so
  // a single model at 100% does not run into its token count.
  const int tokR = COL_R1 - p.width(F_R8, "100%") - 5;
  for (int i = 0; i < m.n; i++) {
    const ModelRow& r = m.m[i];
    int b = MODELS_FIRST + i * MODELS_PITCH;
    p.text(F_R8, COL_R0, b, orDash(r.name));
    p.right(F_B8, tokR, b, orDash(r.tok));
    char pct[8];
    if (r.pct >= 0) snprintf(pct, sizeof(pct), "%d%%", r.pct);
    else snprintf(pct, sizeof(pct), "--");
    p.right(F_R8, COL_R1, b, pct);
  }
}

}  // namespace

void draw(Adafruit_GFX& g, U8G2_FOR_ADAFRUIT_GFX& u, const Dash& d, bool online, const Style& style) {
  Painter p{g, u, style};

  // Separators between blocks (50 % dither, never under text).
  dither::vLine(g, VSEP_X, 4, HEIGHT - 8, BLACK);
  dither::hLine(g, COL_L0, SEP_L1, COL_W, BLACK);
  dither::hLine(g, COL_L0, SEP_L2, COL_W, BLACK);
  dither::hLine(g, COL_R0, SEP_R1, COL_W, BLACK);
  dither::hLine(g, COL_R0, SEP_R2, COL_W, BLACK);

  drawTokens(p, d.top);
  drawQuota(p, d.quota);
  drawStatusRow(p, d.top, d.mouse, online);

  drawActivityRow(p, d.heat, d.top);
  drawHeat(p, d.heat);
  drawTrend(p, d.heat);
  drawModels(p, d.models);
}

}  // namespace dashv2
