#pragma once

// =====================================================================
//  1. Panel selection — change EPD_PANEL to match the label on the back
//     of your display (or pass -DEPD_PANEL=... in platformio.ini).
// =====================================================================
#define PANEL_GDEY042T81 1  // 4.2"  400x300  B/W  (Waveshare 4.2" V2, SSD1683)  [default]
#define PANEL_GDEY029T94 2  // 2.9"  296x128  B/W  (Waveshare 2.9" V2, SSD1680)
#define PANEL_GDEY075T7  3  // 7.5"  800x480  B/W  (Waveshare 7.5" V2, UC8179)
#define PANEL_WS42_TRI   4  // 4.2"  400x300  black/white + red/yellow, Waveshare 4.2" (B)/(C)
                            // own driver (src/epd_tri.cpp), env waveshare_esp32_42tri

#ifndef EPD_PANEL
#define EPD_PANEL PANEL_GDEY042T81
#endif

// =====================================================================
//  2. Pins — set by board (env in platformio.ini). Edit the ESP32-S3
//     block if you wired the display differently.
// =====================================================================
#if defined(BOARD_WAVESHARE_ESP32)
// Waveshare e-Paper ESP32 Driver Board: fixed wiring, do not change.
#define EPD_SCK  13
#define EPD_MOSI 14
#define EPD_CS   15
#define EPD_DC   27
#define EPD_RST  26
#define EPD_BUSY 25

#elif defined(BOARD_ESP32S3)
// ESP32-S3 DevKitC + jumper cables (adapter: VCC->3V3, GND->GND, then below).
#ifndef EPD_SCK
#define EPD_SCK  12  // CLK
#endif
#ifndef EPD_MOSI
#define EPD_MOSI 11  // DIN
#endif
#ifndef EPD_CS
#define EPD_CS   10
#endif
#ifndef EPD_DC
#define EPD_DC   9
#endif
#ifndef EPD_RST
#define EPD_RST  8
#endif
#ifndef EPD_BUSY
#define EPD_BUSY 7
#endif

#else
#error "Unknown board: build with -DBOARD_WAVESHARE_ESP32 or -DBOARD_ESP32S3 (use a platformio.ini env)"
#endif

#define EPD_MISO -1  // B/W panels: write-only (three-colour reads back on DIN, see below)

// Reset pulse in ms. Waveshare boards/HATs with the "clever" reset circuit
// need a short pulse (2). Try 10 if a bare panel does not wake up.
#ifndef EPD_RESET_MS
#define EPD_RESET_MS 2
#endif

// =====================================================================
//  3. Behaviour
// =====================================================================
#define SERIAL_BAUD          115200
#define SERIAL_RX_BUFFER     4096       // bytes buffered while the panel refreshes
#define MAX_LINE_LEN         1024       // protocol v1 line limit
#define MIN_REFRESH_MS       5000UL     // min gap between two refreshes
#define FULL_REFRESH_EVERY   20         // full refresh after N partial ones
#define FULL_REFRESH_MS      (30UL * 60UL * 1000UL)
#define OFFLINE_TIMEOUT_MS   120000UL   // (host sends every 30 s) no valid message for this long -> OFFLINE

// WiFi transport + NVS persistence (docs/dashboard-v2-protocol.md, "WiFi
// 传输"). Off by default; only env waveshare_esp32_42tri turns it on
// (-DENABLE_WIFI=1). When 0, none of the WiFi / NVS code is compiled.
#ifndef ENABLE_WIFI
#define ENABLE_WIFI 0
#endif
#define WIFI_TCP_PORT        7330       // one TCP client at a time, same line protocol as serial
#define WIFI_UDP_PORT        7331       // discovery: {"v":2,"type":"discover"} -> unicast "here"
#define WIFI_CONNECT_MS      20000UL    // give up one connection attempt after this long
#define WIFI_RETRY_MS        30000UL    // after "failed", try again this often
#define SNAP_FLUSH_MIN_MS    600000UL   // min gap between two NVS snapshot writes (flash wear)
#define RESET_HOLD_MS        5000UL     // BOOT (GPIO0) held this long at runtime = reset_settings
#define OTA_IDLE_MS          10000UL    // WiFi upgrade: give up after this long without image bytes
#define OTA_REBOOT_DELAY_MS  500UL      // after the final OK, before restarting into the new firmware
#define BOOT_BUTTON_PIN      0

