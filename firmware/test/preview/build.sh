#!/bin/bash
# Build the offline preview (preview.exe) and the host GUI's live renderer (epaper_render.exe)
# into test/preview/build/. Run from firmware/:   bash test/preview/build.sh
# Needs MSYS2 g++ (C:\msys64\ucrt64\bin) and the libraries PlatformIO fetches into .pio/libdeps
# (run `pio run -e waveshare_esp32_42tri` once; PIO_LIBDEPS=<folder> uses another copy).
# epaper_render also draws the weather page (--weather), so it links weather_parse + weather_page_b.
# Linked statically, so the GUI can start epaper_render.exe without MSYS2 on PATH.
set -e
export PATH="/c/msys64/ucrt64/bin:$PATH"
LIB="${PIO_LIBDEPS:-.pio/libdeps/waveshare_esp32_42tri}"
GFX="$LIB/Adafruit GFX Library"
U8="$LIB/U8g2_for_Adafruit_GFX/src"
B=test/preview/build
mkdir -p "$B"

FLAGS="-std=c++17 -O1 -Wall -DARDUINO=100 -DARDUINOJSON_ENABLE_ARDUINO_STRING=0 -DARDUINOJSON_ENABLE_ARDUINO_STREAM=0 -DARDUINOJSON_ENABLE_ARDUINO_PRINT=0 -DARDUINOJSON_ENABLE_PROGMEM=0"
INC=(-Itest/host_shim -Isrc "-I$GFX" "-I$U8" "-I$LIB/ArduinoJson/src")

# Shared objects: the firmware's drawing + protocol code and the two graphics libraries.
gcc -O1 -c "$U8/u8g2_fonts.c" -o "$B/u8g2_fonts.o"
g++ $FLAGS "${INC[@]}" -c src/dashboard_v2.cpp -o "$B/dashboard_v2.o"
g++ $FLAGS "${INC[@]}" -c src/protocol.cpp -o "$B/protocol.o"
g++ $FLAGS "${INC[@]}" -c "$GFX/Adafruit_GFX.cpp" -o "$B/Adafruit_GFX.o"
g++ $FLAGS "${INC[@]}" -c "$U8/U8g2_for_Adafruit_GFX.cpp" -o "$B/U8g2_for_Adafruit_GFX.o"
g++ $FLAGS "${INC[@]}" -c src/weather_parse.cpp -o "$B/weather_parse.o"
g++ $FLAGS "${INC[@]}" -c src/weather_page_b.cpp -o "$B/weather_page_b.o"
OBJS=("$B/dashboard_v2.o" "$B/protocol.o" "$B/Adafruit_GFX.o" "$B/U8g2_for_Adafruit_GFX.o" "$B/u8g2_fonts.o")
WXOBJS=("$B/weather_parse.o" "$B/weather_page_b.o")

g++ $FLAGS "${INC[@]}" -static -o "$B/preview.exe" test/preview/preview.cpp "${OBJS[@]}"
g++ $FLAGS "${INC[@]}" -static -o "$B/epaper_render.exe" test/preview/render.cpp "${OBJS[@]}" "${WXOBJS[@]}" -lshell32
echo "built $B/preview.exe and $B/epaper_render.exe"
