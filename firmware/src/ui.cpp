#include "ui.h"

#include <GxEPD2.h>  // colour constants
#include <SPI.h>
#include <U8g2_for_Adafruit_GFX.h>

#include "config.h"
#include "dashboard_v2.h"
#include "weather_page_b.h"
#if ENABLE_WIFI
#include <stdlib.h>
#include "img_rx.h"
#include "todo_store.h"
#endif

// ---------------------------------------------------------------------
// Panel driver selection (see config.h)
// ---------------------------------------------------------------------
#if EPD_PANEL == PANEL_WS42_TRI
#define EPD_TRICOLOR 1
#define EPD_NAME "WS42TRI"
#elif EPD_PANEL == PANEL_GDEY042T81
#define EPD_DRIVER GxEPD2_420_GDEY042T81
#define EPD_ROTATION 0  // 400x300
#define EPD_NAME "GDEY042T81"
#elif EPD_PANEL == PANEL_GDEY029T94
#define EPD_DRIVER GxEPD2_290_GDEY029T94
#define EPD_ROTATION 1  // native 128x296 -> landscape 296x128
#define EPD_NAME "GDEY029T94"
#elif EPD_PANEL == PANEL_GDEY075T7
#define EPD_DRIVER GxEPD2_750_GDEY075T7
#define EPD_ROTATION 0  // 800x480
#define EPD_NAME "GDEY075T7"
#else
#error "Unsupported EPD_PANEL, see include/config.h"
#endif

#ifndef EPD_TRICOLOR
#define EPD_TRICOLOR 0
#endif

static void (*s_poll)() = nullptr;
static void (*s_log)(const char*) = nullptr;

#if EPD_TRICOLOR
#include "epd_tri.h"
#include "tri_calib.h"
#include "tri_planes.h"

// Adafruit_GFX target backed by the two vendor-format planes (2 x 15000 B).
class TriCanvas : public Adafruit_GFX {
 public:
  TriCanvas() : Adafruit_GFX(EpdTri::WIDTH, EpdTri::HEIGHT) {}
  TriPlanes<EpdTri::WIDTH, EpdTri::HEIGHT> planes;

  void drawPixel(int16_t x, int16_t y, uint16_t color) override {
    if (x < 0 || y < 0 || x >= width() || y >= height()) return;
    int16_t t;
    switch (getRotation()) {
      case 1: t = x; x = WIDTH - 1 - y; y = t; break;
      case 2: x = WIDTH - 1 - x; y = HEIGHT - 1 - y; break;
      case 3: t = x; x = y; y = HEIGHT - 1 - t; break;
    }
    planes.set(x, y, triFromGfx(color));
  }
  void fillScreen(uint16_t color) override { planes.fill(triFromGfx(color)); }
};

// Arduino side of the driver HAL: GPIO + millis, serial serviced while waiting.
class ArduinoTriHal : public EpdTriHal {
 public:
  void pinOutput(int pin) override { pinMode(pin, OUTPUT); }
  void pinInput(int pin) override { pinMode(pin, INPUT); }
  void write(int pin, bool level) override { digitalWrite(pin, level ? HIGH : LOW); }
  bool read(int pin) override { return digitalRead(pin) == HIGH; }
  uint32_t ms() override { return millis(); }
  void pause1ms() override { delay(1); }
  void service() override {
    if (s_poll) s_poll();
  }
  void log(const char* line) override {
    if (s_log)
      s_log(line);
    else
      Serial.println(line);
  }
};

static TriCanvas display;
static ArduinoTriHal triHal;
static EpdTri epd(triHal, EpdTriPins{EPD_SCK, EPD_MOSI, EPD_CS, EPD_DC, EPD_RST, EPD_BUSY},
                  EpdTriConfig{(EpdTriProfile)EPD_TRI_PROFILE, EPD_TRI_V2_OLD_RY_INVERT != 0,
                               (uint16_t)EPD_TRI_RESET_LOW_MS, (uint8_t)EPD_TRI_PROBE_READS,
                               EPD_TRI_BUSY_TIMEOUT_MS, EPD_TRI_REFRESH_TIMEOUT_MS});
