#pragma once
// When to refresh the panel. Pure logic (no Arduino) so it can be tested on
// the host, see test/native.

#include <stdint.h>

enum RefreshKind : uint8_t { REFRESH_NONE = 0, REFRESH_PARTIAL, REFRESH_FULL };

struct RefreshPolicy {
  bool fullOnly;           // three-colour: every refresh is a full refresh
  uint32_t minIntervalMs;  // measured from the end of the previous refresh
  int fullEvery;           // B/W: full refresh after N partial ones
  uint32_t fullMs;         // B/W: full refresh at least this often
  uint32_t maxIdleMs;      // refresh even if unchanged after this long (0 = never)
  // Once a data screen is shown: at least this long between refreshes (the host's
  // `every`; 0 / shorter than minIntervalMs = minIntervalMs). full_refresh ignores it.
  // No default member initializer: older GCC then refuses the brace init in main.cpp.
  uint32_t dataIntervalMs;
};

struct RefreshState {
  uint32_t lastDoneMs = 0;  // millis() when the previous refresh finished
  uint32_t lastFullMs = 0;
  int partialCount = 0;
  bool dashboardShown = false;
};

// changed: what should be shown differs from what is shown.
// forceFull: host sent full_refresh. faulted: driver gave up. refreshNow: host sent refresh_now: refresh even
// if nothing changed and without waiting for the minimum interval (once; the caller clears it).
inline RefreshKind decideRefresh(const RefreshPolicy& p, const RefreshState& s, uint32_t now, bool changed,
                                 bool forceFull, bool hasData, bool faulted, bool refreshNow = false) {
  if (faulted) return REFRESH_NONE;
  if (refreshNow) return REFRESH_FULL;
  bool stale = p.maxIdleMs && now - s.lastDoneMs >= p.maxIdleMs;
  if (!changed && !forceFull && !stale) return REFRESH_NONE;
  uint32_t gap = p.minIntervalMs;
  if (s.dashboardShown && !forceFull && p.dataIntervalMs > gap) gap = p.dataIntervalMs;
  if (now - s.lastDoneMs < gap) return REFRESH_NONE;
  if (p.fullOnly) return REFRESH_FULL;
  bool full = forceFull || stale || (hasData && !s.dashboardShown) || s.partialCount >= p.fullEvery ||
              now - s.lastFullMs >= p.fullMs;
  return full ? REFRESH_FULL : REFRESH_PARTIAL;
}

// Call after the refresh returned; `doneMs` is millis() at that point.
inline void noteRefresh(RefreshState& s, RefreshKind k, uint32_t doneMs, bool hasData) {
  s.lastDoneMs = doneMs;
  if (k == REFRESH_FULL) {
    s.partialCount = 0;
    s.lastFullMs = doneMs;
  } else {
    s.partialCount++;
  }
  if (hasData) s.dashboardShown = true;
}

// Offline is judged from message arrival only, independent of refreshes.
inline bool isOnline(uint32_t lastMsgMs, uint32_t now, uint32_t timeoutMs) {
  return lastMsgMs != 0 && now - lastMsgMs < timeoutMs;
}

// Snapshot bookkeeping shared by the boot refresh and the loop. `render`
// may serve serial (and so change the live view) while the panel is busy;
// only the snapshot actually drawn is recorded as shown, so anything that
// arrives meanwhile stays pending for the next allowed refresh.
//   View: copyable, has `hasData` and `signature()` returning Sig.
template <class View, class Sig>
struct RefreshScheduler {
  RefreshPolicy policy{};
  RefreshState state;
  Sig shownSig{};
  bool forceFull = false;  // set by the protocol on full_refresh
  bool refreshNow = false;  // set by the protocol on refresh_now: bypasses the minimum interval once

  // signature() builds a String (heap) and the loop runs every 5 ms, so it is not recomputed on every pass:
  //  - not at all while the interval alone forbids a refresh (nothing it could say would matter), and
  //  - otherwise only after markDirty() (the caller changed the view) or sigRecheckMs after the last time.
  // sigRecheckMs = 0 (default) = every pass, the original behaviour. When refreshes happen is the same as long as
  // every change to the view is announced with markDirty() or lands within sigRecheckMs.
  uint32_t sigRecheckMs = 0;
  void markDirty() { sigDirty = true; }
  uint32_t sigComputed = 0;  // how many times signature() was computed (diagnostics / tests)

  // Boot screen: unconditional full refresh (skipped if the driver faulted).
  template <class Render, class Clock>
  void boot(const View& live, bool faulted, Render render, Clock clock) {
    View snap = live;
    if (!faulted) render(snap, REFRESH_FULL);
    shownSig = snap.signature();
    sigDirty = true;
    noteRefresh(state, REFRESH_FULL, clock(), snap.hasData);
  }

  template <class Render, class Clock>
  RefreshKind tick(const View& live, uint32_t now, bool faulted, Render render, Clock clock) {
    // decideRefresh() only gets more permissive when `changed` is true, so if it says no even then, the
    // signature cannot matter.
    if (decideRefresh(policy, state, now, true, forceFull, live.hasData, faulted, refreshNow) == REFRESH_NONE)
      return REFRESH_NONE;
    if (sigDirty || !haveSig || sigRecheckMs == 0 || now - sigAtMs >= sigRecheckMs) {
      curSig = live.signature();
      sigAtMs = now;
      sigDirty = false;
      haveSig = true;
      sigComputed++;
    }
    RefreshKind kind = decideRefresh(policy, state, now, !(curSig == shownSig), forceFull, live.hasData, faulted,
                                    refreshNow);
    if (kind == REFRESH_NONE) return kind;
    // Nothing can change `live` before the copy below, so the snapshot is what was just judged; copy only
    // when we actually draw (the v2 view is ~1 KB).
    View snap = live;
    forceFull = false;  // a full_refresh arriving during render stays pending
    refreshNow = false;  // so does a refresh_now: it runs right after this refresh
    render(snap, kind);
    shownSig = snap.signature();  // exactly what was drawn, even if curSig is up to sigRecheckMs old
    sigDirty = true;
    noteRefresh(state, kind, clock(), snap.hasData);
    return kind;
  }

 private:
  Sig curSig{};
  bool haveSig = false;
  bool sigDirty = true;
  uint32_t sigAtMs = 0;
};
