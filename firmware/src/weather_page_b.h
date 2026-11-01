#pragma once
// Offline weather page, variant B ("dashboard twin"): same two-column structure, fonts, dotted
// separators and uppercase titles as dashboard_v2. Left = current conditions, right = NEXT 12H
// (temperature line + precipitation bars) and 3 DAYS. Draws only through Adafruit_GFX + U8g2.
// Pure drawing: no networking; the caller fills WeatherB (e.g. from Open-Meteo later).

#include <Adafruit_GFX.h>
#include <U8g2_for_Adafruit_GFX.h>

#include <stdint.h>

#include "weather_model.h"

namespace wxb {

// Caller has cleared the screen to white, u8g2.begin(g) and setFontMode(1).
// accent = red (or black on a B/W panel).
void draw(Adafruit_GFX& g, U8G2_FOR_ADAFRUIT_GFX& u, const WeatherB& w, uint16_t accent);

}  // namespace wxb
