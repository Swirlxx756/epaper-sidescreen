#pragma once
// Manual page override (host-testable): which page is forced, what is shown, when a forced weather page fetches.

#include <stdint.h>
#include <string.h>

#include "weather_policy.h"

namespace pagep {

enum Mode : uint8_t { PG_AUTO = 0, PG_DASH, PG_WEATHER, PG_TODO };
enum Shown : uint8_t { SH_DASH = 0, SH_WEATHER, SH_TODO };

inline const char* modeName(Mode m) {
  return m == PG_DASH ? "dash" : m == PG_WEATHER ? "weather" : m == PG_TODO ? "todo" : "auto";
}
inline bool parseMode(const char* s, Mode& m) {
  const Mode all[] = {PG_AUTO, PG_DASH, PG_WEATHER, PG_TODO};
  for (Mode x : all)
    if (strcmp(s, modeName(x)) == 0) {
      m = x;
      return true;
    }
  return false;
}
inline const char* shownName(Shown s) { return s == SH_WEATHER ? "weather" : s == SH_TODO ? "todo" : "dash"; }
inline bool validStored(uint8_t v) { return v <= PG_TODO; }

// auto: weather when the offline rules want it (autoWeather), else the dashboard.
inline Shown shown(Mode m, bool autoWeather) {
  switch (m) {
    case PG_DASH: return SH_DASH;
    case PG_WEATHER: return SH_WEATHER;
    case PG_TODO: return SH_TODO;
    default: return autoWeather ? SH_WEATHER : SH_DASH;
  }
}

// Forced weather: fetch when there is no data or it is older than the 30 min refetch period (and the normal
// retry gap after a failure has passed).
inline bool forcedFetchDue(const wxp::Fetch& f, uint32_t now) {
  if (f.haveData && now - f.okMs < WX_REFETCH_MS) return false;
  return wxp::fetchDue(f, now);
}

// Forced weather with nothing to draw yet but a fetch on the way: hold the refresh (at most `capMs` after the
// page was set) so the first forced weather draw is not an empty placeholder.
inline bool holdForFetch(Mode m, bool haveData, bool fetching, uint32_t now, uint32_t setMs, uint32_t capMs) {
  return m == PG_WEATHER && !haveData && fetching && now - setMs < capMs;
}

// The forced weather page was drawn as a placeholder (location set, no data yet).
inline bool placeholderDrawn(Mode m, bool hasLoc, bool haveData) { return m == PG_WEATHER && hasLoc && !haveData; }

// The fetch that was held for has just delivered data and the page on the panel is still the placeholder: refresh
// once without waiting for the 180 s gap (like refresh_now), so the real data shows at once. Not for later refetches.
inline bool bypassAfterFetch(Mode m, bool placeholderOnPanel, bool gotData) {
  return m == PG_WEATHER && placeholderOnPanel && gotData;
}

}  // namespace pagep
