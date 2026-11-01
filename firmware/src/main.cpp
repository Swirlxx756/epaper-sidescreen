// ESP32 e-paper sidescreen — receives one JSON object per line over USB
// serial or (ENABLE_WIFI) a TCP connection (protocol v1 stats, protocol v2
// dashboard blocks) and renders the v1 Claude-tokens + system screen or the
// v2 dashboard.

#include <Arduino.h>

#include <esp_system.h>

#include "config.h"
#include "fault_recovery.h"
#include "model.h"
#include "protocol.h"
#include "refresh_policy.h"
#include "ui.h"
#if ENABLE_WIFI
#include "net.h"
#include "ota.h"
#include "settings.h"
#include "weather_net.h"
#include "weather_parse.h"
#include "weather_policy.h"
#include "img_rx.h"
#include "page_policy.h"
#include "todo_store.h"
#endif

static ViewState view;
static uint32_t lastMsgMs = 0;  // 0 = nothing received yet
static RefreshScheduler<ViewState, String> sched;
static bool faultReported = false;
static char readyLine[96];  // "READY <name> <proto> <panel> <w>x<h>", sent on boot and to each TCP client
static char faultLine[160];  // last "DIAG epd fault ..." line, replayed to each new TCP client
static const char STOPPED_LINE[] = "DIAG display stopped (driver fault, see DIAG epd fault); reboot to retry";
static uint32_t rebootAtMs = 0;  // != 0: a new firmware was written, restart at this millis()
static char fwLine[64];          // "DIAG fw version=... test_pattern=N", also sent to each TCP client

// Display fault recovery (fault_recovery.h): the count of fault reboots in a row survives a software reset.
static faultrec::Rtc faultRtc RTC_NOINIT_ATTR;
static uint32_t faultBoots = 0;     // fault reboots that led to this boot (0 = normal boot)
static uint32_t faultSinceMs = 0;   // millis() when the fault was first seen, 0 = none
static char recoveryLine[96];       // last "DIAG recovery ..." line, also sent to each TCP client

// Diagnostic line: USB serial always, and the TCP client if one is connected
// (a board on a charger has no USB host, so faults must reach the WiFi host too).
static void diag(const char* line) {
  Serial.println(line);
  if (strncmp(line, "DIAG epd fault", 14) == 0) strlcpy(faultLine, line, sizeof(faultLine));
#if ENABLE_WIFI
  net::sendLine(line);
#endif
}

// ---------------------------------------------------------------------
// Protocol: parsing lives in protocol.cpp (no Serial there, so every
// transport feeds the same lines). Here: one reply sink per transport, the
// device commands and the serial line reader.
// ---------------------------------------------------------------------
static_assert(MAX_LINE_LEN == PROTO_MAX_LINE_LEN, "serial line buffer and protocol line limit differ");

enum Transport : uint8_t { TR_SERIAL, TR_TCP };

// Replies go back on the transport the line came from.
static Print& sinkFor(Transport t) {
#if ENABLE_WIFI
  if (t == TR_TCP) return net::client();
#endif
  (void)t;
  return Serial;
}

#if ENABLE_WIFI
// Factory reset: WiFi credentials + saved dashboard. `announce`: also print a
// DIAG line (the BOOT button has no reply channel).
static void wxReset();
static void imgAbort();
static void resetSettings(bool announce) {
  settings::resetAll(millis());
  todostore::clear();
  imgAbort();
  wxReset();
  net::disconnect();
  if (announce) diag("DIAG settings cleared");
}

// ---------------------------------------------------------------------
// Offline weather. Rules live in weather_policy.h (host-tested); here: the glue to settings, the fetch
// task (weather_net) and the view. The page replaces the dashboard after `after_min` minutes without a
// valid host message and goes away with the next one; it is redrawn only when its content changes, through
// the normal refresh scheduler (so never faster than the panel allows).
// ---------------------------------------------------------------------
static wxp::Fetch wxFetch;
static bool wxWanted = false;
static char wxErr[24] = "";    // last failed fetch reason, "" after a good one
static wxb::WeatherB wxData;   // last good answer
static double wxReqLat, wxReqLon;
static char wxReqPlace[WX_PLACE_MAX + 1];

