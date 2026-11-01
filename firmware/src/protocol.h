#pragma once
// Serial / WiFi / BLE independent protocol layer: one JSON line in, the view
// model updated, a reply decided. Nothing here touches Serial, so the same
// function can be fed by any transport and tested on the host.
//
// Protocol v1: stats / ping / full_refresh.            (README §6)
// Protocol v2: dash / quota / models / heat / mouse    (docs/dashboard-v2-protocol.md)
//              plus v2 ping / full_refresh.

#include <stddef.h>
#include <stdint.h>

#include "img_rx.h"
#include "model.h"
#include "page_policy.h"
#include "weather_policy.h"

// One line (without the trailing '\n') may be at most this long. Keep in
// step with MAX_LINE_LEN in config.h (the serial buffer).
#define PROTO_MAX_LINE_LEN 1024

// Which v2 data block a line carried (set only when the line was accepted).
enum Kind : uint8_t { KIND_NONE = 0, KIND_DASH, KIND_QUOTA, KIND_MODELS, KIND_HEAT, KIND_MOUSE, KIND_COUNT };

// Device commands (v2 only). They never touch `view`; the transport layer
// executes them (and refuses them on WiFi: they are USB-only, except
// ota_begin which is TCP-only).
enum Cmd : uint8_t {
  CMD_NONE = 0,
  CMD_WIFI_SCAN,
  CMD_WIFI_SET,
  CMD_WIFI_STATUS,
  CMD_WIFI_CLEAR,
  CMD_RESET_SETTINGS,
  CMD_PAIR,
  CMD_OTA_BEGIN,
  CMD_WX_CFG,    // offline weather settings (USB or TCP)
  CMD_WX_STATUS, // reply: one "WX {...}" line, then OK
  CMD_PAGE,         // manual page override (USB or TCP)
  CMD_PAGE_STATUS,  // reply: one "PAGE {...}" line, then OK
  CMD_REFRESH_NOW,  // next refresh bypasses the minimum interval once
  CMD_IMG_BEGIN,    // TODO image transfer (USB or TCP)
  CMD_IMG_CHUNK,
  CMD_IMG_END
};

#define PROTO_SSID_MAX 32         // bytes
#define PROTO_PASS_MAX 63         // bytes
#define PROTO_TOKEN_MAX 32        // bytes
#define PROTO_OTA_SIZE_MAX 16777215UL

struct HandleResult {
  bool ok = false;         // reply "OK"; otherwise reply "ERR <err>"
  bool blank = false;      // empty line: ignore, no reply at all
  bool forceFull = false;  // full_refresh: the next refresh must be a full one
  char err[48] = "";       // error text without the "ERR " prefix, e.g. "bad_version"
  Kind kind = KIND_NONE;   // v2 data block accepted (so the caller can store the raw line)
  Cmd cmd = CMD_NONE;      // device command accepted (ok is true; the caller executes it)
  char ssid[PROTO_SSID_MAX + 1] = "";  // CMD_WIFI_SET only
  char pass[PROTO_PASS_MAX + 1] = "";  // CMD_WIFI_SET only; may be empty (open network)
  uint32_t otaSize = 0;                    // CMD_OTA_BEGIN only: image bytes that follow the OK
  char md5[33] = "";                       // CMD_OTA_BEGIN only: lowercase hex
  char token[PROTO_TOKEN_MAX + 1] = "";    // CMD_OTA_BEGIN only: pairing code
  // CMD_WX_CFG only. wxOn = enable flag; wxSetLoc: place/lat/lon were given (all three, validated);
  // wxAfterMin 0 = not given (keep the stored value).
  bool wxOn = false;
  bool wxSetLoc = false;
  char wxPlace[WX_PLACE_MAX + 1] = "";
  double wxLat = 0, wxLon = 0;
  uint16_t wxAfterMin = 0;
  // CMD_PAGE only.
  pagep::Mode pageMode = pagep::PG_AUTO;
  // CMD_IMG_BEGIN: imgSize / imgCrc. CMD_IMG_CHUNK: imgOff + imgLen bytes at imgData, which points into a static
  // buffer that is valid until the next handleLine() call.
  uint32_t imgSize = 0, imgCrc = 0, imgOff = 0;
  const uint8_t* imgData = nullptr;
  uint16_t imgLen = 0;
};

// Parse `line` and, if valid, update `view` (hasData / v2 / stats / blocks).
// On error `view` is left unchanged. Error texts:
//   bad_json <ArduinoJson error>, not_object, bad_version, missing_type,
//   unknown_type, too_long, missing_field <name>, bad_field <name>
// The device commands wifi_scan / wifi_set / wifi_status / wifi_clear /
// reset_settings need "v":2 (else bad_version); wifi_set validates ssid
// (1..32 bytes, required) and pass (0..63 bytes, missing = empty). pair
// takes no fields; ota_begin needs size (1..16777215), md5 (32 hex digits,
// returned lowercase) and token (1..32 bytes). wx_cfg (v2): "on" (bool, required); when on, optional
// place (1..23 bytes) + lat (-90..90) + lon (-180..180) which must come together, and after_min (1..240);
// wx_status (v2) takes no fields. page (v2): p = auto|dash|weather|todo; page_status / refresh_now take no fields.
// img_begin (v2): id "todo", size (1..IMG_MAX_COMPRESSED, larger = too_big), crc (uint32); img_chunk: id, off (uint32),
// d (base64, 1..IMG_CHUNK_MAX bytes); img_end: id. Wrong id = bad_field id.
HandleResult handleLine(ViewState& view, const char* line, size_t len);

// The transport dropped an oversized line before it got here.
HandleResult tooLongResult();
