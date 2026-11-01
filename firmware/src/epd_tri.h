#pragma once
// 4.2" 400x300 three-colour (black/white + red/yellow) driver for the
// Waveshare e-Paper ESP32 Driver Board. Command sequences follow Waveshare's
// own drivers (EPD_4in2b_V2 / EPD_4in2bc), see firmware/VENDOR_SOURCES.md.
//
// This file has no Arduino dependency: all pin / timing access goes through
// EpdTriHal so the sequencing can be unit-tested on the host (test/native).

#include <stddef.h>
#include <stdint.h>

// Which vendor code path to run. AUTO_V2 is the vendor's own new/old
// selection in EPD_4in2b_V2 (0x2F read-back over DIN); the others force one
// path. AUTO only distinguishes those two V2 init paths. It does not
// identify the product model or tell red from yellow, and it never cycles
// through waveforms.
enum EpdTriProfile : uint8_t {
  TRI_PROFILE_AUTO_V2 = 0,  // EPD_4in2b_V2 Init(): read 0x2F, 0x01 -> new, else old
  TRI_PROFILE_V2_NEW = 1,   // EPD_4in2b_V2 *_new path (0x24/0x26, 0x22 0xF7 0x20)
  TRI_PROFILE_V2_OLD = 2,   // EPD_4in2b_V2 *_old path (0x10/0x13, 0x12, 0x71 busy poll)
  TRI_PROFILE_BC_V1 = 3,    // EPD_4in2bc (4.2" B/C V1: 0x06 booster, 0x10/0x13, 0x12)
};

// Path actually in use after begin().
enum EpdTriPath : uint8_t { TRI_PATH_NONE = 0, TRI_PATH_V2_NEW, TRI_PATH_V2_OLD, TRI_PATH_BC_V1 };

enum EpdTriFault : uint8_t {
  TRI_OK = 0,
  TRI_FAULT_BUSY_TIMEOUT,   // BUSY never released within the bound
  TRI_FAULT_PROBE_UNSTABLE, // AUTO: repeated 0x2F reads disagree -> cannot decide
};

class EpdTriHal {
 public:
  virtual ~EpdTriHal() {}
  virtual void pinOutput(int pin) = 0;
  virtual void pinInput(int pin) = 0;
  virtual void write(int pin, bool level) = 0;
  virtual bool read(int pin) = 0;
  virtual uint32_t ms() = 0;
  virtual void pause1ms() = 0;          // sleep ~1 ms
  virtual void service() = 0;           // keep serial alive; must not draw
  virtual void log(const char* line) = 0;  // one diagnostic line, no newline
};

struct EpdTriPins {
  int sck, din, cs, dc, rst, busy;
};

struct EpdTriConfig {
  EpdTriProfile profile;
  bool v2OldRyInvert;      // V2 old path: send ~ry (EPD_4in2b_V2) or ry (EPD_4in2b_V2_old)
  uint16_t resetLowMs;     // 0 = vendor default for the path
  uint8_t probeReads;      // AUTO: identical 0x2F reads required (vendor reads once)
  uint32_t busyTimeoutMs;  // reset / power-on / power-off waits
  uint32_t refreshTimeoutMs;
};

class EpdTri {
 public:
  static const int WIDTH = 400;
  static const int HEIGHT = 300;
  static const size_t PLANE_BYTES = WIDTH / 8 * HEIGHT;  // 15000

  EpdTri(EpdTriHal& hal, const EpdTriPins& pins, const EpdTriConfig& cfg) : hal_(hal), p_(pins), cfg_(cfg) {}

  // Configure pins and select the path (AUTO: reset + 0x2F read-back once).
  // Returns false on fault (see fault()).
  bool begin();

  // Full refresh: reset, init, write both planes, refresh, deep sleep.
  // Plane convention (vendor Paint): bit 1 = white, bit 0 = ink. `ry` 0 bits
  // are drawn in the third colour. Returns false on fault; once faulted the
  // driver refuses further refreshes until reboot. On a BUSY timeout it first
  // sends the power-off / deep-sleep commands without waiting (best effort).
  bool display(const uint8_t* black, const uint8_t* ry);

  EpdTriPath path() const { return path_; }
  EpdTriFault fault() const { return fault_; }
  bool faulted() const { return fault_ != TRI_OK; }
  int probeValue() const { return probe_; }  // last 0x2F read-back, -1 = not read
  uint32_t lastRefreshMs() const { return lastRefreshMs_; }
  static const char* pathName(EpdTriPath p);
  static const char* profileName(EpdTriProfile p);

 private:
  void cmd(uint8_t c);
  void data(uint8_t d);
  void writeByte(uint8_t b);
  uint8_t readByte();
  void waitMs(uint32_t ms);
  void reset();
  uint8_t probe();
  bool busyNow();
  bool waitIdle(const char* stage, uint32_t timeoutMs);
  bool initPanel();
  uint32_t sendPlane(uint8_t command, const uint8_t* plane, bool invert);
  void sleepPanel();
  void emergencySleep();
  void fail(EpdTriFault f, const char* detail);
  void logf(const char* fmt, ...);

  EpdTriHal& hal_;
  EpdTriPins p_;
  EpdTriConfig cfg_;
  EpdTriPath path_ = TRI_PATH_NONE;
  EpdTriFault fault_ = TRI_OK;
  int probe_ = -1;
  bool emergencyDone_ = false;
  uint32_t lastRefreshMs_ = 0;
};
