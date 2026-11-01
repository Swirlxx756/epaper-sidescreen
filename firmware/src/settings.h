#pragma once
// Persistent settings in NVS (Preferences). Only built with ENABLE_WIFI.
//   namespace "wifi": ssid, pass
//   namespace "snap": raw text of the last accepted dash / quota / models /
//                     heat / mouse line (one key each)
//   namespace "wx":   offline weather config, one blob "cfg" (wxp::encodeCfg); the separate keys of 1.5.0 / 1.6.0
//                     (on, after, place, lat, lon) are read once when there is no blob
//   namespace "page": manual page override (one byte)
//   namespace "ota":  token (WiFi firmware upgrade pairing code, 16 hex)
// Nothing here prints; the password and the token are never logged.

#include <stddef.h>
#include <stdint.h>

#include "config.h"
#include "page_policy.h"
#include "protocol.h"
#include "weather_policy.h"

#if ENABLE_WIFI
namespace settings {

// Load the saved snapshot lines into RAM. `now` = millis() at boot.
void begin(uint32_t now);

// WiFi credentials. ssid needs 33 bytes, pass 64. Returns true if a SSID is saved.
bool loadWifi(char* ssid, char* pass);
bool saveWifi(const char* ssid, const char* pass);  // false if the NVS write failed
void clearWifi();

// Snapshot. snapLine() returns the RAM copy ("" = none). snapPut() stores an
// accepted raw line and marks it for the next flush only if it differs.
const char* snapLine(Kind k);
void snapPut(Kind k, const char* line, size_t len);
// Call from loop(): writes the changed lines when SnapFlushPolicy says so.
void service(uint32_t now);

// Pairing code for WiFi upgrades into `out` (17 bytes). `create`: make and
// save a random one if none exists. False (out = "") if none / not saved.
bool otaToken(char* out, bool create);

// Offline weather configuration (RAM copy loaded by begin()). wxSave() stores and updates the copy;
// false if the NVS write failed (the copy is then unchanged).
const wxp::Cfg& wxCfg();
bool wxSave(const wxp::Cfg& c);

// Manual page override (RAM copy loaded by begin()). pageSave() false if the NVS write failed.
pagep::Mode pageMode();
bool pageSave(pagep::Mode m);

// Factory reset: wipes all namespaces and the RAM copies.
void resetAll(uint32_t now);

}  // namespace settings
#endif