// ---------------------------------------------------------------------
// Manual page override + TODO image. Rules in page_policy.h / img_rx.h (host-tested); here: NVS / flash glue.
// ---------------------------------------------------------------------
static uint32_t pageSetMs = 0;    // millis() of the last `page` message
static imgrx::Rx imgRx;
static uint8_t* imgBuf = nullptr; // staging buffer of the running transfer (heap, exactly `size` bytes)
static const uint32_t PAGE_FETCH_HOLD_MS = 30000;

static void imgAbort() {
  imgRx.abort();
  free(imgBuf);
  imgBuf = nullptr;
}

static void runPage(const HandleResult& r, Print& out) {
  if (r.pageMode != settings::pageMode() && !settings::pageSave(r.pageMode)) {
    out.println("ERR nvs_write");
    return;
  }
  pageSetMs = millis();
  out.println("OK");
}

static void runPageStatus(Print& out) {
  char buf[96];
  const pagep::Shown sh = view.todo ? pagep::SH_TODO : view.weather ? pagep::SH_WEATHER : pagep::SH_DASH;
  int n = snprintf(buf, sizeof(buf), "PAGE {\"type\":\"status\",\"p\":\"%s\",\"shown\":\"%s\",\"todo_crc\":",
                   pagep::modeName(settings::pageMode()), pagep::shownName(sh));
  if (todostore::has())
    snprintf(buf + n, sizeof(buf) - n, "%lu}", (unsigned long)todostore::crc());
  else
    snprintf(buf + n, sizeof(buf) - n, "null}");
  out.println(buf);
  out.println("OK");
}

static void runImg(const HandleResult& r, Print& out) {
  const char* err = nullptr;
  switch (r.cmd) {
    case CMD_IMG_BEGIN:
      imgAbort();  // a new begin replaces an unfinished transfer
      imgBuf = (uint8_t*)malloc(r.imgSize);
      if (!imgBuf) {
        err = "low_mem";
        break;
      }
      err = imgRx.begin(imgBuf, r.imgSize, r.imgCrc, millis());
      if (err) imgAbort();
      break;
    case CMD_IMG_CHUNK:
      err = imgRx.chunk(r.imgOff, r.imgData, r.imgLen, millis());
      break;
    default: {  // CMD_IMG_END
      uint32_t size = imgRx.size, crc = imgRx.crc;
      err = imgRx.end();
      // The host re-sends the page it already pushed (e.g. after a reconnect): same size + crc = same image,
      // skip the 12 KB flash erase + write.
      if (!err && !imgrx::sameAsStored(todostore::has(), todostore::size(), todostore::crc(), size, crc) &&
          !todostore::save(imgBuf, size, crc))
        err = "flash_write";
      if (!imgRx.active) {
        free(imgBuf);
        imgBuf = nullptr;
      }
      break;
    }
  }
  if (err) {
    out.print("ERR ");
    out.println(err);
  } else {
    out.println("OK");
  }
}

static void wxReset() {
  wxFetch = wxp::Fetch();
  wxWanted = false;
  wxErr[0] = '\0';
  view.weather = false;
}

static bool wxSameCfg(const wxp::Cfg& a, const wxp::Cfg& b) {
  return a.on == b.on && a.hasLoc == b.hasLoc && a.afterMin == b.afterMin &&
         (!a.hasLoc || (a.lat == b.lat && a.lon == b.lon && strcmp(a.place, b.place) == 0));
}

