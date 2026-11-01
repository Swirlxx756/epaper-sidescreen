// Host-side tests for firmware 1.6.0: TODO image (crc32, base64, PackBits RLE, chunk assembly, plane conversion),
// page / page_status / refresh_now / img_* messages, the page override rules and the refresh_now bypass.
// Shared fixtures with the host encoder: ../docs/todo-image-fixtures.json (run from firmware/).
// No hardware, no network. Build + run (from firmware/, after `pio run -e waveshare_esp32_42tri` has
// fetched the libraries; Windows: g++ from MSYS2 in C:/msys64/ucrt64/bin):
//   g++ -std=c++17 -Wall -Wextra -DARDUINO=100 -DARDUINOJSON_ENABLE_ARDUINO_STRING=0 -DARDUINOJSON_ENABLE_ARDUINO_STREAM=0 -DARDUINOJSON_ENABLE_ARDUINO_PRINT=0 -DARDUINOJSON_ENABLE_PROGMEM=0 -Itest/host_shim -Isrc -I.pio/libdeps/waveshare_esp32_42tri/ArduinoJson/src test/native/test_img.cpp src/protocol.cpp -o test_img && ./test_img

#include <ArduinoJson.h>
#include <stdio.h>
#include <string.h>

#include <fstream>
#include <sstream>
#include <string>
#include <vector>

#include "img_rx.h"
#include "page_policy.h"
#include "protocol.h"
#include "refresh_policy.h"

