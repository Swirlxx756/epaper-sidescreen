#pragma once
// Strict UTF-8 validation (no Arduino, host-testable). Rejects stray / truncated continuation bytes,
// overlong forms (C0, C1, E0 80..9F, F0 80..8F), UTF-16 surrogates (ED A0..BF) and code points above
// U+10FFFF (F4 90.., F5..FF). Shared by the Wi-Fi scan filter (net.cpp) and the weather place check.

#include <stddef.h>

inline bool validUtf8(const char* s, size_t n) {
  const unsigned char* p = (const unsigned char*)s;
  const unsigned char* end = p + n;
  while (p < end) {
    unsigned char c = *p++;
    if (c < 0x80) continue;
    int extra;
    unsigned char lo = 0x80, hi = 0xBF;  // allowed range of the first continuation byte
    if (c >= 0xC2 && c <= 0xDF) {
      extra = 1;
    } else if (c >= 0xE0 && c <= 0xEF) {
      extra = 2;
      if (c == 0xE0) lo = 0xA0;  // no overlong
      if (c == 0xED) hi = 0x9F;  // no surrogates
    } else if (c >= 0xF0 && c <= 0xF4) {
      extra = 3;
      if (c == 0xF0) lo = 0x90;  // no overlong
      if (c == 0xF4) hi = 0x8F;  // <= U+10FFFF
    } else {
      return false;
    }
    if (end - p < extra) return false;
    if (*p < lo || *p > hi) return false;
    p++;
    for (int i = 1; i < extra; i++, p++)
      if ((*p & 0xC0) != 0x80) return false;
  }
  return true;
}
