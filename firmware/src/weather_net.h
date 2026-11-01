#pragma once
// Open-Meteo fetch over HTTPS, run in its own FreeRTOS task so the main loop (serial, TCP, panel
// busy callback) is never blocked by DNS / TLS / download. Only built with ENABLE_WIFI.
// One fetch at a time: start(), then poll take() from loop().

#include <stddef.h>
#include <stdint.h>

#include "config.h"
#include "weather_model.h"

#if ENABLE_WIFI
namespace wxnet {

// Begin a fetch for (lat, lon); `place` is copied into the result. False if one is already running.
bool start(double lat, double lon, const char* place);

// True while the task is running.
bool busy();

// Call from loop(): a task running longer than WX_TASK_MAX_MS (90 s) is reported as failed ("timeout" via take()).
// Returns true on the call that gave up. start() refuses until that task has really ended.
bool watchdog(uint32_t now);

// Least free stack of the last finished task, bytes (0 = unknown / timed out), for the DIAG line.
uint32_t stackFreeBytes();

// If a fetch finished since the last call: true, `ok` tells success; on success `out` holds the data
// (status WX_OK), otherwise `err` a short reason ("sntp", "tls", "http_503", "parse", "low_mem", ...).
bool take(bool& ok, wxb::WeatherB& out, char* err, size_t errN);

}  // namespace wxnet
#endif