static void runWxCfg(const HandleResult& r, Print& out) {
  wxp::Cfg c = settings::wxCfg();
  c.on = r.wxOn;
  if (r.wxSetLoc) {
    c.hasLoc = true;
    strlcpy(c.place, r.wxPlace, sizeof(c.place));
    c.lat = r.wxLat;
    c.lon = r.wxLon;
  }
  if (r.wxAfterMin) c.afterMin = r.wxAfterMin;
  const wxp::Cfg old = settings::wxCfg();
  if (!wxSameCfg(c, old)) {  // the host re-sends the same config on every connect: no NVS write then
    if (!settings::wxSave(c)) {
      out.println("ERR nvs_write");
      return;
    }
    bool moved = c.hasLoc != old.hasLoc || (c.hasLoc && (c.lat != old.lat || c.lon != old.lon || strcmp(c.place, old.place) != 0));
    if (moved) {
      wxFetch = wxp::Fetch();  // data of the old place is useless
      wxErr[0] = '\0';         // and so is the reason the old place failed
    }
  }
  out.println("OK");
}

static void runWxStatus(Print& out) {
  char buf[200];
  if (!wxb::statusJson(settings::wxCfg(), view.weather, wxFetch.haveData ? wxData.updated_at : "", wxErr, buf,
                       sizeof(buf))) {
    out.println("ERR too_long");
    return;
  }
  out.print("WX ");
  out.println(buf);
  out.println("OK");
}

static uint32_t wxViewGen = 0;            // +1 whenever wxData is replaced
static bool wxPlaceholderOnPanel = false; // the forced weather page last drawn was the "no data yet" placeholder

// A running OTA / TODO-image transfer owns the connection (and the heap): no new TCP client may take over
// (net.cpp) and no weather fetch (TLS needs ~40 KB) starts meanwhile.
static bool transferActive() { return ota::active() || imgRx.active; }

// Loop only. Decides the mode, runs the fetch task and keeps view.weather / view.wx current.
static void wxServiceInner(uint32_t now) {
  const wxp::Cfg& c = settings::wxCfg();
  bool wifiUp = net::state() == net::WST_CONNECTED;
  const pagep::Mode pm = settings::pageMode();
  // The offline rules only run in auto; a forced page is not "offline weather".
  wxWanted = pm == pagep::PG_AUTO && wxp::wantWeather(c, wxWanted, lastMsgMs, now, wifiUp, OFFLINE_TIMEOUT_MS);

  if (pm != pagep::PG_WEATHER) wxPlaceholderOnPanel = false;
  // A fetch task that hangs (TLS / DNS stuck beyond its own timeouts) is reported as failed after 90 s so the
  // normal retry gap applies; a new fetch starts only once the old task has really ended (wxnet::start()).
  if (wxnet::watchdog(now)) diag("DIAG wx fetch watchdog: task over 90 s, given up");

  bool ok;
  wxb::WeatherB fresh;
  char err[24];
  if (wxnet::take(ok, fresh, err, sizeof(err))) {
    bool same = c.hasLoc && c.lat == wxReqLat && c.lon == wxReqLon && strcmp(c.place, wxReqPlace) == 0;
    if (!same) {
      wxFetch.everTried = false;  // the place changed while fetching: ask again for the new one
    } else if (ok) {
      wxData = fresh;
      wxViewGen++;
      wxFetch.haveData = true;
      wxFetch.okMs = now;
      wxFetch.lastFailed = false;
      wxErr[0] = '\0';
      // The forced weather page was drawn as a placeholder while this fetch ran: show the data right away
      // (once, like refresh_now) instead of after the 180 s gap.
      if (pagep::bypassAfterFetch(pm, wxPlaceholderOnPanel, true)) sched.refreshNow = true;
      wxPlaceholderOnPanel = false;
      char line[80];
      snprintf(line, sizeof(line), "DIAG wx fetch ok %s stack_free=%lu", wxData.updated_at,
               (unsigned long)wxnet::stackFreeBytes());
      diag(line);
    } else {
      wxFetch.lastFailed = true;
      strlcpy(wxErr, err, sizeof(wxErr));
      char line[80];
      snprintf(line, sizeof(line), "DIAG wx fetch fail %s stack_free=%lu", err, (unsigned long)wxnet::stackFreeBytes());
      diag(line);
    }
  }
  bool fetchNeeded = pm == pagep::PG_WEATHER ? pagep::forcedFetchDue(wxFetch, now)
                                             : (wxWanted && wxp::fetchDue(wxFetch, now));
  if (fetchNeeded && c.hasLoc && wifiUp && !wxnet::busy() && !ui::faulted() && !transferActive()) {
    if (wxnet::start(c.lat, c.lon, c.place)) {
      wxReqLat = c.lat;
      wxReqLon = c.lon;
      strlcpy(wxReqPlace, c.place, sizeof(wxReqPlace));
      wxFetch.everTried = true;
      wxFetch.tryMs = now;
    }
  }

  view.todo = pm == pagep::PG_TODO;
  view.todoCrc = todostore::crc();
  if (pm == pagep::PG_DASH || pm == pagep::PG_TODO) {
    view.weather = false;
    return;
  }
  if (pm == pagep::PG_WEATHER) {  // always the weather page; no data yet = the existing placeholder states
    view.weather = true;
    if (!c.hasLoc) {
      memset(&view.wx, 0, sizeof(view.wx));
      view.wx.status = wxb::WX_NO_LOCATION;
      return;
    }
    if (wxFetch.haveData) {
      view.wx = wxData;
    } else {
      memset(&view.wx, 0, sizeof(view.wx));
      strlcpy(view.wx.place, c.place, sizeof(view.wx.place));
    }
    view.wx.status = !wifiUp ? wxb::WX_NO_WIFI : (wxFetch.lastFailed || !wxp::dataFresh(wxFetch, now)) ? wxb::WX_STALE : wxb::WX_OK;
    return;
  }
  if (!wxp::showWeather(c, wxFetch, wxWanted, wifiUp, now)) {
    view.weather = false;
    return;
  }
  view.weather = true;
  if (!c.hasLoc) {
    memset(&view.wx, 0, sizeof(view.wx));
    view.wx.status = wxb::WX_NO_LOCATION;
    return;
  }
  view.wx = wxData;
  view.wx.status = !wifiUp ? wxb::WX_NO_WIFI : (wxFetch.lastFailed || !wxp::dataFresh(wxFetch, now)) ? wxb::WX_STALE : wxb::WX_OK;
}

