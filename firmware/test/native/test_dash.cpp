// Host-side tests for dashboard v2: dither, string / heat decoding, the
// protocol layer (handleLine) for v1 and v2 messages, error codes and the
// content signature. No hardware, no Serial. Build + run (from firmware/,
// after `pio run -e waveshare_esp32_42tri` has fetched the libraries; see
// README §9.7):
//   g++ -std=c++17 -Wall -Wextra -DARDUINO=100 -DARDUINOJSON_ENABLE_ARDUINO_STRING=0 -DARDUINOJSON_ENABLE_ARDUINO_STREAM=0 -DARDUINOJSON_ENABLE_ARDUINO_PRINT=0 -DARDUINOJSON_ENABLE_PROGMEM=0 -Itest/host_shim -Isrc -I.pio/libdeps/waveshare_esp32_42tri/ArduinoJson/src test/native/test_dash.cpp src/protocol.cpp -o test_dash && ./test_dash

#include <stdio.h>
#include <string.h>

#include <string>

#include "dash_model.h"
#include "dither.h"
#include "diffuse.h"
#include "protocol.h"
#include "snap_flush_policy.h"

static int failures = 0, checks = 0;
#define CHECK(cond)                                            \
  do {                                                         \
    checks++;                                                  \
    if (!(cond)) {                                             \
      failures++;                                              \
      printf("  FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
    }                                                          \
  } while (0)
#define CHECK_STR(a, b)                                                                  \
  do {                                                                                   \
    checks++;                                                                            \
    if (strcmp((a), (b)) != 0) {                                                         \
      failures++;                                                                        \
      printf("  FAIL %s:%d: \"%s\" != \"%s\"\n", __FILE__, __LINE__, (a), (b));          \
    }                                                                                    \
  } while (0)

using namespace dash;

// ---------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------
static HandleResult feed(ViewState& v, const std::string& line) { return handleLine(v, line.c_str(), line.size()); }

static std::string rep(char c, int n) { return std::string(n, c); }

// 15 weeks = 101 days (2026-07-20, a Monday .. 2026-10-28, a Wednesday): first 60 unknown, last 41 known (0..5 cycling).
static std::string sampleHeatD() {
  std::string s = rep('-', 60);
  for (int i = 0; i < 41; i++) s += (char)('0' + i % 6);
  return s;
}
static std::string sampleHeatT() {
  const char* alpha = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  std::string s = rep('-', 60);
  for (int i = 0; i < 41; i++) s += alpha[(i * 3) % 64];
  return s;
}
static std::string heatMsg(const std::string& d, const std::string& t, const char* extra = ",\"rows\":7") {
  return "{\"v\":2,\"type\":\"heat\",\"st\":\"ok\",\"start\":\"2026-07-20\",\"end\":\"2026-10-28\",\"d\":\"" + d +
         "\",\"t\":\"" + t + "\",\"peak\":\"1.19B\",\"active\":41" + extra + "}";
}

static const char* DASH =
    "{\"v\":2,\"type\":\"dash\",\"st\":\"ok\",\"time\":\"10-28 20:20\","
    "\"main\":{\"l\":\"TODAY\",\"tok\":\"1,430,736,150\",\"cost\":\"$363.69\"},"
    "\"alt\":{\"l\":\"TOTAL\",\"tok\":\"7.43B\"}}";
static const char* QUOTA =
    "{\"v\":2,\"type\":\"quota\",\"st\":\"ok\",\"p\":["
    "{\"n\":\"Claude\",\"plan\":\"Max 5x\",\"st\":\"ok\",\"i\":[{\"l\":\"5h\",\"left\":63,\"rst\":\"1h57m\"},{\"l\":\"Wk\",\"left\":64,\"rst\":\"5d8h\"}]},"
    "{\"n\":\"Codex\",\"plan\":\"Plus\",\"i\":[{\"l\":\"5h\",\"left\":53,\"rst\":\"2h46m\"},{\"l\":\"Wk\",\"left\":93,\"rst\":\"6d21h\"}]},"
    "{\"n\":\"Antigrav\",\"plan\":\"Pro\",\"i\":[{\"l\":\"5h\",\"left\":100,\"rst\":\"4h59m\"},{\"l\":\"Wk\",\"left\":97,\"rst\":\"1d20h\"}],"
    "\"txt\":[{\"s\":\"C/G 72%/64%\"},{\"s\":\"4 Argon 0%\",\"w\":1}]},"
    "{\"n\":\"DeepSeek\",\"plan\":\"PAYG\",\"bal\":\"\xC2\xA5" "38.66\"}]}";
static const char* MODELS =
    "{\"v\":2,\"type\":\"models\",\"st\":\"ok\",\"scope\":\"TODAY\",\"m\":[[\"claude-opus-5-5\",\"4.30B\",60],[\"gpt-6.1-sol\",\"753M\",11]]}";
static const char* MOUSE = "{\"v\":2,\"type\":\"mouse\",\"st\":\"ok\",\"pct\":68,\"chg\":false,\"at\":\"19:39\"}";

struct Gfx {  // counts / records ink pixels
  bool ink[64][64] = {};
  int count = 0;
  void drawPixel(int16_t x, int16_t y, uint16_t) {
    if (x >= 0 && y >= 0 && x < 64 && y < 64 && !ink[y][x]) {
      ink[y][x] = true;
      count++;
    }
  }
};

// ---------------------------------------------------------------------
static void test_dither() {
  printf("dither\n");
  // Ink per 4x4 tile and per level: 0 / 2 / 4 / 8 / 12 / 16 of 16 = 0 / 12.5 / 25 / 50 / 75 / 100 %.
  const int expect[6] = {0, 2, 4, 8, 12, 16};
  for (int lv = 0; lv < 6; lv++) {
    int n = 0;
    for (int y = 0; y < 4; y++)
      for (int x = 0; x < 4; x++) n += dither::isInk(lv, x, y);
    CHECK(n == expect[lv]);
  }
  // Levels nest: ink at level k is also ink at k+1 (no flicker between levels).
  bool nested = true;
  for (int lv = 0; lv < 5; lv++)
    for (int y = 0; y < 4; y++)
      for (int x = 0; x < 4; x++)
        if (dither::isInk(lv, x, y) && !dither::isInk(lv + 1, x, y)) nested = false;
  CHECK(nested);
  // Clamped outside 0..5.
  CHECK(!dither::isInk(-3, 1, 1));
  CHECK(dither::isInk(9, 1, 1));

  // Pattern is a function of absolute coordinates: period 4, also for negative x/y.
  bool periodic = true;
  for (int lv = 1; lv < 5; lv++)
    for (int y = -8; y < 8; y++)
      for (int x = -8; x < 8; x++)
        if (dither::isInk(lv, x, y) != dither::isInk(lv, x + 4, y + 8)) periodic = false;
  CHECK(periodic);

  // 12.5 %: sparse, no two ink pixels touch; 50 %: a perfect checkerboard.
  bool sparse = true, checker = true;
  for (int y = 0; y < 8; y++)
    for (int x = 0; x < 8; x++) {
      if (dither::isInk(1, x, y) && (dither::isInk(1, x + 1, y) || dither::isInk(1, x, y + 1))) sparse = false;
      if (dither::isInk(3, x, y) == dither::isInk(3, x + 1, y)) checker = false;
      if (dither::isInk(3, x, y) == dither::isInk(3, x, y + 1)) checker = false;
    }
  CHECK(sparse);
  CHECK(checker);

  // Alignment: two adjacent rectangles of one level draw exactly what one big one does.
  for (int lv = 1; lv <= 4; lv++) {
    Gfx whole, parts;
    dither::fillRect(whole, 3, 5, 20, 12, lv, 0);
    dither::fillRect(parts, 3, 5, 7, 12, lv, 0);
    dither::fillRect(parts, 10, 5, 13, 12, lv, 0);
    bool same = true;
    for (int y = 0; y < 64; y++)
      for (int x = 0; x < 64; x++)
        if (whole.ink[y][x] != parts.ink[y][x]) same = false;
    CHECK(same);
  }
  // A rect of 8x8 (two whole tiles each way) has exactly 4x the tile ink.
  for (int lv = 0; lv < 6; lv++) {
    Gfx g;
    dither::fillRect(g, 8, 8, 8, 8, lv, 0);
    CHECK(g.count == expect[lv] * 4);
  }
  // Separator line: every other dot.
  Gfx hl;
  dither::hLine(hl, 0, 3, 16, 0);
  CHECK(hl.count == 8);
  bool alt = true;
  for (int x = 0; x < 15; x++)
    if (hl.ink[3][x] == hl.ink[3][x + 1]) alt = false;
  CHECK(alt);
  Gfx vl;
  dither::vLine(vl, 5, 0, 16, 0);
  CHECK(vl.count == 8);
}

static void test_strings() {
  printf("strings / decoding\n");
  char b[8];
  setStr(b, sizeof(b), "  hello  ");
  CHECK_STR(b, "hello");
  setStr(b, sizeof(b), "0123456789");  // 7 chars + NUL
  CHECK_STR(b, "0123456");
  setStr(b, sizeof(b), nullptr);
  CHECK_STR(b, "");
  setStr(b, sizeof(b), "a\nb\tc");  // control characters dropped
  CHECK_STR(b, "abc");
  // UTF-8: "¥" is 2 bytes, never cut in half.
  char c[4];
  setStr(c, sizeof(c), "\xC2\xA5" "38.66");  // fits 3 bytes: "¥3"
  CHECK_STR(c, "\xC2\xA5" "3");
  char d[3];
  setStr(d, sizeof(d), "a\xC2\xA5" "b");  // cap 3 -> 2 bytes: 'a' + half of ¥ -> only 'a'
  CHECK_STR(d, "a");
  char e[2];
  setStr(e, sizeof(e), "\xC2\xA5");
  CHECK_STR(e, "");
  // A complete character right at the end is kept.
  char f[4];
  setStr(f, sizeof(f), "a\xC2\xA5");
  CHECK_STR(f, "a\xC2\xA5");

  // Model names: <= 18 chars kept, longer -> 17 chars + '~'.
  char n[24];
  setModelName(n, sizeof(n), "claude-opus-5-5");
  CHECK_STR(n, "claude-opus-5-5");
  setModelName(n, sizeof(n), "123456789012345678");  // exactly 18
  CHECK_STR(n, "123456789012345678");
  setModelName(n, sizeof(n), "1234567890123456789");  // 19
  CHECK_STR(n, "12345678901234567~");
  setModelName(n, sizeof(n), "anthropic/claude-3-7-sonnet-20250219-extra-long-name-here");
  CHECK_STR(n, "anthropic/claude-~");
  CHECK(utf8Len(n) == 18);
  setModelName(n, sizeof(n), "\xC2\xA5\xC2\xA5\xC2\xA5\xC2\xA5\xC2\xA5\xC2\xA5\xC2\xA5\xC2\xA5\xC2\xA5\xC2\xA5\xC2\xA5\xC2\xA5\xC2\xA5\xC2\xA5\xC2\xA5\xC2\xA5\xC2\xA5\xC2\xA5\xC2\xA5");  // 19 x ¥
  CHECK(utf8Len(n) <= 18 && strlen(n) < sizeof(n));  // multi-byte: shortened to fit the field
  CHECK(n[strlen(n) - 1] == '~');

  // Heat digits and trend alphabet.
  CHECK(decodeHeat('0') == 0 && decodeHeat('9') == 9 && decodeHeat('A') == 10 && decodeHeat('C') == 12);
  CHECK(decodeHeat('-') == UNKNOWN);
  CHECK(decodeHeat('D') == BAD_CHAR && decodeHeat('a') == BAD_CHAR);
  CHECK(decodeTrend('A') == 0 && decodeTrend('Z') == 25);
  CHECK(decodeTrend('a') == 26 && decodeTrend('z') == 51);
  CHECK(decodeTrend('0') == 52 && decodeTrend('9') == 61);
  CHECK(decodeTrend('+') == 62 && decodeTrend('/') == 63);
  CHECK(decodeTrend('-') == UNKNOWN);
  CHECK(decodeTrend('!') == BAD_CHAR && decodeTrend(' ') == BAD_CHAR);
  // The 64 symbols are all distinct and cover 0..63.
  bool seen[64] = {};
  const char* alpha = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  for (int i = 0; i < 64; i++) seen[decodeTrend(alpha[i])] = true;
  bool all = true;
  for (int i = 0; i < 64; i++) all = all && seen[i];
  CHECK(all);

  // Dates.
  Ymd s = parseYmd("2026-04-30");
  CHECK(s.valid() && s.y == 2026 && s.m == 4 && s.d == 30);
  Ymd w = addDays(s, 7);
  CHECK(w.m == 5 && w.d == 7);
  Ymd e2 = addDays(s, 181);
  CHECK(e2.y == 2026 && e2.m == 10 && e2.d == 28);
  Ymd mid = addDays(s, 90);
  CHECK(mid.m == 7 && mid.d == 29);
  // Weekdays (Monday = 0): 2026-10-28 is a Wednesday, 1970-01-01 a Thursday, 2024-02-29 a Thursday.
  CHECK(weekdayMon0(parseYmd("2026-10-28")) == 2);
  CHECK(weekdayMon0(civilFromDays(0)) == 3);
  CHECK(weekdayMon0(parseYmd("2024-02-29")) == 3);
  CHECK(weekdayMon0(parseYmd("2026-10-25")) == 6 && weekdayMon0(parseYmd("2026-10-26")) == 0);
  // Calendar order (Sunday = 0): 2026-10-25 is a Sunday, 10-24 a Saturday, 10-28 a Wednesday; invalid -> 0.
  CHECK(weekdaySun0(parseYmd("2026-10-25")) == 0 && weekdaySun0(parseYmd("2026-10-24")) == 6);
  CHECK(weekdaySun0(parseYmd("2026-10-28")) == 3 && weekdaySun0(parseYmd("")) == 0);
  Ymd leap = addDays(parseYmd("2028-02-28"), 1);
  CHECK(leap.m == 2 && leap.d == 29);
  Ymd yr = addDays(parseYmd("2026-12-31"), 1);
  CHECK(yr.y == 2027 && yr.m == 1 && yr.d == 1);
  CHECK(!parseYmd("2026-02-31").valid());
  CHECK(!parseYmd("2026/04/30").valid());
  CHECK(!parseYmd("").valid());
  CHECK(!parseYmd(nullptr).valid());
}

static void test_v1() {
  printf("handleLine v1\n");
  ViewState v;
  HandleResult r = feed(v, "{\"v\":1,\"type\":\"ping\"}");
  CHECK(r.ok && !r.forceFull && !v.hasData);
  r = feed(v, "{\"type\":\"ping\"}");  // v missing = v1
  CHECK(r.ok);
  r = feed(v, "{\"v\":1,\"type\":\"full_refresh\"}");
  CHECK(r.ok && r.forceFull);
  r = feed(v,
           "{\"v\":1,\"type\":\"stats\",\"time\":\"10-28 14:05\",\"cpu\":23,\"mem\":61.4,\"disk\":\"72\","
           "\"net_up\":\"120K\",\"net_down\":3400000,\"gpu\":15,\"gpu_temp\":52,"
           "\"tok\":{\"block_tokens\":145900000,\"block_cost\":40.69,\"burn_tpm\":\"3.3K\",\"remain_min\":135,"
           "\"today_tokens\":\"210M\",\"today_cost\":58.2}}");
  CHECK(r.ok && v.hasData && !v.v2);
  CHECK_STR(v.s.time.c_str(), "10-28 14:05");
  CHECK(v.s.cpu == 23 && v.s.mem == 61 && v.s.disk == 72);
  CHECK_STR(v.s.netDown.c_str(), "3.4M");
  CHECK(v.s.hasGpu && v.s.gpu == 15);
  CHECK_STR(v.s.gpuTemp.c_str(), "52\xC2\xB0" "C");
  CHECK_STR(v.s.blockTokens.c_str(), "146M");
  CHECK_STR(v.s.blockCost.c_str(), "$40.69");
  CHECK_STR(v.s.remain.c_str(), "2h15m");
  CHECK_STR(v.s.todayCost.c_str(), "$58.20");
  // Missing fields -> "--" / -1
  ViewState v2;
  r = feed(v2, "{\"v\":1,\"type\":\"stats\"}");
  CHECK(r.ok);
  CHECK_STR(v2.s.time.c_str(), "--");
  CHECK(v2.s.cpu == -1 && !v2.s.hasGpu);
  // Non-finite numbers ("nan" / "inf" parse as doubles) must not reach lround().
  for (const char* bad : {"nan", "NaN", "inf", "-inf", "Infinity"}) {
    ViewState v3;
    r = feed(v3, std::string("{\"v\":1,\"type\":\"stats\",\"cpu\":\"") + bad + "\",\"mem\":\"" + bad + "\",\"gpu\":\"" + bad + "\"}");
    CHECK(r.ok && v3.s.cpu == -1 && v3.s.mem == -1 && v3.s.gpu == -1);
  }
  r = feed(v2, "{\"v\":1,\"type\":\"stats\",\"cpu\":\"250\",\"mem\":-5}");
  CHECK(r.ok && v2.s.cpu == 100 && v2.s.mem == 0);  // finite out-of-range values still clamp

  // Errors
  CHECK(feed(v, "").blank);
  r = feed(v, "{nope");
  CHECK(!r.ok && strncmp(r.err, "bad_json", 8) == 0);
  r = feed(v, "[1,2]");
  CHECK(!r.ok && strcmp(r.err, "not_object") == 0);
  r = feed(v, "{\"v\":1}");
  CHECK(!r.ok && strcmp(r.err, "missing_type") == 0);
  r = feed(v, "{\"v\":1,\"type\":\"nope\"}");
  CHECK(!r.ok && strcmp(r.err, "unknown_type") == 0);
  r = feed(v, "{\"v\":3,\"type\":\"ping\"}");
  CHECK(!r.ok && strcmp(r.err, "bad_version") == 0);
  r = feed(v, "{\"v\":0,\"type\":\"ping\"}");
  CHECK(!r.ok && strcmp(r.err, "bad_version") == 0);
  r = feed(v, "{\"v\":\"1\",\"type\":\"ping\"}");
  CHECK(!r.ok && strcmp(r.err, "bad_version") == 0);
  r = feed(v, "{\"v\":2,\"type\":\"stats\"}");  // stats is v1 only
  CHECK(!r.ok && strcmp(r.err, "bad_version") == 0);
  std::string big = "{\"v\":1,\"type\":\"ping\",\"pad\":\"" + rep('x', 1100) + "\"}";
  r = feed(v, big);
  CHECK(!r.ok && strcmp(r.err, "too_long") == 0);
  r = tooLongResult();
  CHECK(!r.ok && strcmp(r.err, "too_long") == 0);
}

static void test_v2() {
  printf("handleLine v2\n");
  ViewState v;
  CHECK(feed(v, "{\"v\":2,\"type\":\"ping\"}").ok);
  CHECK(!v.hasData && !v.v2);  // ping alone does not switch screens
  CHECK(feed(v, "{\"v\":2,\"type\":\"full_refresh\"}").forceFull);

  // dash
  HandleResult r = feed(v, DASH);
  CHECK(r.ok && v.hasData && v.v2);
  CHECK(v.d.top.got && v.d.top.st == ST_OK);
  CHECK_STR(v.d.top.time, "10-28 20:20");
  CHECK_STR(v.d.top.main.label, "TODAY");
  CHECK_STR(v.d.top.main.tok, "1,430,736,150");
  CHECK_STR(v.d.top.main.cost, "$363.69");
  CHECK_STR(v.d.top.alt.label, "TOTAL");
  CHECK_STR(v.d.top.alt.tok, "7.43B");
  CHECK(v.d.top.alt.cost[0] == 0);
  CHECK(!v.d.quota.got && !v.d.models.got && !v.d.heat.got && !v.d.mouse.got);  // never received

  // quota
  r = feed(v, QUOTA);
  CHECK(r.ok && v.d.quota.got && v.d.quota.n == 4);
  CHECK_STR(v.d.quota.p[0].name, "Claude");
  CHECK_STR(v.d.quota.p[0].plan, "Max 5x");
  CHECK(v.d.quota.p[0].nItems == 2 && v.d.quota.p[0].items[1].left == 64);
  CHECK_STR(v.d.quota.p[0].items[0].reset, "1h57m");
  CHECK_STR(v.d.quota.p[2].items[0].label, "5h");
  CHECK(v.d.quota.p[2].items[0].left == 100 && v.d.quota.p[2].items[1].left == 97);
  CHECK(v.d.quota.p[2].nSegs == 2);
  CHECK_STR(v.d.quota.p[2].segs[0].s, "C/G 72%/64%");
  CHECK(!v.d.quota.p[2].segs[0].warn);
  CHECK_STR(v.d.quota.p[2].segs[1].s, "4 Argon 0%");
  CHECK(v.d.quota.p[2].segs[1].warn);
  CHECK(v.d.quota.p[0].nSegs == 0);
  // txt on any provider, also a balance-only one (no i) and Claude
  r = feed(v, "{\"v\":2,\"type\":\"quota\",\"p\":[{\"n\":\"Claude\",\"i\":[{\"l\":\"5h\",\"left\":1}],\"txt\":[{\"s\":\"Fable 94%\"}]},"
               "{\"n\":\"DeepSeek\",\"plan\":\"PAYG\",\"bal\":\"Â¥" "38.66\",\"txt\":[{\"s\":\"Today Â¥" "7.42\"}]}]}");
  CHECK(r.ok && v.d.quota.n == 2 && v.d.quota.p[0].nSegs == 1 && v.d.quota.p[1].nItems == 0 && v.d.quota.p[1].nSegs == 1);
  CHECK_STR(v.d.quota.p[1].segs[0].s, "Today Â¥" "7.42");
  CHECK(v.d.quota.p[1].bal[0] != 0);
  feed(v, QUOTA);  // restore the 4-provider sample for the checks below
  CHECK(v.d.quota.p[3].nItems == 0);
  CHECK_STR(v.d.quota.p[3].bal, "\xC2\xA5" "38.66");
  CHECK(v.d.quota.p[1].st == ST_OK);  // st omitted = ok

  // quota details: null left, extra providers / items ignored, label cut to 4+NUL
  r = feed(v,
           "{\"v\":2,\"type\":\"quota\",\"p\":[{\"n\":\"A\",\"i\":[{\"l\":\"abcdefgh\",\"left\":null},{\"l\":\"b\",\"left\":5},"
           "{\"l\":\"c\",\"left\":150},{\"l\":\"d\",\"left\":1}]},{\"n\":\"B\"},{\"n\":\"C\"},{\"n\":\"D\"},{\"n\":\"E\"}]}");
  CHECK(r.ok && v.d.quota.n == 4);
  CHECK(v.d.quota.p[0].nItems == 3);
  CHECK(v.d.quota.p[0].items[0].left == -1);
  CHECK(v.d.quota.p[0].items[2].left == 100);  // clamped
  CHECK(strlen(v.d.quota.p[0].items[0].label) <= 5);
  CHECK(v.d.quota.p[3].name[0] == 'D');

  // models
  r = feed(v, MODELS);
  CHECK(r.ok && v.d.models.got && v.d.models.n == 2);
  CHECK_STR(v.d.models.m[0].name, "claude-opus-5-5");
  CHECK_STR(v.d.models.m[0].tok, "4.30B");
  CHECK(v.d.models.m[0].pct == 60);
  CHECK_STR(v.d.models.scope, "TODAY");
  CHECK_STR(v.d.models.m[1].name, "gpt-6.1-sol");
  r = feed(v, "{\"v\":2,\"type\":\"models\",\"m\":[[\"a-very-long-model-name-indeed\",\"1M\",1],[\"b\",\"1M\",null],[\"c\",\"1\",1],[\"d\",\"1\",1],[\"e\",\"1\",1],[\"f\",\"1\",1]]}");
  CHECK(r.ok && v.d.models.n == 5);  // 6th dropped
  CHECK(utf8Len(v.d.models.m[0].name) == 18);
  CHECK(v.d.models.m[0].name[strlen(v.d.models.m[0].name) - 1] == '~');
  CHECK(v.d.models.m[1].pct == -1);

  // heat: the sample is 15 weeks = 101 days starting on a Monday; the calendar shows the last 5 weeks.
  r = feed(v, heatMsg(sampleHeatD(), sampleHeatT()));
  CHECK(r.ok && v.d.heat.got && v.d.heat.hasDays && v.d.heat.hasTrend);
  CHECK(v.d.heat.n == 101);
  CHECK(v.d.heat.days[0] == UNKNOWN && v.d.heat.days[59] == UNKNOWN);
  CHECK(v.d.heat.days[60] == 0 && v.d.heat.days[61] == 1 && v.d.heat.days[65] == 5 && v.d.heat.days[66] == 0);
  CHECK(v.d.heat.trend[59] == UNKNOWN && v.d.heat.trend[60] == 0 && v.d.heat.trend[61] == 3);
  CHECK(v.d.heat.trend[100] == (40 * 3) % 64);
  CHECK(v.d.heat.start.valid() && v.d.heat.start.m == 7 && v.d.heat.start.d == 20);
  CHECK_STR(v.d.heat.peak, "1.19B");
  CHECK(v.d.heat.active == 41);
  // Calendar weeks run Sunday..Saturday (slot 0 = Sunday, the top row on screen); this sample starts on a
  // Monday, so day 0 is slot 1 of the first week.
  CHECK(weekdaySun0(v.d.heat.start) == 1);
  CHECK(v.d.heat.col(0) == 1 && v.d.heat.row(0) == 0);
  CHECK(v.d.heat.col(5) == 6 && v.d.heat.row(5) == 0);
  CHECK(v.d.heat.col(6) == 0 && v.d.heat.row(6) == 1);  // the first Sunday starts the next week
  // Today is 2026-10-28, a Wednesday: slot 3 of the last week. 101 / 7 = 14 -> 15 weeks in total.
  CHECK(v.d.heat.col(100) == 3 && v.d.heat.row(100) == 14);
  CHECK(v.d.heat.totalRows() == 15);
  // Only the last 10 weeks are shown: weeks 5..14 -> shown weeks 0..9, the first shown day is 34 (a Sunday).
  CHECK(v.d.heat.firstRow() == 5 && v.d.heat.shownRows() == 10 && v.d.heat.firstDay() == 34);
  CHECK(v.d.heat.row(34) - v.d.heat.firstRow() == 0 && v.d.heat.col(34) == 0);
  CHECK(v.d.heat.row(100) - v.d.heat.firstRow() == 9);
  // Every shown day has its own cell.
  {
    bool used[10][7];
    memset(used, 0, sizeof(used));
    bool unique = true;
    for (int i = v.d.heat.firstDay(); i < 101; i++) {
      int c = v.d.heat.col(i), rr = v.d.heat.row(i) - v.d.heat.firstRow();
      if (c >= 7 || rr < 0 || rr >= 10 || used[rr][c]) unique = false;
      else used[rr][c] = true;
    }
    CHECK(unique);
  }
  // dash `every`: refresh interval in seconds, 0..3600, missing = 0; not part of the signature.
  {
    ViewState e;
    std::string base = "{\"v\":2,\"type\":\"dash\",\"time\":\"10-28 20:20\"";
    CHECK(feed(e, base + "}").ok && e.d.top.everyS == 0);
    std::string before = e.signature().c_str();
    CHECK(feed(e, base + ",\"every\":600}").ok && e.d.top.everyS == 600);
    CHECK(std::string(e.signature().c_str()) == before);
    CHECK(feed(e, base + ",\"every\":99999}").ok && e.d.top.everyS == 3600);
    CHECK(feed(e, "{\"v\":2,\"type\":\"dash\",\"st\":\"na\",\"every\":300}").ok && e.d.top.everyS == 300);
  }
  // view: "cal" = calendar style, anything else or missing = heat grid; kept on st:"na".
  CHECK(!v.d.heat.calendar);
  r = feed(v, "{\"v\":2,\"type\":\"heat\",\"view\":\"cal\",\"d\":\"" + rep('3', 35) + "\"}");
  CHECK(r.ok && v.d.heat.calendar);
  r = feed(v, "{\"v\":2,\"type\":\"heat\",\"st\":\"na\",\"view\":\"cal\"}");
  CHECK(r.ok && v.d.heat.calendar);
  r = feed(v, "{\"v\":2,\"type\":\"heat\",\"view\":\"grid\",\"d\":\"" + rep('3', 35) + "\"}");
  CHECK(r.ok && !v.d.heat.calendar);
  // Old name active30 is still understood.
  r = feed(v, "{\"v\":2,\"type\":\"heat\",\"d\":\"" + rep('3', 35) + "\",\"active30\":7}");
  CHECK(r.ok && v.d.heat.active == 7);
  // The line stays under the 1024 byte limit, also for the longest tolerated window.
  CHECK(heatMsg(sampleHeatD(), sampleHeatT()).size() < 1024);
  CHECK(heatMsg(rep('3', 112), rep('B', 112)).size() < 1024);
  // only d, only t; start missing = no weekday shift (day 0 in the first column)
  r = feed(v, "{\"v\":2,\"type\":\"heat\",\"d\":\"" + rep('3', 35) + "\"}");
  CHECK(r.ok && v.d.heat.hasDays && !v.d.heat.hasTrend && v.d.heat.n == 35 && v.d.heat.days[20] == 3);
  CHECK(v.d.heat.active == -1 && !v.d.heat.start.valid() && v.d.heat.col(0) == 0 && v.d.heat.row(0) == 0);
  CHECK(v.d.heat.totalRows() == 5);
  r = feed(v, "{\"v\":2,\"type\":\"heat\",\"t\":\"" + rep('B', 35) + "\"}");
  CHECK(r.ok && !v.d.heat.hasDays && v.d.heat.hasTrend && v.d.heat.trend[20] == 1);

  // The window length comes from the data length. Whole weeks: 5 weeks, 29..35 days, first day a Sunday.
  for (int len : {29, 30, 33, 35}) {
    r = feed(v, "{\"v\":2,\"type\":\"heat\",\"start\":\"2026-09-27\",\"d\":\"" + rep('2', len) + "\"}");
    CHECK(r.ok && v.d.heat.n == len && v.d.heat.totalRows() == 5 && v.d.heat.firstRow() == 0 && v.d.heat.firstDay() == 0);
  }
  r = feed(v, "{\"v\":2,\"type\":\"heat\",\"start\":\"2026-09-27\",\"d\":\"" + rep('2', 28) + "\"}");  // exactly 4 weeks
  CHECK(r.ok && v.d.heat.totalRows() == 4 && v.d.heat.shownRows() == 4);
  r = feed(v, "{\"v\":2,\"type\":\"heat\",\"start\":\"2026-08-23\",\"d\":\"" + rep('2', 71) + "\"}");  // 11th week: oldest week dropped
  CHECK(r.ok && v.d.heat.totalRows() == 11 && v.d.heat.firstRow() == 1 && v.d.heat.shownRows() == 10 && v.d.heat.firstDay() == 7);
  r = feed(v, "{\"v\":2,\"type\":\"heat\",\"start\":\"2026-10-25\",\"d\":\"01234512\"}");  // 8 days from a Sunday: 2 weeks
  CHECK(r.ok && v.d.heat.n == 8 && v.d.heat.col(7) == 0 && v.d.heat.row(7) == 1 && v.d.heat.totalRows() == 2);
  // A start that is not a Sunday shifts day 0 to its weekday: Saturday start -> slot 6, then the next week.
  r = feed(v, "{\"v\":2,\"type\":\"heat\",\"start\":\"2026-10-24\",\"d\":\"" + rep('2', 10) + "\"}");
  CHECK(r.ok && weekdaySun0(v.d.heat.start) == 6 && v.d.heat.col(0) == 6 && v.d.heat.row(0) == 0);
  CHECK(v.d.heat.col(1) == 0 && v.d.heat.row(1) == 1 && v.d.heat.totalRows() == 3);
  // Short strings and a single day
  r = feed(v, "{\"v\":2,\"type\":\"heat\",\"start\":\"2026-10-26\",\"d\":\"0123\",\"t\":\"A-BC\"}");
  CHECK(r.ok && v.d.heat.n == 4 && v.d.heat.days[3] == 3 && v.d.heat.trend[1] == UNKNOWN && v.d.heat.trend[3] == 2);
  CHECK(v.d.heat.days[4] == UNKNOWN && v.d.heat.totalRows() == 1);  // beyond n: unknown
  r = feed(v, "{\"v\":2,\"type\":\"heat\",\"start\":\"2026-10-28\",\"d\":\"5\"}");
  CHECK(r.ok && v.d.heat.n == 1 && v.d.heat.days[0] == 5 && v.d.heat.totalRows() == 1 && v.d.heat.col(0) == 3);
  r = feed(v, "{\"v\":2,\"type\":\"heat\",\"start\":\"2026-10-26\",\"d\":\"0123458D\"}");
  CHECK(r.ok == false);  // 'D' is not a density level (0..9, A..C)
  // The old `rows` field is ignored (any value, even a bad one, is accepted and changes nothing).
  r = feed(v, "{\"v\":2,\"type\":\"heat\",\"start\":\"2026-09-27\",\"rows\":5,\"d\":\"" + rep('2', 35) + "\"}");
  CHECK(r.ok && v.d.heat.totalRows() == 5 && v.d.heat.col(7) == 0 && v.d.heat.row(7) == 1);
  r = feed(v, "{\"v\":2,\"type\":\"heat\",\"start\":\"2026-09-27\",\"rows\":99,\"d\":\"" + rep('2', 35) + "\"}");
  CHECK(r.ok && v.d.heat.totalRows() == 5);
  // An old host sending 182 days: only the last 112 are kept (16 weeks, start moves forward by 70 days);
  // the calendar still shows the last 10 weeks.
  std::string d182 = rep('-', 70) + rep('1', 56) + rep('3', 56);  // 70 + 112 = 182
  CHECK(d182.size() == 182);
  r = feed(v, "{\"v\":2,\"type\":\"heat\",\"start\":\"2026-04-27\",\"d\":\"" + d182 + "\",\"t\":\"" + d182 + "\"}");
  CHECK(r.ok && v.d.heat.n == 112);
  CHECK(v.d.heat.days[0] == 1 && v.d.heat.days[55] == 1 && v.d.heat.days[56] == 3 && v.d.heat.days[111] == 3);
  Ymd moved = addDays(parseYmd("2026-04-27"), 70);
  CHECK(v.d.heat.start.m == moved.m && v.d.heat.start.d == moved.d);
  CHECK(v.d.heat.trend[56] == decodeTrend('3'));
  CHECK(v.d.heat.totalRows() == 17 && v.d.heat.shownRows() == 10 && v.d.heat.firstDay() == 48);  // day 0 is a Monday = slot 1

  // mouse
  r = feed(v, MOUSE);
  CHECK(r.ok && v.d.mouse.got && v.d.mouse.st == ST_OK && v.d.mouse.pct == 68 && !v.d.mouse.chg);
  CHECK_STR(v.d.mouse.at, "19:39");
  r = feed(v, "{\"v\":2,\"type\":\"mouse\",\"st\":\"stale\",\"pct\":12,\"chg\":true,\"at\":\"13:40\"}");
  CHECK(r.ok && v.d.mouse.st == ST_STALE && v.d.mouse.pct == 12 && v.d.mouse.chg);
  r = feed(v, "{\"v\":2,\"type\":\"mouse\",\"st\":\"off\",\"pct\":null}");
  CHECK(r.ok && v.d.mouse.st == ST_OFF && v.d.mouse.pct == -1);
  r = feed(v, "{\"v\":2,\"type\":\"mouse\",\"st\":\"na\"}");
  CHECK(r.ok && v.d.mouse.st == ST_NA);
  r = feed(v, "{\"v\":2,\"type\":\"mouse\",\"pct\":250}");
  CHECK(r.ok && v.d.mouse.pct == 100);

  // st: na clears the block (no stale values kept)
  feed(v, QUOTA);
  r = feed(v, "{\"v\":2,\"type\":\"quota\",\"st\":\"na\"}");
  CHECK(r.ok && v.d.quota.got && v.d.quota.st == ST_NA && v.d.quota.n == 0);
  feed(v, DASH);
  r = feed(v, "{\"v\":2,\"type\":\"dash\",\"st\":\"na\",\"time\":\"x\"}");
  CHECK(r.ok && v.d.top.st == ST_NA && v.d.top.time[0] == 0);
  feed(v, DASH);
  r = feed(v, "{\"v\":2,\"type\":\"dash\",\"st\":\"stale\",\"time\":\"10-28 19:50\",\"main\":{\"l\":\"TODAY\",\"tok\":\"1,000\"}}");
  CHECK(r.ok && v.d.top.st == ST_STALE);
  CHECK_STR(v.d.top.time, "10-28 19:50");
  CHECK_STR(v.d.top.main.tok, "1,000");
  CHECK(v.d.top.main.cost[0] == 0 && v.d.top.alt.tok[0] == 0);  // omitted -> empty -> shown as "--"
  // null / missing values are stored empty
  r = feed(v, "{\"v\":2,\"type\":\"dash\",\"main\":{\"tok\":null,\"cost\":5}}");
  CHECK(r.ok && v.d.top.main.tok[0] == 0 && v.d.top.main.cost[0] == 0);
  // An old-format dash (total / today / tm, no main / alt) is accepted and shows "--".
  r = feed(v, "{\"v\":2,\"type\":\"dash\",\"time\":\"10-28 19:40\",\"total\":{\"tok\":\"7.13B\",\"cost\":\"$1\"},\"today\":{\"tok\":\"1\"},\"tm\":\"TM 19:39\"}");
  CHECK(r.ok && v.d.top.got && v.d.top.main.tok[0] == 0 && v.d.top.alt.tok[0] == 0 && v.d.top.main.label[0] == 0);

  // Long strings are cut to the field size.
  r = feed(v, "{\"v\":2,\"type\":\"dash\",\"time\":\"" + rep('t', 200) + "\",\"main\":{\"tok\":\"" + rep('9', 200) + "\"}}");
  CHECK(r.ok && strlen(v.d.top.time) == sizeof(v.d.top.time) - 1);
  CHECK(strlen(v.d.top.main.tok) == sizeof(v.d.top.main.tok) - 1);
  r = feed(v, "{\"v\":2,\"type\":\"quota\",\"p\":[{\"n\":\"" + rep('n', 80) + "\",\"plan\":\"" + rep('p', 80) + "\",\"bal\":\"" +
                  rep('9', 80) + "\"}]}");
  CHECK(r.ok && strlen(v.d.quota.p[0].name) == sizeof(v.d.quota.p[0].name) - 1);
  CHECK(strlen(v.d.quota.p[0].plan) == sizeof(v.d.quota.p[0].plan) - 1);
  CHECK(strlen(v.d.quota.p[0].bal) == sizeof(v.d.quota.p[0].bal) - 1);

  // Mode switching: v1 stats -> v1 screen, next v2 data message -> v2 screen.
  CHECK(v.v2);
  CHECK(feed(v, "{\"v\":1,\"type\":\"stats\",\"time\":\"x\"}").ok);
  CHECK(!v.v2 && v.hasData);
  CHECK(feed(v, "{\"v\":1,\"type\":\"ping\"}").ok);
  CHECK(!v.v2);
  CHECK(feed(v, MOUSE).ok);
  CHECK(v.v2);
  CHECK(v.d.top.got);  // v2 blocks survived the detour
}

static void test_v2_errors() {
  printf("handleLine v2 errors\n");
  ViewState v;
  feed(v, DASH);
  feed(v, QUOTA);
  feed(v, MODELS);
  feed(v, heatMsg(sampleHeatD(), sampleHeatT()));
  feed(v, MOUSE);
  std::string before = v.signature().c_str();
  HandleResult r;
#define EXPECT_ERR(line, text)                  \
  do {                                          \
    r = feed(v, line);                          \
    CHECK(!r.ok && strcmp(r.err, text) == 0);   \
    if (strcmp(r.err, text) != 0) printf("    got \"%s\" for %s\n", r.err, #line); \
  } while (0)

  EXPECT_ERR("{\"v\":3,\"type\":\"dash\"}", "bad_version");
  EXPECT_ERR("{\"v\":\"2\",\"type\":\"dash\"}", "bad_version");
  EXPECT_ERR("{\"v\":2.5,\"type\":\"dash\"}", "bad_version");
  EXPECT_ERR("{\"type\":\"dash\"}", "bad_version");        // v missing = v1, dash is v2 only
  EXPECT_ERR("{\"v\":1,\"type\":\"quota\"}", "bad_version");
  EXPECT_ERR("{\"v\":2,\"type\":\"nope\"}", "unknown_type");
  EXPECT_ERR("{\"v\":2}", "missing_type");
  // missing / wrong fields
  EXPECT_ERR("{\"v\":2,\"type\":\"quota\",\"st\":\"ok\"}", "missing_field p");
  EXPECT_ERR("{\"v\":2,\"type\":\"quota\",\"p\":5}", "bad_field p");
  EXPECT_ERR("{\"v\":2,\"type\":\"quota\",\"p\":[1]}", "bad_field p");
  EXPECT_ERR("{\"v\":2,\"type\":\"quota\",\"p\":[{\"n\":\"a\",\"i\":3}]}", "bad_field i");
  EXPECT_ERR("{\"v\":2,\"type\":\"models\"}", "missing_field m");
  EXPECT_ERR("{\"v\":2,\"type\":\"models\",\"m\":[\"x\"]}", "bad_field m");
  EXPECT_ERR("{\"v\":2,\"type\":\"heat\",\"start\":\"2026-04-30\"}", "missing_field d");
  EXPECT_ERR("{\"v\":2,\"type\":\"quota\",\"p\":[{\"n\":\"a\",\"txt\":3}]}", "bad_field txt");
  EXPECT_ERR("{\"v\":2,\"type\":\"quota\",\"p\":[{\"n\":\"a\",\"txt\":[\"x\"]}]}", "bad_field txt");
  EXPECT_ERR("{\"v\":2,\"type\":\"heat\",\"d\":\"\"}", "bad_field d");
  EXPECT_ERR("{\"v\":2,\"type\":\"heat\",\"t\":\"\"}", "bad_field t");
  EXPECT_ERR(("{\"v\":2,\"type\":\"heat\",\"d\":\"" + rep('1', 30) + "\",\"t\":\"" + rep('A', 29) + "\"}"), "bad_field t");  // lengths differ
  EXPECT_ERR(("{\"v\":2,\"type\":\"heat\",\"d\":\"" + rep('D', 30) + "\"}"), "bad_field d");
  EXPECT_ERR(("{\"v\":2,\"type\":\"heat\",\"t\":\"" + rep('!', 30) + "\"}"), "bad_field t");
  EXPECT_ERR(("{\"v\":2,\"type\":\"heat\",\"d\":" + std::string("12") + "}"), "bad_field d");
  EXPECT_ERR("{\"v\":2,\"type\":\"mouse\",\"st\":\"weird\"}", "bad_field st");
  EXPECT_ERR("{\"v\":2,\"type\":\"dash\",\"st\":5}", "bad_field st");
  // too long / not json
  EXPECT_ERR(("{\"v\":2,\"type\":\"dash\",\"time\":\"" + rep('x', 1100) + "\"}"), "too_long");
  r = feed(v, "{\"v\":2,\"type\":");
  CHECK(!r.ok && strncmp(r.err, "bad_json", 8) == 0);
  // 1024 bytes is fine, 1025 is not
  std::string pad = "{\"v\":2,\"type\":\"ping\",\"x\":\"";
  pad += rep('a', 1024 - pad.size() - 2) + "\"}";
  CHECK(pad.size() == 1024);
  CHECK(feed(v, pad).ok);
  CHECK(!feed(v, pad + " ").ok);

  // None of the failed messages changed the view.
  CHECK(std::string(v.signature().c_str()) == before);
  CHECK(v.v2);
}

static void test_signature() {
  printf("signature\n");
  ViewState a, b;
  const std::string heat = heatMsg(sampleHeatD(), sampleHeatT());
  for (ViewState* v : {&a, &b}) {
    v->online = true;
    feed(*v, DASH);
    feed(*v, QUOTA);
    feed(*v, MODELS);
    feed(*v, heat);
    feed(*v, MOUSE);
  }
  std::string base = a.signature().c_str();
  CHECK(base == b.signature().c_str());
  CHECK(base != "boot");

  // Repeating the same message changes nothing.
  feed(a, DASH);
  feed(a, QUOTA);
  feed(a, heat);
  CHECK(base == a.signature().c_str());

  // Every displayed thing changes the signature.
  auto changed = [&](const std::string& line) {
    ViewState c = b;
    feed(c, line);
    return std::string(c.signature().c_str()) != base;
  };
  auto dashWith = [&](const char* time, const char* st, const char* mtok, const char* cost, const char* altTok, const char* ml = "TODAY") {
    return std::string("{\"v\":2,\"type\":\"dash\",\"st\":\"") + st + "\",\"time\":\"" + time + "\",\"main\":{\"l\":\"" + ml +
           "\",\"tok\":\"" + mtok + "\",\"cost\":\"" + cost + "\"},\"alt\":{\"l\":\"TOTAL\",\"tok\":\"" + altTok + "\"}}";
  };
  CHECK(!changed(dashWith("10-28 20:20", "ok", "1,430,736,150", "$363.69", "7.43B")));  // same content
  {  // switching to the calendar style is a visible change too
    std::string cal = heat;
    cal.insert(cal.find("\"type\":\"heat\",") + 14, "\"view\":\"cal\",");
    CHECK(changed(cal));
  }
  CHECK(changed(dashWith("10-28 20:21", "ok", "1,430,736,150", "$363.69", "7.43B")));
  CHECK(changed(dashWith("10-28 20:20", "stale", "1,430,736,150", "$363.69", "7.43B")));
  CHECK(changed(dashWith("10-28 20:20", "ok", "1,430,736,151", "$363.69", "7.43B")));
  CHECK(changed(dashWith("10-28 20:20", "ok", "1,430,736,150", "$363.70", "7.43B")));
  CHECK(changed(dashWith("10-28 20:20", "ok", "1,430,736,150", "$363.69", "7.44B")));
  CHECK(changed(dashWith("10-28 20:20", "ok", "1,430,736,150", "$363.69", "7.43B", "TOTAL")));
  CHECK(changed("{\"v\":2,\"type\":\"quota\",\"p\":[{\"n\":\"Claude\",\"plan\":\"Max 5x\",\"i\":[{\"l\":\"5h\",\"left\":62,\"rst\":\"1h57m\"}]}]}"));
  CHECK(changed("{\"v\":2,\"type\":\"quota\",\"st\":\"stale\",\"p\":[]}"));
  CHECK(changed("{\"v\":2,\"type\":\"models\",\"scope\":\"TODAY\",\"m\":[[\"claude-opus-5-5\",\"4.30B\",61]]}"));
  CHECK(changed("{\"v\":2,\"type\":\"models\",\"scope\":\"TOTAL\",\"m\":[[\"claude-opus-5-5\",\"4.30B\",60],[\"gpt-6.1-sol\",\"753M\",11]]}"));
  // quota text segments: text and the warn flag
  CHECK(changed(std::string(QUOTA).replace(std::string(QUOTA).find("4 Argon 0%"), 10, "4 Argon 1%")));
  CHECK(changed(std::string(QUOTA).replace(std::string(QUOTA).find(",\"w\":1"), 6, "")));
  CHECK(changed("{\"v\":2,\"type\":\"mouse\",\"st\":\"ok\",\"pct\":67,\"chg\":false,\"at\":\"19:39\"}"));
  CHECK(changed("{\"v\":2,\"type\":\"mouse\",\"st\":\"ok\",\"pct\":68,\"chg\":true,\"at\":\"19:39\"}"));
  CHECK(changed("{\"v\":2,\"type\":\"mouse\",\"st\":\"off\"}"));
  {
    std::string d = sampleHeatD();
    d[20] = d[20] == '2' ? '3' : '2';  // one heat cell
    CHECK(changed(heatMsg(d, sampleHeatT())));
    std::string t = sampleHeatT();
    t[29] = t[29] == 'B' ? 'C' : 'B';  // one trend point
    CHECK(changed(heatMsg(sampleHeatD(), t)));
    std::string d2 = sampleHeatD();
    d2[3] = '1';  // an unknown day becomes known
    CHECK(changed(heatMsg(d2, sampleHeatT())));
    std::string pk = heatMsg(sampleHeatD(), sampleHeatT());
    pk.replace(pk.find("1.19B"), 5, "1.20B");  // peak label
    CHECK(changed(pk));
    std::string st = heatMsg(sampleHeatD(), sampleHeatT());
    st.replace(st.find("2026-07-20"), 10, "2026-07-27");  // a different start date
    CHECK(changed(st));
    CHECK(!changed(heatMsg(sampleHeatD(), sampleHeatT(), ",\"rows\":5")));  // rows is ignored
  }
  // online flag and screen mode matter too
  ViewState c = b;
  c.online = false;
  CHECK(std::string(c.signature().c_str()) != base);
  ViewState d = b;
  feed(d, "{\"v\":1,\"type\":\"stats\"}");
  CHECK(std::string(d.signature().c_str()) != base);
  // not-yet-received block differs from an empty received one
  ViewState e1, e2;
  feed(e1, DASH);
  feed(e2, DASH);
  feed(e2, "{\"v\":2,\"type\":\"models\",\"st\":\"na\"}");
  CHECK(std::string(e1.signature().c_str()) != e2.signature().c_str());
  // field boundaries matter: ("ab","c") != ("a","bc")
  Dash x, y;
  setStr(x.top.time, sizeof(x.top.time), "ab");
  setStr(x.top.main.tok, sizeof(x.top.main.tok), "c");
  setStr(y.top.time, sizeof(y.top.time), "a");
  setStr(y.top.main.tok, sizeof(y.top.main.tok), "bc");
  CHECK(x.signature() != y.signature());
  // The model stays small (it is copied on the stack by the scheduler).
  printf("  sizeof(Dash)=%zu sizeof(Heat)=%zu\n", sizeof(Dash), sizeof(Heat));
  CHECK(sizeof(Dash) < 1500);
}

// ---------------------------------------------------------------------
static void test_heat_ink() {
  printf("heat ink\n");
  // Count red / black per 4x4 tile for every level.
  bool ok = true;
  for (int lv = 1; lv <= HEAT_LEVELS; lv++) {
    int red = 0, black = 0;
    for (int y = 0; y < 4; y++)
      for (int x = 0; x < 4; x++) {
        uint8_t c = heatInk(lv, x, y);
        red += c == 1;
        black += c == 2;
      }
    int wantRed = lv <= 8 ? lv * 2 : 16 - (lv - 8) * 2, wantBlack = lv <= 8 ? 0 : (lv - 8) * 2;
    if (red != wantRed || black != wantBlack) ok = false;
  }
  CHECK(ok);
  CHECK(heatInk(8, 5, 7) == 1);    // level 8: solid red
  // The pattern follows screen coordinates: a 4 px shift gives the same colour.
  CHECK(heatInk(3, 1, 2) == heatInk(3, 5, 6) && heatInk(10, 2, 3) == heatInk(10, 6, 11));
  // The grid fits the right column: 10 weeks x 7 days of 18 px plus the today triangle.
  CHECK(1 + HEAT_CAL_WEEKS * HEAT_CELL + 1 + 3 < 188 && 2 + 7 * HEAT_CELL <= 130);
}

struct Canvas {  // records the colour of every pixel; a guard border must stay untouched
  static const int W = 260, H = 40;
  uint8_t px[H][W];
  int outside = 0;  // writes outside the rectangle under test
  int x0 = 0, y0 = 0, w = 0, h = 0;
  Canvas() { memset(px, 255, sizeof(px)); }
  void put(int x, int y, diffuse::Color c) {
    if (x < x0 || y < y0 || x >= x0 + w || y >= y0 + h) outside++;
    if (x >= 0 && y >= 0 && x < W && y < H) px[y][x] = (uint8_t)c;
  }
  int count(diffuse::Color c) const {
    int n = 0;
    for (int y = y0; y < y0 + h; y++)
      for (int x = x0; x < x0 + w; x++) n += px[y][x] == c;
    return n;
  }
  int touched() const {
    int n = 0;
    for (int y = 0; y < H; y++)
      for (int x = 0; x < W; x++) n += px[y][x] != 255;
    return n;
  }
};

template <class T>
static Canvas run(int x0, int y0, int w, int h, bool allowRed, T target) {
  Canvas c;
  c.x0 = x0; c.y0 = y0; c.w = w; c.h = h;
  diffuse::rect(x0, y0, w, h, allowRed, target, [&](int x, int y, diffuse::Color col) { c.put(x, y, col); });
  return c;
}

static void test_diffuse() {
  printf("error diffusion\n");
  using namespace diffuse;
  auto flat = [](Px p) { return [p](int, int) { return p; }; };
  // Pure white / red / black targets give exactly that colour everywhere.
  Canvas w = run(5, 3, 32, 20, true, flat(fractions(256, 0, 0)));
  CHECK(w.count(WHITE) == 32 * 20 && w.outside == 0);
  Canvas k = run(5, 3, 32, 20, true, flat(fractions(0, 0, 256)));
  CHECK(k.count(BLACK) == 32 * 20);
  Canvas r = run(5, 3, 32, 20, true, flat(fractions(0, 256, 0)));
  CHECK(r.count(RED) == 32 * 20);
  // gradient() end points
  Canvas g0 = run(0, 0, 16, 16, true, flat(gradient(0)));
  CHECK(g0.count(WHITE) == 256);
  Canvas g1 = run(0, 0, 16, 16, true, flat(gradient(ONE)));
  CHECK(g1.count(BLACK) == 256);
  Canvas g2 = run(0, 0, 16, 16, true, flat(gradient(ONE / 2)));
  CHECK(g2.count(RED) == 256);
  // 50 % grey: about half black, half white, never red.
  Canvas gr = run(0, 0, 32, 32, true, flat(fractions(128, 0, 128)));
  int kb = gr.count(BLACK);
  CHECK(kb > 32 * 32 * 47 / 100 && kb < 32 * 32 * 53 / 100);
  CHECK(gr.count(RED) == 0);
  // 25 % black
  Canvas g25 = run(0, 0, 32, 32, true, flat(fractions(192, 0, 64)));
  CHECK(g25.count(BLACK) > 32 * 32 * 22 / 100 && g25.count(BLACK) < 32 * 32 * 28 / 100);
  // half white / half red: about half red, no black
  Canvas wr = run(0, 0, 32, 32, true, flat(fractions(128, 128, 0)));
  CHECK(wr.count(RED) > 32 * 32 * 42 / 100 && wr.count(RED) < 32 * 32 * 58 / 100);
  CHECK(wr.count(BLACK) < 32 * 32 * 6 / 100);
  // red and black half and half: mostly red and black, almost no white
  Canvas rk = run(0, 0, 32, 32, true, flat(fractions(0, 128, 128)));
  CHECK(rk.count(WHITE) < 32 * 32 * 8 / 100 && rk.count(RED) > 32 * 32 * 35 / 100 && rk.count(BLACK) > 32 * 32 * 35 / 100);
  // Darkness grows monotonically along the white -> red -> black gradient (average lightness).
  {
    int prev = 1 << 30;
    bool mono = true;
    for (int t = 0; t <= ONE; t += 32) {
      Canvas q = run(0, 0, 24, 24, true, flat(gradient(t)));
      int light = q.count(WHITE) * ONE + q.count(RED) * RED_LIGHT;
      if (light > prev) mono = false;
      prev = light;
    }
    CHECK(mono);
  }
  // Black / white screens: red is never produced; pure red becomes grey (lightness 0.30 -> about 70 % black).
  Canvas bw = run(0, 0, 32, 32, false, flat(fractions(0, 256, 0)));
  CHECK(bw.count(RED) == 0);
  CHECK(bw.count(BLACK) > 32 * 32 * 66 / 100 && bw.count(BLACK) < 32 * 32 * 74 / 100);
  Canvas bw2 = run(0, 0, 32, 32, false, flat(gradient(ONE * 3 / 8)));
  CHECK(bw2.count(RED) == 0 && bw2.count(WHITE) + bw2.count(BLACK) == 32 * 32);
  // Stays inside the rectangle: nothing is drawn outside, whatever the target.
  Canvas in = run(40, 10, 70, 15, true, [](int x, int y) { return gradient(((x * 7 + y * 13) % 257)); });
  CHECK(in.outside == 0);
  CHECK(in.touched() == 70 * 15);  // every pixel of the rectangle exactly once, nothing else
  // Skipped pixels are neither drawn nor disturb their neighbours.
  Canvas sk = run(0, 0, 32, 10, true, [](int x, int) { return x % 2 ? skipPx() : fractions(0, 0, 256); });
  CHECK(sk.count(BLACK) == 16 * 10 && sk.count(WHITE) == 0 && sk.count(RED) == 0);
  CHECK(sk.px[0][1] == 255);
  // Degenerate sizes do nothing; very wide rectangles are processed in strips (still exact).
  Canvas e0 = run(0, 0, 0, 5, true, flat(fractions(0, 0, 256)));
  CHECK(e0.touched() == 0);
  Canvas wide = run(0, 0, 250, 4, true, flat(fractions(128, 0, 128)));
  CHECK(wide.outside == 0 && wide.touched() == 250 * 4);
  CHECK(wide.count(BLACK) > 250 * 4 * 45 / 100 && wide.count(BLACK) < 250 * 4 * 55 / 100);
  // Deterministic: the same input gives the same pattern.
  Canvas a1 = run(3, 3, 20, 20, true, flat(gradient(70))), a2 = run(3, 3, 20, 20, true, flat(gradient(70)));
  CHECK(memcmp(a1.px, a2.px, sizeof(a1.px)) == 0);
  // Error buffer budget: two rows, two channels, int16.
  CHECK(sizeof(int16_t) * 2 * 2 * (MAXW + 2) < 2000);
}

static void test_dashed_and_trend() {
  printf("dashed outline / trend values\n");
  // Dashed outline: all four corners are black for every size, and it is a dashed (not solid) line.
  bool corners = true, dashed = true;
  for (int w = 3; w <= 16; w++)
    for (int h = 3; h <= 16; h++) {
      Gfx g;
      dither::dashedRect(g, 10, 10, w, h, 0);
      if (!(g.ink[10][10] && g.ink[10][10 + w - 1] && g.ink[10 + h - 1][10] && g.ink[10 + h - 1][10 + w - 1])) corners = false;
      int top = 0;
      for (int x = 0; x < w; x++) top += g.ink[10][10 + x];
      if (w >= 6 && (top >= w || top < (w + 1) / 2 - 1)) dashed = false;
      for (int y = 1; y < h - 1; y++)  // inside is empty
        for (int x = 1; x < w - 1; x++)
          if (g.ink[10 + y][10 + x]) dashed = false;
    }
  CHECK(corners);
  CHECK(dashed);
  // Sizes used on screen: 8 .. 13 px cells.
  for (int side = 6; side <= 13; side++) {
    Gfx g;
    dither::dashedRect(g, 0, 0, side, side, 0);
    CHECK(g.ink[0][0] && g.ink[0][side - 1] && g.ink[side - 1][0] && g.ink[side - 1][side - 1]);
  }
  CHECK(dither::dashOn(0, 10) && dither::dashOn(9, 10) && !dither::dashOn(1, 10) && !dither::dashOn(10, 10));

  // Trend on the calendar's time axis: one value per shown day, unknown = 0, no averaging.
  uint8_t t[HEAT_MAX];
  for (int i = 0; i < HEAT_MAX; i++) t[i] = UNKNOWN;
  float out[HEAT_MAX];
  CHECK(trendValues(t, 0, 0, out) == 0);
  CHECK(trendValues(t, 5, 5, out) == 0);
  // All unknown -> all 0
  int m = trendValues(t, 0, 35, out);
  bool zero = m == 35;
  for (int i = 0; i < m; i++) zero = zero && out[i] == 0.0f;
  CHECK(zero);
  // A spike stays a spike at its full height; its neighbours are not lifted.
  t[10] = 30;
  m = trendValues(t, 0, 35, out);
  CHECK(m == 35 && out[10] == 30.0f && out[9] == 0.0f && out[11] == 0.0f);
  // Peak yesterday, little today: today is not pulled up by yesterday.
  t[33] = 63;
  t[34] = 4;
  m = trendValues(t, 0, 35, out);
  CHECK(out[33] == 63.0f && out[34] == 4.0f);
  // `from` selects the days shown (the same days as the calendar): the first days are ignored entirely.
  m = trendValues(t, 7, 35, out);
  CHECK(m == 28);
  CHECK(out[3] == 30.0f);  // day 10 is now index 3
  m = trendValues(t, 34, 35, out);
  CHECK(m == 1 && out[0] == 4.0f);
  // Full tolerated window
  m = trendValues(t, 0, 112, out);
  CHECK(m == 112);

  // Split time axis: the last 15 day steps take the right half, the earlier days the left half.
  const int W = 180;
  CHECK(trendSplit(67) == 51 && trendSplit(16) == 0 && trendSplit(17) == 1 && trendSplit(3) == 0);
  CHECK(trendX(0, 67, W) == 0.0f && trendX(66, 67, W) == (float)(W - 1));
  CHECK(trendX(51, 67, W) == (float)(W / 2));  // 15 days before today sits in the middle
  bool inc = true;
  for (int k = 1; k < 67; k++) inc = inc && trendX(k, 67, W) > trendX(k - 1, 67, W);
  CHECK(inc);
  // Every step in the right half is equally wide, the step into it included, and wider than the left ones.
  float step = trendX(66, 67, W) - trendX(65, 67, W);
  CHECK(trendX(52, 67, W) - trendX(51, 67, W) == step);
  CHECK(step > 3 * (trendX(1, 67, W) - trendX(0, 67, W)));
  // 16 days or fewer: plain linear over the full width; a single value sits in the middle.
  CHECK(trendX(0, 16, W) == 0.0f && trendX(15, 16, W) == (float)(W - 1));
  CHECK(trendX(0, 15, W) == 0.0f && trendX(14, 15, W) == (float)(W - 1) && trendX(7, 15, W) == (float)(W - 1) / 2.0f);
  CHECK(trendX(0, 1, W) == (float)(W - 1) / 2.0f);
  // 17 days: the first day at the left edge, the second in the middle, then 15 steps to the right edge.
  CHECK(trendX(0, 17, W) == 0.0f && trendX(1, 17, W) == (float)(W / 2) && trendX(16, 17, W) == (float)(W - 1));

  // White + red only (heat fill): no black pixels, every level; levels get denser (more red).
  using namespace diffuse;
  int prevRed = -1;
  bool nb = true, mono = true;
  for (int level = 1; level <= 5; level++) {
    Px tg = whiteToRed(level >= 5 ? ONE : level * 51);
    Canvas q = run(0, 0, 8, 8, true, [tg](int, int) { return tg; });
    Canvas q2;
    q2.x0 = 0; q2.y0 = 0; q2.w = 8; q2.h = 8;
    diffuse::rect(0, 0, 8, 8, true, [tg](int, int) { return tg; }, [&](int px, int py, Color c) { q2.put(px, py, c); }, false);
    if (q2.count(BLACK) != 0) nb = false;
    int red = q2.count(RED);
    if (red <= prevRed) mono = false;
    prevRed = red;
    if (level == 5 && red != 64) mono = false;  // solid red
    (void)q;
  }
  CHECK(nb);
  CHECK(mono);
  // allowBlack = false is ignored on a B/W screen (it needs black for grey).
  Canvas g0;
  g0.x0 = 0; g0.y0 = 0; g0.w = 16; g0.h = 16;
  diffuse::rect(0, 0, 16, 16, false, [](int, int) { return fractions(128, 0, 128); }, [&](int px, int py, Color c) { g0.put(px, py, c); }, false);
  CHECK(g0.count(BLACK) > 100 && g0.count(BLACK) < 156);
}

// ---------------------------------------------------------------------
// Device commands (WiFi provisioning), `kind` reporting, snapshot flush policy
// ---------------------------------------------------------------------
static void test_commands() {
  printf("handleLine device commands + kind\n");
  ViewState v;
  feed(v, DASH);
  feed(v, QUOTA);
  std::string before = v.signature().c_str();

  struct {
    const char* type;
    Cmd cmd;
  } simple[] = {{"wifi_scan", CMD_WIFI_SCAN},
                {"wifi_status", CMD_WIFI_STATUS},
                {"wifi_clear", CMD_WIFI_CLEAR},
                {"reset_settings", CMD_RESET_SETTINGS}};
  for (auto& s : simple) {
    HandleResult r = feed(v, std::string("{\"v\":2,\"type\":\"") + s.type + "\"}");
    CHECK(r.ok && r.cmd == s.cmd && r.kind == KIND_NONE && !r.forceFull && !r.blank);
    // v1 or no version: refused
    r = feed(v, std::string("{\"v\":1,\"type\":\"") + s.type + "\"}");
    CHECK(!r.ok && strcmp(r.err, "bad_version") == 0 && r.cmd == CMD_NONE);
    r = feed(v, std::string("{\"type\":\"") + s.type + "\"}");
    CHECK(!r.ok && strcmp(r.err, "bad_version") == 0);
  }

  HandleResult r = feed(v, "{\"v\":2,\"type\":\"wifi_set\",\"ssid\":\"Home-2.4G\",\"pass\":\"secret pass\"}");
  CHECK(r.ok && r.cmd == CMD_WIFI_SET);
  CHECK_STR(r.ssid, "Home-2.4G");
  CHECK_STR(r.pass, "secret pass");
  // open network: empty or missing pass
  r = feed(v, "{\"v\":2,\"type\":\"wifi_set\",\"ssid\":\"Cafe\",\"pass\":\"\"}");
  CHECK(r.ok && r.cmd == CMD_WIFI_SET);
  CHECK_STR(r.pass, "");
  r = feed(v, "{\"v\":2,\"type\":\"wifi_set\",\"ssid\":\"Cafe\"}");
  CHECK(r.ok && r.cmd == CMD_WIFI_SET);
  CHECK_STR(r.ssid, "Cafe");
  CHECK_STR(r.pass, "");
  // UTF-8 SSID and JSON escapes survive; limits are in bytes
  r = feed(v, "{\"v\":2,\"type\":\"wifi_set\",\"ssid\":\"\xE5\xAE\xB6\\\"\xE7\xBD\x91\",\"pass\":\"p\\\\w\"}");
  CHECK(r.ok);
  CHECK_STR(r.ssid, "\xE5\xAE\xB6\"\xE7\xBD\x91");
  CHECK_STR(r.pass, "p\\w");
  r = feed(v, "{\"v\":2,\"type\":\"wifi_set\",\"ssid\":\"" + rep('s', 32) + "\",\"pass\":\"" + rep('p', 63) + "\"}");
  CHECK(r.ok && strlen(r.ssid) == 32 && strlen(r.pass) == 63);
  // 11 three-byte characters = 33 bytes: too long although only 11 characters
  std::string cjk33;
  for (int i = 0; i < 11; i++) cjk33 += "\xE5\xAE\xB6";
  r = feed(v, "{\"v\":2,\"type\":\"wifi_set\",\"ssid\":\"" + cjk33 + "\"}");
  CHECK(!r.ok && strcmp(r.err, "bad_field ssid") == 0);

  // validation errors; nothing half-parsed is handed back
  struct {
    const char* line;
    const char* err;
  } bad[] = {
      {"{\"v\":2,\"type\":\"wifi_set\"}", "missing_field ssid"},
      {"{\"v\":2,\"type\":\"wifi_set\",\"ssid\":null}", "missing_field ssid"},
      {"{\"v\":2,\"type\":\"wifi_set\",\"ssid\":\"\"}", "bad_field ssid"},
      {"{\"v\":2,\"type\":\"wifi_set\",\"ssid\":5}", "bad_field ssid"},
      {"{\"v\":2,\"type\":\"wifi_set\",\"ssid\":\"a\\u0000b\"}", "bad_field ssid"},
      {"{\"v\":2,\"type\":\"wifi_set\",\"ssid\":\"ok\",\"pass\":12345678}", "bad_field pass"},
      {"{\"v\":2,\"type\":\"wifi_set\",\"ssid\":\"ok\",\"pass\":\"short\\u0000x\"}", "bad_field pass"},
  };
  for (auto& b : bad) {
    r = feed(v, b.line);
    CHECK(!r.ok && r.cmd == CMD_NONE && strcmp(r.err, b.err) == 0);
    if (strcmp(r.err, b.err) != 0) printf("    got \"%s\" for %s\n", r.err, b.line);
    CHECK(r.ssid[0] == '\0' && r.pass[0] == '\0');
  }
  r = feed(v, "{\"v\":2,\"type\":\"wifi_set\",\"ssid\":\"" + rep('s', 33) + "\",\"pass\":\"hunter2\"}");
  CHECK(!r.ok && strcmp(r.err, "bad_field ssid") == 0 && r.ssid[0] == '\0');
  r = feed(v, "{\"v\":2,\"type\":\"wifi_set\",\"ssid\":\"ok\",\"pass\":\"" + rep('p', 64) + "\"}");
  CHECK(!r.ok && strcmp(r.err, "bad_field pass") == 0 && r.ssid[0] == '\0' && r.pass[0] == '\0');
  CHECK(strstr(r.err, "ppp") == nullptr);  // the password is never in an error text

  // pair / ota_begin
  r = feed(v, "{\"v\":2,\"type\":\"pair\"}");
  CHECK(r.ok && r.cmd == CMD_PAIR);
  r = feed(v, "{\"v\":1,\"type\":\"pair\"}");
  CHECK(!r.ok && strcmp(r.err, "bad_version") == 0);
  const std::string MD5U = "0123456789ABCDEF0123456789abcdef";
  r = feed(v, "{\"v\":2,\"type\":\"ota_begin\",\"size\":889953,\"md5\":\"" + MD5U + "\",\"token\":\"a1b2c3d4e5f60718\"}");
  CHECK(r.ok && r.cmd == CMD_OTA_BEGIN && r.otaSize == 889953);
  CHECK_STR(r.md5, "0123456789abcdef0123456789abcdef");  // lowercased for Update.setMD5
  CHECK_STR(r.token, "a1b2c3d4e5f60718");
  r = feed(v, "{\"v\":2,\"type\":\"ota_begin\",\"size\":16777215,\"md5\":\"" + MD5U + "\",\"token\":\"" + rep('t', 32) + "\"}");
  CHECK(r.ok && r.otaSize == 16777215 && strlen(r.token) == 32);
  const std::string M = "\"md5\":\"" + MD5U + "\"";
  struct {
    std::string line;
    const char* err;
  } badOta[] = {
      {"{\"v\":1,\"type\":\"ota_begin\",\"size\":5," + M + ",\"token\":\"t\"}", "bad_version"},
      {"{\"v\":2,\"type\":\"ota_begin\"," + M + ",\"token\":\"t\"}", "missing_field size"},
      {"{\"v\":2,\"type\":\"ota_begin\",\"size\":0," + M + ",\"token\":\"t\"}", "bad_field size"},
      {"{\"v\":2,\"type\":\"ota_begin\",\"size\":-5," + M + ",\"token\":\"t\"}", "bad_field size"},
      {"{\"v\":2,\"type\":\"ota_begin\",\"size\":1.5," + M + ",\"token\":\"t\"}", "bad_field size"},
      {"{\"v\":2,\"type\":\"ota_begin\",\"size\":\"5\"," + M + ",\"token\":\"t\"}", "bad_field size"},
      {"{\"v\":2,\"type\":\"ota_begin\",\"size\":16777216," + M + ",\"token\":\"t\"}", "bad_field size"},
      {"{\"v\":2,\"type\":\"ota_begin\",\"size\":5,\"token\":\"t\"}", "missing_field md5"},
      {"{\"v\":2,\"type\":\"ota_begin\",\"size\":5,\"md5\":\"abc\",\"token\":\"t\"}", "bad_field md5"},
      {"{\"v\":2,\"type\":\"ota_begin\",\"size\":5,\"md5\":\"" + rep('g', 32) + "\",\"token\":\"t\"}", "bad_field md5"},
      {"{\"v\":2,\"type\":\"ota_begin\",\"size\":5," + M + "}", "missing_field token"},
      {"{\"v\":2,\"type\":\"ota_begin\",\"size\":5," + M + ",\"token\":\"\"}", "bad_field token"},
      {"{\"v\":2,\"type\":\"ota_begin\",\"size\":5," + M + ",\"token\":\"" + rep('t', 33) + "\"}", "bad_field token"},
  };
  for (auto& b : badOta) {
    r = feed(v, b.line);
    CHECK(!r.ok && r.cmd == CMD_NONE && strcmp(r.err, b.err) == 0);
    if (strcmp(r.err, b.err) != 0) printf("    got \"%s\" for %s\n", r.err, b.line.c_str());
    CHECK(r.otaSize == 0 && r.md5[0] == '\0' && r.token[0] == '\0');
  }

  // commands never touch the view
  CHECK(std::string(v.signature().c_str()) == before);

  // `kind` is reported for every accepted data block, and only for those
  CHECK(feed(v, DASH).kind == KIND_DASH);
  CHECK(feed(v, QUOTA).kind == KIND_QUOTA);
  CHECK(feed(v, MODELS).kind == KIND_MODELS);
  CHECK(feed(v, heatMsg(sampleHeatD(), sampleHeatT())).kind == KIND_HEAT);
  CHECK(feed(v, MOUSE).kind == KIND_MOUSE);
  CHECK(feed(v, "{\"v\":2,\"type\":\"ping\"}").kind == KIND_NONE);
  CHECK(feed(v, "{\"v\":2,\"type\":\"full_refresh\"}").kind == KIND_NONE);
  CHECK(feed(v, "{\"v\":1,\"type\":\"stats\",\"cpu\":5}").kind == KIND_NONE);
  CHECK(feed(v, "{\"v\":2,\"type\":\"quota\",\"p\":5}").kind == KIND_NONE);  // rejected: nothing to store
  CHECK(feed(v, "{\"v\":2,\"type\":\"mouse\",\"st\":\"na\"}").kind == KIND_MOUSE);  // valid, na is still stored
  HandleResult blank = feed(v, "");
  CHECK(blank.blank && blank.kind == KIND_NONE && blank.cmd == CMD_NONE);
}

static void test_snap_flush_policy() {
  printf("SnapFlushPolicy\n");
  const uint32_t GAP = 600000UL;
  // Normal boot (something already saved): the gap counts from boot.
  SnapFlushPolicy p(GAP);
  p.begin(1000, false);
  CHECK(!p.dirty() && !p.due(1000 + GAP + 5));  // nothing changed: never due
  p.markDirty();
  CHECK(p.dirty());
  CHECK(!p.due(1000));
  CHECK(!p.due(1000 + GAP - 1));  // changed but too soon
  CHECK(p.due(1000 + GAP));       // changed and gap elapsed
  p.noteFlushed(1000 + GAP + 20);
  CHECK(!p.dirty() && !p.due(1000 + 10 * GAP));  // flushed: clean again
  p.markDirty();
  CHECK(!p.due(1000 + GAP + 20 + GAP - 1));
  CHECK(p.due(1000 + GAP + 20 + GAP));  // gap counts from the last flush
  // A failed flush keeps the data dirty but waits another gap.
  p.noteFlushed(5000000, true);
  CHECK(p.dirty() && !p.due(5000000 + GAP - 1) && p.due(5000000 + GAP));

  // Empty store: the first write is not delayed, the next ones are.
  SnapFlushPolicy q(GAP);
  q.begin(2000, true);
  CHECK(!q.due(2000));  // clean
  q.markDirty();
  CHECK(q.due(2000));  // immediately
  q.noteFlushed(2500);
  q.markDirty();
  CHECK(!q.due(2500 + GAP - 1) && q.due(2500 + GAP));

  // millis() wraps after 49 days: still correct
  SnapFlushPolicy w(GAP);
  w.begin(0xFFFFFFFFUL - 1000, false);
  w.markDirty();
  CHECK(!w.due(5000));  // 6 s after boot, counter wrapped
  CHECK(!w.due((uint32_t)(0xFFFFFFFFUL - 1000 + GAP - 1)));
  CHECK(w.due((uint32_t)(0xFFFFFFFFUL - 1000 + GAP)));
  w.noteFlushed(0xFFFFFFF0UL);
  w.markDirty();
  CHECK(!w.due(100) && w.due((uint32_t)(0xFFFFFFF0UL + GAP)));

  // restart() (factory reset): pending changes dropped, gap restarts.
  SnapFlushPolicy r(GAP);
  r.begin(0, true);
  r.markDirty();
  r.restart(7000);
  CHECK(!r.dirty() && !r.due(7000 + GAP + 1));
  r.markDirty();
  CHECK(!r.due(7000 + GAP - 1) && r.due(7000 + GAP));
}

int main() {
  test_dither();
  test_strings();
  test_v1();
  test_v2();
  test_v2_errors();
  test_signature();
  test_commands();
  test_snap_flush_policy();
  test_heat_ink();
  test_diffuse();
  test_dashed_and_trend();
  printf("\n%d checks, %d failed\n", checks, failures);
  return failures ? 1 : 0;
}
