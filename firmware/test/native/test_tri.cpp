// Host-side tests for the three-colour driver sequencing, the two-plane
// buffer and the refresh policy. No hardware involved: a simulated panel
// decodes the bit-banged SPI, answers the 0x2F read-back on DIN and drives
// BUSY. Build + run (from firmware/):
//   g++ -std=c++17 -Wall -Isrc test/native/test_tri.cpp src/epd_tri.cpp -o test_tri && ./test_tri

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <map>
#include <string>
#include <vector>

#include "epd_tri.h"
#include "fault_recovery.h"
#include "refresh_policy.h"
#include "tri_calib.h"
#include "tri_planes.h"

static int failures = 0, checks = 0;
#define CHECK(cond)                                                    \
  do {                                                                 \
    checks++;                                                          \
    if (!(cond)) {                                                     \
      failures++;                                                      \
      printf("  FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);         \
    }                                                                  \
  } while (0)

enum { SCK = 13, DIN = 14, CS = 15, DC = 27, RST = 26, BUSY = 25 };

struct Byte {
  bool dc;  // false = command
  uint8_t v;
};

// Simulated panel. kind NEW: answers 0x2F with 0x01, BUSY high = busy.
// kind OLD: never drives DIN (reads return `floatBits`), BUSY low = busy.
struct FakePanel : EpdTriHal {
  enum Kind { NEW, OLD } kind;
  uint32_t now = 1000;
  uint32_t busyUntil = 0;
  bool stuckBusy = false;  // BUSY never releases
  uint32_t refreshMs = 15000;
  std::vector<uint8_t> floatBits;  // per read for OLD (cycled)
  size_t floatIdx = 0;

  bool level[64] = {};
  bool isInput[64] = {};
  int bits = 0;
  uint8_t shift = 0;
  uint8_t readVal = 0xFF;
  bool readArmed = false;
  std::vector<Byte> bytes;
  std::vector<std::string> logs;
  uint32_t rstLowAt = 0, rstLowMs = 0;

  // serial-service accounting
  uint32_t lastService = 0, maxServiceGapMs = 0;
  size_t bytesSinceService = 0, maxBytesBetweenService = 0;
  long services = 0;

  explicit FakePanel(Kind k) : kind(k) { lastService = now; }

  bool busyLevel() const {
    bool busy = stuckBusy || now < busyUntil;
    return kind == NEW ? busy : !busy;
  }
  void startBusy(uint32_t ms) { busyUntil = now + ms; }

  void onByte(bool dc, uint8_t v) {
    bytes.push_back({dc, v});
    bytesSinceService++;
    if (bytesSinceService > maxBytesBetweenService) maxBytesBetweenService = bytesSinceService;
    if (dc) return;
    if (kind == NEW) {
      if (v == 0x2F) {
        readVal = 0x01;
        readArmed = true;
      }
      if (v == 0x12) startBusy(5);
      if (v == 0x20) startBusy(refreshMs);
    } else {
      if (v == 0x2F) {
        readVal = floatBits.empty() ? 0x00 : floatBits[floatIdx++ % floatBits.size()];
        readArmed = true;
      }
      if (v == 0x04) startBusy(40);
      if (v == 0x12) startBusy(refreshMs);
      if (v == 0x02) startBusy(20);
    }
  }

  void pinOutput(int pin) override { isInput[pin] = false; }
  void pinInput(int pin) override { isInput[pin] = true; }
  void write(int pin, bool v) override {
    bool old = level[pin];
    level[pin] = v;
    if (pin == CS && v) bits = 0;  // frame end
    if (pin == RST) {
      if (old && !v) rstLowAt = now;
      if (!old && v && rstLowAt) {
        rstLowMs = now - rstLowAt;
        startBusy(3);
      }
    }
    if (pin == SCK && !old && v && !level[CS]) {  // rising edge, CS active
      if (!isInput[DIN]) {
        shift = (uint8_t)((shift << 1) | (level[DIN] ? 1 : 0));
        if (++bits == 8) {
          onByte(level[DC], shift);
          bits = 0;
        }
      } else {
        bits++;
      }
    }
  }
  bool read(int pin) override {
    if (pin == BUSY) return busyLevel();
    if (pin == DIN && isInput[DIN]) {
      // bit sampled before the rising edge -> MSB first
      return readArmed ? ((readVal >> (7 - bits)) & 1) : true;
    }
    return level[pin];
  }
  uint32_t ms() override { return now; }
  void pause1ms() override { now++; }
  void service() override {
    services++;
    uint32_t gap = now - lastService;
    if (gap > maxServiceGapMs) maxServiceGapMs = gap;
    lastService = now;
    bytesSinceService = 0;
  }
  void log(const char* line) override { logs.push_back(line); }

  // helpers
  int countCmd(uint8_t c) const {
    int n = 0;
    for (auto& b : bytes) n += !b.dc && b.v == c;
    return n;
  }
  // data bytes following the first occurrence of command c (until next command)
  std::vector<uint8_t> dataAfter(uint8_t c, int nth = 0) const {
    std::vector<uint8_t> out;
    int seen = -1;
    for (size_t i = 0; i < bytes.size(); i++) {
      if (!bytes[i].dc && bytes[i].v == c && ++seen == nth) {
        for (size_t j = i + 1; j < bytes.size() && bytes[j].dc; j++) out.push_back(bytes[j].v);
        break;
      }
    }
    return out;
  }
  bool logHas(const char* s) const {
    for (auto& l : logs)
      if (l.find(s) != std::string::npos) return true;
    return false;
  }
};

static EpdTriConfig cfg(EpdTriProfile p, bool oldInv = true, uint8_t reads = 3) {
  return EpdTriConfig{p, oldInv, 0, reads, 5000, 45000};
}
static const EpdTriPins PINS{SCK, DIN, CS, DC, RST, BUSY};

static TriPlanes<400, 300> planes;
static void makeImage() {
  planes.fill(TRI_WHITE);
  for (int x = 0; x < 100; x++) planes.set(x, 10, TRI_BLACK);
  for (int x = 200; x < 300; x++) planes.set(x, 20, TRI_COLOR);
}
static bool equals(const std::vector<uint8_t>& v, const uint8_t* p, bool inv) {
  if (v.size() != EpdTri::PLANE_BYTES) return false;
  for (size_t i = 0; i < v.size(); i++)
    if (v[i] != (inv ? (uint8_t)~p[i] : p[i])) return false;
  return true;
}

static void test_auto_new() {
  puts("auto_v2 on a panel answering 0x01 -> v2_new");
  FakePanel f(FakePanel::NEW);
  EpdTri e(f, PINS, cfg(TRI_PROFILE_AUTO_V2));
  CHECK(e.begin());
  CHECK(e.path() == TRI_PATH_V2_NEW);
  CHECK(e.probeValue() == 0x01);
  CHECK(f.countCmd(0x2F) == 3);
  CHECK(f.logHas("probe cmd=0x2F reads=0x01,0x01,0x01"));
  f.bytes.clear();
  makeImage();
  CHECK(e.display(planes.black, planes.ry));
  CHECK(f.rstLowMs == 2);
  CHECK(f.countCmd(0x2F) == 0);  // no re-probe per refresh
  CHECK(f.dataAfter(0x44) == (std::vector<uint8_t>{0x00, 0x31}));
  CHECK(f.dataAfter(0x45) == (std::vector<uint8_t>{0x00, 0x00, 0x2B, 0x01}));
  CHECK(equals(f.dataAfter(0x24), planes.black, false));
  CHECK(equals(f.dataAfter(0x26), planes.ry, true));  // ~ry
  CHECK(f.dataAfter(0x22) == std::vector<uint8_t>{0xF7});
  CHECK(f.countCmd(0x20) == 1);
  CHECK(f.countCmd(0x13) == 0);  // never the old plane command
  CHECK(f.dataAfter(0x10) == std::vector<uint8_t>{0x03});  // deep sleep, last
  CHECK(!f.bytes[f.bytes.size() - 2].dc && f.bytes[f.bytes.size() - 2].v == 0x10);
  CHECK(e.lastRefreshMs() >= 15000);
  CHECK(f.logHas("refresh done path=v2_new"));
}

static void test_auto_old(bool inv) {
  printf("auto_v2 on a panel not answering -> v2_old, ry_invert=%d\n", inv);
  FakePanel f(FakePanel::OLD);
  EpdTri e(f, PINS, cfg(TRI_PROFILE_AUTO_V2, inv));
  CHECK(e.begin());
  CHECK(e.path() == TRI_PATH_V2_OLD);
  CHECK(e.probeValue() == 0x00);
  f.bytes.clear();
  makeImage();
  CHECK(e.display(planes.black, planes.ry));
  CHECK(f.countCmd(0x04) == 1);
  CHECK(f.dataAfter(0x00) == std::vector<uint8_t>{0x0F});
  CHECK(equals(f.dataAfter(0x10), planes.black, false));
  CHECK(equals(f.dataAfter(0x13), planes.ry, inv));
  CHECK(f.countCmd(0x24) == 0 && f.countCmd(0x26) == 0 && f.countCmd(0x20) == 0);
  CHECK(f.countCmd(0x71) > 0);  // ReadBusy_old polls GET_STATUS
  CHECK(f.dataAfter(0x50) == std::vector<uint8_t>{0xF7});
  CHECK(f.dataAfter(0x07) == std::vector<uint8_t>{0xA5});
}

static void test_bc() {
  puts("forced bc_v1");
  FakePanel f(FakePanel::OLD);
  EpdTri e(f, PINS, cfg(TRI_PROFILE_BC_V1));
  CHECK(e.begin());
  CHECK(f.countCmd(0x2F) == 0);  // forced profiles never probe
  makeImage();
  CHECK(e.display(planes.black, planes.ry));
  CHECK(f.rstLowMs == 200);
  CHECK(f.dataAfter(0x06) == (std::vector<uint8_t>{0x17, 0x17, 0x17}));
  CHECK(equals(f.dataAfter(0x13), planes.ry, false));  // raw ry
  CHECK(f.countCmd(0x71) == 0);
  CHECK(f.countCmd(0x50) == 0);
}

static void test_forced_new_no_probe() {
  puts("forced v2_new never probes");
  FakePanel f(FakePanel::NEW);
  EpdTri e(f, PINS, cfg(TRI_PROFILE_V2_NEW));
  CHECK(e.begin());
  CHECK(e.probeValue() == -1);
  CHECK(f.countCmd(0x2F) == 0);
}

static void test_probe_unstable() {
  puts("auto_v2 with disagreeing read-backs -> fault, no refresh");
  FakePanel f(FakePanel::OLD);
  f.floatBits = {0x01, 0xFF, 0x01};
  EpdTri e(f, PINS, cfg(TRI_PROFILE_AUTO_V2));
  CHECK(!e.begin());
  CHECK(e.fault() == TRI_FAULT_PROBE_UNSTABLE);
  CHECK(f.logHas("fault probe_unstable reads=0x01,0xFF,0x01"));
  f.bytes.clear();
  makeImage();
  CHECK(!e.display(planes.black, planes.ry));
  CHECK(f.bytes.empty());  // nothing sent, no other waveform tried
}

static void test_busy_timeout(FakePanel::Kind k, EpdTriProfile p) {
  printf("BUSY stuck during refresh (%s) -> bounded fault, latched\n", EpdTri::profileName(p));
  FakePanel f(k);
  EpdTri e(f, PINS, cfg(p));
  CHECK(e.begin());
  makeImage();
  f.refreshMs = 0xFFFFFFF;  // never finishes
  uint32_t t0 = f.now;
  CHECK(!e.display(planes.black, planes.ry));
  uint32_t took = f.now - t0;
  CHECK(e.fault() == TRI_FAULT_BUSY_TIMEOUT);
  CHECK(took >= 45000 && took < 47000);
  CHECK(f.logHas("fault busy_timeout stage=refresh"));
  CHECK(f.maxServiceGapMs <= 2);  // serial served while waiting
  // The fault path puts the panel to sleep without waiting for BUSY (it is not answering).
  if (p == TRI_PROFILE_V2_NEW) {
    CHECK(f.countCmd(0x10) == 1 && f.dataAfter(0x10) == std::vector<uint8_t>({0x03}));  // deep sleep
  } else {
    CHECK(f.countCmd(0x02) == 1 && f.countCmd(0x07) == 1);                              // power off, deep sleep
    CHECK(f.dataAfter(0x07) == std::vector<uint8_t>({0xA5}));
  }
  CHECK(f.logHas("fault sleep sent"));
  size_t n = f.bytes.size();
  uint32_t t1 = f.now;
  CHECK(!e.display(planes.black, planes.ry));
  CHECK(f.bytes.size() == n && f.now == t1);  // no further attempts
}

static void test_init_timeout() {
  puts("BUSY stuck from power-on (v2_old) -> fault at init, bounded");
  FakePanel f(FakePanel::OLD);
  EpdTri e(f, PINS, cfg(TRI_PROFILE_V2_OLD));
  CHECK(e.begin());
  f.stuckBusy = true;
  uint32_t t0 = f.now;
  makeImage();
  CHECK(!e.display(planes.black, planes.ry));
  CHECK(f.now - t0 < 5000 + 500 + 100);
  CHECK(f.logHas("fault busy_timeout stage=init"));
  CHECK(f.countCmd(0x10) == 0);  // never got to the planes
  CHECK(f.countCmd(0x02) == 1 && f.countCmd(0x07) == 1);  // but asleep after the fault
  CHECK(f.dataAfter(0x07) == std::vector<uint8_t>({0xA5}));
}

static void test_fault_sleep_in_power_off() {
  puts("BUSY stuck at power-off (after a good refresh) -> fault, sleep sent once");
  FakePanel f(FakePanel::OLD);
  EpdTri e(f, PINS, cfg(TRI_PROFILE_V2_OLD));
  CHECK(e.begin());
  makeImage();
  CHECK(e.display(planes.black, planes.ry));
  CHECK(f.countCmd(0x07) == 1);
  f.stuckBusy = true;  // next frame: init fails at once
  CHECK(!e.display(planes.black, planes.ry));
  CHECK(e.faulted());
  CHECK(f.countCmd(0x07) == 2);  // one more, from the fault path only
  size_t n = f.bytes.size();
  CHECK(!e.display(planes.black, planes.ry));
  CHECK(f.bytes.size() == n);
}

static void test_probe_fault_no_sleep() {
  puts("probe_unstable -> no sleep commands (panel never initialised)");
  FakePanel f(FakePanel::OLD);
  f.floatBits = {0x01, 0x80, 0x01};
  EpdTri e(f, PINS, cfg(TRI_PROFILE_AUTO_V2));
  CHECK(!e.begin());
  CHECK(e.fault() == TRI_FAULT_PROBE_UNSTABLE);
  CHECK(f.countCmd(0x07) == 0 && f.countCmd(0x02) == 0 && f.countCmd(0x10) == 0);
}

static void test_fault_recovery() {
  puts("fault recovery: reboot after 3 min, at most 3 in a row, cleared by a good refresh");
  using namespace faultrec;
  Rtc r{0, 0};
  CHECK(bootsSoFar(r, true) == 0);                // garbage / never set
  r.magic = 0x12345678; r.count = 9;
  CHECK(bootsSoFar(r, true) == 0);                // wrong magic
  CHECK(!rebootDue(0, 999999, 0));                // no fault
  CHECK(!rebootDue(1000, 1000 + FAULT_REBOOT_AFTER_MS - 1, 0));
  CHECK(rebootDue(1000, 1000 + FAULT_REBOOT_AFTER_MS, 0));
  CHECK(rebootDue(0xFFFFFF00u, 0xFFFFFF00u + (uint32_t)FAULT_REBOOT_AFTER_MS, 2));  // millis() wrap
  uint32_t boots = 0;
  for (int i = 0; i < FAULT_MAX_BOOTS; i++) {     // fault, reboot, fault, reboot ...
    CHECK(rebootDue(1, 1 + FAULT_REBOOT_AFTER_MS, boots));
    noteReboot(r, boots);
    boots = bootsSoFar(r, true);                  // the next boot (software reset)
    CHECK(boots == (uint32_t)i + 1);
  }
  CHECK(!rebootDue(1, 1 + 10 * FAULT_REBOOT_AFTER_MS, boots));  // third fault boot in a row: stay stopped
  CHECK(bootsSoFar(r, false) == 0);               // power-on / brown-out starts over
  clear(r);                                       // a refresh worked
  CHECK(bootsSoFar(r, true) == 0);
  CHECK(rebootDue(1, 1 + FAULT_REBOOT_AFTER_MS, bootsSoFar(r, true)));
}

static void test_service_during_refresh() {
  puts("serial serviced throughout a refresh (reset, transfer, BUSY)");
  for (int k = 0; k < 2; k++) {
    FakePanel f(k ? FakePanel::OLD : FakePanel::NEW);
    EpdTri e(f, PINS, cfg(TRI_PROFILE_AUTO_V2));
    CHECK(e.begin());
    f.maxServiceGapMs = 0;
    f.maxBytesBetweenService = 0;
    makeImage();
    CHECK(e.display(planes.black, planes.ry));
    printf("  %s: services=%ld max_gap_ms=%u max_spi_bytes_between=%zu\n", k ? "v2_old" : "v2_new", f.services,
           f.maxServiceGapMs, f.maxBytesBetweenService);
    CHECK(f.maxServiceGapMs <= 2);
    CHECK(f.maxBytesBetweenService <= 60);
  }
}

static void test_planes() {
  puts("two-plane buffer");
  TriPlanes<16, 2> p;
  p.fill(TRI_WHITE);
  CHECK(p.black[0] == 0xFF && p.ry[0] == 0xFF);
  p.set(0, 0, TRI_BLACK);
  p.set(1, 0, TRI_COLOR);
  CHECK(p.black[0] == 0x7F && p.ry[0] == 0xBF);
  CHECK(p.get(0, 0) == TRI_BLACK && p.get(1, 0) == TRI_COLOR && p.get(2, 0) == TRI_WHITE);
  p.set(0, 0, TRI_COLOR);  // black -> colour clears the black bit
  CHECK(p.black[0] == 0xFF && p.ry[0] == 0x3F);
  p.set(1, 0, TRI_WHITE);
  CHECK(p.ry[0] == 0x7F);
  p.set(16, 0, TRI_BLACK);  // out of range ignored
  p.set(-1, 0, TRI_BLACK);
  CHECK(p.black[1] == 0xFF);
  CHECK(triFromGfx(0xFFFF) == TRI_WHITE && triFromGfx(0) == TRI_BLACK);
  CHECK(triFromGfx(0xF800) == TRI_COLOR && triFromGfx(0xFFE0) == TRI_COLOR);
  p.fill(TRI_COLOR);
  CHECK(p.black[3] == 0xFF && p.ry[3] == 0x00);
}

// Bring-up on the real panel read 0xFF,0xFF,0xFF. That must be logged as the
// vendor's else branch, never as a confirmed old panel.
static void test_probe_ff_fallback() {
  puts("auto_v2 reading 0xFF x3 -> v2_old, logged as fallback, not as identification");
  FakePanel f(FakePanel::OLD);
  f.floatBits = {0xFF};
  EpdTri e(f, PINS, cfg(TRI_PROFILE_AUTO_V2));
  CHECK(e.begin());
  CHECK(e.path() == TRI_PATH_V2_OLD);
  CHECK(e.probeValue() == 0xFF);
  CHECK(f.logHas("probe cmd=0x2F reads=0xFF,0xFF,0xFF rule=0x01->v2_new,else->v2_old basis=else_fallback "
                 "note=0xFF_same_as_undriven_DIN"));

  FakePanel g(FakePanel::OLD);  // 0x00: still a fallback, but no 0xFF note
  EpdTri e2(g, PINS, cfg(TRI_PROFILE_AUTO_V2));
  CHECK(e2.begin());
  CHECK(g.logHas("basis=else_fallback") && !g.logHas("note="));

  FakePanel n(FakePanel::NEW);
  EpdTri e3(n, PINS, cfg(TRI_PROFILE_AUTO_V2));
  CHECK(e3.begin());
  CHECK(n.logHas("basis=read_0x01") && !n.logHas("else_fallback"));
}

// DIAG planes line counts the bits that actually went out.
static void test_planes_diag() {
  puts("DIAG planes line reflects the sent bits (100 black px, 100 colour px)");
  for (int inv = 0; inv < 2; inv++) {
    FakePanel f(FakePanel::OLD);
    EpdTri e(f, PINS, cfg(TRI_PROFILE_V2_OLD, inv != 0));
    CHECK(e.begin());
    makeImage();
    CHECK(e.display(planes.black, planes.ry));
    std::vector<uint8_t> sent = f.dataAfter(0x13);
    long zeros = 0;
    for (uint8_t b : sent)
      for (int k = 0; k < 8; k++) zeros += !((b >> k) & 1);
    CHECK(zeros == (inv ? 119900 : 100));
    CHECK(f.logHas(inv ? "planes path=v2_old black_cmd=0x10 black_ink_px=100 ry_cmd=0x13 ry_invert=1 "
                         "ry_color_px=100 ry_sent_zero_bits=119900 ry_sent_one_bits=100"
                       : "planes path=v2_old black_cmd=0x10 black_ink_px=100 ry_cmd=0x13 ry_invert=0 "
                         "ry_color_px=100 ry_sent_zero_bits=100 ry_sent_one_bits=119900"));
  }
  FakePanel n(FakePanel::NEW);
  EpdTri e(n, PINS, cfg(TRI_PROFILE_V2_NEW));
  CHECK(e.begin());
  makeImage();
  CHECK(e.display(planes.black, planes.ry));
  CHECK(n.logHas("planes path=v2_new black_cmd=0x24 black_ink_px=100 ry_cmd=0x26 ry_invert=1 ry_color_px=100"));
}

// Calibration frame: expected bytes are written out by hand from the band
// layout documented in tri_calib.h.
static void test_calibration(bool inv) {
  printf("calibration frame through v2_old, ry_invert=%d: exact bytes on the wire\n", inv);
  FakePanel f(FakePanel::OLD);
  EpdTri e(f, PINS, cfg(TRI_PROFILE_V2_OLD, inv));
  CHECK(e.begin());
  tricalib::draw(planes);
  CHECK(e.display(planes.black, planes.ry));
  std::vector<uint8_t> b = f.dataAfter(0x10), r = f.dataAfter(0x13);
  CHECK(b.size() == 15000 && r.size() == 15000);
  if (b.size() != 15000 || r.size() != 15000) return;
  const uint8_t R0 = inv ? 0x00 : 0xFF, R1 = inv ? 0xFF : 0x00;  // ry byte: no colour / colour
  // y=130: band rows. col 0 margin, col 8 band 1, col 24 band 2, col 40 inside band 3, col 49 margin
  const int row = 130 * 50;
  CHECK(b[row + 0] == 0xFF && r[row + 0] == R0);
  CHECK(b[row + 8] == 0x00 && r[row + 8] == R0);
  CHECK(b[row + 1] == 0x00 && b[row + 16] == 0x00);  // band 1 = cols 1..16
  CHECK(b[row + 24] == 0xFF && r[row + 24] == R1);
  CHECK(r[row + 17] == R1 && r[row + 32] == R1);      // band 2 = cols 17..32
  CHECK(b[row + 40] == 0xFF && r[row + 40] == R0);
  CHECK(b[row + 33] == 0x3F && b[row + 48] == 0xFC);  // band 3 side frame, 2 px
  CHECK(b[row + 49] == 0xFF && r[row + 49] == R0);
  // y=20 above the bands: all white; y=40 top frame of band 3 is black
  CHECK(b[20 * 50 + 24] == 0xFF && r[20 * 50 + 24] == R0);
  CHECK(b[40 * 50 + 40] == 0x00 && r[40 * 50 + 40] == R0);
  CHECK(f.logHas(inv ? "black_ink_px=24256 ry_cmd=0x13 ry_invert=1 ry_color_px=23040 ry_sent_zero_bits=96960"
                     : "black_ink_px=24256 ry_cmd=0x13 ry_invert=0 ry_color_px=23040 ry_sent_zero_bits=23040"));
}

// Hypothesis model, not evidence: a panel that reads 0x13 the way the vendor
// EPD_4in2bc / EPD_4in2b_V2_old code clears it (0xFF = no colour, so a 0 bit
// is colour), with colour taking precedence over black. Under that model the
// boot frame sent with ry_invert=1 comes out as colour everywhere except the
// pixels drawn in colour, i.e. the reported "whole screen red, no text";
// with ry_invert=0 it comes out as drawn. Real panel behaviour is what the
// calibration run decides.
static TriColor modelPanel(const std::vector<uint8_t>& b, const std::vector<uint8_t>& r, int x, int y) {
  int i = (x + y * 400) / 8;
  uint8_t m = 0x80 >> (x & 7);
  if (!(r[i] & m)) return TRI_COLOR;
  return (b[i] & m) ? TRI_WHITE : TRI_BLACK;
}

static void test_red_screen_model() {
  puts("model: raw-0x13 panel + ry_invert=1 reproduces 'all red, no text'; ry_invert=0 shows the frame");
  for (int inv = 0; inv < 2; inv++) {
    FakePanel f(FakePanel::OLD);
    f.floatBits = {0xFF};  // as observed
    EpdTri e(f, PINS, cfg(TRI_PROFILE_AUTO_V2, inv != 0));
    CHECK(e.begin());
    // boot-like frame: white, black frame + black "text" block, small colour title
    planes.fill(TRI_WHITE);
    tricalib::fillRect(planes, 5, 5, 390, 2, TRI_BLACK);
    tricalib::fillRect(planes, 100, 150, 200, 20, TRI_BLACK);
    tricalib::fillRect(planes, 120, 100, 160, 16, TRI_COLOR);
    CHECK(e.display(planes.black, planes.ry));
    std::vector<uint8_t> b = f.dataAfter(0x10), r = f.dataAfter(0x13);
    CHECK(b.size() == 15000 && r.size() == 15000);
    if (b.size() != 15000 || r.size() != 15000) continue;
    long same = 0, colour = 0, black = 0;
    for (int y = 0; y < 300; y++)
      for (int x = 0; x < 400; x++) {
        TriColor c = modelPanel(b, r, x, y);
        same += c == planes.get(x, y);
        colour += c == TRI_COLOR;
        black += c == TRI_BLACK;
      }
    if (inv) {
      CHECK(black == 0);                     // no text survives
      CHECK(colour == 120000 - 160 * 16);    // everything but the colour title
    } else {
      CHECK(same == 120000);
    }
  }
}

// ---------------------------------------------------------------------
// Production env. The flags live in platformio.ini, so read them
// from there: run the test from firmware/ (see README §9.7).
// ---------------------------------------------------------------------
typedef std::map<std::string, std::vector<std::string>> EnvFlags;  // env -> -D tokens

static EnvFlags readEnvFlags(const char* path) {
  EnvFlags out;
  FILE* f = fopen(path, "r");
  if (!f) return out;
  char line[512];
  std::string env;
  while (fgets(line, sizeof(line), f)) {
    std::string l(line);
    size_t c = l.find(';');
    if (c != std::string::npos) l.erase(c);
    if (l.compare(0, 5, "[env:") == 0) {
      env = l.substr(5, l.find(']') - 5);
      out[env];
      continue;
    }
    if (!l.empty() && l[0] == '[') env.clear();
    if (env.empty()) continue;
    for (size_t i = l.find("-D"); i != std::string::npos; i = l.find("-D", i + 2)) {
      size_t e = l.find_first_of(" \t\r\n", i);
      out[env].push_back(l.substr(i, e == std::string::npos ? std::string::npos : e - i));
    }
  }
  fclose(f);
  return out;
}

static bool hasFlag(const std::vector<std::string>& v, const char* flag) {
  for (auto& t : v)
    if (t == flag) return true;
  return false;
}
static int countPrefix(const std::vector<std::string>& v, const char* prefix) {
  int n = 0;
  for (auto& t : v) n += t.compare(0, strlen(prefix), prefix) == 0;
  return n;
}

static void test_env_flags() {
  puts("platformio.ini: production env pinned to the calibrated parameters, others unchanged");
  EnvFlags envs = readEnvFlags("platformio.ini");
  CHECK(envs.size() == 4);
  if (envs.size() != 4) {
    puts("  (run from firmware/ so platformio.ini is found)");
    return;
  }
  const auto& prod = envs["waveshare_esp32_42tri"];
  CHECK(hasFlag(prod, "-DEPD_PANEL=4"));
  CHECK(hasFlag(prod, "-DEPD_TRI_PROFILE=2"));          // v2_old, no 0x2F read-back
  CHECK(hasFlag(prod, "-DEPD_TRI_V2_OLD_RY_INVERT=0"));  // 0x13 = raw ry (calibrated on the real panel)
  CHECK(hasFlag(prod, "-DEPD_TRI_TEST_PATTERN=0"));      // normal boot screen, not calibration
  CHECK(countPrefix(prod, "-DEPD_TRI_MIN_REFRESH_MS") == 0);  // 180 s default kept
  CHECK(countPrefix(prod, "-DEPD_TRI_PROFILE") == 1 && countPrefix(prod, "-DEPD_TRI_V2_OLD_RY_INVERT") == 1 &&
        countPrefix(prod, "-DEPD_TRI_TEST_PATTERN") == 1);  // one definition each, no clash
  const auto& cal = envs["waveshare_esp32_42tri_calib"];
  CHECK(hasFlag(cal, "-DEPD_TRI_PROFILE=2") && hasFlag(cal, "-DEPD_TRI_V2_OLD_RY_INVERT=0") &&
        hasFlag(cal, "-DEPD_TRI_TEST_PATTERN=1"));
  for (const char* bw : {"waveshare_esp32", "esp32s3"}) {
    const auto& v = envs[bw];
    CHECK(!v.empty());
    CHECK(countPrefix(v, "-DEPD_TRI_") == 0 && countPrefix(v, "-DEPD_PANEL") == 0);  // B/W default panel
  }
}

// The production driver configuration (fixed v2_old, ry_invert 0) on a
// dashboard-like frame, twice in a row (online, then with the OFFLINE
// badge): no read-back, every refresh starts from reset, both planes go out
// exactly as drawn. Under the H1 panel model (supported by the calibration
// run on the real panel, still a model) the panel shows the frame as drawn: white background,
// black text, colour only where drawn in colour.
static void drawDashboardLike(bool offline) {
  planes.fill(TRI_WHITE);
  tricalib::fillRect(planes, 0, 0, 400, 28, TRI_BLACK);     // header bar
  tricalib::fillRect(planes, 8, 8, 60, 12, TRI_WHITE);      // time text cut out of the bar
  tricalib::fillRect(planes, 199, 32, 1, 264, TRI_BLACK);   // column divider
  tricalib::fillRect(planes, 10, 40, 120, 2, TRI_COLOR);    // section underline
  tricalib::fillRect(planes, 210, 40, 120, 2, TRI_COLOR);
  tricalib::fillRect(planes, 10, 60, 150, 32, TRI_BLACK);   // big number
  tricalib::fillRect(planes, 210, 60, 170, 12, TRI_BLACK);  // CPU bar
  tricalib::fillRect(planes, 210, 80, 160, 12, TRI_COLOR);  // bar >= alert %
  if (offline) {
    tricalib::fillRect(planes, 300, 2, 96, 24, TRI_COLOR);  // OFFLINE badge
    tricalib::fillRect(planes, 310, 8, 70, 12, TRI_WHITE);  // white text in it
  }
}

static void test_production_dashboard() {
  puts("production config (v2_old fixed, ry_invert=0): two dashboard frames sent as drawn");
  FakePanel f(FakePanel::OLD);
  f.floatBits = {0xFF};
  EpdTri e(f, PINS, cfg(TRI_PROFILE_V2_OLD, false));
  CHECK(e.begin());
  CHECK(f.countCmd(0x2F) == 0);  // fixed path: no read-back
  CHECK(f.logHas("config profile=v2_old path=v2_old ry_invert=0"));
  for (int frame = 0; frame < 2; frame++) {
    drawDashboardLike(frame == 1);
    CHECK(e.display(planes.black, planes.ry));
    std::vector<uint8_t> b = f.dataAfter(0x10, frame), r = f.dataAfter(0x13, frame);
    CHECK(equals(b, planes.black, false));
    CHECK(equals(r, planes.ry, false));  // raw ry, not inverted
    if (b.size() != 15000 || r.size() != 15000) continue;
    long same = 0, white = 0, colour = 0;
    for (int y = 0; y < 300; y++)
      for (int x = 0; x < 400; x++) {
        TriColor c = modelPanel(b, r, x, y);
        same += c == planes.get(x, y);
        white += c == TRI_WHITE;
        colour += c == TRI_COLOR;
      }
    CHECK(same == 120000);
    CHECK(white > 120000 / 2);  // white background, not a coloured screen
    CHECK(colour < 120000 / 20);
  }
  // reset -> power on -> refresh -> deep sleep for each frame
  CHECK(f.countCmd(0x04) == 2 && f.countCmd(0x12) == 2 && f.countCmd(0x07) == 2);
  CHECK(!e.faulted());
}

static const RefreshPolicy TRI{true, 180000, 0, 0, 24UL * 3600 * 1000, 0};
static const RefreshPolicy BW{false, 5000, 20, 30UL * 60 * 1000, 0, 0};

static void test_policy() {
  puts("refresh policy");
  RefreshState s;
  noteRefresh(s, REFRESH_FULL, 100000, false);  // boot screen done
  CHECK(decideRefresh(TRI, s, 100000 + 179999, true, false, true, false) == REFRESH_NONE);
  CHECK(decideRefresh(TRI, s, 100000 + 180000, true, false, true, false) == REFRESH_FULL);
  CHECK(decideRefresh(TRI, s, 100000 + 180000, false, false, true, false) == REFRESH_NONE);
  CHECK(decideRefresh(TRI, s, 100000 + 180000, false, true, true, false) == REFRESH_FULL);  // full_refresh
  CHECK(decideRefresh(TRI, s, 100000 + 180000, true, true, true, true) == REFRESH_NONE);    // faulted
  CHECK(decideRefresh(TRI, s, 100000 + 24UL * 3600 * 1000, false, false, true, false) == REFRESH_FULL);
  // Host refresh interval (`every`): only once a data screen is shown; full_refresh ignores it.
  {
    RefreshPolicy slow = TRI;
    slow.dataIntervalMs = 600000;
    RefreshState d;
    noteRefresh(d, REFRESH_FULL, 100000, false);  // boot screen: the first data screen still comes after 180 s
    CHECK(decideRefresh(slow, d, 100000 + 180000, true, false, true, false) == REFRESH_FULL);
    noteRefresh(d, REFRESH_FULL, 300000, true);   // data screen shown
    CHECK(decideRefresh(slow, d, 300000 + 180000, true, false, true, false) == REFRESH_NONE);
    CHECK(decideRefresh(slow, d, 300000 + 599999, true, false, true, false) == REFRESH_NONE);
    CHECK(decideRefresh(slow, d, 300000 + 600000, true, false, true, false) == REFRESH_FULL);
    CHECK(decideRefresh(slow, d, 300000 + 180000, false, true, true, false) == REFRESH_FULL);  // full_refresh
    slow.dataIntervalMs = 60000;  // shorter than the panel minimum: the minimum wins
    CHECK(decideRefresh(slow, d, 300000 + 179999, true, false, true, false) == REFRESH_NONE);
  }
  // Three-colour never partial, however many refreshes
  for (int i = 0; i < 50; i++) {
    uint32_t t = 300000 + i * 200000;
    RefreshKind k = decideRefresh(TRI, s, t, true, false, true, false);
    CHECK(k == REFRESH_FULL);
    noteRefresh(s, k, t + 15000, true);
  }
  // B/W keeps its old behaviour: first dashboard full, then partial, full every 20
  RefreshState b;
  noteRefresh(b, REFRESH_FULL, 1000, false);
  CHECK(decideRefresh(BW, b, 5999, true, false, true, false) == REFRESH_NONE);
  CHECK(decideRefresh(BW, b, 6000, true, false, true, false) == REFRESH_FULL);
  noteRefresh(b, REFRESH_FULL, 6000, true);
  int partials = 0;
  uint32_t t = 6000;
  for (int i = 0; i < 21; i++) {
    t += 5000;
    RefreshKind k = decideRefresh(BW, b, t, true, false, true, false);
    if (k == REFRESH_PARTIAL) partials++;
    if (i == 20) CHECK(k == REFRESH_FULL);
    noteRefresh(b, k, t, true);
  }
  CHECK(partials == 20);
}

// Loop simulation: host sends stats every 5 s (value = send time), a refresh
// takes 15 s during which serial is still served. Every refresh must show
// the newest snapshot, gaps are >= 180 s from the end of the previous one.
static void test_coalescing() {
  puts("snapshot coalescing + throttling (simulated 1 h)");
  RefreshState s;
  uint32_t now = 0;
  noteRefresh(s, REFRESH_FULL, now, false);
  uint32_t latest = 0, shown = 0, nextMsg = 0, lastDone = 0;
  int refreshes = 0, received = 0;
  uint32_t maxLatency = 0;  // send -> handled
  bool ok = true;
  auto serve = [&]() {
    while (nextMsg <= now) {
      latest = nextMsg;
      received++;
      if (now - nextMsg > maxLatency) maxLatency = now - nextMsg;
      nextMsg += 5000;
    }
  };
  while (now < 3600000) {
    serve();
    RefreshKind k = decideRefresh(TRI, s, now, latest != shown, false, true, false);
    if (k != REFRESH_NONE) {
      uint32_t snap = latest;  // render a snapshot
      if (refreshes && now - lastDone < 180000) ok = false;
      if (snap != latest) ok = false;
      for (int i = 0; i < 15000; i++) {  // busy: messages keep arriving
        now++;
        serve();
      }
      shown = snap;
      lastDone = now;
      noteRefresh(s, k, now, true);
      refreshes++;
    }
    now += 5;
  }
  printf("  received=%d refreshes=%d max_latency_ms=%u\n", received, refreshes, maxLatency);
  CHECK(ok);
  CHECK(received == 720);  // one every 5 s for 1 h, none lost while busy
  CHECK(maxLatency <= 5);  // handled within one loop pass, also mid-refresh
  CHECK(refreshes >= 18 && refreshes <= 19);  // 3600 / (180 + 15)
}

// ---------------------------------------------------------------------
// Boot / loop hand-off through the same RefreshScheduler main.cpp uses.
// A simulated device: `serve()` is the serial poll (also called from the
// busy wait inside render), messages are (time, value) stats.
// ---------------------------------------------------------------------
struct SimView {
  bool hasData = false;
  bool online = false;
  int value = -1;
  std::string signature() const {
    if (!hasData) return "boot";
    return std::string(online ? "1|" : "0|") + std::to_string(value);
  }
};

struct SimDevice {
  RefreshScheduler<SimView, std::string> sched;
  SimView view;
  uint32_t now = 0, lastMsg = 0;
  std::vector<std::pair<uint32_t, int>> msgs;  // sorted by time
  size_t next = 0;
  uint32_t refreshMs = 15000;
  struct Shown {
    uint32_t start, done;
    SimView v;
  };
  std::vector<Shown> shown;

  explicit SimDevice(const RefreshPolicy& p) { sched.policy = p; }
  void serve() {
    while (next < msgs.size() && msgs[next].first <= now) {
      view.value = msgs[next].second;
      view.hasData = true;
      lastMsg = msgs[next].first ? msgs[next].first : 1;
      next++;
    }
  }
  void render(const SimView& snap) {
    uint32_t start = now;
    for (uint32_t i = 0; i < refreshMs; i++) {  // panel busy, serial served
      now++;
      serve();
    }
    shown.push_back({start, now, snap});
  }
  void boot() {
    sched.boot(view, false, [&](const SimView& s, RefreshKind) { render(s); }, [&] { return now; });
  }
  void runUntil(uint32_t end) {
    while (now < end) {
      serve();
      view.online = isOnline(lastMsg, now, 60000);
      sched.tick(view, now, false, [&](const SimView& s, RefreshKind) { render(s); }, [&] { return now; });
      now += 5;  // loop() delay
    }
  }
};

static void test_boot_handoff() {
  puts("stats during the boot refresh, then host gone -> shown at the first allowed refresh");
  SimDevice d(TRI);
  d.msgs = {{1000, 11}, {2000, 12}, {3000, 13}};  // all while boot screen is busy
  d.boot();
  CHECK(d.shown.size() == 1 && !d.shown[0].v.hasData);  // boot screen drawn
  uint32_t bootDone = d.shown[0].done;
  CHECK(bootDone == 15000);
  d.runUntil(bootDone + 10UL * 60 * 1000);  // 10 min, far below the 24 h keep-alive
  CHECK(d.shown.size() == 2);
  if (d.shown.size() >= 2) {
    const auto& r = d.shown[1];
    printf("  dashboard refresh started at %u ms (boot done %u), value=%d online=%d\n", r.start, bootDone,
           r.v.value, r.v.online);
    CHECK(r.start >= bootDone + 180000 && r.start < bootDone + 180000 + 10);  // throttled from completion
    CHECK(r.v.hasData && r.v.value == 13);  // only the newest of the three
    CHECK(!r.v.online);                     // host silent > 60 s -> OFFLINE
  }
}

static void test_boot_handoff_online() {
  puts("stats during boot, host keeps sending -> newest at boot+180 s, then every 180 s after completion");
  SimDevice d(TRI);
  for (uint32_t t = 1000; t < 900000; t += 5000) d.msgs.push_back({t, (int)t});
  d.boot();
  d.runUntil(900000);
  CHECK(d.shown.size() >= 4);
  for (size_t i = 1; i < d.shown.size(); i++) {
    CHECK(d.shown[i].start - d.shown[i - 1].done >= 180000);
    CHECK(d.shown[i].start - d.shown[i - 1].done < 180000 + 10);
    CHECK(d.shown[i].v.online);
    // newest message at refresh start
    int newest = (int)(1000 + (d.shown[i].start - 1000) / 5000 * 5000);
    CHECK(d.shown[i].v.value == newest);
  }
}

static void test_boot_handoff_bw() {
  puts("B/W: stats during the boot refresh are drawn right after it (old 5 s gap)");
  SimDevice d(BW);
  d.refreshMs = 3000;
  d.msgs = {{500, 7}};
  d.boot();
  d.runUntil(20000);
  CHECK(d.shown.size() == 2);
  if (d.shown.size() >= 2) CHECK(d.shown[1].v.value == 7 && d.shown[1].start == d.shown[0].done + 5000);
}

// Host stops (disconnect) and later comes back: OFFLINE is drawn once at the
// first allowed refresh after the 60 s timeout, nothing is refreshed while
// the host stays away, and the first refresh after it returns shows ONLINE
// with the newest values. All gaps >= 180 s from the end of the previous one.
static void test_offline_recovery() {
  puts("host gone -> OFFLINE once; host back -> ONLINE with newest value; 180 s gaps kept");
  SimDevice d(TRI);
  d.refreshMs = 23000;  // measured total_ms ~23 s on the real panel
  const uint32_t stop = 400000, resume = 1000000, end = 1600000;
  for (uint32_t t = 1000; t < stop; t += 10000) d.msgs.push_back({t, (int)t});
  for (uint32_t t = resume; t < end; t += 10000) d.msgs.push_back({t, (int)t});
  const int lastBefore = 391000;
  d.boot();
  d.runUntil(end);
  for (size_t i = 1; i < d.shown.size(); i++) CHECK(d.shown[i].start - d.shown[i - 1].done >= 180000);
  int offlineIdx = -1;
  for (size_t i = 0; i < d.shown.size(); i++)
    if (d.shown[i].v.hasData && !d.shown[i].v.online) {
      offlineIdx = (int)i;
      break;
    }
  CHECK(offlineIdx > 0);
  if (offlineIdx <= 0) return;
  const auto& off = d.shown[offlineIdx];
  printf("  OFFLINE refresh started at %u ms (last message %d ms)\n", off.start, lastBefore);
  CHECK(off.v.value == lastBefore);  // last values kept
  CHECK(off.start >= (uint32_t)lastBefore + 60000);
  CHECK(off.start < resume);
  CHECK((size_t)offlineIdx + 1 < d.shown.size());
  if ((size_t)offlineIdx + 1 >= d.shown.size()) return;
  const auto& back = d.shown[offlineIdx + 1];  // nothing else drawn while the host is away
  printf("  first refresh after resume started at %u ms, online=%d value=%d\n", back.start, back.v.online,
         back.v.value);
  CHECK(back.start >= resume);
  CHECK(back.start < resume + 10);  // OFFLINE was long enough ago: shown right away
  CHECK(back.v.online);
  CHECK(back.v.value == (int)(resume + (back.start - resume) / 10000 * 10000));  // newest
}

int main() {
  test_auto_new();
  test_auto_old(true);
  test_auto_old(false);
  test_bc();
  test_forced_new_no_probe();
  test_probe_unstable();
  test_busy_timeout(FakePanel::NEW, TRI_PROFILE_V2_NEW);
  test_busy_timeout(FakePanel::OLD, TRI_PROFILE_V2_OLD);
  test_busy_timeout(FakePanel::OLD, TRI_PROFILE_BC_V1);
  test_init_timeout();
  test_fault_sleep_in_power_off();
  test_probe_fault_no_sleep();
  test_fault_recovery();
  test_service_during_refresh();
  test_planes();
  test_probe_ff_fallback();
  test_planes_diag();
  test_calibration(true);
  test_calibration(false);
  test_red_screen_model();
  test_policy();
  test_coalescing();
  test_boot_handoff();
  test_boot_handoff_online();
  test_boot_handoff_bw();
  test_env_flags();
  test_production_dashboard();
  test_offline_recovery();
  printf("%d checks, %d failures\n", checks, failures);
  return failures ? 1 : 0;
}