// Wrapper: wxServiceInner() assigns view fields on every pass; tell the scheduler when something it sees changed
// (it does not recompute the signature on every pass, see RefreshScheduler::sigRecheckMs).
static void wxService(uint32_t now) {
  const bool w0 = view.weather, t0 = view.todo;
  const uint32_t c0 = view.todoCrc, g0 = wxViewGen;
  const int s0 = (int)view.wx.status;
  wxServiceInner(now);
  if (w0 != view.weather || t0 != view.todo || c0 != view.todoCrc || g0 != wxViewGen || s0 != (int)view.wx.status)
    sched.markDirty();
}

// A firmware written over WiFi stays "pending verify" until the host can
// reach it again (TCP client) or a valid message arrives; see ota.h. Done once it is confirmed or there is
// nothing to confirm; if confirming fails (flash / state read error) it is tried again at the next message.
static void confirmFirmware() {
  static bool done = false, failedReported = false;
  if (done) return;
  switch (ota::confirm()) {
    case ota::CONFIRM_OK:
      done = true;
      diag("DIAG ota confirmed (new firmware kept)");
      break;
    case ota::CONFIRM_NOT_PENDING:
      done = true;
      break;
    default:
      if (!failedReported) {
        failedReported = true;
        diag("DIAG ota confirm failed, will retry");
      }
      break;
  }
}

// Same length and every byte equal, without stopping at the first difference.
static bool tokenEquals(const char* a, const char* b) {
  size_t n = strlen(a);
  if (n == 0 || n != strlen(b)) return false;
  uint8_t diff = 0;
  for (size_t i = 0; i < n; i++) diff |= (uint8_t)(a[i] ^ b[i]);
  return diff == 0;
}