static int failures = 0, checks = 0;
#define CHECK(cond)                                            \
  do {                                                         \
    checks++;                                                  \
    if (!(cond)) {                                             \
      failures++;                                              \
      printf("  FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
    }                                                          \
  } while (0)
#define CHECK_STR(a, b)                                                         \
  do {                                                                          \
    checks++;                                                                   \
    if (strcmp((a), (b)) != 0) {                                                \
      failures++;                                                               \
      printf("  FAIL %s:%d: \"%s\" != \"%s\"\n", __FILE__, __LINE__, (a), (b)); \
    }                                                                           \
  } while (0)

typedef std::vector<uint8_t> Bytes;

static Bytes fromHex(const std::string& h) {
  Bytes b;
  for (size_t i = 0; i + 1 < h.size(); i += 2) b.push_back((uint8_t)strtoul(h.substr(i, 2).c_str(), nullptr, 16));
  return b;
}

// Reference encoder = the canonical algorithm of the protocol doc.
static size_t runLen(const Bytes& b, size_t i) {
  size_t n = 1;
  while (i + n < b.size() && b[i + n] == b[i]) n++;
  return n;
}
static Bytes encode(const Bytes& raw) {
  Bytes out;
  size_t i = 0, n = raw.size();
  while (i < n) {
    size_t r = runLen(raw, i);
    if (r > 128) r = 128;
    if (r >= 3) {
      out.push_back((uint8_t)(257 - r));
      out.push_back(raw[i]);
      i += r;
    } else {
      size_t j = i;
      while (j < n && j - i < 128) {
        if (j > i && runLen(raw, j) >= 3) break;
        j++;
      }
      out.push_back((uint8_t)(j - i - 1));
      out.insert(out.end(), raw.begin() + i, raw.begin() + j);
      i = j;
    }
  }
  return out;
}
static Bytes decode(const Bytes& rle, size_t outLen, bool* ok = nullptr) {
  Bytes out(outLen);
  size_t used = 0;
  imgrx::RleErr e = imgrx::rleDecode(rle.data(), rle.size(), used, out.data(), outLen);
  if (ok) *ok = e == imgrx::RLE_OK && used == rle.size();
  return out;
}

static std::string readFile(const char* path) {
  std::ifstream f(path, std::ios::binary);
  std::stringstream ss;
  ss << f.rdbuf();
  return ss.str();
}

static JsonDocument fixture;
static bool loadFixture() {
  std::string s = readFile("../docs/todo-image-fixtures.json");
  if (s.empty()) s = readFile("docs/todo-image-fixtures.json");
  return !deserializeJson(fixture, s);
}

// "00*2207 ff*1 ..." -> bytes
static Bytes expandOps(const char* s) {
  Bytes b;
  while (*s) {
    while (*s == ' ') s++;
    if (!*s) break;
    unsigned v = (unsigned)strtoul(s, (char**)&s, 16);
    s++;  // '*'
    unsigned long c = strtoul(s, (char**)&s, 10);
    b.insert(b.end(), c, (uint8_t)v);
  }
  return b;
}

static HandleResult feed(const std::string& line) {
  ViewState v;
  return handleLine(v, line.c_str(), line.size());
}

// ---------------------------------------------------------------------
static void test_crc_b64() {
  const uint8_t s[] = {'1', '2', '3', '4', '5', '6', '7', '8', '9'};
  CHECK(imgrx::crc32(s, 9) == 0xCBF43926u);
  CHECK(imgrx::crc32(s, 0) == 0);
  CHECK(imgrx::crc32(s + 4, 5, imgrx::crc32(s, 4)) == 0xCBF43926u);  // incremental
  for (JsonObjectConst v : fixture["crc32_vectors"].as<JsonArrayConst>()) {
    const char* a = v["ascii"];
    CHECK(imgrx::crc32((const uint8_t*)a, strlen(a)) == v["crc32"].as<uint32_t>());
  }

  uint8_t o[16];
  CHECK(imgrx::b64Decode("TWFu", 4, o, 16) == 3 && !memcmp(o, "Man", 3));
  CHECK(imgrx::b64Decode("TWE=", 4, o, 16) == 2 && !memcmp(o, "Ma", 2));
  CHECK(imgrx::b64Decode("TQ==", 4, o, 16) == 1 && o[0] == 'M');
  CHECK(imgrx::b64Decode("+/8=", 4, o, 16) == 2 && o[0] == 0xFB && o[1] == 0xFF);
  CHECK(imgrx::b64Decode("", 0, o, 16) == -1);
  CHECK(imgrx::b64Decode("TWF", 3, o, 16) == -1);       // not a multiple of 4
  CHECK(imgrx::b64Decode("TW=u", 4, o, 16) == -1);      // data after padding
  CHECK(imgrx::b64Decode("====", 4, o, 16) == -1);
  CHECK(imgrx::b64Decode("TQ==TWFu", 8, o, 16) == -1);  // padding in the middle
  CHECK(imgrx::b64Decode("TW Fu", 5, o, 16) == -1);
  CHECK(imgrx::b64Decode("TWF-", 4, o, 16) == -1);      // url-safe alphabet is not accepted
  CHECK(imgrx::b64Decode("TWFuTWFu", 8, o, 5) == -1);   // does not fit
}

static void test_rle_cases() {
  for (JsonObjectConst c : fixture["rle_cases"].as<JsonArrayConst>()) {
    Bytes raw = fromHex(c["raw_hex"]), rle = fromHex(c["rle_hex"]);
    CHECK(encode(raw) == rle);
    bool ok = false;
    CHECK(decode(rle, raw.size(), &ok) == raw);
    CHECK(ok);
  }
  for (JsonObjectConst c : fixture["rle_decode_only"].as<JsonArrayConst>()) {
    Bytes raw = fromHex(c["raw_hex"]), rle = fromHex(c["rle_hex"]);
    bool ok = false;
    CHECK(decode(rle, raw.size(), &ok) == raw);
    CHECK(ok);
  }
  for (JsonObjectConst c : fixture["rle_invalid"].as<JsonArrayConst>()) {
    Bytes rle = fromHex(c["rle_hex"]);
    size_t used = 0;
    Bytes out(c["out_len"].as<size_t>());
    CHECK(imgrx::rleDecode(rle.data(), rle.size(), used, out.data(), out.size()) != imgrx::RLE_OK);
  }
  // Specific error classes.
  Bytes o(4);
  size_t used = 0;
  const uint8_t bad[] = {0x80, 0x00};
  CHECK(imgrx::rleDecode(bad, 2, used, o.data(), 1) == imgrx::RLE_BADCTRL);
  const uint8_t run[] = {0xFE, 0xAA};  // 3 x AA
  CHECK(imgrx::rleDecode(run, 2, used, o.data(), 2) == imgrx::RLE_OVERRUN);
  CHECK(imgrx::rleDecode(run, 1, used, o.data(), 3) == imgrx::RLE_TRUNC);
  CHECK(imgrx::rleDecode(run, 2, used, nullptr, 3) == imgrx::RLE_OK && used == 2);  // validate only
}

static void test_frames() {
  CHECK(fixture["frames"].size() >= 4);
  for (JsonObjectConst f : fixture["frames"].as<JsonArrayConst>()) {
    const char* name = f["name"];
    Bytes black = expandOps(f["black"]), red = expandOps(f["red"]);
    CHECK(black.size() == IMG_PLANE_BYTES && red.size() == IMG_PLANE_BYTES);
    Bytes pb = encode(black), pr = encode(red), payload = pb;
    payload.insert(payload.end(), pr.begin(), pr.end());
    CHECK(payload == fromHex(f["payload_hex"]));
    CHECK(payload.size() == f["payload_size"].as<size_t>());
    CHECK(pb.size() == f["black_rle_len"].as<size_t>() && pr.size() == f["red_rle_len"].as<size_t>());
    CHECK(imgrx::crc32(payload.data(), payload.size()) == f["payload_crc32"].as<uint32_t>());
    CHECK(payload.size() <= IMG_MAX_COMPRESSED);

    Bytes b2(IMG_PLANE_BYTES), r2(IMG_PLANE_BYTES);
    CHECK(imgrx::decodeImage(payload.data(), payload.size(), b2.data(), r2.data()));
    CHECK(b2 == black && r2 == red);
    // validate-only gives the same verdict
    CHECK(imgrx::decodeImage(payload.data(), payload.size(), nullptr, nullptr));
    // trailing byte / missing byte / swapped planes' lengths are rejected
    Bytes longer = payload;
    longer.push_back(0);
    CHECK(!imgrx::decodeImage(longer.data(), longer.size(), nullptr, nullptr));
    CHECK(!imgrx::decodeImage(payload.data(), payload.size() - 1, nullptr, nullptr));

    // Panel conversion: red wins; 1 = ink -> 0 bits.
    imgrx::hostToPanel(b2.data(), r2.data(), b2.size());
    for (size_t i = 0; i < black.size(); i += 97) {
      CHECK(r2[i] == (uint8_t)~red[i]);
      CHECK(b2[i] == (uint8_t)~(black[i] & ~red[i]));
    }
    (void)name;

    // The documented wire lines parse and reassemble to the same payload.
    JsonArrayConst lines = f["lines"].as<JsonArrayConst>();
    imgrx::Rx rx;
    Bytes buf(payload.size());
    uint32_t t = 1000;
    size_t n = 0;
    for (JsonVariantConst l : lines) {
      std::string line = l.as<std::string>();
      CHECK(line.size() <= PROTO_MAX_LINE_LEN);
      HandleResult r = feed(line);
      CHECK(r.ok);
      if (n == 0) {
        CHECK(r.cmd == CMD_IMG_BEGIN && r.imgSize == payload.size() && r.imgCrc == f["payload_crc32"].as<uint32_t>());
        CHECK(rx.begin(buf.data(), r.imgSize, r.imgCrc, t) == nullptr);
      } else if (n + 1 < lines.size()) {
        CHECK(r.cmd == CMD_IMG_CHUNK);
        CHECK(rx.chunk(r.imgOff, r.imgData, r.imgLen, t += 10) == nullptr);
      } else {
        CHECK(r.cmd == CMD_IMG_END);
        CHECK(rx.end() == nullptr);
      }
      n++;
    }
    CHECK(buf == payload);
  }
}

static std::string b64(const uint8_t* d, size_t n) {
  static const char* A = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  std::string s;
  for (size_t i = 0; i < n; i += 3) {
    uint32_t v = d[i] << 16 | (i + 1 < n ? d[i + 1] << 8 : 0) | (i + 2 < n ? d[i + 2] : 0);
    s += A[v >> 18 & 63];
    s += A[v >> 12 & 63];
    s += i + 1 < n ? A[v >> 6 & 63] : '=';
    s += i + 2 < n ? A[v & 63] : '=';
  }
  return s;
}

// A small valid image: black plane has a few ink bytes, red plane some.
static Bytes sampleImage() {
  Bytes black(IMG_PLANE_BYTES, 0), red(IMG_PLANE_BYTES, 0);
  for (int i = 0; i < 300; i++) black[1000 + i] = (uint8_t)(i * 31 + 5);
  for (int i = 0; i < 50; i++) red[2000 + i] = 0xF0;
  Bytes p = encode(black), q = encode(red);
  p.insert(p.end(), q.begin(), q.end());
  return p;
}

static void test_rx() {
  Bytes img = sampleImage();
  uint32_t crc = imgrx::crc32(img.data(), img.size());
  Bytes buf(img.size());
  imgrx::Rx rx;

  // In order, 100-byte chunks.
  CHECK(!rx.begin(buf.data(), img.size(), crc, 0));
  for (size_t off = 0; off < img.size(); off += 100) {
    size_t n = img.size() - off < 100 ? img.size() - off : 100;
    CHECK(!rx.chunk(off, img.data() + off, n, off));
  }
  CHECK(!rx.end());
  CHECK(buf == img);
  CHECK(!rx.active);

  // No transfer: chunk / end.
  CHECK_STR(rx.chunk(0, img.data(), 10, 0), "no_transfer");
  CHECK_STR(rx.end(), "no_transfer");

  // Out of order: a gap is refused, the transfer survives and the right chunk still works.
  CHECK(!rx.begin(buf.data(), img.size(), crc, 0));
  CHECK_STR(rx.chunk(100, img.data() + 100, 100, 1), "bad_offset");
  CHECK(rx.active && rx.got == 0);
  CHECK(!rx.chunk(0, img.data(), 100, 2));
  CHECK_STR(rx.chunk(50, img.data() + 50, 100, 3), "bad_offset");  // partial overlap
  CHECK(rx.got == 100);
  // Duplicate of a stored chunk: accepted, not applied (corrupt resend does not change the buffer).
  uint8_t junk[100];
  memset(junk, 0xEE, sizeof junk);
  CHECK(!rx.chunk(0, junk, 100, 4));
  CHECK(rx.got == 100 && buf[0] == img[0]);
  CHECK(!rx.chunk(100, img.data() + 100, img.size() - 100, 5));
  CHECK(!rx.end());

  // Oversize chunk (past `size`) and empty chunk.
  CHECK(!rx.begin(buf.data(), img.size(), crc, 0));
  CHECK_STR(rx.chunk(0, img.data(), img.size() + 1, 1), "bad_range");
  CHECK_STR(rx.chunk(img.size() - 5, img.data(), 10, 1), "bad_range");
  CHECK_STR(rx.chunk(0xFFFFFFF0u, img.data(), 100, 1), "bad_range");
  CHECK_STR(rx.chunk(0, img.data(), 0, 1), "bad_range");
  rx.abort();

  // begin limits.
  CHECK_STR(rx.begin(buf.data(), 0, 0, 0), "bad_field size");
  CHECK_STR(rx.begin(buf.data(), IMG_MAX_COMPRESSED + 1, 0, 0), "too_big");
  CHECK(!rx.active);
  Bytes big(IMG_MAX_COMPRESSED);
  CHECK(!rx.begin(big.data(), IMG_MAX_COMPRESSED, 0, 0));
  rx.abort();

  // incomplete / bad crc / bad rle; end always finishes the transfer.
  CHECK(!rx.begin(buf.data(), img.size(), crc, 0));
  CHECK(!rx.chunk(0, img.data(), 100, 1));
  CHECK_STR(rx.end(), "incomplete");
  CHECK(!rx.active);
  CHECK(!rx.begin(buf.data(), img.size(), crc ^ 1, 0));
  CHECK(!rx.chunk(0, img.data(), img.size(), 1));
  CHECK_STR(rx.end(), "bad_crc");
  Bytes garbage(200, 0x80);  // control byte 0x80 is invalid
  CHECK(!rx.begin(buf.data(), garbage.size(), imgrx::crc32(garbage.data(), garbage.size()), 0));
  CHECK(!rx.chunk(0, garbage.data(), garbage.size(), 1));
  CHECK_STR(rx.end(), "bad_rle");
  Bytes shortImg = img;
  shortImg.pop_back();  // red plane one byte short, crc fine
  CHECK(!rx.begin(buf.data(), shortImg.size(), imgrx::crc32(shortImg.data(), shortImg.size()), 0));
  CHECK(!rx.chunk(0, shortImg.data(), shortImg.size(), 1));
  CHECK_STR(rx.end(), "bad_rle");

  // Idle timeout counts from the last accepted chunk; begin restarts a transfer.
  CHECK(!rx.begin(buf.data(), img.size(), crc, 1000));
  CHECK(!rx.idle(1000 + IMG_IDLE_MS - 1, IMG_IDLE_MS));
  CHECK(rx.idle(1000 + IMG_IDLE_MS, IMG_IDLE_MS));
  CHECK(!rx.chunk(0, img.data(), 100, 15000));
  CHECK(!rx.idle(15000 + IMG_IDLE_MS - 1, IMG_IDLE_MS));
  CHECK(!rx.begin(buf.data(), img.size(), crc, 16000));
  CHECK(rx.got == 0);
  rx.abort();
  CHECK(!rx.idle(1u << 30, IMG_IDLE_MS));  // nothing running, nothing to time out
  CHECK(IMG_IDLE_MS == 20000UL);

  // Worst case sizes: a noisy plane cannot be sent (RLE expands), the limit is on the compressed bytes.
  Bytes noisy(IMG_PLANE_BYTES);
  uint32_t x = 12345;
  for (auto& b : noisy) { x = x * 1664525u + 1013904223u; b = (uint8_t)(x >> 24); }
  CHECK(encode(noisy).size() > IMG_MAX_COMPRESSED);
  // A blank page is tiny.
  Bytes blank(IMG_PLANE_BYTES, 0);
  CHECK(encode(blank).size() == 236);
}

static void test_protocol() {
  HandleResult r;
  // page
  const char* modes[] = {"auto", "dash", "weather", "todo"};
  for (int i = 0; i < 4; i++) {
    r = feed(std::string("{\"v\":2,\"type\":\"page\",\"p\":\"") + modes[i] + "\"}");
    CHECK(r.ok && r.cmd == CMD_PAGE && (int)r.pageMode == i);
  }
  r = feed("{\"v\":2,\"type\":\"page\",\"p\":\"x\"}");
  CHECK(!r.ok); CHECK_STR(r.err, "bad_field p");
  r = feed("{\"v\":2,\"type\":\"page\",\"p\":\"Dash\"}");
  CHECK(!r.ok); CHECK_STR(r.err, "bad_field p");
  r = feed("{\"v\":2,\"type\":\"page\",\"p\":3}");
  CHECK(!r.ok); CHECK_STR(r.err, "bad_field p");
  r = feed("{\"v\":2,\"type\":\"page\"}");
  CHECK(!r.ok); CHECK_STR(r.err, "missing_field p");
  r = feed("{\"v\":1,\"type\":\"page\",\"p\":\"dash\"}");
  CHECK(!r.ok); CHECK_STR(r.err, "bad_version");
  // page_status / refresh_now
  r = feed("{\"v\":2,\"type\":\"page_status\"}");
  CHECK(r.ok && r.cmd == CMD_PAGE_STATUS);
  r = feed("{\"v\":2,\"type\":\"refresh_now\"}");
  CHECK(r.ok && r.cmd == CMD_REFRESH_NOW && !r.forceFull);
  r = feed("{\"v\":1,\"type\":\"refresh_now\"}");
  CHECK(!r.ok); CHECK_STR(r.err, "bad_version");
  r = feed("{\"v\":2,\"type\":\"refresh_nowx\"}");
  CHECK(!r.ok); CHECK_STR(r.err, "unknown_type");
  // They never touch the view.
  ViewState v;
  handleLine(v, "{\"v\":2,\"type\":\"page\",\"p\":\"todo\"}", 33);
  CHECK(!v.hasData && !v.todo);

  // img_begin
  r = feed("{\"v\":2,\"type\":\"img_begin\",\"id\":\"todo\",\"size\":1000,\"crc\":4294967295}");
  CHECK(r.ok && r.cmd == CMD_IMG_BEGIN && r.imgSize == 1000 && r.imgCrc == 4294967295u);
  r = feed("{\"v\":2,\"type\":\"img_begin\",\"id\":\"todo\",\"size\":12288,\"crc\":0}");
  CHECK(r.ok && r.imgSize == 12288);
  r = feed("{\"v\":2,\"type\":\"img_begin\",\"id\":\"todo\",\"size\":12289,\"crc\":0}");
  CHECK(!r.ok); CHECK_STR(r.err, "too_big");
  r = feed("{\"v\":2,\"type\":\"img_begin\",\"id\":\"todo\",\"size\":0,\"crc\":0}");
  CHECK(!r.ok); CHECK_STR(r.err, "bad_field size");
  r = feed("{\"v\":2,\"type\":\"img_begin\",\"id\":\"todo\",\"size\":-5,\"crc\":0}");
  CHECK(!r.ok); CHECK_STR(r.err, "bad_field size");
  r = feed("{\"v\":2,\"type\":\"img_begin\",\"id\":\"todo\",\"size\":\"9\",\"crc\":0}");
  CHECK(!r.ok); CHECK_STR(r.err, "bad_field size");
  r = feed("{\"v\":2,\"type\":\"img_begin\",\"id\":\"todo\",\"crc\":0}");
  CHECK(!r.ok); CHECK_STR(r.err, "missing_field size");
  r = feed("{\"v\":2,\"type\":\"img_begin\",\"id\":\"todo\",\"size\":9}");
  CHECK(!r.ok); CHECK_STR(r.err, "missing_field crc");
  r = feed("{\"v\":2,\"type\":\"img_begin\",\"id\":\"todo\",\"size\":9,\"crc\":4294967296}");
  CHECK(!r.ok); CHECK_STR(r.err, "bad_field crc");
  r = feed("{\"v\":2,\"type\":\"img_begin\",\"id\":\"todo\",\"size\":9,\"crc\":-1}");
  CHECK(!r.ok); CHECK_STR(r.err, "bad_field crc");
  r = feed("{\"v\":2,\"type\":\"img_begin\",\"id\":\"cover\",\"size\":9,\"crc\":1}");
  CHECK(!r.ok); CHECK_STR(r.err, "bad_field id");
  r = feed("{\"v\":2,\"type\":\"img_begin\",\"size\":9,\"crc\":1}");
  CHECK(!r.ok); CHECK_STR(r.err, "missing_field id");
  r = feed("{\"v\":1,\"type\":\"img_begin\",\"id\":\"todo\",\"size\":9,\"crc\":1}");
  CHECK(!r.ok); CHECK_STR(r.err, "bad_version");
  // img_chunk
  r = feed("{\"v\":2,\"type\":\"img_chunk\",\"id\":\"todo\",\"off\":4096,\"d\":\"TWFu\"}");
  CHECK(r.ok && r.cmd == CMD_IMG_CHUNK && r.imgOff == 4096 && r.imgLen == 3 && !memcmp(r.imgData, "Man", 3));
  r = feed("{\"v\":2,\"type\":\"img_chunk\",\"id\":\"todo\",\"off\":0,\"d\":\"TWF\"}");
  CHECK(!r.ok); CHECK_STR(r.err, "bad_field d");
  r = feed("{\"v\":2,\"type\":\"img_chunk\",\"id\":\"todo\",\"off\":0,\"d\":\"\"}");
  CHECK(!r.ok); CHECK_STR(r.err, "bad_field d");
  r = feed("{\"v\":2,\"type\":\"img_chunk\",\"id\":\"todo\",\"off\":0,\"d\":5}");
  CHECK(!r.ok); CHECK_STR(r.err, "bad_field d");
  r = feed("{\"v\":2,\"type\":\"img_chunk\",\"id\":\"todo\",\"off\":0}");
  CHECK(!r.ok); CHECK_STR(r.err, "missing_field d");
  r = feed("{\"v\":2,\"type\":\"img_chunk\",\"id\":\"todo\",\"d\":\"TWFu\"}");
  CHECK(!r.ok); CHECK_STR(r.err, "missing_field off");
  r = feed("{\"v\":2,\"type\":\"img_chunk\",\"id\":\"todo\",\"off\":-1,\"d\":\"TWFu\"}");
  CHECK(!r.ok); CHECK_STR(r.err, "bad_field off");
  r = feed("{\"v\":2,\"type\":\"img_chunk\",\"id\":\"x\",\"off\":0,\"d\":\"TWFu\"}");
  CHECK(!r.ok); CHECK_STR(r.err, "bad_field id");
  // Largest chunk that still fits a 1024-byte line: 768 raw bytes = 1024 base64 chars is too long for a line,
  // so the documented maximum is 600 raw bytes (800 chars); 768 decodes only if the line limit allows.
  Bytes raw600(600, 0xA5);
  std::string line = "{\"v\":2,\"type\":\"img_chunk\",\"id\":\"todo\",\"off\":12000,\"d\":\"" + b64(raw600.data(), 600) + "\"}";
  CHECK(line.size() <= PROTO_MAX_LINE_LEN);
  r = feed(line);
  CHECK(r.ok && r.imgLen == 600 && r.imgData[599] == 0xA5);
  Bytes raw768(768, 0x5A);
  line = "{\"v\":2,\"type\":\"img_chunk\",\"id\":\"todo\",\"off\":0,\"d\":\"" + b64(raw768.data(), 768) + "\"}";
  CHECK(line.size() > PROTO_MAX_LINE_LEN);
  r = feed(line);
  CHECK(!r.ok); CHECK_STR(r.err, "too_long");
  // img_end
  r = feed("{\"v\":2,\"type\":\"img_end\",\"id\":\"todo\"}");
  CHECK(r.ok && r.cmd == CMD_IMG_END);
  r = feed("{\"v\":2,\"type\":\"img_end\"}");
  CHECK(!r.ok); CHECK_STR(r.err, "missing_field id");
  r = feed("{\"v\":2,\"type\":\"img_other\",\"id\":\"todo\"}");
  CHECK(!r.ok); CHECK_STR(r.err, "unknown_type");
}

static void test_page_policy() {
  using namespace pagep;
  Mode m;
  CHECK(parseMode("weather", m) && m == PG_WEATHER);
  CHECK(!parseMode("", m) && !parseMode("TODO", m) && !parseMode("auto ", m));
  CHECK_STR(modeName(PG_TODO), "todo");
  CHECK_STR(shownName(SH_WEATHER), "weather");
  CHECK(validStored(0) && validStored(3) && !validStored(4) && !validStored(255));
  // Shown page: auto follows the offline rule, the others ignore it.
  CHECK(shown(PG_AUTO, false) == SH_DASH);
  CHECK(shown(PG_AUTO, true) == SH_WEATHER);
  for (bool aw : {false, true}) {
    CHECK(shown(PG_DASH, aw) == SH_DASH);
    CHECK(shown(PG_WEATHER, aw) == SH_WEATHER);
    CHECK(shown(PG_TODO, aw) == SH_TODO);
  }
  // Forced weather fetch: no data = now; fresh data (< 30 min) = no; older = yes; failed = wait the retry gap.
  wxp::Fetch f;
  CHECK(forcedFetchDue(f, 1000));  // never tried
  f.everTried = true;
  f.tryMs = 1000;
  f.haveData = true;
  f.okMs = 1000;
  CHECK(!forcedFetchDue(f, 1000 + WX_REFETCH_MS - 1));
  CHECK(forcedFetchDue(f, 1000 + WX_REFETCH_MS));
  f.lastFailed = true;  // stale data, last try failed 1 min ago: retry gap (5 min) first
  f.tryMs = 1000 + WX_REFETCH_MS + 1000;
  CHECK(!forcedFetchDue(f, f.tryMs + 60000));
  CHECK(forcedFetchDue(f, f.tryMs + WX_RETRY_MS));
  wxp::Fetch nd;  // no data, failed once
  nd.everTried = true;
  nd.lastFailed = true;
  nd.tryMs = 5000;
  CHECK(!forcedFetchDue(nd, 5000 + 1000));
  CHECK(forcedFetchDue(nd, 5000 + WX_RETRY_MS));
  // Hold the forced weather refresh while the first fetch runs (bounded).
  CHECK(holdForFetch(PG_WEATHER, false, true, 1000, 0, 30000));
  CHECK(!holdForFetch(PG_WEATHER, true, true, 1000, 0, 30000));    // data present
  CHECK(!holdForFetch(PG_WEATHER, false, false, 1000, 0, 30000));  // nothing running
  CHECK(!holdForFetch(PG_WEATHER, false, true, 30000, 0, 30000));  // cap reached
  CHECK(!holdForFetch(PG_AUTO, false, true, 1000, 0, 30000));
  CHECK(!holdForFetch(PG_TODO, false, true, 1000, 0, 30000));
}

// ---------------------------------------------------------------------
// refresh_now: bypasses the minimum interval once.
struct V {
  bool hasData = true;
  int val = 0;
  std::string signature() const { return std::to_string(val); }
};

static void test_refresh_now() {
  RefreshPolicy p{true, 180000, 0, 0, 86400000UL, 0};
  RefreshState s;
  s.lastDoneMs = 100000;
  // Plain decision function.
  CHECK(decideRefresh(p, s, 110000, true, false, true, false) == REFRESH_NONE);  // 10 s after: the 180 s rule
  CHECK(decideRefresh(p, s, 110000, true, false, true, false, true) == REFRESH_FULL);
  CHECK(decideRefresh(p, s, 110000, false, false, true, false, true) == REFRESH_FULL);  // unchanged: still redrawn
  CHECK(decideRefresh(p, s, 110000, true, false, true, true, true) == REFRESH_NONE);    // driver faulted
  CHECK(decideRefresh(p, s, 110000, false, false, true, false, false) == REFRESH_NONE);

  // Scheduler: one bypass, then the 180 s rule again.
  RefreshScheduler<V, std::string> sc;
  sc.policy = p;
  V v;
  int draws = 0;
  uint32_t now = 0;
  auto render = [&](const V&, RefreshKind) { draws++; };
  auto clock = [&] { return now; };
  now = 1000;
  sc.boot(v, false, render, clock);
  CHECK(draws == 1);
  now = 20000;
  v.val = 1;  // content changed but only 19 s after the last refresh
  CHECK(sc.tick(v, now, false, render, clock) == REFRESH_NONE);
  sc.refreshNow = true;
  CHECK(sc.tick(v, now, false, render, clock) == REFRESH_FULL);
  CHECK(draws == 2 && !sc.refreshNow);
  now = 21000;
  v.val = 2;  // automatic update right after: the 180 s rule applies again
  CHECK(sc.tick(v, now, false, render, clock) == REFRESH_NONE);
  now = 20000 + 180000;
  CHECK(sc.tick(v, now, false, render, clock) == REFRESH_FULL);
  CHECK(draws == 3);
  // Nothing changed + refresh_now: redraws anyway, right away.
  now += 5000;
  sc.refreshNow = true;
  CHECK(sc.tick(v, now, false, render, clock) == REFRESH_FULL);
  CHECK(draws == 4);
  // refresh_now arriving while a refresh runs stays pending and runs right after it.
  sc.refreshNow = true;
  now += 5000;
  auto renderBusy = [&](const V&, RefreshKind) {
    draws++;
    sc.refreshNow = true;  // arrives during the refresh
    now += 23000;
  };
  CHECK(sc.tick(v, now, false, renderBusy, clock) == REFRESH_FULL);
  CHECK(sc.refreshNow);
  CHECK(sc.tick(v, now, false, render, clock) == REFRESH_FULL);
  CHECK(!sc.refreshNow);
  CHECK(sc.tick(v, now, false, render, clock) == REFRESH_NONE);
  // Faulted driver: no refresh, the request is kept.
  sc.refreshNow = true;
  CHECK(sc.tick(v, now, true, render, clock) == REFRESH_NONE);
  CHECK(sc.refreshNow);
}

static void test_same_as_stored() {
  puts("img_end of the stored image -> no rewrite");
  CHECK(imgrx::sameAsStored(true, 5000, 0xDEADBEEF, 5000, 0xDEADBEEF));
  CHECK(!imgrx::sameAsStored(false, 0, 0, 5000, 0xDEADBEEF));                // nothing stored
  CHECK(!imgrx::sameAsStored(true, 5000, 0xDEADBEEF, 5001, 0xDEADBEEF));    // size differs
  CHECK(!imgrx::sameAsStored(true, 5000, 0xDEADBEEF, 5000, 0xDEADBEEE));    // crc differs
}

static void test_forced_weather_bypass() {
  puts("forced weather page: placeholder drawn, then data -> one bypass");
  using namespace pagep;
  CHECK(placeholderDrawn(PG_WEATHER, true, false));
  CHECK(!placeholderDrawn(PG_WEATHER, true, true));    // had data
  CHECK(!placeholderDrawn(PG_WEATHER, false, false));  // no location: its own page, not a placeholder
  CHECK(!placeholderDrawn(PG_AUTO, true, false) && !placeholderDrawn(PG_DASH, true, false));
  CHECK(bypassAfterFetch(PG_WEATHER, true, true));
  CHECK(!bypassAfterFetch(PG_WEATHER, false, true));   // the panel already shows data: normal gap
  CHECK(!bypassAfterFetch(PG_WEATHER, true, false));
  CHECK(!bypassAfterFetch(PG_AUTO, true, true) && !bypassAfterFetch(PG_TODO, true, true));
  // Through the scheduler: the bypass is a refreshNow, so exactly one early refresh, then the 180 s rule.
  RefreshPolicy p{true, 180000, 0, 0, 86400000UL, 0};
  RefreshScheduler<V, std::string> sc;
  sc.policy = p;
  V v;
  int draws = 0;
  uint32_t now = 1000;
  auto render = [&](const V&, RefreshKind) { draws++; };
  auto clock = [&] { return now; };
  sc.boot(v, false, render, clock);
  now = 40000;
  v.val = 1;  // the fetch delivered
  CHECK(sc.tick(v, now, false, render, clock) == REFRESH_NONE);
  sc.refreshNow = bypassAfterFetch(PG_WEATHER, true, true);
  CHECK(sc.tick(v, now, false, render, clock) == REFRESH_FULL);
  v.val = 2;
  now = 41000;
  CHECK(sc.tick(v, now, false, render, clock) == REFRESH_NONE);
}

// signature() caching: same refreshes, far fewer signature builds.
static void test_signature_cache() {
  puts("scheduler: cached signature gives the same refreshes");
  RefreshPolicy p{true, 180000, 0, 0, 86400000UL, 0};
  for (uint32_t recheck : {0u, 1000u}) {
    RefreshScheduler<V, std::string> sc;
    sc.policy = p;
    sc.sigRecheckMs = recheck;
    V v;
    std::vector<uint32_t> drawn;
    uint32_t now = 1000;
    auto render = [&](const V&, RefreshKind) { drawn.push_back(now); now += 15000; };
    auto clock = [&] { return now; };
    sc.boot(v, false, render, clock);
    // 40 minutes in 5 ms steps, the view changes every 400 s (announced with markDirty, as main.cpp does).
    uint32_t nextChange = now + 400000;
    for (; now < 40UL * 60 * 1000; now += 5) {
      if (now >= nextChange) {
        v.val++;
        sc.markDirty();
        nextChange += 400000;
      }
      sc.tick(v, now, false, render, clock);
    }
    static std::vector<uint32_t> ref;
    if (recheck == 0) {
      ref = drawn;
      CHECK(drawn.size() >= 5);
      CHECK(sc.sigComputed > 100000);  // the old behaviour: every pass once a refresh is allowed
    } else {
      CHECK(drawn == ref);             // not a single refresh moved
      CHECK(sc.sigComputed < 3000);    // ~ once per second at most, and only while a refresh is allowed
    }
  }
  // A change that nobody announced is still seen within sigRecheckMs.
  RefreshScheduler<V, std::string> sc;
  sc.policy = p;
  sc.sigRecheckMs = 1000;
  V v;
  uint32_t now = 1000;
  int draws = 0;
  auto render = [&](const V&, RefreshKind) { draws++; };
  auto clock = [&] { return now; };
  sc.boot(v, false, render, clock);
  now = 400000;
  CHECK(sc.tick(v, now, false, render, clock) == REFRESH_NONE);  // computes the signature: unchanged
  v.val = 7;                                                     // silent change
  now += 500;
  CHECK(sc.tick(v, now, false, render, clock) == REFRESH_NONE);  // cached, not recomputed yet
  now += 500;
  CHECK(sc.tick(v, now, false, render, clock) == REFRESH_FULL);  // 1 s later it is picked up
  // What was drawn is what is recorded as shown, even when the cache was stale.
  now += 200000;
  CHECK(sc.tick(v, now, false, render, clock) == REFRESH_NONE);
  // The cheap pre-check: while the interval forbids a refresh no signature is built at all.
  uint32_t before = sc.sigComputed;
  now += 10;
  for (int i = 0; i < 100; i++) sc.tick(v, now + i, false, render, clock);
  CHECK(sc.sigComputed == before + 1 || sc.sigComputed == before);
}

static void test_view_signature() {
  ViewState v;
  String boot = v.signature();
  v.todo = true;
  String a = v.signature();  // empty-state todo page
  CHECK(!(a == boot));
  v.todoCrc = 0x1234;
  String b = v.signature();
  CHECK(!(a == b));
  CHECK(b == v.signature());
  v.todoCrc = 0x1235;
  CHECK(!(b == v.signature()));
  // Dash data arriving while the TODO page is shown does not change it.
  String c = v.signature();
  v.hasData = true;
  v.weather = true;
  CHECK(c == v.signature());
  v.todo = false;
  CHECK(!(c == v.signature()));
}

int main() {
  if (!loadFixture()) {
    printf("cannot read ../docs/todo-image-fixtures.json (run from firmware/)\n");
    return 2;
  }
  test_crc_b64();
  test_rle_cases();
  test_frames();
  test_rx();
  test_protocol();
  test_page_policy();
  test_refresh_now();
  test_same_as_stored();
  test_forced_weather_bypass();
  test_signature_cache();
  test_view_signature();
  printf("\n%d checks, %d failed\n", checks, failures);
  return failures ? 1 : 0;
}
