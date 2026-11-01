#pragma once
// WiFi transport: station connection management, the TCP line server and
// the UDP discovery responder. Only built with ENABLE_WIFI. Everything is
// non-blocking except printScan() (a few seconds). Safe to call poll() from
// the display busy callback.
//
// Status changes are reported only from poll() (never from connect() /
// disconnect()), so a command's "OK" is printed before its WIFI status line.

#include <Arduino.h>
#include <stddef.h>

#include "config.h"

#if ENABLE_WIFI
namespace net {

enum State : uint8_t { WST_OFF, WST_CONNECTING, WST_CONNECTED, WST_FAILED };

struct Handlers {
  void (*onStatus)();  // the public state (or IP) changed
  void (*onLine)(const char* line, size_t len, bool tooLong);  // one TCP line, without '\n'
  const char* ready;   // sent to every new TCP client
  void (*onClient)();  // a new TCP client got `ready`; may write to client() (e.g. a standing fault)
  void (*onClientGone)();  // the TCP client disconnected or was replaced by a new one
  // Raw mode (firmware upgrade): while rawWant() > 0, TCP bytes go to onRaw()
  // (at most rawWant() per call) instead of the line parser.
  size_t (*rawWant)();
  void (*onRaw)(const uint8_t* data, size_t len);
  // True while a transfer (firmware upgrade / TODO image) is running on the current client: a new TCP
  // connection is then closed instead of replacing it. nullptr = never keep.
  bool (*keepClient)();
};

void begin(const Handlers& h);

// Save credentials in RAM and start connecting (pass may be empty = open network).
void connect(const char* ssid, const char* pass);
// Forget credentials, close sockets, radio off.
void disconnect();

void poll();

// "off" until credentials are set, "failed" also while silently retrying.
State state();

// Treat the current state as already reported (after the caller printed it itself).
void markReported();

// "WIFI {"type":"status",...}" + newline. Never contains the password.
void printStatus(Print& out);
// "WIFI {"type":"scan","nets":[...]}" + newline. Blocks 2-4 s. False if the scan failed.
bool printScan(Print& out);
// "WIFI {"type":"pair","token":"...","id":"..."}" + newline (USB only).
void printPair(Print& out, const char* token);
// Close the TCP client (before a reboot).
void dropClient();

// The current TCP client; only meaningful inside Handlers::onLine / onClient. Everything printed up to a
// newline goes out in ONE socket write; a failed write closes the client (and calls onClientGone).
Print& client();
// One line + newline to the TCP client, if one is connected (unsolicited DIAG lines). Dropped (not queued, never
// blocking) when the socket's send buffer is nearly full.
void sendLine(const char* line);

}  // namespace net
#endif
