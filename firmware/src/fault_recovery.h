#pragma once
// Display fault recovery rules (no Arduino, host-testable). A driver fault (BUSY never released) stops all
// refreshes until reboot; on a board nobody is watching that means a dead screen. So: a fault that lasts
// FAULT_REBOOT_AFTER_MS reboots the board, at most FAULT_MAX_BOOTS times in a row. The count lives in RTC
// memory (survives a software reset, not a power cycle) and is cleared by the first refresh that works.
// A panel that is really broken therefore reboots 3 times, then stays stopped (and says so in a DIAG line).

#include <stdint.h>

#define FAULT_REBOOT_AFTER_MS 180000UL  // fault -> reboot (>= the 180 s refresh gap, so a hot-plugged host sees it first)
#define FAULT_MAX_BOOTS 3               // consecutive fault reboots before giving up

namespace faultrec {

static const uint32_t MAGIC = 0xFA17B007u;

struct Rtc {  // placed in RTC_NOINIT memory by the caller
  uint32_t magic;
  uint32_t count;  // fault reboots so far, valid only with magic
};

// Fault reboots in a row that led to this boot. `softwareReset`: the reset reason was a software reset (a
// power-on or brown-out leaves RTC memory undefined and starts over).
inline uint32_t bootsSoFar(const Rtc& r, bool softwareReset) { return (softwareReset && r.magic == MAGIC) ? r.count : 0; }

// Called right before the fault reboot.
inline void noteReboot(Rtc& r, uint32_t bootsBefore) {
  r.magic = MAGIC;
  r.count = bootsBefore + 1;
}

// A refresh worked (or this boot was not a fault reboot): forget the count.
inline void clear(Rtc& r) {
  r.magic = 0;
  r.count = 0;
}

// Reboot now? `faultSinceMs`: millis() when the fault was first seen (0 = no fault).
inline bool rebootDue(uint32_t faultSinceMs, uint32_t now, uint32_t bootsBefore) {
  return faultSinceMs != 0 && bootsBefore < FAULT_MAX_BOOTS && now - faultSinceMs >= FAULT_REBOOT_AFTER_MS;
}

}  // namespace faultrec
