#pragma once
#include <Arduino.h>

#include "dash_model.h"
#include "weather_model.h"

// Everything the dashboard shows, already formatted for display.
// Missing / null fields are stored as "--" (strings) or -1 (percentages).
struct Stats {
  String time = "--";
  // System
  int cpu = -1, mem = -1, disk = -1;  // 0..100, -1 = unknown
  String netUp = "--", netDown = "--";
  bool hasGpu = false;  // false -> GPU row hidden
  int gpu = -1;
  String gpuTemp = "--";
  // Claude tokens
  String blockTokens = "--", blockCost = "--", burn = "--", remain = "--";
  String todayTokens = "--", todayCost = "--";
};

// What the screen should look like right now.
struct ViewState {
  bool hasData = false;  // false -> boot / waiting screen
  bool online = false;
  Stats s;            // protocol v1 (stats)
  bool v2 = false;    // true: the last data message was v2 -> draw the v2 dashboard
  dash::Dash d;       // protocol v2 blocks
  bool weather = false;  // host offline long enough: show the weather page (wx) instead
  wxb::WeatherB wx{};
  bool todo = false;     // TODO page (host-rendered image) instead of everything else
  uint32_t todoCrc = 0;  // crc32 of the stored compressed image; 0 = none stored (empty-state page)

  // Used to detect "content changed".
  String signature() const {
    if (todo) {
      char buf[24];
      snprintf(buf, sizeof(buf), "td|%08lx", (unsigned long)todoCrc);
      return String(buf);
    }
    if (weather) {
      char buf[24];
      uint64_t h = wxb::signature(wx);
      snprintf(buf, sizeof(buf), "wx|%08lx%08lx", (unsigned long)(h >> 32), (unsigned long)(h & 0xFFFFFFFFUL));
      return String(buf);
    }
    if (!hasData) return "boot";
    if (v2) {
      char buf[24];
      uint64_t h = d.signature();
      snprintf(buf, sizeof(buf), "v2|%c|%08lx%08lx", online ? '1' : '0', (unsigned long)(h >> 32),
               (unsigned long)(h & 0xFFFFFFFFUL));
      return String(buf);
    }
    String sig;
    sig.reserve(160);
    sig += online ? '1' : '0';
    const String parts[] = {s.time, String(s.cpu), String(s.mem), String(s.disk), s.netUp, s.netDown,
                            String(s.hasGpu), String(s.gpu), s.gpuTemp, s.blockTokens, s.blockCost,
                            s.burn, s.remain, s.todayTokens, s.todayCost};
    for (const String& p : parts) {
      sig += '|';
      sig += p;
    }
    return sig;
  }
};