#define EPD_ROTATION 0
// Third colour (red on B, yellow on C panels; the planes don't care).
static const uint16_t ACCENT = GxEPD_RED;
#else
#include <GxEPD2_BW.h>
// Full frame buffer (max 800x480/8 = 48 KB), single page per refresh.
static GxEPD2_BW<EPD_DRIVER, EPD_DRIVER::HEIGHT> display(EPD_DRIVER(EPD_CS, EPD_DC, EPD_RST, EPD_BUSY));
static const uint16_t ACCENT = GxEPD_BLACK;
#endif
static U8G2_FOR_ADAFRUIT_GFX u8g2;

// ---------------------------------------------------------------------
// Theme: fonts and sizes per resolution class. Labels are English-only so
// we only need the small Latin fonts bundled with U8g2 (no CJK font blob).
// ---------------------------------------------------------------------
struct Theme {
  const uint8_t* header;  // top bar (time, status)
  const uint8_t* title;   // section titles
  const uint8_t* big;     // hero number (block tokens)
  const uint8_t* label;   // row labels
  const uint8_t* value;   // row values
  int16_t headerH, pad, barH, gap;
  bool separators;
};

static const Theme THEME_SMALL = {u8g2_font_helvB10_tf, u8g2_font_helvB08_tf, u8g2_font_logisoso18_tr,
                                  u8g2_font_helvR08_tf,  u8g2_font_helvB08_tf, 16, 4, 7, 4, false};
static const Theme THEME_MEDIUM = {u8g2_font_helvB14_tf, u8g2_font_helvB10_tf, u8g2_font_logisoso32_tr,
                                   u8g2_font_helvR10_tf,  u8g2_font_helvB12_tf, 28, 10, 12, 8, true};
static const Theme THEME_LARGE = {u8g2_font_helvB24_tf, u8g2_font_helvB18_tf, u8g2_font_logisoso54_tr,
                                  u8g2_font_helvR18_tf,  u8g2_font_helvB24_tf, 48, 20, 22, 16, true};

static const Theme& theme() {
  int16_t w = display.width();
  if (w >= 640) return THEME_LARGE;
  if (w >= 360) return THEME_MEDIUM;
  return THEME_SMALL;
}

// ---------------------------------------------------------------------
// Drawing helpers (y = text baseline)
// ---------------------------------------------------------------------
static int16_t ascent(const uint8_t* f) {
  u8g2.setFont(f);
  return u8g2.getFontAscent();
}

static int16_t textW(const uint8_t* f, const String& s) {
  u8g2.setFont(f);
  return u8g2.getUTF8Width(s.c_str());
}

static void text(const uint8_t* f, int16_t x, int16_t y, const String& s, uint16_t fg = GxEPD_BLACK) {
  u8g2.setFont(f);
  u8g2.setForegroundColor(fg);
  u8g2.setBackgroundColor(fg == GxEPD_WHITE ? GxEPD_BLACK : GxEPD_WHITE);
  u8g2.drawUTF8(x, y, s.c_str());
}

static void textRight(const uint8_t* f, int16_t xRight, int16_t y, const String& s, uint16_t fg = GxEPD_BLACK) {
  text(f, xRight - textW(f, s), y, s, fg);
}

static void textCenter(const uint8_t* f, int16_t xMid, int16_t y, const String& s, uint16_t fg = GxEPD_BLACK) {
  text(f, xMid - textW(f, s) / 2, y, s, fg);
}

// Section title with an underline; returns the y just below it.
static int16_t sectionTitle(const Theme& t, int16_t x, int16_t y, int16_t w, const char* title) {
  int16_t base = y + ascent(t.title);
  text(t.title, x, base, title);
  int16_t ly = base + 3;
  display.drawFastHLine(x, ly, w, ACCENT);
  if (EPD_TRICOLOR) display.drawFastHLine(x, ly + 1, w, ACCENT);  // thicker so the colour reads
  return ly + 2;
}

// "Label ..... value" on one row.
static void kvRow(const Theme& t, int16_t x, int16_t w, int16_t base, const char* label, const String& value) {
  text(t.label, x, base, label);
  textRight(t.value, x + w, base, value);
}

