#!/bin/bash
# Build + run the weather page variant B preview. Run from firmware/:   bash test/preview/build_weather_b.sh
# Writes test/preview/out/b_wx_*.ppm (scale 3) and test/preview/build_weather_b/.
set -e
export PATH="/c/msys64/ucrt64/bin:$PATH"
LIB="${PIO_LIBDEPS:-.pio/libdeps/waveshare_esp32_42tri}"
GFX="$LIB/Adafruit GFX Library"
U8="$LIB/U8g2_for_Adafruit_GFX/src"
B=test/preview/build_weather_b
mkdir -p "$B" test/preview/out

FLAGS="-std=c++17 -O1 -Wall -DARDUINO=100"
INC=(-Itest/host_shim -Isrc "-I$GFX" "-I$U8")

gcc -O1 -c "$U8/u8g2_fonts.c" -o "$B/u8g2_fonts.o"
g++ $FLAGS "${INC[@]}" -c "$GFX/Adafruit_GFX.cpp" -o "$B/Adafruit_GFX.o"
g++ $FLAGS "${INC[@]}" -c "$U8/U8g2_for_Adafruit_GFX.cpp" -o "$B/U8g2_for_Adafruit_GFX.o"
g++ $FLAGS "${INC[@]}" -c src/weather_page_b.cpp -o "$B/weather_page_b.o"
g++ $FLAGS "${INC[@]}" -static -o "$B/weather_preview_b.exe" test/preview/weather_preview_b.cpp \
  "$B/weather_page_b.o" "$B/Adafruit_GFX.o" "$B/U8g2_for_Adafruit_GFX.o" "$B/u8g2_fonts.o"
"$B/weather_preview_b.exe" test/preview/out 3
