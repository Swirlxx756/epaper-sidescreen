// Three-colour 4.2" driver. Each step below is annotated with the vendor
// function it mirrors (Waveshare EPD_4in2b_V2.c / EPD_4in2bc.c, see
// firmware/VENDOR_SOURCES.md). Differences from the vendor code are limited
// to: serial is serviced during every wait, every BUSY wait is bounded, and
// the AUTO read-back can be required to repeat identically.

#include "epd_tri.h"

#include <stdarg.h>
#include <stdio.h>

// Reset low pulse (vendor Reset(): high 200 ms, low N, high 200 ms).
static const uint16_t RESET_LOW_V2_MS = 2;    // EPD_4in2b_V2 Reset()
static const uint16_t RESET_LOW_BC_MS = 200;  // ESP32 board package EPD_4in2bc Reset()

const char* EpdTri::pathName(EpdTriPath p) {
  switch (p) {
    case TRI_PATH_V2_NEW: return "v2_new";
    case TRI_PATH_V2_OLD: return "v2_old";
    case TRI_PATH_BC_V1: return "bc_v1";
    default: return "none";
  }
}

const char* EpdTri::profileName(EpdTriProfile p) {
  switch (p) {
    case TRI_PROFILE_AUTO_V2: return "auto_v2";
    case TRI_PROFILE_V2_NEW: return "v2_new";
    case TRI_PROFILE_V2_OLD: return "v2_old";
    case TRI_PROFILE_BC_V1: return "bc_v1";
    default: return "?";
  }
}

void EpdTri::logf(const char* fmt, ...) {
  char buf[160];
  int n = snprintf(buf, sizeof(buf), "DIAG epd ");
  va_list ap;
  va_start(ap, fmt);
  vsnprintf(buf + n, sizeof(buf) - n, fmt, ap);
  va_end(ap);
  hal_.log(buf);
}

void EpdTri::fail(EpdTriFault f, const char* detail) {
  fault_ = f;
  logf("fault %s %s path=%s; refresh stopped until reboot",
       f == TRI_FAULT_BUSY_TIMEOUT ? "busy_timeout" : "probe_unstable", detail, pathName(path_));
  // A BUSY timeout can leave the panel powered with the booster on (a full refresh drives high voltages):
  // put it to sleep the best we can, but never wait for it (it is not answering).
  if (f == TRI_FAULT_BUSY_TIMEOUT) emergencySleep();
}

// Power-off / deep sleep without any BUSY wait, for the fault path. Same commands as sleepPanel().
void EpdTri::emergencySleep() {
  if (emergencyDone_) return;
  emergencyDone_ = true;
  switch (path_) {
    case TRI_PATH_V2_NEW:
      cmd(0x10); data(0x03);
      break;
    case TRI_PATH_V2_OLD:
      cmd(0x50); data(0xF7);
      cmd(0x02);
      cmd(0x07); data(0xA5);
      break;
    case TRI_PATH_BC_V1:
      cmd(0x02);
      cmd(0x07); data(0xA5);
      break;
    default:
      return;
  }
  logf("fault sleep sent (no BUSY wait) path=%s", pathName(path_));
}

// ---------------------------------------------------------------------
// 4-wire bit-banged SPI, mode 0, MSB first. Same pin dance as the vendor
// EPD_SendData()/EPD_ReadData() (epdif.cpp): CS is framed per byte and DIN
// turns into an input for reads (3-wire read-back on the data line).
// ---------------------------------------------------------------------
void EpdTri::writeByte(uint8_t b) {
  hal_.write(p_.cs, false);
  for (int i = 0; i < 8; i++) {
    hal_.write(p_.sck, false);
    hal_.write(p_.din, (b & 0x80) != 0);
    hal_.write(p_.sck, true);
    b <<= 1;
  }
  hal_.write(p_.sck, false);
  hal_.write(p_.cs, true);
}

