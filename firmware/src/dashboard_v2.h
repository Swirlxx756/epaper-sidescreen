#pragma once
// Dashboard v2 screen (400x300). Draws only through Adafruit_GFX + U8g2 for
// Adafruit_GFX, so the same code runs on the panel and in the offline
// preview (test/preview) that renders it to an image.
//
// Data meaning and layout: docs/dashboard-v2-protocol.md.

#include <Adafruit_GFX.h>
#include <U8g2_for_Adafruit_GFX.h>

#include "dash_model.h"

namespace dashv2 {

constexpr int WIDTH = 400;
constexpr int HEIGHT = 300;

struct Style {
  uint16_t accent;  // colour for alerts (GxEPD_RED / GxEPD_YELLOW; black on B/W panels)
  bool tri;         // true: accent is a real third colour
};

// The caller has cleared the screen to white and called u8g2.begin(g),
// setFontMode(1). `online` = host heard from within OFFLINE_TIMEOUT_MS.
void draw(Adafruit_GFX& g, U8G2_FOR_ADAFRUIT_GFX& u, const dash::Dash& d, bool online, const Style& style);

}  // namespace dashv2
