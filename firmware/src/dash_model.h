#pragma once
// Dashboard v2 data model (protocol: docs/dashboard-v2-protocol.md).
// Pure C++ (no Arduino, no ArduinoJson) so it is compiled by the host tests
// and the offline preview too. Everything is fixed-size and compact: the
// whole model is about 1.2 KB and is copied on the stack by the refresh
// scheduler, so no String / heap here.
//
// Each block keeps its own `got` (was it ever received) and `st` flag; the
// latest message of a block replaces the previous one. A block whose st is
// "na" is stored empty, so stale values never reach the signature or the UI.

#include <stdint.h>
#include <string.h>

#include <initializer_list>

namespace dash {

// Heat / trend: the host sends whole weeks (10 weeks = 64..70 days by default,
// the first day is a Sunday); the firmware lays out by the string length it
// gets and keeps at most the last HEAT_MAX days (16 weeks, tolerance for older
// hosts). The calendar shows the last HEAT_CAL_WEEKS weeks, 7 days each.
constexpr int HEAT_MAX = 112;
constexpr int HEAT_CAL_COLS = 7;    // days per week
constexpr int HEAT_CAL_WEEKS = 10;
constexpr int HEAT_CELL = 18;       // calendar cell edge, no gap between cells
constexpr int HEAT_LEVELS = 12;     // heat density 1..12 (0 = no use)
constexpr uint8_t UNKNOWN = 0xFF;  // heat / trend cell without data

constexpr int MAX_PROVIDERS = 4;
constexpr int MAX_ITEMS = 3;  // the host sends at most 2; 3 is tolerated
constexpr int MAX_SEGS = 3;   // quota text segments per provider
constexpr int MAX_MODELS = 5;
constexpr int MODEL_NAME_CHARS = 18;  // longer names are cut to 17 chars + '~'

enum St : uint8_t { ST_OK = 0, ST_STALE, ST_NA, ST_OFF };

// ---------------------------------------------------------------------
// String helpers
// ---------------------------------------------------------------------
// Copy `src` into dst[cap], dropping control characters and surrounding
// spaces, cutting at a UTF-8 character boundary when it does not fit.
inline void setStr(char* dst, size_t cap, const char* src) {
  if (cap == 0) return;
  size_t n = 0;
  if (src) {
    while (*src == ' ') src++;
    for (; *src && n + 1 < cap; src++) {
      unsigned char c = (unsigned char)*src;
      if (c < 0x20 || c == 0x7F) continue;
      dst[n++] = (char)c;
    }
    // Stopped because the buffer is full: if the next byte continues a
    // UTF-8 character (10xxxxxx), drop that character's start as well.
    if (*src && ((unsigned char)*src & 0xC0) == 0x80) {
      while (n > 0 && ((unsigned char)dst[n - 1] & 0xC0) == 0x80) n--;
      if (n > 0) n--;  // the lead byte
    }
    while (n > 0 && dst[n - 1] == ' ') n--;
  }
  dst[n] = '\0';
}

// Number of UTF-8 characters.
inline size_t utf8Len(const char* s) {
  size_t n = 0;
  for (; *s; s++)
    if (((unsigned char)*s & 0xC0) != 0x80) n++;
  return n;
}

// Model name rule: more than MODEL_NAME_CHARS characters -> first
// MODEL_NAME_CHARS-1 characters + '~'.
inline void setModelName(char* dst, size_t cap, const char* src) {
  char tmp[64];
  setStr(tmp, sizeof(tmp), src);
  if (utf8Len(tmp) > (size_t)MODEL_NAME_CHARS) {
    size_t chars = 0, i = 0;
    while (tmp[i]) {
      if (((unsigned char)tmp[i] & 0xC0) != 0x80) {
        if (chars == (size_t)MODEL_NAME_CHARS - 1) break;
        chars++;
      }
      i++;
    }
    tmp[i] = '~';
    tmp[i + 1] = '\0';
    // Multi-byte names may not fit `cap` bytes: drop characters before the '~'.
    while (i > 0 && i + 2 > cap) {
      size_t j = i - 1;
      while (j > 0 && ((unsigned char)tmp[j] & 0xC0) == 0x80) j--;
      tmp[j] = '~';
      tmp[j + 1] = '\0';
      i = j;
    }
  }
  setStr(dst, cap, tmp);
}

// ---------------------------------------------------------------------
// Heat / trend decoding
// ---------------------------------------------------------------------
// Heat digit '0'..'9' -> 0..9, 'A'..'C' -> 10..12, '-' -> UNKNOWN. Anything else -> 0xFE.
constexpr uint8_t BAD_CHAR = 0xFE;
inline uint8_t decodeHeat(char c) {
  if (c >= '0' && c <= '9') return (uint8_t)(c - '0');
  if (c >= 'A' && c < 'A' + HEAT_LEVELS - 9) return (uint8_t)(c - 'A' + 10);
  if (c == '-') return UNKNOWN;
  return BAD_CHAR;
}

// Trend char in the Base64 alphabet (A-Z a-z 0-9 + /) -> 0..63, '-' -> UNKNOWN.
inline uint8_t decodeTrend(char c) {
  if (c >= 'A' && c <= 'Z') return (uint8_t)(c - 'A');
  if (c >= 'a' && c <= 'z') return (uint8_t)(26 + c - 'a');
  if (c >= '0' && c <= '9') return (uint8_t)(52 + c - '0');
  if (c == '+') return 62;
  if (c == '/') return 63;
  if (c == '-') return UNKNOWN;
  return BAD_CHAR;
}

// ---------------------------------------------------------------------
// Dates (heat start / end, month labels)
// ---------------------------------------------------------------------
struct Ymd {
  uint16_t y = 0;  // 0 = unknown
  uint8_t m = 0, d = 0;
  bool valid() const { return y != 0; }
};

// Days since 1970-01-01 (proleptic Gregorian; Howard Hinnant's algorithm).
inline int32_t daysFromCivil(int y, int m, int d) {
  y -= m <= 2;
  int32_t era = (y >= 0 ? y : y - 399) / 400;
  int yoe = y - era * 400;
  int doy = (153 * (m + (m > 2 ? -3 : 9)) + 2) / 5 + d - 1;
  int doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
  return era * 146097 + doe - 719468;
}

inline Ymd civilFromDays(int32_t z) {
  z += 719468;
  int32_t era = (z >= 0 ? z : z - 146096) / 146097;
  int doe = (int)(z - era * 146097);
  int yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
  int y = yoe + (int)era * 400;
  int doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
  int mp = (5 * doy + 2) / 153;
  int d = doy - (153 * mp + 2) / 5 + 1;
  int m = mp + (mp < 10 ? 3 : -9);
  Ymd r;
  r.y = (uint16_t)(y + (m <= 2));
  r.m = (uint8_t)m;
  r.d = (uint8_t)d;
  return r;
}

inline Ymd addDays(const Ymd& a, int n) {
  if (!a.valid()) return a;
  return civilFromDays(daysFromCivil(a.y, a.m, a.d) + n);
}

// "YYYY-MM-DD" -> Ymd; anything else (or an impossible date) -> invalid.
// Weekday, Monday = 0 .. Sunday = 6 (1970-01-01 was a Thursday).
inline int weekdayMon0(const Ymd& a) {
  if (!a.valid()) return 0;
  int w = (int)((daysFromCivil(a.y, a.m, a.d) + 3) % 7);
  return w < 0 ? w + 7 : w;
}
// Weekday, Sunday = 0 .. Saturday = 6: the calendar's row order (the week starts on Sunday).
inline int weekdaySun0(const Ymd& a) { return a.valid() ? (weekdayMon0(a) + 1) % 7 : 0; }

inline Ymd parseYmd(const char* s) {
  Ymd r;
  if (!s || strlen(s) != 10 || s[4] != '-' || s[7] != '-') return r;
  for (int i : {0, 1, 2, 3, 5, 6, 8, 9})
    if (s[i] < '0' || s[i] > '9') return r;
  int y = (s[0] - '0') * 1000 + (s[1] - '0') * 100 + (s[2] - '0') * 10 + (s[3] - '0');
  int m = (s[5] - '0') * 10 + (s[6] - '0');
  int d = (s[8] - '0') * 10 + (s[9] - '0');
  if (y < 2000 || m < 1 || m > 12 || d < 1 || d > 31) return r;
  Ymd t = civilFromDays(daysFromCivil(y, m, d));
  if (t.m != m || t.d != d) return r;  // e.g. 02-31
  return t;
}

// ---------------------------------------------------------------------
// Blocks
// ---------------------------------------------------------------------
struct Period {  // main (big number) or alt (small line)
  char label[8] = "";  // TODAY / TOTAL
  char tok[20] = "";   // main: full number with thousands separators; alt: abbreviated
  char cost[16] = "";  // main only
};

struct Top {  // "dash": TOKENS block + corner time
  bool got = false;
  St st = ST_OK;
  char time[16] = "";  // bottom-left corner
  Period main, alt;
  uint16_t everyS = 0;  // `every`: refresh the screen at most this often (s); 0 = panel minimum; not drawn
};

struct QItem {
  char label[6] = "";  // <= 4 characters
  int8_t left = -1;    // 0..100, -1 = unknown
  char reset[10] = "";
};

struct QSeg {  // one text segment of the `txt` line
  char s[16] = "";
  bool warn = false;  // drawn inverted
};

struct QProvider {
  char name[14] = "";
  char plan[10] = "";
  char bal[12] = "";  // balance-type provider: shows "Bal <bal>" instead of items
  St st = ST_OK;      // as sent; the UI combines it with the block st
  uint8_t nItems = 0;
  QItem items[MAX_ITEMS];
  uint8_t nSegs = 0;
  QSeg segs[MAX_SEGS];
};

struct Quota {
  bool got = false;
  St st = ST_OK;
  uint8_t n = 0;
  QProvider p[MAX_PROVIDERS];
};

struct ModelRow {
  char name[24] = "";
  char tok[10] = "";
  int8_t pct = -1;
};

struct Models {
  bool got = false;
  St st = ST_OK;
  char scope[8] = "";  // TODAY / TOTAL, shown in the title
  uint8_t n = 0;
  ModelRow m[MAX_MODELS];
};

struct Heat {
  bool got = false;
  St st = ST_OK;
  bool hasDays = false;   // `d` received
  bool hasTrend = false;  // `t` received
  uint8_t n = 0;          // number of days (1..HEAT_MAX); the last one is today
  Ymd start;              // date of day 0
  char peak[12] = "";
  int16_t active = -1;
  bool calendar = false;  // `view`:"cal": the ACTIVITY block is drawn as this month's calendar
  uint8_t days[HEAT_MAX];   // 0..HEAT_LEVELS or UNKNOWN
  uint8_t trend[HEAT_MAX];  // 0..63 or UNKNOWN
  Heat() {
    memset(days, UNKNOWN, sizeof(days));
    memset(trend, UNKNOWN, sizeof(trend));
  }
  // Calendar slots: col = weekday (0 = Sunday), row = week (Sunday..Saturday,
  // oldest first, today in the last week). Day 0 sits at its weekday (start is
  // a Sunday on the default host). On screen a week is a column and a weekday is a row.
  int slot(int i) const { return weekdaySun0(start) + i; }
  int col(int i) const { return slot(i) % HEAT_CAL_COLS; }
  int row(int i) const { return slot(i) / HEAT_CAL_COLS; }
  int totalRows() const { return n == 0 ? 0 : row(n - 1) + 1; }
  // Only the last HEAT_CAL_WEEKS weeks are shown.
  int firstRow() const { return totalRows() > HEAT_CAL_WEEKS ? totalRows() - HEAT_CAL_WEEKS : 0; }
  int shownRows() const { return totalRows() - firstRow(); }
  // First day (index) that is on screen; TREND uses the same days.
  int firstDay() const {
    int d = firstRow() * HEAT_CAL_COLS - weekdaySun0(start);
    return d < 0 ? 0 : d;
  }
};

// Heat cell colour at absolute (x, y) for level 1..HEAT_LEVELS, 4x4 regular
// pattern indexed by screen coordinates (equal neighbours join without a seam):
// 1..8 add 2/16 red dots on white per level (8 = solid red), 9..12 add 2/16
// black dots on red per level (12 = half black). 0 = white, 1 = red, 2 = black.
inline uint8_t heatInk(int level, int x, int y) {
  static const uint8_t kBayer[4][4] = {{0, 8, 2, 10}, {12, 4, 14, 6}, {3, 11, 1, 9}, {15, 7, 13, 5}};
  int t = kBayer[y & 3][x & 3];
  if (level <= 8) return t < level * 2 ? 1 : 0;
  return t < (level - 8) * 2 ? 2 : 1;
}

// Trend values: one per day the calendar shows (days from..n-1), unknown days
// count as 0, no averaging (the curve goes through each day's real value, so
// the peak day reaches the top and today shows what today used). out[] gets
// n - from values (0..63); returns the count.
inline int trendValues(const uint8_t* trend, int from, int n, float* out) {
  if (from < 0) from = 0;
  int cnt = n - from;
  if (cnt <= 0) return 0;
  for (int k = 0; k < cnt; k++) out[k] = trend[from + k] == UNKNOWN ? 0.0f : (float)trend[from + k];
  return cnt;
}

// Split time axis of the trend: the last TREND_RECENT days (day steps) take the
// right half, i.e. value trendSplit(cnt) sits at width / 2 and today on the
// right edge, every step there equally wide; the earlier values are squeezed
// into the left half (0 .. width / 2, linear). The step into the right half is
// a wide one too, so a jump there is not drawn as a vertical line. With
// cnt <= TREND_RECENT + 1 values the axis is plain linear over the full width;
// a single value sits in the middle. Returns the x offset of value k
// (0..cnt-1), increasing with k.
constexpr int TREND_RECENT = 15;
inline int trendSplit(int cnt) { return cnt > TREND_RECENT + 1 ? cnt - 1 - TREND_RECENT : 0; }  // value at the middle
inline float trendX(int k, int cnt, int width) {
  if (cnt <= 1) return (float)(width - 1) / 2.0f;
  const int split = trendSplit(cnt);
  if (split == 0) return (float)k * (float)(width - 1) / (float)(cnt - 1);
  const float half = (float)(width / 2);
  if (k <= split) return (float)k * half / (float)split;
  return half + (float)(k - split) * ((float)(width - 1) - half) / (float)TREND_RECENT;
}

struct Mouse {
  bool got = false;
  St st = ST_OK;  // ok / stale / na / off
  int8_t pct = -1;
  bool chg = false;
  char at[10] = "";
};

struct Dash {
  Top top;
  Quota quota;
  Models models;
  Heat heat;
  Mouse mouse;