// "LBL [#####     ] 42%" on one row.
static void barRow(const Theme& t, int16_t x, int16_t w, int16_t base, const char* label, int pct) {
  int16_t labelW = textW(t.label, "DISK") + t.gap;
  int16_t pctW = textW(t.value, "100%");
  text(t.label, x, base, label);
  textRight(t.value, x + w, base, pct < 0 ? String("--") : String(pct) + "%");

  int16_t bx = x + labelW;
  int16_t bw = w - labelW - pctW - t.gap;
  int16_t by = base - ascent(t.label) / 2 - t.barH / 2;
  display.drawRect(bx, by, bw, t.barH, GxEPD_BLACK);
  if (pct > 0) {
    int16_t fw = (int32_t)(bw - 4) * min(pct, 100) / 100;
    // Three-colour: high load is filled in the third colour.
    uint16_t fill = (EPD_TRICOLOR && pct >= EPD_TRI_ALERT_PCT) ? ACCENT : GxEPD_BLACK;
    display.fillRect(bx + 2, by + 2, fw, t.barH - 4, fill);
  }
}

// Baselines for `n` rows spread evenly over [top, bottom].
static int16_t rowBase(const Theme& t, int16_t top, int16_t bottom, int n, int i) {
  int16_t pitch = (bottom - top) / n;
  int16_t a = ascent(t.value);
  return top + pitch * i + (pitch + a) / 2;
}

// ---------------------------------------------------------------------
// Screens
// ---------------------------------------------------------------------
static void drawHeader(const Theme& t, const ViewState& v) {
  int16_t w = display.width();
  display.fillRect(0, 0, w, t.headerH, GxEPD_BLACK);
  int16_t base = (t.headerH + ascent(t.header)) / 2;
  text(t.header, t.pad, base, v.s.time, GxEPD_WHITE);

  if (v.online) {
    textRight(t.header, w - t.pad, base, "ONLINE", GxEPD_WHITE);
  } else {
    // Badge so OFFLINE stands out: inverted on B/W, white on colour otherwise.
    String s = "OFFLINE";
    int16_t bw = textW(t.header, s) + t.pad * 2;
    int16_t bx = w - t.pad / 2 - bw;
    display.fillRect(bx, 2, bw, t.headerH - 4, EPD_TRICOLOR ? ACCENT : GxEPD_WHITE);
    text(t.header, bx + t.pad, base, s, EPD_TRICOLOR ? GxEPD_WHITE : GxEPD_BLACK);
  }
}

static void drawTokens(const Theme& t, const Stats& s, int16_t x, int16_t top, int16_t w, int16_t bottom) {
  int16_t y = sectionTitle(t, x, top, w, "CLAUDE TOKENS") + t.gap;

  // "5h block   $40.69" then the hero token count.
  int16_t base = y + ascent(t.value);
  text(t.label, x, base, "5h block");
  textRight(t.value, x + w, base, s.blockCost);
  y = base + t.gap / 2;
  base = y + ascent(t.big);
  text(t.big, x, base, s.blockTokens);
  y = base + t.gap;

  if (t.separators) {
    // Two groups: current block details, then today.
    int16_t mid = y + (bottom - y) / 2;
    kvRow(t, x, w, rowBase(t, y, mid, 2, 0), "Burn /min", s.burn);
    kvRow(t, x, w, rowBase(t, y, mid, 2, 1), "Block left", s.remain);
    display.drawFastHLine(x, mid, w, GxEPD_BLACK);
    kvRow(t, x, w, rowBase(t, mid, bottom, 2, 0), "Today", s.todayTokens);
    kvRow(t, x, w, rowBase(t, mid, bottom, 2, 1), "Today cost", s.todayCost);
  } else {
    kvRow(t, x, w, rowBase(t, y, bottom, 4, 0), "Burn/min", s.burn);
    kvRow(t, x, w, rowBase(t, y, bottom, 4, 1), "Left", s.remain);
    kvRow(t, x, w, rowBase(t, y, bottom, 4, 2), "Today", s.todayTokens);
    kvRow(t, x, w, rowBase(t, y, bottom, 4, 3), "Today $", s.todayCost);
  }
}

static void drawSystem(const Theme& t, const Stats& s, int16_t x, int16_t top, int16_t w, int16_t bottom) {
  int16_t y = sectionTitle(t, x, top, w, "SYSTEM") + t.gap / 2;
  int n = s.hasGpu ? 7 : 5;
  int i = 0;
  barRow(t, x, w, rowBase(t, y, bottom, n, i++), "CPU", s.cpu);
  barRow(t, x, w, rowBase(t, y, bottom, n, i++), "MEM", s.mem);
  barRow(t, x, w, rowBase(t, y, bottom, n, i++), "DISK", s.disk);
  kvRow(t, x, w, rowBase(t, y, bottom, n, i++), "Net up", s.netUp);
  kvRow(t, x, w, rowBase(t, y, bottom, n, i++), "Net down", s.netDown);
  if (s.hasGpu) {
    barRow(t, x, w, rowBase(t, y, bottom, n, i++), "GPU", s.gpu);
    kvRow(t, x, w, rowBase(t, y, bottom, n, i++), "GPU temp", s.gpuTemp);
  }
}

