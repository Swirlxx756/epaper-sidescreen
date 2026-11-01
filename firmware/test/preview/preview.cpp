// Offline preview of the dashboard v2 screen: compiles the REAL drawing code
// (src/dashboard_v2.cpp) with Adafruit_GFX and U8g2_for_Adafruit_GFX from the
// PlatformIO library folder, feeds example protocol lines through the REAL
// handleLine, and writes the result as PPM images (white / black / red).
// Build and run: bash test/preview/run.sh   (from firmware/)
//
// This shows what the drawing code produces; it is not a photo of the panel
// (e-paper contrast, dither looks and red-vs-yellow need the real screen).

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <string>
#include <vector>

#include "preview_canvas.h"

// ---------------------------------------------------------------------
// Example data
// ---------------------------------------------------------------------
// 5 weeks: start 2026-09-28 (a Monday) .. 2026-10-28 = 31 days. First 9 unknown, then 22 days with activity.
static std::string heatD() { return std::string(9, '-') + "0012345431205423551234"; }
static std::string heatT() {
  const char* alpha = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  int h[22] = {2, 5, 14, 30, 41, 55, 36, 22, 9, 4, 18, 33, 50, 63, 47, 28, 12, 20, 44, 58, 39, 52};
  std::string s(9, '-');
  for (int i = 0; i < 22; i++) s += (i == 8) ? '-' : alpha[h[i]];
  return s;
}
static std::string heatMsg(const char* st = "ok") {
  return std::string("{\"v\":2,\"type\":\"heat\",\"st\":\"") + st +
         "\",\"start\":\"2026-09-28\",\"end\":\"2026-10-28\",\"d\":\"" + heatD() + "\",\"t\":\"" + heatT() +
         "\",\"peak\":\"1.41B\",\"active\":21}";
}
// All days known, every level used. `days` > 35: the calendar shows only the last 5 weeks.
static std::string heatFullMsg(int days = 35, const char* start = "2026-09-28") {
  std::string d, t;
  const char* alpha = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  for (int i = 0; i < days; i++) {
    { int lv = (i * 7 + i / 5) % 13; d += lv < 10 ? (char)('0' + lv) : (char)('A' + lv - 10); }
    t += alpha[(i * 11 + 3) % 64];
  }
  return std::string("{\"v\":2,\"type\":\"heat\",\"start\":\"") + start + "\",\"d\":\"" + d + "\",\"t\":\"" + t +
         "\",\"peak\":\"1.30B\",\"active\":" + std::to_string(days) + "}";
}

// The same heat message with `view`:"cal" (calendar style).
static std::string calView(std::string m) { return m.insert(m.find("\"type\":\"heat\",") + 14, "\"view\":\"cal\","); }

static std::string dashMsg(const char* st = "ok", const char* tok = "1,430,736,150") {
  return std::string("{\"v\":2,\"type\":\"dash\",\"st\":\"") + st + "\",\"time\":\"10-28 20:20\",\"main\":{\"l\":\"TODAY\",\"tok\":\"" +
         tok + "\",\"cost\":\"$363.69\"},\"alt\":{\"l\":\"TOTAL\",\"tok\":\"7.43B\"}}";
}
static std::string quotaMsg(int claudeWk = 60, int codex5h = 53, const char* st = "ok", int argon = 0) {
  char b[1200];
  snprintf(b, sizeof(b),
           "{\"v\":2,\"type\":\"quota\",\"st\":\"%s\",\"p\":["
           "{\"n\":\"Claude\",\"plan\":\"Max 5x\",\"st\":\"ok\",\"i\":[{\"l\":\"5h\",\"left\":83,\"rst\":\"4h29m\"},"
           "{\"l\":\"Wk\",\"left\":%d,\"rst\":\"5d7h\"}]},"
           "{\"n\":\"Codex\",\"plan\":\"Plus\",\"i\":[{\"l\":\"5h\",\"left\":%d,\"rst\":\"1h55m\"},"
           "{\"l\":\"Wk\",\"left\":93,\"rst\":\"6d20h\"}]},"
           "{\"n\":\"Antigrav\",\"plan\":\"Pro\",\"i\":[{\"l\":\"5h\",\"left\":100,\"rst\":\"4h59m\"},"
           "{\"l\":\"Wk\",\"left\":97,\"rst\":\"1d20h\"}],\"txt\":[{\"s\":\"4 Argon %d%%\",\"w\":%d},{\"s\":\"C/G 72%%/64%%\"}]},"
           "{\"n\":\"DeepSeek\",\"plan\":\"PAYG\",\"bal\":\"\xC2\xA5"
           "38.66\"}]}",
           st, claudeWk, codex5h, argon, argon <= 10 ? 1 : 0);
  return b;
}
static std::string modelsMsg(const char* st = "ok", const char* scope = "TODAY") {
  return std::string("{\"v\":2,\"type\":\"models\",\"st\":\"") + st + "\",\"scope\":\"" + scope +
         "\",\"m\":[[\"claude-opus-5-5\",\"750M\",52],[\"claude-sonnet-5-5\",\"403M\",28],[\"gpt-6.1-sol\",\"153M\",10],"
         "[\"deepseek-flash\",\"62M\",4],[\"anthropic/claude-haiku-5-5-20260301\",\"31M\",2]]}";
}
static std::string mouseMsg(const char* body) { return std::string("{\"v\":2,\"type\":\"mouse\",") + body + "}"; }

