#pragma once
// Firmware upgrade over the TCP connection (docs/dashboard-v2-protocol.md,
// "WiFi 升级固件"). Only built with ENABLE_WIFI. One session at a time: after
// begin() the transport hands every raw byte to write() until want() is 0.
// Nothing here prints; the caller sends the replies.
//
// Rollback: an image written here boots as "pending verify"; confirm() makes
// it permanent. Until then any reboot returns to the previous firmware.

#include <stddef.h>
#include <stdint.h>

#include "config.h"

#if ENABLE_WIFI
namespace ota {

enum Begin : uint8_t { BEGIN_OK, BEGIN_TOO_BIG, BEGIN_FAIL };
enum Feed : uint8_t { FEED_MORE, FEED_OK, FEED_MD5, FEED_WRITE, FEED_END };

// Start writing `size` bytes into the next OTA partition, checked against
// `md5` (32 lowercase hex) at the end. The pairing code is checked by the caller.
Begin begin(uint32_t size, const char* md5, uint32_t now);
bool active();
size_t want();  // raw bytes still expected (0 = not in a session)
// Feed raw bytes (at most want()). FEED_MORE until the last byte; then the
// image is verified and set as the boot partition (FEED_OK) or dropped.
Feed write(const uint8_t* data, size_t len, uint32_t now);
// Drop the session (client gone / timeout); the running firmware is untouched.
void abort();
// Session open and no data for `idleMs`.
bool idle(uint32_t now, uint32_t idleMs);

// Mark the running image valid if it is still pending verification.
//   CONFIRM_OK          this call confirmed it
//   CONFIRM_NOT_PENDING nothing to do (USB-flashed / already valid build): do not ask again
//   CONFIRM_FAILED      could not read the state or the confirm call failed: ask again later
enum Confirm : uint8_t { CONFIRM_NOT_PENDING, CONFIRM_OK, CONFIRM_FAILED };
Confirm confirm();

}  // namespace ota
#endif