// ota_begin: TCP only, pairing code required. On OK the next r.otaSize bytes
// of this connection are the image (net raw mode -> onOtaRaw).
static void runOtaBegin(Transport t, const HandleResult& r, Print& out) {
  if (t != TR_TCP) {
    out.println("ERR tcp_only");
    return;
  }
  char tok[17];
  if (!settings::otaToken(tok, false) || !tokenEquals(tok, r.token)) {
    out.println("ERR ota_token");
    return;
  }
  switch (ota::begin(r.otaSize, r.md5, millis())) {
    case ota::BEGIN_OK: out.println("OK"); break;
    case ota::BEGIN_TOO_BIG: out.println("ERR ota_too_big"); break;
    default: out.println("ERR ota_begin"); break;
  }
}

static void runCommand(Transport t, const HandleResult& r, Print& out) {
  if (r.cmd == CMD_OTA_BEGIN) {
    runOtaBegin(t, r, out);
    return;
  }
  if (r.cmd == CMD_WX_CFG) {
    runWxCfg(r, out);
    return;
  }
  if (r.cmd == CMD_WX_STATUS) {
    runWxStatus(out);
    return;
  }
  if (r.cmd == CMD_PAGE) {
    runPage(r, out);
    return;
  }
  if (r.cmd == CMD_PAGE_STATUS) {
    runPageStatus(out);
    return;
  }
  if (r.cmd == CMD_REFRESH_NOW) {
    sched.refreshNow = true;
    out.println("OK");
    return;
  }
  if (r.cmd == CMD_IMG_BEGIN || r.cmd == CMD_IMG_CHUNK || r.cmd == CMD_IMG_END) {
    runImg(r, out);
    return;
  }
  if (t != TR_SERIAL) {  // provisioning is USB-only: nobody on the network may change WiFi settings
    out.println("ERR usb_only");
    return;
  }
  switch (r.cmd) {
    case CMD_WIFI_SCAN:
      if (net::printScan(out))
        out.println("OK");
      else
        out.println("ERR scan_failed");
      break;
    case CMD_WIFI_SET:
      if (!settings::saveWifi(r.ssid, r.pass)) {
        out.println("ERR nvs_write");
        break;
      }
      net::connect(r.ssid, r.pass);  // the result arrives as an unsolicited WIFI status line
      out.println("OK");
      break;
    case CMD_WIFI_STATUS:
      net::printStatus(out);
      out.println("OK");
      break;
    case CMD_WIFI_CLEAR:
      settings::clearWifi();
      net::disconnect();
      out.println("OK");
      break;
    case CMD_RESET_SETTINGS:
      resetSettings(false);
      out.println("OK");
      break;
    case CMD_PAIR: {
      char tok[17];
      if (!settings::otaToken(tok, true)) {
        out.println("ERR nvs_write");
        break;
      }
      net::printPair(out, tok);
      out.println("OK");
      break;
    }
    default:
      out.println("ERR unknown_type");
      break;
  }
}
#endif

// One complete line from `t`. Valid messages (not commands) count as "online".
static void handleInput(Transport t, const char* line, size_t len, bool tooLong) {
  HandleResult r = tooLong ? tooLongResult() : handleLine(view, line, len);
  if (r.blank) return;
  sched.markDirty();  // handleLine may have changed the view
  Print& out = sinkFor(t);
  if (!r.ok) {
    out.print("ERR ");
    out.println(r.err);
    return;
  }
  if (r.cmd != CMD_NONE) {
#if ENABLE_WIFI
    runCommand(t, r, out);
    memset(r.pass, 0, sizeof(r.pass));  // do not leave the password / pairing code on the stack longer than needed
    memset(r.token, 0, sizeof(r.token));
#else
    out.println("ERR unsupported");  // this build has no WiFi / NVS
#endif
    return;
  }
#if ENABLE_WIFI
  if (r.kind != KIND_NONE) settings::snapPut(r.kind, line, len);  // goes to NVS later, rate limited
  confirmFirmware();
#endif
  if (r.forceFull) sched.forceFull = true;
  lastMsgMs = millis();
  if (lastMsgMs == 0) lastMsgMs = 1;
  out.println("OK");
}

