"""WiFi 传输（tcp:// Link）、`WIFI {json}` 行、Link.command、UDP 发现、命令行入口的测试。
只用本机回环上的假板子（TCP 7330 / UDP 7331 的替身），不开真串口，不碰网络上的真实设备。"""
import contextlib
import hashlib
import io
import json
import logging
import queue
import socket
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import discovery
import epaper_host as host
from test_dashboard_v2 import NOW, FakeMouse, FakeTm, snapshot
from test_host import FakeSerial, eventually

READY_V2 = b"READY epaper-sidescreen v2 GDEY042T81 400x300\n"
USB_ONLY = ("wifi_scan", "wifi_set", "wifi_status", "wifi_clear", "reset_settings")
SECRET = "hunter2-very-secret"
OTA_TOKEN = "5ec7e7c0ffee0042"     # 假配对码；测试检查它不出现在任何日志里
FW_LINE = b"DIAG fw version=1.3.1 test_pattern=0\n"


def fw_image(size=20000, magic=0xE9):
    """像 ESP32 固件的字节串：首字节 0xE9，其余是可辨认的伪随机内容。"""
    return bytes([magic]) + bytes((i * 131 + 7) & 0xFF for i in range(size - 1))


class FakeBoard:
    """TCP 7330 的替身：连上先发 READY，每行 JSON 回 OK；wifi_* / pair 回 ERR usb_only；可以把客户端踢掉。

    ota_begin：核对 token、大小，回 OK 后把接下来 size 个字节当固件原文收进 `image`，再核 MD5，回 OK +
    `DIAG ota done, rebooting` 然后断开。`ota` 选择故障：ok | md5（回 ERR ota_md5）| stall（收到一块后回
    ERR ota_timeout 回到按行模式）| drop（收到一块后断开）| silent_end（收满后不回话）| silent_begin（不回 begin）。"""

    def __init__(self, ready=READY_V2, token=OTA_TOKEN, ota="ok"):
        self.ready = ready
        self.token = token
        self.ota = ota
        self.image: bytes | None = None    # 最近一次升级收到的原始字节
        self.junk = 0                      # 按行模式下收到的非 JSON 行
        self.received: list[dict] = []
        self.connections = 0
        self._clients: list[socket.socket] = []
        self._stop = threading.Event()
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(4)
        self.srv.settimeout(0.05)
        self.port = self.srv.getsockname()[1]
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"tcp://127.0.0.1:{self.port}"

    def _accept(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.connections += 1
            self._clients.append(conn)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        buf = b""
        need, got, md5 = 0, bytearray(), ""     # need > 0：正在收固件原文
        try:
            conn.sendall(self.ready)
            conn.settimeout(0.05)
            while not self._stop.is_set():
                try:
                    chunk = conn.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    return
                buf += chunk
                while buf:
                    if need:
                        take = buf[:need - len(got)]
                        got += take
                        buf = buf[len(take):]
                        self.image = bytes(got)
                        if self.ota == "drop" and len(got) >= 4096:
                            return
                        if self.ota == "stall" and len(got) >= 4096:
                            conn.sendall(b"ERR ota_timeout\n")
                            need, buf = 0, b""
                            continue
                        if len(got) < need:
                            continue
                        need = 0
                        if self.ota == "silent_end":
                            continue
                        if self.ota == "md5" or hashlib.md5(got).hexdigest() != md5:
                            conn.sendall(b"ERR ota_md5\n")
                            continue
                        conn.sendall(b"OK\nDIAG ota done, rebooting\n")
                        time.sleep(0.1)
                        return
                    if b"\n" not in buf:
                        break
                    line, buf = buf.split(b"\n", 1)
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        self.junk += 1
                        continue
                    self.received.append(msg)
                    kind = msg.get("type")
                    if kind in USB_ONLY or kind == "pair":
                        conn.sendall(b"ERR usb_only\n")
                    elif kind == "ota_begin":
                        if self.ota == "silent_begin":
                            continue
                        if msg.get("token") != self.token:
                            conn.sendall(b"ERR ota_token\n")
                        elif msg.get("size", 0) > host.OTA_PARTITION_SIZE:
                            conn.sendall(b"ERR ota_too_big\n")
                        else:
                            need, got, md5 = msg["size"], bytearray(), msg["md5"]
                            conn.sendall(b"OK\n")
                    else:
                        conn.sendall(b"OK\n")
        except OSError:
            pass
        finally:
            conn.close()

    def drop_clients(self):
        for c in self._clients:
            with contextlib.suppress(OSError):
                c.shutdown(socket.SHUT_RDWR)
            c.close()
        self._clients.clear()

    def close(self):
        self._stop.set()
        self.drop_clients()
        self.srv.close()
        self._thread.join(2)


class TcpAddressTests(unittest.TestCase):
    def test_is_tcp_port(self):
        for p in ("tcp://192.168.1.50", "TCP://h:1", "socket://h:7330", " tcp://h"):
            self.assertTrue(host.is_tcp_port(p), p)
        for p in ("COM3", "/dev/ttyUSB0", "", None, "192.168.1.50"):
            self.assertFalse(host.is_tcp_port(p), p)

    def test_parse_default_and_explicit_port(self):
        self.assertEqual(host.parse_tcp_port("tcp://192.168.1.50"), ("192.168.1.50", 7330))
        self.assertEqual(host.parse_tcp_port("tcp://192.168.1.50:9000"), ("192.168.1.50", 9000))
        self.assertEqual(host.parse_tcp_port("socket://board.local"), ("board.local", 7330))

    def test_parse_rejects_malformed(self):
        for bad in ("tcp://", "tcp://:7330", "tcp://h:notaport", "tcp://h:99999", "tcp://h:0"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                host.parse_tcp_port(bad)


class TcpLinkTests(unittest.TestCase):
    def setUp(self):
        self.board = FakeBoard()
        self.addCleanup(self.board.close)
        self.link = host.Link(self.board.url, 115200, 120, 180)
        self.addCleanup(self.link.close)

    def types(self):
        return [m["type"] for m in self.board.received]

    def test_negotiate_and_push_v2_end_to_end(self):
        with patch.object(host, "find_port") as finder:
            self.assertTrue(self.link.connect())
            finder.assert_not_called()
        self.assertEqual(self.link.transport, "tcp")
        self.assertTrue(self.link.wait_handshake(3))
        self.assertEqual(self.link.negotiate(), "v2")
        pusher = host.V2Pusher(FakeTm(snapshot()), mouse=FakeMouse(None))
        self.assertEqual(host.push_v2(self.link, pusher, 0.0, NOW), 5)
        eventually_types = [t for t in self.types() if t in ("dash", "mouse", "quota", "models", "heat")]
        self.assertEqual(sorted(eventually_types), sorted(("dash", "mouse", "quota", "models", "heat")))
        self.assertEqual(self.link.err_count, 0)
        self.assertGreaterEqual(self.link.ok_count, 5)

    def test_wifi_commands_are_usb_only_over_tcp(self):
        self.assertTrue(self.link.connect())
        self.assertTrue(self.link.wait_ack(0, 3))
        res = self.link.command({"v": 2, "type": "wifi_status"}, 3)
        self.assertFalse(res.ok)
        self.assertEqual(res.err, "usb_only")
        res = self.link.command({"v": 2, "type": "ping"}, 3)
        self.assertTrue(res.ok)

    def test_reconnects_after_board_drops_the_client(self):
        self.assertTrue(self.link.connect())
        self.assertTrue(self.link.wait_handshake(3))
        self.board.drop_clients()
        eventually_ok = False
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if not self.link.connected:
                eventually_ok = True
                break
            time.sleep(0.02)
        self.assertTrue(eventually_ok, "reader did not notice the dropped client")
        self.assertTrue(self.link.connect())
        self.assertTrue(self.link.wait_handshake(3))
        self.assertEqual(self.link.negotiate(), "v2")
        self.assertEqual(self.board.connections, 2)

    def test_send_after_drop_reports_failure(self):
        self.assertTrue(self.link.connect())
        self.board.drop_clients()
        deadline = time.monotonic() + 3
        while self.link.connected and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(self.link.send({"v": 2, "type": "ping"}))

    def test_refused_connection_returns_false_with_tcp_wording(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()    # nobody listens here now
        link = host.Link(f"tcp://127.0.0.1:{port}", 115200)
        with self.assertLogs(host.log, level="WARNING") as logs:
            self.assertFalse(link.connect())
        self.assertIn("connect tcp", logs.output[0])
        self.assertFalse(link.connected)

    def test_malformed_tcp_address_does_not_raise(self):
        link = host.Link("tcp://", 115200)
        with self.assertLogs(host.log, level="WARNING"):
            self.assertFalse(link.connect())

    def test_close_leaves_no_reader(self):
        self.assertTrue(self.link.connect())
        self.link.close()
        self.assertIsNone(self.link._reader)
        self.assertFalse(self.link.connected)

    def test_log_lines_say_tcp(self):
        with self.assertLogs(host.log, level="INFO") as logs:
            self.link.connect()
        self.assertIn("tcp connection opened: 127.0.0.1:%d" % self.board.port, " ".join(logs.output))
        self.assertNotIn("serial port", " ".join(logs.output))


class TcpMainTests(unittest.TestCase):
    def test_once_over_tcp_via_port_flag(self):
        board = FakeBoard()
        self.addCleanup(board.close)
        tok = SimpleNamespace(get=lambda: dict(host.EMPTY_TOK), wait_first=lambda _t: True,
                              stop=lambda: None, start=lambda: None, source="x", cmd="x")
        with patch.object(host, "TokscaleSource", return_value=tok):
            self.assertEqual(host.main(["--once", "--port", board.url]), 0)
        self.assertEqual([m["type"] for m in board.received], ["ping", "stats"])

    def test_config_port_can_be_a_tcp_url(self):
        board = FakeBoard()
        self.addCleanup(board.close)
        cfg = dict(host.DEFAULT_CONFIG, port=board.url)
        tok = SimpleNamespace(get=lambda: dict(host.EMPTY_TOK), wait_first=lambda _t: True,
                              stop=lambda: None, start=lambda: None, source="x", cmd="x")
        with patch.object(host, "load_config", return_value=cfg), patch.object(host, "TokscaleSource", return_value=tok):
            self.assertEqual(host.main(["--once"]), 0)
        self.assertEqual(len(board.received), 2)

    def test_list_ports_mentions_tcp(self):
        out = io.StringIO()
        with patch.object(host, "list_ports", SimpleNamespace(comports=lambda: [])), contextlib.redirect_stdout(out):
            self.assertEqual(host.main(["--list-ports"]), 0)
        self.assertIn("tcp://", out.getvalue())

    def test_discover_prints_boards_and_exits(self):
        boards = [{"name": "epaper-sidescreen", "id": "a1b2c3", "ip": "192.168.1.50", "port": 7330,
                   "fw": "1.3.0", "busy": True}]
        out = io.StringIO()
        with patch.object(discovery, "discover", return_value=boards), contextlib.redirect_stdout(out):
            self.assertEqual(host.main(["--discover"]), 0)
        self.assertEqual(out.getvalue().split(), ["epaper-sidescreen", "a1b2c3", "192.168.1.50:7330", "1.3.0", "busy"])
        out = io.StringIO()
        with patch.object(discovery, "discover", return_value=[]), contextlib.redirect_stdout(out):
            self.assertEqual(host.main(["--discover"]), 1)
        self.assertIn("No boards", out.getvalue())


# ---------------------------------------------------------------------------
# WIFI 行和 Link.command（USB 串口的假板子）
# ---------------------------------------------------------------------------
class WifiSerial(FakeSerial):
    """USB 串口上的假板子：wifi_scan 先吐一行 WIFI scan 再 OK；wifi_set 只 OK；wifi_status 吐 status 再 OK。"""

    SCAN = {"type": "scan", "nets": [{"ssid": "Home-2.4G", "rssi": -52, "sec": True},
                                     {"ssid": "Cafe", "rssi": -70, "sec": False}]}
    STATUS = {"type": "status", "st": "connected", "ssid": "Home-2.4G", "ip": "192.168.1.50",
              "rssi": -52, "port": 7330, "id": "a1b2c3"}
    PAIR = {"type": "pair", "token": OTA_TOKEN, "id": "a1b2c3"}

    def __init__(self):
        super().__init__(respond=False)
        self.rx.put(READY_V2)
        self.mode = {}      # message type -> "silent" | "err:<reason>"

    def write(self, data):
        if self.fail_write:
            raise OSError("unplugged during write")
        msg = json.loads(data)
        self.writes.append(msg)
        kind = msg.get("type")
        how = self.mode.get(kind)
        if how == "silent":
            return len(data)
        if how and how.startswith("err:"):
            self.rx.put(f"ERR {how[4:]}\n".encode())
        elif kind == "wifi_scan":
            self.rx.put(b"WIFI " + json.dumps(self.SCAN).encode() + b"\nOK\n")
        elif kind == "wifi_status":
            self.rx.put(b"WIFI " + json.dumps(self.STATUS).encode() + b"\nOK\n")
        elif kind == "pair":
            self.rx.put(b"WIFI " + json.dumps(self.PAIR).encode() + b"\nOK\n")
        else:
            self.rx.put(b"OK\n")
        return len(data)


class WifiLinkCase(unittest.TestCase):
    def setUp(self):
        self.ser = WifiSerial()
        p = patch.object(host, "serial", SimpleNamespace(Serial=lambda: self.ser, SerialException=OSError))
        p.start()
        self.addCleanup(p.stop)
        self.link = host.Link("COM_TEST", 115200, 120, 180)
        self.addCleanup(self.link.close)
        self.assertTrue(self.link.connect())
        self.assertTrue(self.link.wait_ack(0, 2))    # the handshake ping's OK, so it can't pass for a later reply


class WifiLineTests(WifiLinkCase):
    def test_status_line_sets_wifi_status_and_logs_short_line(self):
        with self.assertLogs(host.log, level="INFO") as logs:
            self.link._line('WIFI {"type":"status","st":"connected","ssid":"Home-2.4G","ip":"192.168.1.50","rssi":-52}')
        self.assertEqual(self.link.wifi_status["st"], "connected")
        self.assertEqual(self.link.wifi_status["ip"], "192.168.1.50")
        self.assertIn("board wifi: connected Home-2.4G 192.168.1.50", logs.output[0])

    def test_status_without_ssid_is_still_logged(self):
        with self.assertLogs(host.log, level="INFO") as logs:
            self.link._line('WIFI {"type":"status","st":"off"}')
        self.assertTrue(logs.output[0].endswith("board wifi: off"))

    def test_scan_line_sets_wifi_scan(self):
        self.link._line('WIFI {"type":"scan","nets":[{"ssid":"A","rssi":-40,"sec":true},"junk",{"ssid":"B","rssi":-80,"sec":false}]}')
        self.assertEqual([n["ssid"] for n in self.link.wifi_scan], ["A", "B"])

    def test_bad_json_warns_without_crashing(self):
        for payload in ("WIFI {not json", "WIFI", "WIFI [1,2]", 'WIFI {"type":"mystery"}'):
            with self.subTest(payload=payload), self.assertLogs(host.log, level="WARNING"):
                self.link._line(payload)
        self.assertIsNone(self.link.wifi_status)
        self.assertIsNone(self.link.wifi_scan)

    def test_wifi_line_arriving_over_the_wire_and_listener(self):
        seen = []
        self.link.on_wifi = lambda kind, data: seen.append((kind, data["st"]))
        self.ser.rx.put(b'WIFI {"type":"status","st":"connecting","ssid":"X"}\n')
        eventually(lambda: seen)
        self.assertEqual(seen, [("status", "connecting")])

    def test_listener_exception_does_not_kill_reader(self):
        self.link.on_wifi = lambda kind, data: 1 / 0
        with self.assertLogs(host.log, level="WARNING"):
            self.link._line('WIFI {"type":"status","st":"off"}')
        self.assertTrue(self.link.connected)

    def test_wifi_line_does_not_count_as_an_ack(self):
        before = self.link.ack_count
        self.link._line('WIFI {"type":"status","st":"off"}')
        self.assertEqual(self.link.ack_count, before)

    def test_new_connection_forgets_wifi_status(self):
        self.link._line('WIFI {"type":"status","st":"off"}')
        self.assertIsNotNone(self.link.wifi_status)
        self.assertTrue(self.link.connect())
        self.assertIsNone(self.link.wifi_status)


FAULT_LINES = (b"DIAG epd fault busy_timeout stage=refresh waited_ms=45000 path=v2_old; refresh stopped until reboot\n"
               b"DIAG display stopped (driver fault, see DIAG epd fault); reboot to retry\n")
REFRESH_LINE = "DIAG epd refresh done path=v2_old busy_ms=22140 total_ms=23078"


class DiagLineTests(WifiLinkCase):
    def test_diag_kind(self):
        for line, kind in (("DIAG epd fault busy_timeout stage=refresh", "fault"),
                           ("DIAG display stopped (driver fault); reboot to retry", "fault"),
                           (REFRESH_LINE, "refresh"),
                           ("DIAG epd warn refresh_fast busy_ms=3", "warn"),
                           ("DIAG epd planes path=v2_old black_cmd=0x10", "info"),
                           ("DIAG settings cleared", "info")):
            with self.subTest(line=line):
                self.assertEqual(host.diag_kind(line), kind)

    def test_fault_sets_display_fault_logs_error_and_calls_listener(self):
        seen = []
        self.link.on_diag = lambda kind, line: seen.append(kind)
        line = FAULT_LINES.split(b"\n")[0].decode()
        with self.assertLogs(host.log, level="ERROR") as logs:
            self.link._line(line)
        self.assertEqual(self.link.display_fault, line)
        self.assertIn("press EN/reset", logs.output[0])
        self.assertEqual(seen, ["fault"])

    def test_refresh_line_is_info_not_unrecognized_and_not_an_ack(self):
        seen = []
        self.link.on_diag = lambda kind, line: seen.append((kind, line))
        before = self.link.ack_count
        with self.assertLogs(host.log, level="INFO") as logs:
            self.link._line(REFRESH_LINE)
        self.assertEqual([r.levelno for r in logs.records], [logging.INFO])
        self.assertNotIn("unrecognized", logs.output[0])
        self.assertEqual(seen, [("refresh", REFRESH_LINE)])
        self.assertIsNone(self.link.display_fault)
        self.assertEqual(self.link.ack_count, before)

    def test_listener_exception_does_not_kill_reader(self):
        self.link.on_diag = lambda kind, line: 1 / 0
        with self.assertLogs(host.log, level="WARNING"):
            self.link._line(REFRESH_LINE)
        self.assertTrue(self.link.connected)

    def test_new_connection_forgets_fault(self):
        self.link._line("DIAG display stopped (driver fault); reboot to retry")
        self.assertIsNotNone(self.link.display_fault)
        self.assertTrue(self.link.connect())
        self.assertIsNone(self.link.display_fault)


class TcpDiagTests(unittest.TestCase):
    def test_fault_replayed_after_ready_over_tcp(self):
        board = FakeBoard(ready=READY_V2 + FAULT_LINES)
        self.addCleanup(board.close)
        link = host.Link(board.url, 115200, 120, 180)
        self.addCleanup(link.close)
        with self.assertLogs(host.log, level="INFO"):
            self.assertTrue(link.connect())
            eventually(lambda: link.display_fault and "display stopped" in link.display_fault)
        self.assertEqual(link.negotiate(), "v2")    # the board still acks; only the panel stopped


class CommandTests(WifiLinkCase):
    def test_ok_with_scan_result(self):
        res = self.link.command({"v": 2, "type": "wifi_scan"}, 3)
        self.assertTrue(res.ok)
        self.assertEqual(res.err, "")
        self.assertEqual([n["ssid"] for n in res.wifi_scan], ["Home-2.4G", "Cafe"])
        self.assertIsNone(res.wifi_status)

    def test_ok_with_status_result(self):
        res = self.link.command({"v": 2, "type": "wifi_status"}, 3)
        self.assertTrue(res.ok)
        self.assertEqual(res.wifi_status["ip"], "192.168.1.50")
        self.assertIsNone(res.wifi_scan)

    def test_plain_ok_has_no_wifi_data(self):
        res = self.link.command({"v": 2, "type": "wifi_clear"}, 3)
        self.assertEqual((res.ok, res.err, res.wifi_scan, res.wifi_status), (True, "", None, None))
        self.assertEqual(res.as_dict(), {"ok": True, "err": "", "scan": None, "status": None, "pair": None})

    def test_err_reason(self):
        self.ser.mode["wifi_set"] = "err:bad_field ssid"
        res = self.link.command({"v": 2, "type": "wifi_set", "ssid": "", "pass": ""}, 3)
        self.assertFalse(res.ok)
        self.assertEqual(res.err, "bad_field ssid")

    def test_timeout(self):
        self.ser.mode["wifi_scan"] = "silent"
        t0 = time.monotonic()
        res = self.link.command({"v": 2, "type": "wifi_scan"}, 0.2)
        self.assertFalse(res.ok)
        self.assertEqual(res.err, "timeout")
        self.assertLess(time.monotonic() - t0, 2)

    def test_old_scan_is_not_reported_for_a_later_command(self):
        self.link.command({"v": 2, "type": "wifi_scan"}, 3)
        res = self.link.command({"v": 2, "type": "wifi_clear"}, 3)
        self.assertIsNone(res.wifi_scan)

    def test_disconnect_while_waiting(self):
        self.ser.mode["wifi_scan"] = "silent"
        threading.Timer(0.1, lambda: self.ser.rx.put(OSError("unplugged"))).start()
        t0 = time.monotonic()
        res = self.link.command({"v": 2, "type": "wifi_scan"}, 5)
        self.assertEqual((res.ok, res.err), (False, "disconnected"))
        self.assertLess(time.monotonic() - t0, 2)

    def test_send_failure(self):
        self.ser.fail_write = True
        res = self.link.command({"v": 2, "type": "wifi_scan"}, 1)
        self.assertEqual((res.ok, res.err), (False, "disconnected"))


class PasswordLeakTests(WifiLinkCase):
    def test_wifi_set_password_never_reaches_any_log(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setLevel(logging.DEBUG)
        root = logging.getLogger()
        old_level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.removeHandler, handler)
        self.addCleanup(root.setLevel, old_level)
        res = self.link.command({"v": 2, "type": "wifi_set", "ssid": "Home-2.4G", "pass": SECRET}, 3)
        self.assertTrue(res.ok)
        self.ser.mode["wifi_set"] = "err:bad_field pass"
        self.link.command({"v": 2, "type": "wifi_set", "ssid": "Home-2.4G", "pass": SECRET}, 3)
        self.link.send({"v": 2, "type": "wifi_set", "ssid": "x", "pass": SECRET})
        self.assertEqual(self.ser.writes[-1]["pass"], SECRET)     # it was really sent
        self.assertNotIn(SECRET, stream.getvalue())

    def test_too_long_message_error_does_not_echo_it(self):
        with self.assertRaises(ValueError) as cm:
            self.link.send({"v": 2, "type": "wifi_set", "ssid": "x", "pass": SECRET * 100})
        self.assertNotIn(SECRET, str(cm.exception))


# ---------------------------------------------------------------------------
# WiFi 升级固件：配对码、固件版本、Link.ota、命令行 --pair / --ota
# ---------------------------------------------------------------------------
def capture_all_logs(case):
    """把根 logger 的全部输出（DEBUG 起）收进一个 StringIO，测试结束自动摘掉。"""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.DEBUG)
    root = logging.getLogger()
    old = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    case.addCleanup(root.removeHandler, handler)
    case.addCleanup(root.setLevel, old)
    return stream


class PairLineTests(WifiLinkCase):
    def test_pair_command_returns_token_and_id_without_logging_the_token(self):
        logs = capture_all_logs(self)
        seen = []
        self.link.on_wifi = lambda kind, data: seen.append((kind, data))
        res = self.link.command({"v": 2, "type": "pair"}, 3)
        self.assertTrue(res.ok)
        self.assertEqual(res.pair, {"type": "pair", "token": OTA_TOKEN, "id": "a1b2c3"})
        self.assertEqual(res.as_dict()["pair"]["token"], OTA_TOKEN)
        self.assertEqual(self.link.pair_info["token"], OTA_TOKEN)
        self.assertEqual(seen, [("pair", res.pair)])
        self.assertIn("pairing code received (id a1b2c3)", logs.getvalue())
        self.assertNotIn(OTA_TOKEN, logs.getvalue())

    def test_pair_is_not_reported_for_a_later_command(self):
        self.link.command({"v": 2, "type": "pair"}, 3)
        self.assertIsNone(self.link.command({"v": 2, "type": "wifi_clear"}, 3).pair)

    def test_bad_pair_lines_warn_without_echoing_the_token(self):
        logs = capture_all_logs(self)
        for payload in ('WIFI {"type":"pair","id":"a1b2c3"}', 'WIFI {"type":"pair","token":""}',
                        'WIFI {"type":"pair","token":"' + OTA_TOKEN):
            with self.subTest(payload=payload), self.assertLogs(host.log, level="WARNING"):
                self.link._line(payload)
        self.assertIsNone(self.link.pair_info)
        self.assertNotIn(OTA_TOKEN, logs.getvalue())


class FwVersionTests(WifiLinkCase):
    def test_parse_fw_version(self):
        self.assertEqual(host.parse_fw_version("DIAG fw version=1.3.1 test_pattern=0"), "1.3.1")
        self.assertEqual(host.parse_fw_version("DIAG fw test_pattern=1 version=1.4.0-dev"), "1.4.0-dev")
        for line in ("DIAG fw", "DIAG fw version=", "DIAG epd refresh done", "DIAG firmware version=1", "OK"):
            self.assertIsNone(host.parse_fw_version(line), line)

    def test_line_sets_version_is_info_and_new_connection_forgets_it(self):
        with self.assertLogs(host.log, level="INFO") as logs:
            self.link._line(FW_LINE.decode().strip())
        self.assertEqual(self.link.fw_version, "1.3.1")
        self.assertEqual([r.levelno for r in logs.records], [logging.INFO])
        self.assertTrue(self.link.connect())
        self.assertIsNone(self.link.fw_version)


class FwVersionTcpTests(unittest.TestCase):
    def test_sent_after_ready_over_tcp(self):
        board = FakeBoard(ready=READY_V2 + FW_LINE)
        self.addCleanup(board.close)
        link = host.Link(board.url, 115200, 120, 180)
        self.addCleanup(link.close)
        self.assertTrue(link.connect())
        eventually(lambda: link.fw_version == "1.3.1", 3)

    def test_lines_sent_right_after_accept_are_never_lost(self):
        # pyserial 的 socket open() 会清空接收缓冲：板子连上就发的 READY / DIAG fw 曾经约 1/4 的概率丢掉
        board = FakeBoard(ready=READY_V2 + FW_LINE)
        self.addCleanup(board.close)
        link = host.Link(board.url, 115200, 120, 180)
        self.addCleanup(link.close)
        for _ in range(15):
            self.assertTrue(link.connect())
            eventually(lambda: link.fw_version == "1.3.1" and link.ready_proto == "v2", 3)


class FirmwareImageTests(unittest.TestCase):
    def test_check_image(self):
        self.assertEqual(host.check_firmware_image(fw_image()), "")
        self.assertEqual(host.check_firmware_image(fw_image(host.OTA_PARTITION_SIZE)), "")
        self.assertEqual(host.check_firmware_image(b""), "bad_image")
        self.assertEqual(host.check_firmware_image(fw_image(magic=0x7F)), "bad_image")
        self.assertEqual(host.check_firmware_image(fw_image(host.OTA_PARTITION_SIZE + 1)), "too_big")

    def test_read_firmware(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        good, bad = Path(d.name) / "firmware.bin", Path(d.name) / "notes.txt"
        good.write_bytes(fw_image())
        bad.write_bytes(b"hello")
        self.assertEqual(host.read_firmware(good), (fw_image(), ""))
        self.assertEqual(host.read_firmware(bad), (None, "bad_image"))
        with self.assertLogs(host.log, level="WARNING"):
            self.assertEqual(host.read_firmware(Path(d.name) / "missing.bin"), (None, "unreadable"))


class OtaTests(unittest.TestCase):
    def connect(self, **board_kw):
        self.board = FakeBoard(ready=READY_V2 + FW_LINE, **board_kw)
        self.addCleanup(self.board.close)
        self.link = host.Link(self.board.url, 115200, 120, 180)
        self.addCleanup(self.link.close)
        self.assertTrue(self.link.connect())
        self.assertTrue(self.link.wait_ack(0, 3))      # 握手 ping 的 OK
        return self.link

    def types(self):
        return [m["type"] for m in self.board.received]

    def test_success_delivers_the_image_intact_and_board_drops(self):
        link = self.connect()
        image, steps = fw_image(30000), []
        res = link.ota(image, OTA_TOKEN, lambda sent, total: steps.append((sent, total)))
        self.assertEqual((res.ok, res.err), (True, ""))
        self.assertEqual(self.board.image, image)
        begin = self.board.received[-1]
        self.assertEqual(begin, {"v": 2, "type": "ota_begin", "size": 30000,
                                 "md5": hashlib.md5(image).hexdigest(), "token": OTA_TOKEN})
        self.assertEqual(self.types(), ["ping", "ota_begin"])    # 传输期间没有别的行
        self.assertEqual(self.board.junk, 0)
        self.assertEqual(steps[-1], (30000, 30000))
        self.assertEqual([s for s, _ in steps], sorted(s for s, _ in steps))
        self.assertTrue(all(b - a <= host.OTA_CHUNK for (a, _), (b, _) in zip([(0, 0)] + steps, steps)))
        eventually_dropped = time.monotonic() + 3
        while link.connected and time.monotonic() < eventually_dropped:
            time.sleep(0.02)
        self.assertFalse(link.connected)

    def test_late_handshake_reply_is_not_taken_for_the_begin_reply(self):
        board = FakeBoard()
        self.addCleanup(board.close)
        link = host.Link(board.url, 115200, 120, 180)
        self.addCleanup(link.close)
        self.assertTrue(link.connect())
        self.assertTrue(link.wait_handshake(3))      # READY only; the ping's OK may still be on its way
        image = fw_image(40000)
        self.assertTrue(link.ota(image, OTA_TOKEN).ok)
        self.assertEqual(board.image, image)

    def test_wrong_token_sends_no_bytes_and_link_stays_usable(self):
        link = self.connect(token="another-token")
        res = link.ota(fw_image(), OTA_TOKEN)
        self.assertEqual((res.ok, res.err), (False, "ota_token"))
        self.assertIsNone(self.board.image)
        self.assertTrue(link.command({"v": 2, "type": "ping"}, 3).ok)

    def test_md5_error_returns_to_line_mode(self):
        link = self.connect(ota="md5")
        res = link.ota(fw_image(), OTA_TOKEN)
        self.assertEqual((res.ok, res.err), (False, "ota_md5"))
        self.assertTrue(link.connected)
        self.assertTrue(link.command({"v": 2, "type": "ping"}, 3).ok)

    def test_board_timeout_midway(self):
        link = self.connect(ota="stall")
        res = link.ota(fw_image(60000), OTA_TOKEN)
        self.assertEqual((res.ok, res.err), (False, "ota_timeout"))
        self.assertFalse(link.connected)     # 剩下的固件字节还在路上：重连，别让板子当成消息行

    def test_disconnect_midway(self):
        link = self.connect(ota="drop")
        res = link.ota(fw_image(200000), OTA_TOKEN)
        self.assertEqual((res.ok, res.err), (False, "disconnected"))
        self.assertFalse(link.connected)

    def test_silent_board_times_out_and_forces_reconnect(self):
        link = self.connect(ota="silent_end")
        res = link.ota(fw_image(), OTA_TOKEN, end_timeout=0.3)
        self.assertEqual((res.ok, res.err), (False, "timeout"))
        self.assertFalse(link.connected)       # 板子状态不明：断开重连
        link = self.connect(ota="silent_begin")
        res = link.ota(fw_image(), OTA_TOKEN, begin_timeout=0.3)
        self.assertEqual((res.ok, res.err), (False, "timeout"))
        self.assertFalse(link.connected)

    def test_local_checks_send_nothing(self):
        link = self.connect()
        for image, token, err in ((b"", OTA_TOKEN, "bad_image"), (fw_image(magic=0x00), OTA_TOKEN, "bad_image"),
                                  (fw_image(host.OTA_PARTITION_SIZE + 1), OTA_TOKEN, "too_big"),
                                  (fw_image(), "", "no_token"), (fw_image(), None, "no_token")):
            with self.subTest(err=err):
                res = link.ota(image, token)
                self.assertEqual((res.ok, res.err), (False, err))
        time.sleep(0.1)
        self.assertEqual(self.types(), ["ping"])

    def test_token_never_logged(self):
        logs = capture_all_logs(self)
        link = self.connect(ota="md5")
        link.ota(fw_image(), OTA_TOKEN)
        self.board.ota = "ok"
        self.assertTrue(link.ota(fw_image(), OTA_TOKEN).ok)
        self.assertIn("firmware upload", logs.getvalue())
        self.assertNotIn(OTA_TOKEN, logs.getvalue())


class OtaSerialTests(WifiLinkCase):
    def test_usb_serial_is_refused_locally(self):
        n = len(self.ser.writes)
        res = self.link.ota(fw_image(), OTA_TOKEN)
        self.assertEqual((res.ok, res.err), (False, "tcp_only"))
        self.assertEqual(len(self.ser.writes), n)


class OtaCliTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.dir = Path(d.name)
        self.cfg = self.dir / "config.json"
        self.bin = self.dir / "firmware.bin"
        self.bin.write_bytes(fw_image(25000))
        self.logs = capture_all_logs(self)

    def run_main(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = host.main([*argv, "--config", str(self.cfg)])
        return code, out.getvalue()

    def read_cfg(self):
        return json.loads(self.cfg.read_text(encoding="utf-8"))

    def test_pair_saves_token_keeps_other_keys_and_prints_id_only(self):
        self.cfg.write_text(json.dumps({"_comment": "说明", "port": "COM9", "v2": {"mouse": False}}, ensure_ascii=False),
                            encoding="utf-8")
        ser = WifiSerial()
        with patch.object(host, "serial", SimpleNamespace(Serial=lambda: ser, SerialException=OSError)):
            code, out = self.run_main("--pair", "--port", "COM_TEST")
        self.assertEqual(code, 0)
        self.assertEqual(self.read_cfg(), {"_comment": "说明", "port": "COM9", "v2": {"mouse": False},
                                           "ota_token": OTA_TOKEN})
        self.assertIn("a1b2c3", out)
        self.assertNotIn(OTA_TOKEN, out + self.logs.getvalue())
        self.assertEqual([m["type"] for m in ser.writes], ["ping", "pair"])

    def test_pair_failures(self):
        self.cfg.write_text("{}", encoding="utf-8")
        code, _ = self.run_main("--pair", "--port", "tcp://127.0.0.1:9")
        self.assertEqual(code, 1)
        ser = WifiSerial()
        ser.mode["pair"] = "err:unknown_type"
        with patch.object(host, "serial", SimpleNamespace(Serial=lambda: ser, SerialException=OSError)):
            code, _ = self.run_main("--pair", "--port", "COM_TEST")
        self.assertEqual(code, 2)
        self.assertEqual(self.read_cfg(), {})

    def test_ota_via_port_flag(self):
        board = FakeBoard()
        self.addCleanup(board.close)
        self.cfg.write_text(json.dumps({"ota_token": OTA_TOKEN}), encoding="utf-8")
        code, out = self.run_main("--ota", str(self.bin), "--port", board.url)
        self.assertEqual(code, 0)
        self.assertEqual(board.image, fw_image(25000))
        self.assertIn("upload 100%", out)
        self.assertNotIn(OTA_TOKEN, out + self.logs.getvalue())

    def test_ota_uses_wifi_addr_from_config(self):
        board = FakeBoard()
        self.addCleanup(board.close)
        self.cfg.write_text(json.dumps({"ota_token": OTA_TOKEN, "wifi_addr": f"127.0.0.1:{board.port}"}),
                            encoding="utf-8")
        code, _ = self.run_main("--ota", str(self.bin))
        self.assertEqual(code, 0)
        self.assertEqual(board.image, fw_image(25000))

    def test_ota_refusals(self):
        board = FakeBoard(token="another-token")
        self.addCleanup(board.close)
        self.cfg.write_text(json.dumps({"wifi_addr": f"127.0.0.1:{board.port}"}), encoding="utf-8")
        self.assertEqual(self.run_main("--ota", str(self.bin))[0], 1)                  # 没配对码
        self.cfg.write_text(json.dumps({"ota_token": OTA_TOKEN}), encoding="utf-8")
        self.assertEqual(self.run_main("--ota", str(self.bin), "--port", "COM_TEST")[0], 1)   # 不是 WiFi
        junk = self.dir / "notes.txt"
        junk.write_bytes(b"not firmware")
        self.assertEqual(self.run_main("--ota", str(junk), "--port", board.url)[0], 1)
        self.assertEqual(board.connections, 0)
        self.assertEqual(self.run_main("--ota", str(self.bin), "--port", board.url)[0], 2)   # 配对码不对
        self.assertIsNone(board.image)


# ---------------------------------------------------------------------------
# UDP 发现
# ---------------------------------------------------------------------------
class FakeResponder:
    """UDP 7331 的替身：收到 discover 就把 `replies` 里的每个报文单播回去。"""

    def __init__(self, replies):
        self.replies = replies
        self.probes = []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.05)
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(1024)
            except socket.timeout:
                continue
            except OSError:
                return
            self.probes.append(data)
            if json.loads(data) == {"v": 2, "type": "discover"}:
                for reply in self.replies:
                    self.sock.sendto(reply if isinstance(reply, bytes) else json.dumps(reply).encode(), addr)

    def close(self):
        self._stop.set()
        self._thread.join(2)
        self.sock.close()

    @property
    def targets(self):
        return [("127.0.0.1", self.port)]


def here(id_, ip="192.168.1.50", **extra):
    return {"v": 2, "type": "here", "name": "epaper-sidescreen", "id": id_, "ip": ip, "port": 7330,
            "fw": "1.3.0", "busy": False, **extra}


class DiscoveryTests(unittest.TestCase):
    def run_discover(self, replies, timeout=0.4):
        responder = FakeResponder(replies)
        self.addCleanup(responder.close)
        self.responder = responder
        return discovery.discover(timeout=timeout, port=responder.port, targets=responder.targets)

    def test_finds_a_board(self):
        boards = self.run_discover([here("a1b2c3", busy=True)])
        self.assertEqual(boards, [{"name": "epaper-sidescreen", "id": "a1b2c3", "ip": "192.168.1.50",
                                   "port": 7330, "fw": "1.3.0", "busy": True}])
        self.assertEqual(json.loads(self.responder.probes[0]), {"v": 2, "type": "discover"})

    def test_dedupes_by_id_even_with_resend_and_different_source(self):
        boards = self.run_discover([here("a1b2c3"), here("a1b2c3", ip="192.168.1.51"), here("ffeedd", ip="192.168.1.60")])
        self.assertEqual([b["id"] for b in boards], ["a1b2c3", "ffeedd"])
        self.assertEqual(boards[0]["ip"], "192.168.1.50")    # first reply wins

    def test_falls_back_to_ip_when_id_missing(self):
        boards = self.run_discover([here("", ip="192.168.1.70"), here("", ip="192.168.1.70"), here("", ip="192.168.1.71")])
        self.assertEqual([b["ip"] for b in boards], ["192.168.1.70", "192.168.1.71"])

    def test_ignores_junk(self):
        boards = self.run_discover([
            b"hello", b"\xff\xfe", b"[1,2]", b'{"type":"discover"}', b'{"v":2,"type":"here"',
            {"type": "here", "id": 5, "ip": "not an ip", "port": "x", "fw": 3, "busy": "yes", "name": ""},
            here("a1b2c3"),
        ])
        by_id = {b["id"]: b for b in boards}
        self.assertIn("a1b2c3", by_id)
        junk = by_id[""]     # the odd one is kept but sanitized
        self.assertEqual((junk["ip"], junk["port"], junk["fw"], junk["busy"], junk["name"]),
                         ("127.0.0.1", 7330, "", False, "epaper-sidescreen"))
        self.assertEqual(len(boards), 2)

    def test_no_reply_returns_empty_after_about_the_timeout(self):
        t0 = time.monotonic()
        boards = self.run_discover([], timeout=0.3)
        elapsed = time.monotonic() - t0
        self.assertEqual(boards, [])
        self.assertGreaterEqual(elapsed, 0.25)
        self.assertLess(elapsed, 1.5)

    def test_probe_is_sent_twice_for_lossy_wifi(self):
        self.run_discover([], timeout=0.5)
        self.assertGreaterEqual(len(self.responder.probes), 2)

    def test_nobody_listening_is_not_an_error(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        self.assertEqual(discovery.discover(timeout=0.2, targets=[("127.0.0.1", port)]), [])

    def test_sorted_by_name_then_id(self):
        boards = self.run_discover([here("bbbbbb", name="zeta"), here("aaaaaa", name="alpha"), here("cccccc", name="alpha")])
        self.assertEqual([b["id"] for b in boards], ["aaaaaa", "cccccc", "bbbbbb"])


class BroadcastTargetTests(unittest.TestCase):
    def test_interface_directed_broadcasts(self):
        def addr(ip, mask):
            return SimpleNamespace(family=socket.AF_INET, address=ip, netmask=mask)
        fake = {
            "Wi-Fi": [addr("192.168.1.20", "255.255.255.0"), SimpleNamespace(family=socket.AF_INET6, address="fe80::1", netmask=None)],
            "Loopback": [addr("127.0.0.1", "255.0.0.0")],
            "VPN": [addr("10.8.0.2", "255.255.255.255")],
            "Eth": [addr("172.16.5.9", "255.255.0.0"), addr("192.168.1.30", "255.255.255.0")],
        }
        with patch("psutil.net_if_addrs", return_value=fake):
            targets = discovery.broadcast_targets(7331)
        self.assertEqual(targets, [("255.255.255.255", 7331), ("192.168.1.255", 7331), ("172.16.255.255", 7331)])

    def test_without_psutil_only_global_broadcast(self):
        with patch.dict(sys.modules, {"psutil": None}):
            self.assertEqual(discovery.broadcast_targets(7331), [("255.255.255.255", 7331)])


if __name__ == "__main__":
    unittest.main()
