#include "ota.h"

#if ENABLE_WIFI

#include <Update.h>
#include <esp_ota_ops.h>

namespace ota {

static bool open_ = false;
static size_t left = 0;
static uint32_t lastMs = 0;

Begin begin(uint32_t size, const char* md5, uint32_t now) {
  if (open_) abort();
  const esp_partition_t* next = esp_ota_get_next_update_partition(nullptr);
  if (!next) return BEGIN_FAIL;
  if (size > next->size) return BEGIN_TOO_BIG;
  if (!Update.begin(size, U_FLASH)) return BEGIN_FAIL;
  if (!Update.setMD5(md5)) {
    Update.abort();
    return BEGIN_FAIL;
  }
  open_ = true;
  left = size;
  lastMs = now;
  return BEGIN_OK;
}

bool active() { return open_; }
size_t want() { return open_ ? left : 0; }

Feed write(const uint8_t* data, size_t len, uint32_t now) {
  if (!open_) return FEED_END;
  if (len > left) len = left;
  lastMs = now;
  if (Update.write(const_cast<uint8_t*>(data), len) != len) {
    abort();
    return FEED_WRITE;
  }
  left -= len;
  if (left > 0) return FEED_MORE;
  open_ = false;
  if (Update.end()) return FEED_OK;  // verified; boots from the new partition next time
  Feed f = Update.getError() == UPDATE_ERROR_MD5 ? FEED_MD5 : FEED_END;
  Update.abort();
  return f;
}

void abort() {
  if (!open_) return;
  open_ = false;
  left = 0;
  Update.abort();
}

bool idle(uint32_t now, uint32_t idleMs) { return open_ && now - lastMs >= idleMs; }

Confirm confirm() {
  esp_ota_img_states_t st;
  const esp_partition_t* run = esp_ota_get_running_partition();
  if (!run) return CONFIRM_FAILED;
  esp_err_t e = esp_ota_get_state_partition(run, &st);
  // A partition without OTA state (factory / USB-flashed): nothing is pending.
  if (e == ESP_ERR_NOT_SUPPORTED || e == ESP_ERR_NOT_FOUND) return CONFIRM_NOT_PENDING;
  if (e != ESP_OK) return CONFIRM_FAILED;
  if (st != ESP_OTA_IMG_PENDING_VERIFY) return CONFIRM_NOT_PENDING;
  return esp_ota_mark_app_valid_cancel_rollback() == ESP_OK ? CONFIRM_OK : CONFIRM_FAILED;
}

}  // namespace ota

// Arduino core hook: keep a freshly written image "pending verify" until
// confirm() instead of marking it valid at boot (see esp32-hal-misc.c).
extern "C" bool verifyRollbackLater() { return true; }

#endif