// Non-blocking: consumes whatever is in the RX buffer.
static void pollSerial() {
  static char buf[MAX_LINE_LEN + 1];
  static size_t len = 0;
  static bool tooLong = false;

  while (Serial.available() > 0) {
    int c = Serial.read();
    if (c < 0) break;
    if (c == '\r') continue;
    if (c == '\n') {
      if (tooLong) {
        handleInput(TR_SERIAL, "", 0, true);
      } else {
        buf[len] = '\0';
        handleInput(TR_SERIAL, buf, len, false);
      }
      len = 0;
      tooLong = false;
      continue;
    }
    if (tooLong) continue;  // drop the rest of an oversized line
    if (len >= MAX_LINE_LEN) {
      tooLong = true;
      len = 0;
      continue;
    }
    buf[len++] = (char)c;
  }
}

#if ENABLE_WIFI
static void onNetStatus() { net::printStatus(Serial); }  // unsolicited, USB only
static void onTcpLine(const char* line, size_t len, bool tooLong) { handleInput(TR_TCP, line, len, tooLong); }

// After READY: the firmware version (so the host can check an upgrade), and a
// standing fault (a host that connects after it still has to learn about it).
static void onTcpClient() {
  confirmFirmware();
  net::client().println(fwLine);
  if (recoveryLine[0]) net::client().println(recoveryLine);
  if (!ui::faulted()) return;
  if (faultLine[0]) net::client().println(faultLine);
  net::client().println(STOPPED_LINE);
}

static void onTcpClientGone() { ota::abort(); }  // half an image is useless; the running firmware is untouched
static size_t otaWant() { return ota::want(); }

static void onOtaRaw(const uint8_t* data, size_t len) {
  Print& out = net::client();
  switch (ota::write(data, len, millis())) {
    case ota::FEED_MORE: return;
    case ota::FEED_OK:
      out.println("OK");
      diag("DIAG ota done, rebooting");
      rebootAtMs = millis() + OTA_REBOOT_DELAY_MS;
      if (rebootAtMs == 0) rebootAtMs = 1;
      return;
    case ota::FEED_MD5: out.println("ERR ota_md5"); break;
    case ota::FEED_WRITE: out.println("ERR ota_write"); break;
    default: out.println("ERR ota_end"); break;
  }
}

// No image bytes for OTA_IDLE_MS: give up, back to lines.
static void pollOtaTimeout() {
  if (!ota::idle(millis(), OTA_IDLE_MS)) return;
  ota::abort();
  net::sendLine("ERR ota_timeout");
}

// BOOT button (GPIO0) held RESET_HOLD_MS at runtime = factory reset. Only read
// after boot, so flashing (BOOT held while resetting) is not affected.
static void pollResetButton() {
  static uint32_t downSince = 0;  // 0 = not pressed
  static bool fired = false;
  if (digitalRead(BOOT_BUTTON_PIN) != LOW) {
    downSince = 0;
    fired = false;
    return;
  }
  uint32_t now = millis();
  if (downSince == 0) downSince = now ? now : 1;
  if (!fired && now - downSince >= RESET_HOLD_MS) {
    fired = true;  // once per press
    resetSettings(true);
  }
}
#endif

// Everything that must keep running while the panel refreshes (~23 s): serial
// replies, WiFi / TCP / UDP service, the reset button. Also the display busy
// callback. The periodic snapshot flush (settings::service) is loop-only, but
// commands that arrive here do write flash like anywhere else: wx_cfg / page /
// wifi_set (NVS), img_end (erase + write of the TODO partition, tens of ms) and
// reset_settings; the panel does not mind (the bit-banged SPI has no timing
// to keep). The one long blocking call that can arrive here is a USB wifi_scan
// (2-4 s).
static void pollAll() {
  pollSerial();
#if ENABLE_WIFI
  net::poll();
  pollOtaTimeout();
  pollResetButton();
#endif
}