uint8_t EpdTri::readByte() {
  uint8_t v = 0;
  hal_.pinInput(p_.din);
  hal_.write(p_.cs, false);
  for (int i = 0; i < 8; i++) {
    hal_.write(p_.sck, false);
    v = (uint8_t)((v << 1) | (hal_.read(p_.din) ? 1 : 0));
    hal_.write(p_.sck, true);
  }
  hal_.write(p_.sck, false);
  hal_.write(p_.cs, true);
  hal_.pinOutput(p_.din);
  return v;
}

void EpdTri::cmd(uint8_t c) {
  hal_.write(p_.dc, false);
  writeByte(c);
}

void EpdTri::data(uint8_t d) {
  hal_.write(p_.dc, true);
  writeByte(d);
}

// Delay that keeps the serial port served.
void EpdTri::waitMs(uint32_t ms) {
  uint32_t start = hal_.ms();
  while (hal_.ms() - start < ms) {
    hal_.service();
    hal_.pause1ms();
  }
}

void EpdTri::reset() {
  uint16_t low = cfg_.resetLowMs ? cfg_.resetLowMs : (path_ == TRI_PATH_BC_V1 ? RESET_LOW_BC_MS : RESET_LOW_V2_MS);
  hal_.write(p_.rst, true);
  waitMs(200);
  hal_.write(p_.rst, false);
  waitMs(low);
  hal_.write(p_.rst, true);
  waitMs(200);
}

// EPD_4in2b_V2 Init(): DC low, send 0x2F, wait 50 ms, DC high, read a byte.
uint8_t EpdTri::probe() {
  hal_.write(p_.dc, false);
  writeByte(0x2F);
  waitMs(50);
  hal_.write(p_.dc, true);
  return readByte();
}

// BUSY level meaning differs per path:
//   v2_new: 1 = busy (ReadBusy_new)       v2_old / bc_v1: 0 = busy
bool EpdTri::busyNow() {
  bool level = hal_.read(p_.busy);
  return path_ == TRI_PATH_V2_NEW ? level : !level;
}

bool EpdTri::waitIdle(const char* stage, uint32_t timeoutMs) {
  uint32_t start = hal_.ms();
  for (;;) {
    // v2_old: ReadBusy_old sends GET_STATUS (0x71) then waits 20 ms per poll.
    if (path_ == TRI_PATH_V2_OLD) {
      cmd(0x71);
      waitMs(20);
    }
    if (!busyNow()) break;
    if (hal_.ms() - start >= timeoutMs) {
      char d[48];
      snprintf(d, sizeof(d), "stage=%s waited_ms=%lu", stage, (unsigned long)(hal_.ms() - start));
      fail(TRI_FAULT_BUSY_TIMEOUT, d);
      return false;
    }
    if (path_ == TRI_PATH_V2_NEW) waitMs(10);        // ReadBusy_new: 10 ms
    else if (path_ == TRI_PATH_BC_V1) waitMs(100);   // EPD_4IN2BC_ReadBusy: 100 ms
  }
  if (path_ == TRI_PATH_V2_OLD) waitMs(20);  // ReadBusy_old trailing delay
  return true;
}

bool EpdTri::initPanel() {
  reset();
  switch (path_) {
    case TRI_PATH_V2_NEW:  // EPD_4IN2B_V2_Init_new
      if (!waitIdle("init", cfg_.busyTimeoutMs)) return false;
      cmd(0x12);  // soft reset
      if (!waitIdle("swreset", cfg_.busyTimeoutMs)) return false;
      cmd(0x3C); data(0x05);  // border waveform
      cmd(0x18); data(0x80);  // internal temperature sensor
      cmd(0x11); data(0x03);  // data entry: X+, Y+
      cmd(0x44); data(0x00); data((WIDTH - 1) >> 3);  // RAM X window
      cmd(0x45); data(0x00); data(0x00); data((HEIGHT - 1) & 0xFF); data((HEIGHT - 1) >> 8);
      cmd(0x4E); data(0x00);                          // RAM X counter
      cmd(0x4F); data(0x00); data(0x00);              // RAM Y counter
      return waitIdle("init", cfg_.busyTimeoutMs);
    case TRI_PATH_V2_OLD:  // EPD_4IN2B_V2_Init_old
      if (!waitIdle("init", cfg_.busyTimeoutMs)) return false;
      cmd(0x04);  // power on
      if (!waitIdle("power_on", cfg_.busyTimeoutMs)) return false;
      cmd(0x00); data(0x0F);  // panel setting: LUT from OTP
      return true;
    case TRI_PATH_BC_V1:  // EPD_4IN2BC_Init
      cmd(0x06); data(0x17); data(0x17); data(0x17);  // booster soft start
      cmd(0x04);                                      // power on
      if (!waitIdle("power_on", cfg_.busyTimeoutMs)) return false;
      cmd(0x00); data(0x0F);  // panel setting: LUT from OTP
      return true;
    default:
      return false;
  }
}