static void drawDashboard(const ViewState& v) {
  // Host offline long enough and weather enabled: the weather page (400x300 only).
  if (v.weather && display.width() == dashv2::WIDTH && display.height() == dashv2::HEIGHT) {
    wxb::draw(display, u8g2, v.wx, ACCENT);
    return;
  }
  // Protocol v2 data -> the v2 screen (400x300 only); v2 needs that size.
  if (v.v2 && display.width() == dashv2::WIDTH && display.height() == dashv2::HEIGHT) {
    dashv2::draw(display, u8g2, v.d, v.online, dashv2::Style{ACCENT, EPD_TRICOLOR != 0});
    return;
  }
  const Theme& t = theme();
  int16_t w = display.width(), h = display.height();
  drawHeader(t, v);

  int16_t top = t.headerH + t.pad / 2;
  int16_t bottom = h - t.pad / 2;
  int16_t mid = w / 2;
  display.drawFastVLine(mid, top, bottom - top, GxEPD_BLACK);
  drawTokens(t, v.s, t.pad, top, mid - t.pad * 2, bottom);
  drawSystem(t, v.s, mid + t.pad, top, w - mid - t.pad * 2, bottom);
}

static void drawBoot() {
  const Theme& t = theme();
  int16_t w = display.width(), h = display.height();
  display.drawRect(t.pad / 2, t.pad / 2, w - t.pad, h - t.pad, GxEPD_BLACK);

  int16_t lh = ascent(t.value) + t.gap * 2;
  int16_t y = h / 2 - lh;
  textCenter(t.header, w / 2, y, "E-Paper Sidescreen", ACCENT);
  textCenter(t.value, w / 2, y + lh, "Waiting for host...");
  textCenter(t.label, w / 2, y + lh * 2, "USB serial " + String(SERIAL_BAUD) + "  protocol " FW_PROTO);
  textCenter(t.label, w / 2, y + lh * 3,
             String(EPD_NAME) + "  " + String(display.width()) + "x" + String(display.height()));
#if EPD_TRICOLOR
  textCenter(t.label, w / 2, y + lh * 4,
             String("3-colour, path ") + EpdTri::pathName(epd.path()) + ", full refresh >= " +
                 String(EPD_TRI_MIN_REFRESH_MS / 1000) + "s");
#endif
}

#if EPD_TRICOLOR
// TODO page without an image: same visual language as the other pages (bold uppercase title, dotted rule,
// dotted frame). Latin text only: the real list is rendered on the PC.
static void drawTodoEmpty() {
  u8g2.setFontMode(1);
  u8g2.setForegroundColor(GxEPD_BLACK);
  u8g2.setBackgroundColor(GxEPD_WHITE);
  u8g2.setFont(u8g2_font_helvB08_tf);
  u8g2.drawUTF8(6, 15, "TODO");
  for (int16_t x = 6; x < 394; x += 2) display.drawPixel(x, 22, GxEPD_BLACK);
  int16_t fx = 50, fy = 90, fw = 300, fh = 100;
  for (int16_t x = fx; x < fx + fw; x += 2) {
    display.drawPixel(x, fy, GxEPD_BLACK);
    display.drawPixel(x, fy + fh, GxEPD_BLACK);
  }
  for (int16_t y = fy; y <= fy + fh; y += 2) {
    display.drawPixel(fx, y, GxEPD_BLACK);
    display.drawPixel(fx + fw - 1, y, GxEPD_BLACK);
  }
  textCenter(u8g2_font_helvB12_tf, 200, 138, "NO TODOS");
  textCenter(u8g2_font_helvR10_tf, 200, 162, "Add them in the PC app");
}