struct Scenario {
  const char* name;
  std::vector<std::string> lines;
  bool online;
  bool tri;  // false: B/W panel (accent = black)
};

static void feed(ViewState& v, const std::string& line) {
  HandleResult r = handleLine(v, line.c_str(), line.size());
  if (!r.ok) {
    fprintf(stderr, "preview: handleLine failed (%s) for %s\n", r.err, line.c_str());
    exit(2);
  }
}

static bool render(const ViewState& v, bool tri, const std::string& path, int scale) {
  PlaneCanvas canvas;
  drawView(canvas, v, tri);
  FILE* f = fopen(path.c_str(), "wb");
  bool ok = f && writePpm(canvas, f, scale);
  if (f) ok = fclose(f) == 0 && ok;
  if (!ok) {
    fprintf(stderr, "cannot write %s\n", path.c_str());
    return false;
  }
  printf("wrote %s\n", path.c_str());
  return true;
}

// Self-check of the quota alert drawing (runs on every preview build): red only on alert, clean
// solid bars (no dither in the used part), red text pixels solid.
static int countRed(const PlaneCanvas& c, int x0, int y0, int x1, int y1) {
  int n = 0;
  for (int y = y0; y < y1; y++)
    for (int x = x0; x < x1; x++) n += c.planes.get(x, y) == TRI_COLOR;
  return n;
}
static bool selfCheck() {
  auto draw = [](std::vector<std::string> lines, PlaneCanvas& canvas) {
    ViewState v;
    for (const std::string& l : lines) feed(v, l);
    v.online = true;
    U8G2_FOR_ADAFRUIT_GFX u8g2;
    canvas.fillScreen(0xFFFF);
    u8g2.begin(canvas);
    dashv2::draw(canvas, u8g2, v.d, v.online, dashv2::Style{GXEPD_RED, true});
  };
  bool ok = true;
  // No alert: the left column (quota) has no red at all.
  PlaneCanvas calm;
  draw({dashMsg(), quotaMsg(60, 53, "ok", 50), modelsMsg(), heatMsg(), mouseMsg("\"st\":\"ok\",\"pct\":68")}, calm);
  if (countRed(calm, 0, 90, 199, 300) != 0) {
    fprintf(stderr, "selfCheck: red in the quota block without an alert\n");
    ok = false;
  }
  // The used part of a bar is plain white: the Codex 5h bar (53 % left) has only black outline / fill, no dither.
  // Find the bar row by scanning for the long black top edge of a 66 px wide bar and check its inside.
  // Bar x range: barX = 6 + 28 = 34 .. 99; inner right part (x 80..98) of the second item row must be white.
  int whiteRows = 0;
  for (int y = 90; y < 260; y++) {
    // a bar row: pixel (34, y) black and (99, y) black (outline) and the inner right end white
    if (calm.planes.get(34, y) == TRI_BLACK && calm.planes.get(99, y) == TRI_BLACK && calm.planes.get(95, y) == TRI_WHITE &&
        calm.planes.get(90, y) == TRI_WHITE)
      whiteRows++;
  }
  if (whiteRows < 4) {  // 4 bars with a white used part (Claude 83/60 are not... at least the 53 % and 60 % ones)
    fprintf(stderr, "selfCheck: used part of the bars is not plain white (%d rows)\n", whiteRows);
    ok = false;
  }
  // Alert: Claude Wk 8 % and Codex 5h 3 % bars, percentage text and the Argon text are red.
  PlaneCanvas alarm;
  draw({dashMsg(), quotaMsg(8, 3, "ok", 0), modelsMsg(), heatMsg(), mouseMsg("\"st\":\"ok\",\"pct\":68")}, alarm);
  int redBars = countRed(alarm, 34, 90, 100, 200);    // bar outlines + remaining parts
  int redPct = countRed(alarm, 100, 90, 135, 200);    // percentage text
  int redTxt = countRed(alarm, 6, 212, 70, 225);     // the "4 Argon 0%" segment
  if (redBars < 100 || redPct < 15 || redTxt < 20) {
    fprintf(stderr, "selfCheck: alert not red enough (bars %d, pct %d, txt %d)\n", redBars, redPct, redTxt);
    ok = false;
  }
  // Red text is solid: the "4 Argon 0%" segment has no black pixels (it is not an inverted block).
  int black = 0;
  for (int y = 212; y < 225; y++)
    for (int x = 6; x < 70; x++) black += alarm.planes.get(x, y) == TRI_BLACK;
  if (black != 0) {
    fprintf(stderr, "selfCheck: the red warn segment has %d black pixels\n", black);
    ok = false;
  }
  return ok;
}

