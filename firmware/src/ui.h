#pragma once
#include "model.h"

namespace ui {

// Set up SPI + panel. `pollFn` is called repeatedly while the panel is busy
// refreshing so serial input keeps being consumed (must not draw). `logFn`
// gets each driver DIAG line (no newline); nullptr = USB serial only.
void begin(void (*pollFn)(), void (*logFn)(const char* line) = nullptr);

const char* panelName();
int width();
int height();

// Three-colour panels: every refresh is a full refresh (no partial path).
bool fullRefreshOnly();
// Driver gave up (BUSY timeout / undecidable read-back); stop refreshing.
bool faulted();

// Draw `view`. full=true: full refresh (slow, clears ghosting), else partial.
// Ignored on three-colour panels (always full).
void render(const ViewState& view, bool full);

}  // namespace ui