// ---------------------------------------------------------------------
// Refresh policy
// ---------------------------------------------------------------------
static void renderSnapshot(const ViewState& snap, RefreshKind kind) { ui::render(snap, kind == REFRESH_FULL); }
static uint32_t clockMs() { return millis(); }

// A driver fault stops the display until reboot; see fault_recovery.h. Announce the plan once, reboot when it is due.
static void faultRecovery(uint32_t now) {
  if (!faultSinceMs) {
    faultSinceMs = now ? now : 1;
    if (faultBoots < FAULT_MAX_BOOTS)
      snprintf(recoveryLine, sizeof(recoveryLine), "DIAG recovery: auto reboot in %lu s (fault reboot %lu of %d)",
               (unsigned long)(FAULT_REBOOT_AFTER_MS / 1000UL), (unsigned long)(faultBoots + 1), FAULT_MAX_BOOTS);
    else
      snprintf(recoveryLine, sizeof(recoveryLine), "DIAG recovery: off, %lu fault reboots in a row; power-cycle the board",
               (unsigned long)faultBoots);
    diag(recoveryLine);
  }
#if ENABLE_WIFI
  if (transferActive() || rebootAtMs) return;  // never cut an upgrade / image transfer short
#endif
  if (!faultrec::rebootDue(faultSinceMs, now, faultBoots)) return;
  diag("DIAG recovery: rebooting now");
  faultrec::noteReboot(faultRtc, faultBoots);
#if ENABLE_WIFI
  net::dropClient();
#endif
  delay(100);  // let the DIAG line out
  ESP.restart();
}

static void refreshIfNeeded() {
  uint32_t now = millis();
  bool online = isOnline(lastMsgMs, now, OFFLINE_TIMEOUT_MS);
  if (online != view.online) {
    view.online = online;
    sched.markDirty();
  }

  bool faulted = ui::faulted();
  if (faulted && !faultReported) {
    // Serial / TCP keep working (OK/ERR unchanged); only the display stops.
    diag(STOPPED_LINE);
    faultReported = true;
  }
  if (faulted) faultRecovery(now);
#if ENABLE_WIFI
  if (ota::active() || rebootAtMs) return;  // no 23 s refresh in the middle of an upgrade
#endif

#if ENABLE_WIFI
  // Forced weather page with no data yet and a fetch under way: draw once it has finished (or after 30 s).
  if (pagep::holdForFetch(settings::pageMode(), wxFetch.haveData, wxnet::busy(), now, pageSetMs, PAGE_FETCH_HOLD_MS)) return;
#endif
  sched.policy.dataIntervalMs = (uint32_t)view.d.top.everyS * 1000UL;  // the host's refresh interval
  // Renders a snapshot of `view`; data arriving during the refresh (busy
  // callback) stays pending and only the newest is shown on the next pass.
  RefreshKind drawn = sched.tick(view, now, faulted, renderSnapshot, clockMs);
  if (drawn == REFRESH_NONE) return;
  if (!ui::faulted()) {  // a refresh worked: the panel is fine, forget earlier fault reboots
    faultrec::clear(faultRtc);
    faultBoots = 0;
  }
#if ENABLE_WIFI
  wxPlaceholderOnPanel = view.weather && pagep::placeholderDrawn(settings::pageMode(), settings::wxCfg().hasLoc, wxFetch.haveData);
#endif
}

