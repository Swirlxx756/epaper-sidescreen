#pragma once
// Last good TODO image (compressed, <= IMG_MAX_COMPRESSED bytes) kept across reboots in the otherwise unused
// "spiffs" data partition (default partition table, 1.4 MB at 0x290000), used raw through esp_partition: the NVS
// partition is only 20 KB and already holds the dashboard snapshot. Layout: 16-byte header (magic, size, crc32
// of the data) + data; the header is written last, so a power cut mid-write leaves "no image".
// Only built with ENABLE_WIFI.

#include <stddef.h>
#include <stdint.h>

#include "config.h"

#if ENABLE_WIFI
namespace todostore {

void begin();                              // read the header, verify the data crc
bool has();                                // a valid image is stored
uint32_t crc();                            // crc32 of the stored compressed data (0 = none)
uint32_t size();                           // compressed bytes
bool save(const uint8_t* d, uint32_t n, uint32_t crc);  // false: flash error (nothing valid is stored then)
bool load(uint8_t* out, uint32_t cap);     // copies size() bytes; false if none / too small / crc mismatch
void clear();                              // erase the header

}  // namespace todostore
#endif