// The host-rendered image: decode the stored RLE straight into the two frame planes (no extra 30 KB buffer).
static void drawTodo(const ViewState& v) {
#if ENABLE_WIFI
  uint32_t n = todostore::size();
  if (v.todoCrc && n) {
    uint8_t* buf = (uint8_t*)malloc(n);
    bool ok = buf && todostore::load(buf, n) &&
              imgrx::decodeImage(buf, n, display.planes.black, display.planes.ry);
    free(buf);
    if (ok) {
      imgrx::hostToPanel(display.planes.black, display.planes.ry, sizeof(display.planes.black));
      return;
    }
    display.fillScreen(GxEPD_WHITE);  // a half-decoded image must not stay
  }
#else
  (void)v;
#endif
  drawTodoEmpty();
}

// Diagnostic build only (EPD_TRI_TEST_PATTERN=1): replaces the boot screen
// with three byte-aligned bands (src/tri_calib.h) and black labels.
static void drawCalibration() {
  const Theme& t = theme();
  using namespace tricalib;
  draw(display.planes);
  EpdTriPath p = epd.path();
  bool inv = p == TRI_PATH_V2_NEW || (p == TRI_PATH_V2_OLD && EPD_TRI_V2_OLD_RY_INVERT);
  textCenter(t.value, display.width() / 2, 28,
             String("CALIBRATION  path ") + EpdTri::pathName(p) + "  ry_invert " + (inv ? "1" : "0"));
  int16_t base = BAND_BOTTOM + 8 + ascent(t.value);
  textCenter(t.value, BAND1_X + BAND_W / 2, base, "1 BLACK");
  textCenter(t.value, BAND2_X + BAND_W / 2, base, "2 RED/YEL");
  textCenter(t.value, BAND3_X + BAND_W / 2, base, "3 WHITE");
  textCenter(t.label, display.width() / 2, base + ascent(t.label) + t.gap * 2,
             "expected: black | red/yellow | white");
}
#endif

// ---------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------
namespace ui {

#if EPD_TRICOLOR
void begin(void (*pollFn)(), void (*logFn)(const char*)) {
  s_poll = pollFn;
  s_log = logFn;
  epd.begin();  // AUTO: reset + 0x2F read-back on DIN; logs DIAG lines
  display.setRotation(EPD_ROTATION);
  display.setTextWrap(false);
  u8g2.begin(display);
  u8g2.setFontMode(1);  // transparent background
  u8g2.setFontDirection(0);
}

const char* panelName() { return EPD_NAME; }
int width() { return display.width(); }
int height() { return display.height(); }
bool fullRefreshOnly() { return true; }
bool faulted() { return epd.faulted(); }

void render(const ViewState& view, bool /*full: always full*/) {
  if (epd.faulted()) return;
  display.fillScreen(GxEPD_WHITE);
  if (view.todo) {
    drawTodo(view);
  } else if (view.hasData || view.weather) {
    drawDashboard(view);
  } else if (EPD_TRI_TEST_PATTERN) {
    drawCalibration();
  } else {
    drawBoot();
  }
  epd.display(display.planes.black, display.planes.ry);  // ends in deep sleep
}

#else
static void busyTrampoline(const void*) {
  if (s_poll) s_poll();
  delay(1);
}

void begin(void (*pollFn)(), void (*logFn)(const char*)) {
  s_poll = pollFn;
  s_log = logFn;
  // Remap the default SPI bus onto the panel pins; GxEPD2's own SPI.begin()
  // is then a no-op because the bus is already initialised.
  SPI.end();
  SPI.begin(EPD_SCK, EPD_MISO, EPD_MOSI, EPD_CS);
  // serial_diag_bitrate = 0: keep GxEPD2 debug output off the protocol port.
  display.init(0, true, EPD_RESET_MS, false);
  display.epd2.setBusyCallback(busyTrampoline);
  display.setRotation(EPD_ROTATION);
  display.setTextWrap(false);
  u8g2.begin(display);
  u8g2.setFontMode(1);  // transparent background
  u8g2.setFontDirection(0);
}

const char* panelName() { return EPD_NAME; }
int width() { return display.width(); }
int height() { return display.height(); }
bool fullRefreshOnly() { return false; }
bool faulted() { return false; }

void render(const ViewState& view, bool full) {
  if (full) {
    display.setFullWindow();
  } else {
    display.setPartialWindow(0, 0, display.width(), display.height());
  }
  display.firstPage();
  do {
    display.fillScreen(GxEPD_WHITE);
    if (view.hasData || view.weather) {
      drawDashboard(view);
    } else {
      drawBoot();
    }
  } while (display.nextPage());
  display.powerOff();
}
#endif

}  // namespace ui