// =====================================================================
//  4. Three-colour 4.2" (EPD_PANEL == PANEL_WS42_TRI) — see README §9
// =====================================================================
// The values below are generic defaults. env waveshare_esp32_42tri (the
// production build for this project's panel) overrides PROFILE, RY_INVERT
// and TEST_PATTERN with -D flags in platformio.ini; a -D flag always wins
// over the #ifndef default here.
// Vendor code path:
//   0 = auto_v2 (default): Waveshare EPD_4in2b_V2 rule — send 0x2F, read one
//       byte back on DIN; 0x01 -> v2_new, anything else -> v2_old. Only picks
//       between those two V2 paths; it does not identify the model or the
//       third colour, and nothing is retried with another waveform.
//   1 = v2_new  2 = v2_old  3 = bc_v1 (4.2" B/C V1, EPD_4in2bc; never auto)
#ifndef EPD_TRI_PROFILE
#define EPD_TRI_PROFILE 0
#endif
// v2_old only. The two vendor copies disagree: EPD_4in2b_V2 sends ~ry (1,
// default, also the vendor's Raspberry Pi C/Python drivers) and
// EPD_4in2b_V2_old / the vendor Arduino driver send ry unchanged (0).
// Set 0 if red/yellow areas come out inverted on a v2_old panel.
#ifndef EPD_TRI_V2_OLD_RY_INVERT
#define EPD_TRI_V2_OLD_RY_INVERT 1
#endif
// Reset low pulse; 0 = vendor value for the path (V2 2 ms, B/C V1 200 ms).
#ifndef EPD_TRI_RESET_LOW_MS
#define EPD_TRI_RESET_LOW_MS 0
#endif
// auto_v2: number of identical 0x2F reads required (vendor reads once).
// Disagreeing reads -> fault "probe_unstable", no refresh, set a profile.
#ifndef EPD_TRI_PROBE_READS
#define EPD_TRI_PROBE_READS 3
#endif
// BUSY bounds. Vendor spec: full refresh ~15 s.
#ifndef EPD_TRI_BUSY_TIMEOUT_MS
#define EPD_TRI_BUSY_TIMEOUT_MS 5000UL      // reset / power on / power off
#endif
#ifndef EPD_TRI_REFRESH_TIMEOUT_MS
#define EPD_TRI_REFRESH_TIMEOUT_MS 45000UL  // refresh (3x the 15 s spec)
#endif
// Gap from the END of one refresh to the start of the next. Vendor wiki:
// "refresh interval at least 180s"; never below 60 s.
#ifndef EPD_TRI_MIN_REFRESH_MS
#define EPD_TRI_MIN_REFRESH_MS 180000UL
#endif
// Vendor wiki: refresh three-colour panels at least once every 24 h.
#ifndef EPD_TRI_MAX_IDLE_MS
#define EPD_TRI_MAX_IDLE_MS (24UL * 60UL * 60UL * 1000UL)
#endif
// Bars at or above this percentage are drawn in the third colour.
#ifndef EPD_TRI_ALERT_PCT
#define EPD_TRI_ALERT_PCT 90
#endif

// Diagnostic build only: 1 = boot screen is the calibration frame
// (src/tri_calib.h, three bands black | red/yellow | white) instead of the
// normal boot text. The dashboard is unchanged. See env
// waveshare_esp32_42tri_calib and firmware/README.md section 9.5a.
#ifndef EPD_TRI_TEST_PATTERN
#define EPD_TRI_TEST_PATTERN 0
#endif

#if EPD_PANEL == PANEL_WS42_TRI
static_assert(EPD_TRI_MIN_REFRESH_MS >= 60000UL, "three-colour refresh gap must be >= 60 s");
static_assert(EPD_TRI_PROFILE >= 0 && EPD_TRI_PROFILE <= 3, "EPD_TRI_PROFILE must be 0..3");
#endif

#define FW_NAME    "epaper-sidescreen"
#define FW_PROTO   "v2"  // protocol: v1 stats + v2 dashboard blocks
// Firmware release. Three-colour builds print it before READY as
// "DIAG fw version=... test_pattern=N" (READY itself is unchanged).
//   1.1.0  production three-colour env pinned to v2_old / ry_invert 0
//   1.2.0  dashboard v2: protocol v2 blocks + new 400x300 screen (READY says v2)
//   1.3.0  WiFi transport (TCP 7330 + UDP discovery 7331), USB provisioning,
//          NVS persistence of WiFi credentials and the last dashboard
//   1.3.1  DIAG lines (display faults, refresh done) also go to the TCP client
//          + firmware upgrade over WiFi (pair / ota_begin, rollback until confirmed)
//   1.4.0  heat calendar: 7 rows x 10 weeks, no outline / gap, 12 levels (red, then black dots)
//   1.5.0  offline weather: wx_cfg / wx_status, Open-Meteo fetch over HTTPS, weather page after the host
//          has been silent for after_min minutes
//   1.6.0  manual page override (page / page_status), TODO page from a host-rendered RLE image
//          (img_begin / img_chunk / img_end, kept in the spiffs partition), refresh_now
//   1.7.0  robustness: display fault -> sleep commands + auto reboot (max 3 in a row), TCP replies in one write +
//          keepalive, a running upgrade / image transfer keeps its connection, weather fetch watchdog + bounded
//          body, weather config in one NVS blob, unchanged TODO image not rewritten, OTA confirm retried
#define FW_VERSION "1.7.1"
