#pragma once
// When to write the saved dashboard snapshot to NVS. Pure logic (no Arduino)
// so it can be tested on the host, see test/native/test_dash.cpp.
//
// Rule: write only when something changed AND at least `minGapMs` have passed
// since the last write. This keeps flash wear low (the dashboard time changes
// every minute) and loses at most the last `minGapMs` of changes on power cut.
// Exception: if nothing was saved at all yet, the first write is not delayed.

#include <stdint.h>

class SnapFlushPolicy {
 public:
  explicit SnapFlushPolicy(uint32_t minGapMs = 600000UL) : minGapMs_(minGapMs) {}

  // Call once at boot. `storeEmpty`: no snapshot is saved yet, so the first
  // flush may happen right away; otherwise the gap is counted from `now`.
  void begin(uint32_t now, bool storeEmpty) {
    dirty_ = false;
    counting_ = !storeEmpty;
    lastMs_ = now;
  }

  // Stored content changed in RAM.
  void markDirty() { dirty_ = true; }

  bool dirty() const { return dirty_; }

  // Unsigned subtraction: correct across the 49-day millis() wrap.
  bool due(uint32_t now) const { return dirty_ && (!counting_ || (uint32_t)(now - lastMs_) >= minGapMs_); }

  // Call after a flush attempt (also a failed one, so it is retried only
  // after the gap, not on every loop pass). `stillDirty`: part of it failed.
  void noteFlushed(uint32_t now, bool stillDirty = false) {
    dirty_ = stillDirty;
    counting_ = true;
    lastMs_ = now;
  }

  // Settings were wiped: forget pending changes and restart the gap.
  void restart(uint32_t now) {
    dirty_ = false;
    counting_ = true;
    lastMs_ = now;
  }

 private:
  uint32_t minGapMs_;
  uint32_t lastMs_ = 0;
  bool counting_ = true;  // false until the first flush when the store was empty
  bool dirty_ = false;
};