// Returns the number of 0 bits actually sent (for the DIAG planes line).
uint32_t EpdTri::sendPlane(uint8_t command, const uint8_t* plane, bool invert) {
  hal_.service();
  cmd(command);
  hal_.write(p_.dc, true);
  const size_t row = WIDTH / 8;
  uint32_t zeros = 0;
  for (size_t i = 0; i < PLANE_BYTES; i++) {
    uint8_t b = invert ? (uint8_t)~plane[i] : plane[i];
    writeByte(b);
    for (uint8_t m = 0x80; m; m >>= 1) zeros += !(b & m);
    if ((i + 1) % row == 0) hal_.service();  // ~every 50 bytes
  }
  return zeros;
}

void EpdTri::sleepPanel() {
  switch (path_) {
    case TRI_PATH_V2_NEW:  // EPD_4IN2B_V2_Sleep_new
      cmd(0x10); data(0x03);
      break;
    case TRI_PATH_V2_OLD:  // EPD_4IN2B_V2_Sleep_old
      cmd(0x50); data(0xF7);
      cmd(0x02);
      if (!waitIdle("power_off", cfg_.busyTimeoutMs)) return;
      cmd(0x07); data(0xA5);
      break;
    case TRI_PATH_BC_V1:  // EPD_4IN2BC_Sleep
      cmd(0x02);
      if (!waitIdle("power_off", cfg_.busyTimeoutMs)) return;
      cmd(0x07); data(0xA5);
      break;
    default:
      break;
  }
}

bool EpdTri::begin() {
  hal_.pinOutput(p_.cs);
  hal_.write(p_.cs, true);
  hal_.pinOutput(p_.sck);
  hal_.write(p_.sck, false);
  hal_.pinOutput(p_.din);
  hal_.pinOutput(p_.dc);
  hal_.pinOutput(p_.rst);
  hal_.write(p_.rst, true);
  hal_.pinInput(p_.busy);

  switch (cfg_.profile) {
    case TRI_PROFILE_V2_NEW: path_ = TRI_PATH_V2_NEW; break;
    case TRI_PROFILE_V2_OLD: path_ = TRI_PATH_V2_OLD; break;
    case TRI_PROFILE_BC_V1: path_ = TRI_PATH_BC_V1; break;
    default: {
      // Vendor rule (EPD_4IN2B_V2_Init): read-back 0x01 -> new, anything else -> old.
      path_ = TRI_PATH_NONE;
      reset();
      uint8_t n = cfg_.probeReads ? cfg_.probeReads : 1;
      char seen[40] = "";
      int first = -1;
      bool stable = true;
      for (uint8_t i = 0; i < n; i++) {
        uint8_t v = probe();
        size_t len = 0;
        while (seen[len]) len++;
        snprintf(seen + len, sizeof(seen) - len, "%s0x%02X", i ? "," : "", v);
        if (first < 0) first = v;
        else if (v != first) stable = false;
      }
      probe_ = first;
      if (!stable) {
        char d[64];
        snprintf(d, sizeof(d), "reads=%s", seen);
        fail(TRI_FAULT_PROBE_UNSTABLE, d);
        return false;
      }
      path_ = first == 0x01 ? TRI_PATH_V2_NEW : TRI_PATH_V2_OLD;
      // basis=read_0x01: the panel answered as the vendor's new panel.
      // basis=else_fallback: nothing confirmed the old path, it is only the
      // vendor's else branch. 0xFF is also what an undriven (high) DIN reads.
      logf("probe cmd=0x2F reads=%s rule=0x01->v2_new,else->v2_old basis=%s%s", seen,
           first == 0x01 ? "read_0x01" : "else_fallback", first == 0xFF ? " note=0xFF_same_as_undriven_DIN" : "");
      break;
    }
  }
  bool inv = path_ == TRI_PATH_V2_NEW || (path_ == TRI_PATH_V2_OLD && cfg_.v2OldRyInvert);
  logf("config profile=%s path=%s ry_invert=%d reset_low_ms=%u busy_timeout_ms=%lu refresh_timeout_ms=%lu",
       profileName(cfg_.profile), pathName(path_), inv ? 1 : 0,
       cfg_.resetLowMs ? cfg_.resetLowMs : (path_ == TRI_PATH_BC_V1 ? RESET_LOW_BC_MS : RESET_LOW_V2_MS),
       (unsigned long)cfg_.busyTimeoutMs, (unsigned long)cfg_.refreshTimeoutMs);
  return true;
}

