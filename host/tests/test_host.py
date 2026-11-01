"""Software-only serial lifecycle/protocol tests; never opens a real device."""
import contextlib
import io
import json
import queue
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import epaper_host as host


class FakeSerial:
    def __init__(self, respond=True):
        self.is_open = False
        self.respond = respond
        self.rx = queue.Queue()
        self.writes = []
        self.closed = 0
        self.fail_write = False

    def open(self):
        self.is_open = True

    def write(self, data):
        if self.fail_write:
            raise OSError("unplugged during write")
        self.writes.append(json.loads(data))
        if self.respond:
            self.rx.put(b"OK\n")
        return len(data)

    def read(self, size):
        try:
            result = self.rx.get(timeout=0.02)
        except queue.Empty:
            return b""
        if isinstance(result, Exception):
            raise result
        return result

    def cancel_read(self):
        self.rx.put(b"")

    def close(self):
        self.is_open = False
        self.closed += 1


def eventually(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("reader did not reach expected state")
        time.sleep(0.005)


class LinkTests(unittest.TestCase):
    def setUp(self):
        self.serials = []
        self.respond = True

        def factory():
            s = FakeSerial(self.respond)
            self.serials.append(s)
            return s

        self.patch = patch.object(host, "serial", SimpleNamespace(Serial=factory, SerialException=OSError))
        self.patch.start()
        self.link = host.Link("COM_TEST", 115200, 120, 180)

    def tearDown(self):
        self.link.close()
        self.patch.stop()
        self.assertIsNone(self.link._reader)
        self.assertTrue(all(not s.is_open for s in self.serials))

    def test_long_refresh_keeps_receiving_and_sending(self):
        self.assertTrue(self.link.connect())
        self.assertTrue(self.link.wait_response(1, after_ok=0))
        self.serials[0].rx.put(b"DISPLAY START\n")
        # Simulate 20 samples over >3 minutes while the display may be busy.
        for i in range(20):
            before = self.link.ok_count
            self.assertTrue(self.link.send({"v": 1, "type": "stats", "cpu": i}))
            self.assertTrue(self.link.wait_response(1, after_ok=before))
            with self.link._state:
                self.link.last_response = self.link.opened_at + i * 10
                self.link._next_warning = self.link.last_response + 180
            with self.assertNoLogs(host.log, level="WARNING"):
                self.link.diagnose(self.link.opened_at + i * 10 + 10)
        self.assertEqual(len(self.serials), 1)
        self.assertEqual(len(self.serials[0].writes), 21)
        self.assertFalse(self.serials[0].dtr)
        self.assertFalse(self.serials[0].rts)
        with self.assertLogs(host.log, level="INFO") as logs:
            self.link._line("OK")
            self.link._line("DISPLAY DONE")
        self.assertIn("not display completion", " ".join(logs.output))
        self.assertIn("not visually verified", " ".join(logs.output))

    def test_silence_is_bounded_diagnostic_without_reset(self):
        self.respond = False
        self.assertTrue(self.link.connect())
        self.assertFalse(self.link.wait_response(0.03, after_ok=0))
        start = self.link.opened_at
        with self.assertNoLogs(host.log, level="WARNING"):
            self.link.diagnose(start + 119)
        with self.assertLogs(host.log, level="WARNING") as logs:
            self.link.diagnose(start + 120)
        self.assertIn("handshake deadline", logs.output[0])
        with self.assertNoLogs(host.log, level="WARNING"):
            self.link.diagnose(start + 121)
        self.assertTrue(self.link.send({"v": 1, "type": "stats"}))
        self.assertTrue(self.link.connected)
        self.assertEqual(self.serials[0].closed, 0)
        self.link._line("READY")
        with self.assertLogs(host.log, level="WARNING") as logs:
            self.link.diagnose(self.link.last_response + 181)
        self.assertIn("protocol response timeout", logs.output[0])

    def test_read_disconnect_reconnect_and_no_reader_leaks(self):
        self.assertTrue(self.link.connect())
        for _ in range(12):
            old, reader = self.serials[-1], self.link._reader
            old.rx.put(OSError("USB unplugged"))
            eventually(lambda: not self.link.connected)
            self.assertTrue(self.link.connect())
            self.assertFalse(reader.is_alive())
            self.assertFalse(old.is_open)
            self.assertEqual(self.serials[-1].port, "COM_TEST")
        self.assertTrue(self.link.wait_response(1, after_ok=0))

    def test_write_failure_and_failed_open_close_resources(self):
        self.link.connect()
        self.serials[0].fail_write = True
        self.assertFalse(self.link.send({"v": 1, "type": "stats"}))
        self.assertFalse(self.link.connected)
        self.assertTrue(self.link.connect())
        self.link.close()
        s = FakeSerial()
        s.open = Mock(side_effect=OSError("port busy"))
        with patch.object(host.serial, "Serial", return_value=s):
            self.assertFalse(self.link.connect())
        self.assertEqual(s.closed, 1)

    def test_auto_reconnect_reenumerates_changed_com_number(self):
        self.link.fixed_port = None
        ports = [SimpleNamespace(device="COM_A", vid=0x1A86,
                                 description="USB serial", manufacturer="USB")]
        with patch.object(host, "list_ports", SimpleNamespace(comports=lambda: ports)):
            self.assertTrue(self.link.connect())
            old, reader = self.serials[-1], self.link._reader
            self.assertEqual(old.port, "COM_A")
            old.rx.put(OSError("USB unplugged"))
            eventually(lambda: not self.link.connected)
            ports[0].device = "COM_B"
            self.assertTrue(self.link.connect())
        self.assertIsNone(self.link.fixed_port)
        self.assertEqual([s.port for s in self.serials], ["COM_A", "COM_B"])
        self.assertFalse(old.is_open)
        self.assertFalse(reader.is_alive())
        self.assertTrue(self.link.wait_response(1, after_ok=0))

    def test_explicit_port_does_not_follow_auto_candidate(self):
        with patch.object(host, "find_port", return_value="COM_B") as discover:
            self.assertTrue(self.link.connect())
            self.link.close()
            self.assertTrue(self.link.connect())
        discover.assert_not_called()
        self.assertEqual([s.port for s in self.serials], ["COM_TEST", "COM_TEST"])

    def test_serial_write_timeout_marks_failed_and_recovers(self):
        from serial import SerialException, SerialTimeoutException

        self.assertTrue(self.link.connect())
        old, reader = self.serials[-1], self.link._reader
        with patch.object(host.serial, "SerialException", SerialException), patch.object(
            old, "write", side_effect=SerialTimeoutException("write timeout during refresh")
        ), self.assertLogs(host.log, level="WARNING") as logs:
            self.assertFalse(self.link.send({"v": 1, "type": "stats"}))
        self.assertIn("reconnect scheduled", " ".join(logs.output))
        self.assertFalse(self.link.connected)
        self.assertTrue(self.link.connect())
        self.assertFalse(old.is_open)
        self.assertFalse(reader.is_alive())
        self.assertTrue(self.link.wait_response(1, after_ok=0))

    def test_protocol_error_noise_and_bounded_rx_recovery(self):
        self.respond = False
        self.link.connect()
        with self.assertLogs(host.log, level="WARNING") as logs:
            self.serials[0].rx.put(b"x" * 5000 + b"\nERR bad_json\nOKAY\n\xff\nOK\n")
            eventually(lambda: self.link.ok_count == 1)
        self.assertEqual(self.link.err_count, 1)
        self.assertIn("overlong", " ".join(logs.output))
        self.assertIn("protocol error", " ".join(logs.output))
        self.assertIn("unrecognized", " ".join(logs.output))

    def test_ambiguous_discovery_never_opens_devices(self):
        def port(device, vid):
            return SimpleNamespace(device=device, vid=vid, description="CH343", manufacturer="USB")
        with patch.object(host, "list_ports", SimpleNamespace(comports=lambda: [port("COM_A", 0x303A), port("COM_B", 0x1A86)])):
            self.assertIsNone(host.find_port())
            self.link.fixed_port = None
            self.assertFalse(self.link.connect())
            self.assertEqual(self.serials, [])

    def test_reader_that_has_not_stopped_blocks_new_connection(self):
        stuck = Mock()
        stuck.is_alive.return_value = True
        self.link._reader = stuck
        self.assertFalse(self.link.connect())
        self.assertEqual(self.serials, [])
        stuck.join.assert_called_once_with(timeout=3)
        stuck.is_alive.return_value = False

    def test_error_received_before_wait_is_not_success(self):
        self.respond = False
        self.link.connect()
        self.link._line("ERR bad_version")
        self.link._line("OK")
        self.assertFalse(self.link.wait_response(0.1, after_ok=0))


class DefaultIntervalTests(unittest.TestCase):
    def test_default_send_interval_is_30s_and_example_matches(self):
        self.assertEqual(host.DEFAULT_CONFIG["interval"], 30)
        example = json.loads((Path(__file__).resolve().parents[1] / "config.example.json").read_text("utf-8"))
        self.assertEqual(example["interval"], 30)


class MainTests(unittest.TestCase):
    def token(self):
        tok = Mock()
        tok.get.return_value = dict(host.EMPTY_TOK)
        return tok

    def test_original_dry_once_real_system_format_no_serial(self):
        tok = self.token()
        output = io.StringIO()
        with patch.object(host, "TokscaleSource", return_value=tok), patch.object(host, "Link") as link, contextlib.redirect_stdout(output):
            self.assertEqual(host.main(["--dry-run", "--once"]), 0)
        msg = json.loads(output.getvalue())
        self.assertEqual((msg["v"], msg["type"]), (1, "stats"))
        for name in ("cpu", "mem", "disk", "net_up", "net_down", "time", "tok"):
            self.assertIn(name, msg)
        self.assertLessEqual(len(output.getvalue().encode()), host.MAX_LINE)
        link.assert_not_called()
        tok.stop.assert_called_once()

    def test_once_open_but_no_response_exits_two(self):
        cfg = dict(host.DEFAULT_CONFIG, handshake_timeout=0.03, response_timeout=0.03)
        s = FakeSerial(respond=False)
        with patch.object(host, "serial", SimpleNamespace(Serial=lambda: s, SerialException=OSError)), patch.object(host, "load_config", return_value=cfg), patch.object(host, "TokscaleSource", return_value=self.token()):
            self.assertEqual(host.main(["--once", "--port", "COM_TEST"]), 2)
        self.assertFalse(s.is_open)

    def test_once_ping_then_stats_ok(self):
        s = FakeSerial()
        with patch.object(host, "serial", SimpleNamespace(Serial=lambda: s, SerialException=OSError)), patch.object(host, "TokscaleSource", return_value=self.token()):
            self.assertEqual(host.main(["--once", "--port", "COM_TEST", "--baud", "9600"]), 0)
        self.assertEqual([m["type"] for m in s.writes], ["ping", "stats"])
        self.assertEqual(s.baudrate, 9600)
        self.assertFalse(s.is_open)

    def test_once_interrupted_during_token_wait_exits_130(self):
        for args in (["--dry-run", "--once"], ["--once", "--port", "COM_TEST"]):
            with self.subTest(args=args):
                tok = self.token()
                tok.wait_first.side_effect = KeyboardInterrupt
                with patch.object(host, "TokscaleSource", return_value=tok), patch.object(host, "Link") as link:
                    self.assertEqual(host.main(args), 130)
                tok.stop.assert_called_once()
                link.assert_not_called()

    def test_once_interrupted_during_ping_or_stats_wait_exits_130(self):
        for responses, expected_types in (([KeyboardInterrupt], ["ping"]),
                                          ([True, KeyboardInterrupt], ["ping", "stats"])):
            with self.subTest(expected_types=expected_types):
                s, tok = FakeSerial(respond=False), self.token()
                with patch.object(host, "serial", SimpleNamespace(Serial=lambda: s, SerialException=OSError)), patch.object(
                    host, "TokscaleSource", return_value=tok
                ), patch.object(host.Link, "wait_response", side_effect=responses), patch.object(host.time, "sleep"):
                    self.assertEqual(host.main(["--once", "--port", "COM_TEST"]), 130)
                self.assertEqual([m["type"] for m in s.writes], expected_types)
                tok.stop.assert_called_once()
                self.assertFalse(s.is_open)
                self.assertFalse(any(t.name == "serial-rx" for t in threading.enumerate()))

    def test_continuous_sampling_proceeds_during_silence(self):
        s = FakeSerial(respond=False)
        snapshots = []

        def sleep(_):
            snapshots.append(len(s.writes))
            if len(snapshots) == 20:
                raise KeyboardInterrupt

        with patch.object(host, "serial", SimpleNamespace(Serial=lambda: s, SerialException=OSError)), patch.object(host, "TokscaleSource", return_value=self.token()), patch.object(host.time, "sleep", side_effect=sleep):
            self.assertEqual(host.main(["--port", "COM_TEST"]), 0)
        self.assertEqual(len([m for m in s.writes if m["type"] == "stats"]), 20)
        self.assertFalse(s.is_open)
        self.assertFalse(any(t.name == "serial-rx" for t in threading.enumerate()))

    def test_old_config_gets_new_defaults_without_writing(self):
        config = Path(__file__).resolve().parents[1] / "config.example.json"
        with patch("builtins.open", unittest.mock.mock_open(read_data='{"port":"COM_OLD","interval":12}')) as opened:
            cfg = host.load_config(config)
        self.assertEqual(cfg["port"], "COM_OLD")
        self.assertEqual(cfg["interval"], 12)
        self.assertEqual(cfg["handshake_timeout"], 120)
        self.assertEqual(cfg["response_timeout"], 180)
        self.assertEqual(opened.call_args.kwargs, {"encoding": "utf-8-sig"})

    def test_invalid_diagnostic_timeout_rejected_before_serial(self):
        cfg = dict(host.DEFAULT_CONFIG, handshake_timeout=float("inf"))
        with patch.object(host, "load_config", return_value=cfg), patch.object(host, "Link") as link:
            self.assertEqual(host.main(["--dry-run", "--once"]), 1)
        link.assert_not_called()


if __name__ == "__main__":
    unittest.main()