int main(int argc, char** argv) {
  // preview.exe [outDir] [scale] [lines-file ...]
  // Every lines-file (one protocol message per line, e.g. `epaper_host.py --dry-run-v2` output; lines that
  // are not JSON objects are skipped) is rendered as <outDir>/<file name without extension>.ppm.
  std::string outDir = argc > 1 ? argv[1] : "test/preview/out";
  int scale = argc > 2 ? atoi(argv[2]) : 1;
  if (scale < 1) scale = 1;

  std::vector<Scenario> sc;
  const char* okMouse = "\"st\":\"ok\",\"pct\":68,\"chg\":false,\"at\":\"19:39\"";
  sc.push_back({"normal", {dashMsg(), quotaMsg(), modelsMsg(), heatMsg(), mouseMsg(okMouse)}, true, true});
  sc.push_back({"normal_bw", sc.back().lines, true, false});
  {
    std::vector<std::string> l = {dashMsg(), quotaMsg(), modelsMsg(), heatFullMsg(67, "2026-08-23"), mouseMsg(okMouse)};
    sc.push_back({"heat67", l, true, true});
    std::vector<std::string> cal = l;
    cal[3] = calView(cal[3]);
    sc.push_back({"calendar", cal, true, true});
    sc.push_back({"calendar_bw", cal, true, false});
    // August 2026 starts on a Saturday: 6 rows.
    std::vector<std::string> aug = {dashMsg(), quotaMsg(), modelsMsg(), calView(heatFullMsg(69, "2026-06-21")), mouseMsg(okMouse)};
    aug[0].replace(aug[0].find("10-28"), 5, "08-28");
    sc.push_back({"calendar_6rows", aug, true, true});
  }
  sc.push_back({"full_5w", {dashMsg(), quotaMsg(), modelsMsg("ok", "TOTAL"), heatFullMsg(35), mouseMsg(okMouse)}, true, true});
  sc.push_back({"short_window", {dashMsg(), quotaMsg(), modelsMsg(), heatFullMsg(12), mouseMsg(okMouse)}, true, true});
  sc.push_back({"long_window", {dashMsg(), quotaMsg(), modelsMsg(), heatFullMsg(101, "2026-07-20"), mouseMsg(okMouse)}, true, true});
  sc.push_back({"alert",
                {dashMsg(), quotaMsg(8, 3, "ok", 0), modelsMsg(), heatMsg(), mouseMsg("\"st\":\"ok\",\"pct\":12,\"chg\":false,\"at\":\"19:39\"")},
                false,  // offline icon too
                true});
  sc.push_back({"stale_off",
                {dashMsg("stale"), quotaMsg(60, 53, "stale", 5), modelsMsg("stale"), heatMsg("stale"),
                 mouseMsg("\"st\":\"off\",\"pct\":null")},
                true, true});
  sc.push_back({"stale_mouse",
                {dashMsg(), quotaMsg(), modelsMsg(), heatMsg(), mouseMsg("\"st\":\"stale\",\"pct\":41,\"at\":\"13:40\"")},
                true, true});
  sc.push_back({"charging",
                {dashMsg(), quotaMsg(), modelsMsg(), heatMsg(), mouseMsg("\"st\":\"ok\",\"pct\":100,\"chg\":true,\"at\":\"19:39\"")},
                true, true});
  sc.push_back({"na_all",
                {dashMsg("na"), "{\"v\":2,\"type\":\"quota\",\"st\":\"na\"}", "{\"v\":2,\"type\":\"models\",\"st\":\"na\"}",
                 "{\"v\":2,\"type\":\"heat\",\"st\":\"na\"}", mouseMsg("\"st\":\"na\"")},
                true, true});
  sc.push_back({"partial", {dashMsg()}, false, true});  // only dash received, the rest NO DATA
  // Font fallback for the big number: 10 / 13 / 15 digits.
  sc.push_back({"bignum_short", {dashMsg("ok", "86,420"), quotaMsg(), modelsMsg(), heatMsg(), mouseMsg(okMouse)}, true, true});
  sc.push_back({"bignum_long", {dashMsg("ok", "12,430,736,150,999"), quotaMsg(), modelsMsg(), heatMsg(), mouseMsg(okMouse)}, true, true});
  // Old-format dash (no main / alt): must not crash, shows "--".
  sc.push_back({"old_dash",
                {"{\"v\":2,\"type\":\"dash\",\"time\":\"10-28 19:40\",\"total\":{\"tok\":\"7.13B\",\"cost\":\"$2,292.70\"},"
                 "\"today\":{\"tok\":\"1.19B\",\"cost\":\"$311.23\"},\"tm\":\"TM 19:39\"}",
                 quotaMsg(), modelsMsg(), heatMsg(), mouseMsg(okMouse)},
                true, true});
  // Worst case for the quota block: 4 providers, 3 items each, long text lines.
  sc.push_back({"stress",
                {dashMsg(),
                 "{\"v\":2,\"type\":\"quota\",\"p\":["
                 "{\"n\":\"Claude\",\"plan\":\"Max 20x\",\"i\":[{\"l\":\"5h\",\"left\":100,\"rst\":\"4h59m\"},"
                 "{\"l\":\"Wk\",\"left\":0,\"rst\":\"6d23h\"},{\"l\":\"Opus\",\"left\":10,\"rst\":\"6d23h\"}]},"
                 "{\"n\":\"Codex\",\"plan\":\"Pro\",\"st\":\"stale\",\"i\":[{\"l\":\"5h\",\"left\":9,\"rst\":\"2h46m\"},"
                 "{\"l\":\"Wk\",\"left\":null},{\"l\":\"Spk\",\"left\":55,\"rst\":\"1d1h\"}]},"
                 "{\"n\":\"Antigrav\",\"plan\":\"Ultra\",\"i\":[{\"l\":\"5h\",\"left\":100,\"rst\":\"4h59m\"},"
                 "{\"l\":\"Wk\",\"left\":72,\"rst\":\"4h44m\"}],\"txt\":[{\"s\":\"C/G 72%/64%\"},{\"s\":\"4 Argon 0%\",\"w\":1},{\"s\":\"x\"}]},"
                 "{\"n\":\"DeepSeek\",\"plan\":\"PAYG\",\"bal\":\"\xC2\xA5"
                 "1,038.66\"}]}",
                 modelsMsg(), heatFullMsg(35), mouseMsg("\"st\":\"ok\",\"pct\":100,\"at\":\"19:39\"")},
                true, true});

  for (int i = 3; i < argc; i++) {
    FILE* in = fopen(argv[i], "rb");
    if (!in) {
      fprintf(stderr, "cannot read %s\n", argv[i]);
      return 1;
    }
    std::vector<std::string> lines;
    char buf[2048];
    while (fgets(buf, sizeof buf, in)) {
      std::string l(buf);
      while (!l.empty() && (l.back() == '\n' || l.back() == '\r')) l.pop_back();
      if (!l.empty() && l[0] == '{') lines.push_back(l);
    }
    fclose(in);
    std::string name = argv[i];  // keep the string alive: Scenario stores a pointer
    size_t slash = name.find_last_of("/\\");
    if (slash != std::string::npos) name = name.substr(slash + 1);
    size_t dot = name.find_last_of('.');
    if (dot != std::string::npos) name = name.substr(0, dot);
    static std::vector<std::string> names;
    names.reserve(argc);
    names.push_back(name);
    sc.push_back({names.back().c_str(), lines, true, true});
  }

  int fail = selfCheck() ? 0 : 1;
  for (const Scenario& s : sc) {
    ViewState v;
    for (const std::string& l : s.lines) feed(v, l);
    v.online = s.online;
    if (!render(v, s.tri, outDir + "/" + s.name + ".ppm", scale)) fail++;
  }

  // Everything missing: v2 screen with no block received at all.
  ViewState empty;
  empty.hasData = true;
  empty.v2 = true;
  empty.online = false;
  if (!render(empty, true, outDir + "/missing.ppm", scale)) fail++;
  return fail ? 1 : 0;
}
