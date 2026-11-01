#pragma once
// Minimal Arduino core stand-in for host builds (tests and the offline
// preview). Only what Adafruit_GFX, U8g2_for_Adafruit_GFX, protocol.cpp and
// model.h need. NOT a general Arduino emulation.

#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <algorithm>
#include <string>

#define PROGMEM
#define PGM_P const char*
#define PSTR(s) (s)
#define F(s) (s)
#define pgm_read_byte(addr) (*(const unsigned char*)(addr))
#define pgm_read_word(addr) (*(const unsigned short*)(addr))
#define pgm_read_dword(addr) (*(const unsigned long*)(addr))
#define pgm_read_byte_near(addr) pgm_read_byte(addr)
#define optimistic_yield(x)
#define DEG_TO_RAD 0.017453292519943295769236907684886
#define radians(deg) ((deg) * DEG_TO_RAD)
inline void yield() {}

using std::max;
using std::min;
#ifndef constrain
#define constrain(amt, low, high) ((amt) < (low) ? (low) : ((amt) > (high) ? (high) : (amt)))
#endif

class __FlashStringHelper;

// Arduino String on top of std::string: just the calls this firmware uses.
class String {
 public:
  String() {}
  String(const char* c) : s_(c ? c : "") {}
  String(char c) : s_(1, c) {}
  String(int v) : s_(std::to_string(v)) {}
  String(unsigned int v) : s_(std::to_string(v)) {}
  String(long v) : s_(std::to_string(v)) {}
  String(unsigned long v) : s_(std::to_string(v)) {}
  String(double v, unsigned char decimals = 2) {
    char buf[64];
    snprintf(buf, sizeof(buf), "%.*f", (int)decimals, v);
    s_ = buf;
  }
  const char* c_str() const { return s_.c_str(); }
  unsigned int length() const { return (unsigned int)s_.size(); }
  void reserve(unsigned int n) { s_.reserve(n); }
  void trim() {
    size_t a = s_.find_first_not_of(" \t\r\n");
    if (a == std::string::npos) {
      s_.clear();
      return;
    }
    size_t b = s_.find_last_not_of(" \t\r\n");
    s_ = s_.substr(a, b - a + 1);
  }
  bool startsWith(const char* p) const { return s_.rfind(p, 0) == 0; }
  String& operator+=(const String& o) { s_ += o.s_; return *this; }
  String& operator+=(const char* o) { s_ += o; return *this; }
  String& operator+=(char o) { s_ += o; return *this; }
  friend String operator+(const String& a, const String& b) { String r(a); r.s_ += b.s_; return r; }
  friend String operator+(const String& a, const char* b) { String r(a); r.s_ += b; return r; }
  friend String operator+(const char* a, const String& b) { String r(a); r.s_ += b.s_; return r; }
  bool operator==(const String& o) const { return s_ == o.s_; }
  bool operator==(const char* o) const { return s_ == o; }
  bool operator!=(const String& o) const { return s_ != o.s_; }
  bool operator!=(const char* o) const { return s_ != o; }

 private:
  std::string s_;
};

#include "Print.h"
