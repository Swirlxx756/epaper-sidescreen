#pragma once
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

// Just enough of Arduino's Print for Adafruit_GFX to derive from.
class Print {
 public:
  virtual ~Print() {}
  virtual size_t write(uint8_t) = 0;
  virtual size_t write(const uint8_t* buf, size_t n) {
    size_t c = 0;
    while (n--) c += write(*buf++);
    return c;
  }
  size_t write(const char* s) { return s ? write((const uint8_t*)s, strlen(s)) : 0; }
  size_t print(const char* s) { return write(s); }
  size_t print(char c) { return write((uint8_t)c); }
  size_t print(int v) {
    char b[16];
    snprintf(b, sizeof(b), "%d", v);
    return write(b);
  }
};
