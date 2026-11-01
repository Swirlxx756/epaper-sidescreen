#pragma once
// TODO-page image transfer (no Arduino, host-testable): crc32, base64, PackBits RLE decode, the chunk
// assembler behind img_begin / img_chunk / img_end and the host-plane -> panel-plane conversion.
// Wire format: docs/dashboard-v2-protocol.md "待办页图片". Fixtures: docs/todo-image-fixtures.json.

#include <stddef.h>
#include <stdint.h>
#include <string.h>

#define IMG_W 400
#define IMG_H 300
#define IMG_PLANE_BYTES (IMG_W / 8 * IMG_H)  // 15000: one 1-bit plane
#define IMG_MAX_COMPRESSED 12288UL           // black RLE + red RLE, bytes
#define IMG_CHUNK_MAX 720                    // decoded bytes per img_chunk (a 1024-byte line carries at most ~717)
#define IMG_IDLE_MS 20000UL                  // unfinished transfer is dropped after this long without a chunk

namespace imgrx {

// zlib / PNG crc32 (poly 0xEDB88320, init and final xor 0xFFFFFFFF), bitwise: no table, a few bytes of code.
inline uint32_t crc32(const uint8_t* p, size_t n, uint32_t crc = 0) {
  crc = ~crc;
  while (n--) {
    crc ^= *p++;
    for (int k = 0; k < 8; k++) crc = (crc >> 1) ^ (0xEDB88320UL & (0 - (crc & 1)));
  }
  return ~crc;
}

// Strict RFC 4648 base64 (standard alphabet, '=' padding required, no whitespace). Returns the number of
// bytes written, or -1 (bad character / length / padding, or `cap` too small).
inline int b64Decode(const char* s, size_t n, uint8_t* out, size_t cap) {
  if (n == 0 || n % 4 != 0) return -1;
  size_t o = 0;
  for (size_t i = 0; i < n; i += 4) {
    uint32_t v = 0;
    int pad = 0;
    for (int k = 0; k < 4; k++) {
      char c = s[i + k];
      int d;
      if (c >= 'A' && c <= 'Z') d = c - 'A';
      else if (c >= 'a' && c <= 'z') d = c - 'a' + 26;
      else if (c >= '0' && c <= '9') d = c - '0' + 52;
      else if (c == '+') d = 62;
      else if (c == '/') d = 63;
      else if (c == '=' && i + 4 == n && k >= 2) { d = 0; pad++; }
      else return -1;
      if (pad && c != '=') return -1;  // data after padding
      v = (v << 6) | (uint32_t)d;
    }
    size_t take = 3 - (size_t)pad;
    if (o + take > cap) return -1;
    if (take >= 1) out[o++] = (uint8_t)(v >> 16);
    if (take >= 2) out[o++] = (uint8_t)(v >> 8);
    if (take >= 3) out[o++] = (uint8_t)v;
  }
  return (int)o;
}

enum RleErr : uint8_t { RLE_OK = 0, RLE_TRUNC, RLE_OVERRUN, RLE_BADCTRL };

// PackBits: control byte c; 0..127 = copy the next c+1 bytes; 129..255 = repeat the next byte 257-c times
// (2..128); 128 is invalid. Decodes exactly `outLen` bytes from the start of `src` (a run never crosses the end
// of the plane); `used` = source bytes consumed. dst == nullptr: validate only.
inline RleErr rleDecode(const uint8_t* src, size_t n, size_t& used, uint8_t* dst, size_t outLen) {
  size_t i = 0, o = 0;
  while (o < outLen) {
    if (i >= n) return RLE_TRUNC;
    uint8_t c = src[i++];
    if (c == 128) return RLE_BADCTRL;
    if (c < 128) {
      size_t k = (size_t)c + 1;
      if (i + k > n) return RLE_TRUNC;
      if (o + k > outLen) return RLE_OVERRUN;
      if (dst) memcpy(dst + o, src + i, k);
      i += k;
      o += k;
    } else {
      size_t k = 257 - (size_t)c;
      if (i >= n) return RLE_TRUNC;
      if (o + k > outLen) return RLE_OVERRUN;
      if (dst) memset(dst + o, src[i], k);
      i++;
      o += k;
    }
  }
  used = i;
  return RLE_OK;
}

// A whole image = RLE(black plane) immediately followed by RLE(red plane), both decoding to IMG_PLANE_BYTES,
// and nothing after. black / red may be nullptr (validate only).
inline bool decodeImage(const uint8_t* buf, size_t n, uint8_t* black, uint8_t* red) {
  size_t u1 = 0, u2 = 0;
  if (rleDecode(buf, n, u1, black, IMG_PLANE_BYTES) != RLE_OK) return false;
  if (rleDecode(buf + u1, n - u1, u2, red, IMG_PLANE_BYTES) != RLE_OK) return false;
  return u1 + u2 == n;
}

// Host planes (1 = ink, red wins) -> panel planes (vendor convention: black plane 0 = black, ry plane 0 =
// colour, 1 = white), in place.
inline void hostToPanel(uint8_t* black, uint8_t* red, size_t n) {
  for (size_t i = 0; i < n; i++) {
    uint8_t b = black[i], r = red[i];
    black[i] = (uint8_t)~(b & ~r);
    red[i] = (uint8_t)~r;
  }
}

// img_end of an image that is already stored (same compressed size and crc32): nothing to write. The host
// re-pushes the same TODO page after every reconnect and each save is a 12 KB flash erase + write.
inline bool sameAsStored(bool has, uint32_t storedSize, uint32_t storedCrc, uint32_t size, uint32_t crc) {
  return has && storedSize == size && storedCrc == crc;
}

// Chunk assembler. The caller owns the buffer (size bytes). Every call returns nullptr on success or the error
// text that follows "ERR ". A rejected chunk keeps the transfer alive (the host may resend); end() always
// finishes it. Nothing here touches the stored image: the caller commits only after end() returns nullptr.
struct Rx {
  uint8_t* buf = nullptr;
  uint32_t size = 0, crc = 0, got = 0, lastMs = 0;
  bool active = false;

  const char* begin(uint8_t* b, uint32_t sz, uint32_t c, uint32_t now) {
    if (sz == 0) return "bad_field size";
    if (sz > IMG_MAX_COMPRESSED) return "too_big";
    buf = b;
    size = sz;
    crc = c;
    got = 0;
    lastMs = now;
    active = true;
    return nullptr;
  }
  const char* chunk(uint32_t off, const uint8_t* d, size_t n, uint32_t now) {
    if (!active) return "no_transfer";
    if (n == 0 || (uint64_t)off + n > size) return "bad_range";
    if (off + n <= got) {  // a resend of something already stored (lost OK): accepted, not applied
      lastMs = now;
      return nullptr;
    }
    if (off != got) return "bad_offset";  // gap or partial overlap
    memcpy(buf + off, d, n);
    got += (uint32_t)n;
    lastMs = now;
    return nullptr;
  }
  const char* end() {
    if (!active) return "no_transfer";
    active = false;
    if (got != size) return "incomplete";
    if (crc32(buf, size) != crc) return "bad_crc";
    if (!decodeImage(buf, size, nullptr, nullptr)) return "bad_rle";
    return nullptr;
  }
  void abort() { active = false; }
  bool idle(uint32_t now, uint32_t ms) const { return active && now - lastMs >= ms; }
};

}  // namespace imgrx
