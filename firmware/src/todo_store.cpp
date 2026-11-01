#include "todo_store.h"

#if ENABLE_WIFI

#include <esp_partition.h>
#include <stdlib.h>
#include <string.h>

#include "img_rx.h"

namespace todostore {

static const uint32_t MAGIC = 0x31444F54;  // "TOD1"
static const uint32_t HDR = 16, SECTOR = 4096;
static const uint32_t REGION = ((HDR + IMG_MAX_COMPRESSED + SECTOR - 1) / SECTOR) * SECTOR;
static uint32_t curSize = 0, curCrc = 0;
static bool valid = false;

static const esp_partition_t* part() {
  static const esp_partition_t* p = esp_partition_find_first(ESP_PARTITION_TYPE_DATA, ESP_PARTITION_SUBTYPE_DATA_SPIFFS, nullptr);
  return (p && p->size >= REGION) ? p : nullptr;
}

static bool readVerified(uint8_t* out, uint32_t cap, uint32_t& n, uint32_t& c) {
  const esp_partition_t* p = part();
  uint32_t h[4];
  if (!p || esp_partition_read(p, 0, h, sizeof(h)) != ESP_OK) return false;
  if (h[0] != MAGIC || h[1] == 0 || h[1] > IMG_MAX_COMPRESSED || h[1] > cap) return false;
  if (esp_partition_read(p, HDR, out, h[1]) != ESP_OK) return false;
  if (imgrx::crc32(out, h[1]) != h[2]) return false;
  n = h[1];
  c = h[2];
  return true;
}

void begin() {
  valid = false;
  uint8_t* tmp = (uint8_t*)malloc(IMG_MAX_COMPRESSED);
  if (!tmp) return;
  valid = readVerified(tmp, IMG_MAX_COMPRESSED, curSize, curCrc);
  free(tmp);
  if (!valid) curSize = curCrc = 0;
}

bool has() { return valid; }
uint32_t crc() { return valid ? curCrc : 0; }
uint32_t size() { return valid ? curSize : 0; }

bool save(const uint8_t* d, uint32_t n, uint32_t c) {
  const esp_partition_t* p = part();
  valid = false;
  curSize = curCrc = 0;
  if (!p || n == 0 || n > IMG_MAX_COMPRESSED) return false;
  if (esp_partition_erase_range(p, 0, REGION) != ESP_OK) return false;
  if (esp_partition_write(p, HDR, d, n) != ESP_OK) return false;
  uint32_t h[4] = {MAGIC, n, c, 0};
  if (esp_partition_write(p, 0, h, sizeof(h)) != ESP_OK) return false;
  valid = true;
  curSize = n;
  curCrc = c;
  return true;
}

bool load(uint8_t* out, uint32_t cap) {
  if (!valid) return false;
  uint32_t n, c;
  if (!readVerified(out, cap, n, c)) return false;
  return n == curSize && c == curCrc;
}

void clear() {
  valid = false;
  curSize = curCrc = 0;
  const esp_partition_t* p = part();
  if (p) esp_partition_erase_range(p, 0, SECTOR);
}

}  // namespace todostore
#endif