  // 64-bit FNV-1a over everything that is drawn; equal signature = identical
  // screen. Used for "refresh only when the content changed".
  uint64_t signature() const;
};

// ---------------------------------------------------------------------
// Signature
// ---------------------------------------------------------------------
struct Fnv {
  uint64_t h = 1469598103934665603ULL;
  void byte(uint8_t b) {
    h ^= b;
    h *= 1099511628211ULL;
  }
  void num(int32_t v) {
    for (int i = 0; i < 4; i++) byte((uint8_t)((uint32_t)v >> (8 * i)));
  }
  void str(const char* s) {
    for (; *s; s++) byte((uint8_t)*s);
    byte(0xFF);  // terminator, so "ab"+"c" != "a"+"bc"
  }
  void bytes(const uint8_t* p, size_t n) {
    for (size_t i = 0; i < n; i++) byte(p[i]);
  }
  void blk(bool got, St st) {
    byte(got ? 1 : 0);
    byte((uint8_t)st);
  }
};

inline uint64_t Dash::signature() const {
  Fnv f;
  f.blk(top.got, top.st);
  f.str(top.time);
  for (const Period* pd : {&top.main, &top.alt}) {
    f.str(pd->label); f.str(pd->tok); f.str(pd->cost);
  }

  f.blk(quota.got, quota.st);
  f.byte(quota.n);
  for (int i = 0; i < quota.n; i++) {
    const QProvider& p = quota.p[i];
    f.str(p.name); f.str(p.plan); f.str(p.bal);
    f.byte((uint8_t)p.st);
    f.byte(p.nItems);
    for (int j = 0; j < p.nItems; j++) {
      f.str(p.items[j].label);
      f.num(p.items[j].left);
      f.str(p.items[j].reset);
    }
    f.byte(p.nSegs);
    for (int j = 0; j < p.nSegs; j++) {
      f.str(p.segs[j].s);
      f.byte(p.segs[j].warn);
    }
  }

  f.blk(models.got, models.st);
  f.str(models.scope);
  f.byte(models.n);
  for (int i = 0; i < models.n; i++) {
    f.str(models.m[i].name); f.str(models.m[i].tok); f.num(models.m[i].pct);
  }

  f.blk(heat.got, heat.st);
  f.byte(heat.hasDays); f.byte(heat.hasTrend); f.byte(heat.n);
  f.num(heat.start.y); f.byte(heat.start.m); f.byte(heat.start.d);
  f.str(heat.peak); f.num(heat.active); f.byte(heat.calendar);
  f.bytes(heat.days, HEAT_MAX);
  f.bytes(heat.trend, HEAT_MAX);

  f.blk(mouse.got, mouse.st);
  f.num(mouse.pct); f.byte(mouse.chg); f.str(mouse.at);
  return f.h;
}

}  // namespace dash