// ---------------------------------------------------------------------
void setup() {
  // RTC memory is only meaningful after a software reset (our own fault reboot); anything else starts over.
  const bool swReset = esp_reset_reason() == ESP_RST_SW;
  faultBoots = faultrec::bootsSoFar(faultRtc, swReset);
  if (!swReset) faultrec::clear(faultRtc);

  Serial.setRxBufferSize(SERIAL_RX_BUFFER);  // must precede begin()
  Serial.begin(SERIAL_BAUD);
#if ARDUINO_USB_CDC_ON_BOOT
  Serial.setTxTimeoutMs(0);  // don't stall when no host has the port open
#endif

#if ENABLE_WIFI
  // Saved dashboard: replay the stored lines through the normal parser so the
  // boot screen is the last dashboard. lastMsgMs stays 0, so it shows OFFLINE
  // until the host sends something.
  settings::begin(millis());
  for (int k = KIND_DASH; k < KIND_COUNT; k++) {
    const char* saved = settings::snapLine((Kind)k);
    if (saved[0]) handleLine(view, saved, strlen(saved));
  }
  todostore::begin();
  pinMode(BOOT_BUTTON_PIN, INPUT_PULLUP);
#endif

  if (ui::fullRefreshOnly()) {
    sched.policy = {true, EPD_TRI_MIN_REFRESH_MS, 0, 0, EPD_TRI_MAX_IDLE_MS};
  } else {
    sched.policy = {false, MIN_REFRESH_MS, FULL_REFRESH_EVERY, FULL_REFRESH_MS, 0};
  }
  sched.sigRecheckMs = 1000;  // the signature is rebuilt at most once a second unless the view was marked dirty
  ui::begin(pollAll, diag);
#if EPD_PANEL == PANEL_WS42_TRI
  // Tells the production build (test_pattern=0) from the calibration build.
  snprintf(fwLine, sizeof(fwLine), "DIAG fw version=%s test_pattern=%d", FW_VERSION, EPD_TRI_TEST_PATTERN);
  Serial.println(fwLine);
#else
  snprintf(fwLine, sizeof(fwLine), "DIAG fw version=%s test_pattern=0", FW_VERSION);
#endif
  snprintf(readyLine, sizeof(readyLine), "READY %s %s %s %dx%d", FW_NAME, FW_PROTO, ui::panelName(), ui::width(),
           ui::height());
  Serial.println(readyLine);
  if (faultBoots) {  // this boot is the result of the display fault recovery
    snprintf(recoveryLine, sizeof(recoveryLine), "DIAG recovery: boot after fault reboot %lu of %d",
             (unsigned long)faultBoots, FAULT_MAX_BOOTS);
    Serial.println(recoveryLine);
  }

#if ENABLE_WIFI
  // WiFi starts after READY (the host waits for it). The first status line is
  // printed here; later changes are reported by net::poll().
  net::Handlers h{};
  h.onStatus = onNetStatus;
  h.onLine = onTcpLine;
  h.ready = readyLine;
  h.onClient = onTcpClient;
  h.onClientGone = onTcpClientGone;
  h.rawWant = otaWant;
  h.onRaw = onOtaRaw;
  h.keepClient = transferActive;
  net::begin(h);
  char ssid[PROTO_SSID_MAX + 1], pass[PROTO_PASS_MAX + 1];
  if (settings::loadWifi(ssid, pass)) net::connect(ssid, pass);
  memset(pass, 0, sizeof(pass));
  net::printStatus(Serial);
  net::markReported();
#endif

  // Boot screen (full refresh). Stats received while it is on the panel are
  // not marked as shown, so they appear at the next allowed refresh.
  sched.boot(view, ui::faulted(), renderSnapshot, clockMs);
  if (!ui::faulted()) {
    faultrec::clear(faultRtc);
    faultBoots = 0;
  }
}

void loop() {
  pollAll();
#if ENABLE_WIFI
  settings::service(millis());
  wxService(millis());
  if (imgRx.idle(millis(), IMG_IDLE_MS)) {  // unfinished TODO transfer: drop it, the last good image stays
    imgAbort();
    diag("DIAG img timeout");
  }
  if (rebootAtMs && (int32_t)(millis() - rebootAtMs) >= 0) {  // after the OK / DIAG lines went out
    net::dropClient();
    delay(100);
    ESP.restart();
  }
#endif
  refreshIfNeeded();
  delay(5);
}
