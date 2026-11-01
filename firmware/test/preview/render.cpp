// epaper_render: the host GUI's live e-paper preview (host/gui.py). Reads protocol lines on stdin, feeds them
// through the REAL handleLine and dashv2::draw (same code and canvas as preview.cpp) and writes one 400x300
// screen as a binary PPM to stdout.
// Build: bash test/preview/build.sh   (from firmware/; output test/preview/build/epaper_render.exe)
//
//   epaper_render.exe [--offline] [--bw] [--scale N]  < lines  > screen.ppm
//   epaper_render.exe --weather "<place>" [--bw] [--scale N]  < open-meteo.json  > screen.ppm
//
// --weather: stdin is the Open-Meteo forecast body (the URL wxb::requestUrl builds); it goes through the REAL
// wxb::parseOpenMeteo and wxb::draw, status OK, i.e. the weather page as the board draws it (with its offline
// status line). Exit 4 = the body did not parse (nothing written). <place> is read as UTF-8 also on Windows.
//
// Lines that do not start with '{' are skipped. A line the firmware would answer with ERR is reported on
// stderr ("line N: ERR <err>") and leaves the screen as it was, like on the board.
// Exit code: 0 = image written; 3 = image written but some lines were rejected; 1 = usage / output error.

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <string>
#include <vector>

#ifdef _WIN32
#include <fcntl.h>
#include <io.h>
#endif

#include "preview_canvas.h"
#include "weather_page_b.h"
#include "weather_parse.h"

#ifdef _WIN32
#include <windows.h>
#include <shellapi.h>
// MinGW's argv is in the ANSI code page; a Chinese place name must survive, so take the UTF-16 command line.
static std::vector<std::string> utf8Args() {
  std::vector<std::string> out;
  int n = 0;
  LPWSTR* w = CommandLineToArgvW(GetCommandLineW(), &n);
  if (!w) return out;
  for (int i = 0; i < n; i++) {
    int len = WideCharToMultiByte(CP_UTF8, 0, w[i], -1, nullptr, 0, nullptr, nullptr);
    std::string s(len > 0 ? len - 1 : 0, '\0');
    if (len > 1) WideCharToMultiByte(CP_UTF8, 0, w[i], -1, &s[0], len, nullptr, nullptr);
    out.push_back(s);
  }
  LocalFree(w);
  return out;
}
#else
static std::vector<std::string> utf8Args() { return {}; }
#endif

int main(int argc, char** argv) {
  bool online = true, tri = true, weatherMode = false;
  std::string place;
  int scale = 1;
  std::vector<std::string> wargs = utf8Args();
  for (int i = 1; i < argc; i++) {
    if (!strcmp(argv[i], "--weather") && i + 1 < argc) {
      weatherMode = true;
      place = (size_t)(i + 1) < wargs.size() ? wargs[i + 1] : argv[i + 1];
      i++;
    } else if (!strcmp(argv[i], "--offline")) {
      online = false;
    } else if (!strcmp(argv[i], "--bw")) {
      tri = false;
    } else if (!strcmp(argv[i], "--scale") && i + 1 < argc) {
      scale = atoi(argv[++i]);
      if (scale < 1 || scale > 8) scale = 1;
    } else {
      fprintf(stderr, "usage: epaper_render [--offline] [--bw] [--scale N] < lines > screen.ppm\n"
                    "       epaper_render --weather <place> [--bw] [--scale N] < open-meteo.json > screen.ppm\n");
      return 1;
    }
  }
#ifdef _WIN32
  _setmode(_fileno(stdin), _O_BINARY);  // keep the bytes as sent; "\r\n" is stripped below
  _setmode(_fileno(stdout), _O_BINARY);
#endif

  if (weatherMode) {
    std::string body;
    for (int ch = getchar(); ch != EOF; ch = getchar()) body += (char)ch;
    wxb::WeatherB w;
    if (!wxb::parseOpenMeteo(body.data(), body.size(), place.c_str(), w)) {
      fprintf(stderr, "cannot parse the Open-Meteo response\n");
      return 4;
    }
    w.status = wxb::WX_OK;
    PlaneCanvas canvas;
    U8G2_FOR_ADAFRUIT_GFX u8g2;
    canvas.fillScreen(0xFFFF);
    u8g2.begin(canvas);
    u8g2.setFontMode(1);
    u8g2.setFontDirection(0);
    wxb::draw(canvas, u8g2, w, tri ? GXEPD_RED : (uint16_t)0x0000);
    if (!writePpm(canvas, stdout, scale) || fflush(stdout) != 0) {
      fprintf(stderr, "cannot write the image\n");
      return 1;
    }
    return 0;
  }

  ViewState v;
  int lineNo = 0, rejected = 0;
  std::string line;
  for (int ch = getchar();; ch = getchar()) {
    if (ch != EOF && ch != '\n') {
      line += (char)ch;
      continue;
    }
    if (!line.empty() || ch != EOF) lineNo++;
    while (!line.empty() && line.back() == '\r') line.pop_back();
    if (!line.empty() && line[0] == '{') {
      HandleResult r = handleLine(v, line.c_str(), line.size());
      if (!r.ok) {
        fprintf(stderr, "line %d: ERR %s\n", lineNo, r.err);
        rejected++;
      }
    }
    line.clear();
    if (ch == EOF) break;
  }

  // The v2 screen even when nothing was accepted (every block then shows NO DATA), like preview.cpp's "missing".
  v.hasData = true;
  v.v2 = true;
  v.online = online;
  PlaneCanvas canvas;
  drawView(canvas, v, tri);
  if (!writePpm(canvas, stdout, scale) || fflush(stdout) != 0) {
    fprintf(stderr, "cannot write the image\n");
    return 1;
  }
  return rejected ? 3 : 0;
}