bool EpdTri::display(const uint8_t* black, const uint8_t* ry) {
  if (faulted() || path_ == TRI_PATH_NONE) return false;
  uint32_t t0 = hal_.ms();
  if (!initPanel()) return false;

  // Plane registers and RY polarity per vendor Display_*():
  //   v2_new: 0x24 black, 0x26 ~ry            (EPD_4IN2B_V2_Display_new)
  //   v2_old: 0x10 black, 0x13 ~ry  [default] (EPD_4in2b_V2 Display_old)
  //           0x10 black, 0x13  ry  [opt-out] (EPD_4in2b_V2_old Display_old)
  //   bc_v1:  0x10 black, 0x13  ry            (EPD_4IN2BC_Display)
  bool v2new = path_ == TRI_PATH_V2_NEW;
  bool inv = v2new || (path_ == TRI_PATH_V2_OLD && cfg_.v2OldRyInvert);
  uint8_t bCmd = v2new ? 0x24 : 0x10, rCmd = v2new ? 0x26 : 0x13;
  uint32_t bZeros = sendPlane(bCmd, black, false);
  uint32_t rZeros = sendPlane(rCmd, ry, inv);
  // What the frame asked for vs. the bits that went out. Says nothing about
  // what the panel shows; it lets a screen report be matched to the bytes.
  const uint32_t px = (uint32_t)WIDTH * HEIGHT;
  logf("planes path=%s black_cmd=0x%02X black_ink_px=%lu ry_cmd=0x%02X ry_invert=%d ry_color_px=%lu "
       "ry_sent_zero_bits=%lu ry_sent_one_bits=%lu",
       pathName(path_), bCmd, (unsigned long)bZeros, rCmd, inv ? 1 : 0,
       (unsigned long)(inv ? px - rZeros : rZeros), (unsigned long)rZeros, (unsigned long)(px - rZeros));

  uint32_t t1 = hal_.ms();
  if (v2new) {
    cmd(0x22); data(0xF7);  // display update control: full
    cmd(0x20);              // master activation
  } else {
    cmd(0x12);  // display refresh
    waitMs(100);
  }
  if (!waitIdle("refresh", cfg_.refreshTimeoutMs)) return false;
  uint32_t busyMs = hal_.ms() - t1;
  sleepPanel();
  if (faulted()) return false;

  lastRefreshMs_ = hal_.ms() - t0;
  logf("refresh done path=%s busy_ms=%lu total_ms=%lu", pathName(path_), (unsigned long)busyMs,
       (unsigned long)lastRefreshMs_);
  // A three-colour full refresh takes ~15 s (vendor spec); a very quick
  // BUSY release usually means BUSY is not following the panel.
  if (busyMs < 1000) logf("warn refresh_fast busy_ms=%lu (expected ~15000)", (unsigned long)busyMs);
  return true;
}
