"""worker.py (V2Session / HostWorker) and the host-logic fixes: command pacing (H1), config encoding and atomic
saves (H2/H10), reboot detection (H5), loop robustness (H6), TODO upload and daily re-push (H7/H9), ERR'd blocks
resent (H8), config-reload logging (H11), reply age (H13). Fake serial / sockets only; config.json is never touched."""
import json
import logging
import os
import queue
import socket
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import epaper_host as host
import todo_page as tp
import weather
import worker
from test_dashboard_v2 import NOW, FakeMouse, FakeTm, V2Serial, reading, snapshot
from test_gui import drain_until, wait_for
from test_host import FakeSerial

READY_V2 = b"READY epaper-sidescreen v2 GDEY042T81 400x300\n"
T0 = datetime(2026, 10, 28, 13, 47)


def serial_ns(ser):
    return SimpleNamespace(Serial=lambda: ser, SerialException=OSError)


def new_link(test, ser):
    p = patch.object(host, "serial", serial_ns(ser))
    p.start()
    test.addCleanup(p.stop)
    link = host.Link("COM_TEST", 115200, 120, 180)
    test.addCleanup(link.close)
    return link


class DelayedSerial(FakeSerial):
    """Answers each written line after delays[i] seconds (a missing / None entry = answered at once, 'drop' = never)."""

    def __init__(self, delays=None, reply=b"OK\n", ready=None):
        super().__init__(respond=False)
        self.delays = list(delays or [])
        self.reply = reply
        if ready:
            self.rx.put(ready)

    def write(self, data):
        msg = json.loads(data)
        self.writes.append(msg)
        n = len(self.writes) - 1
        delay = self.delays[n] if n < len(self.delays) else None
        if delay == "drop":
            return len(data)
        reply = self.reply(msg) if callable(self.reply) else self.reply
        if delay:
            threading.Timer(delay, self.rx.put, args=(reply,)).start()
        else:
            self.rx.put(reply)
        return len(data)


# ---------------------------------------------------------------------------
# H1 / H13 / C5: Link
# ---------------------------------------------------------------------------
class CommandPacingTests(unittest.TestCase):
    def test_late_reply_to_an_earlier_line_is_not_taken_for_the_commands_reply(self):
        ser = DelayedSerial(delays=[0.3], reply=lambda m: b"OK\n" if m["type"] == "ping" else b"ERR busy\n")
        link = new_link(self, ser)
        link.connect()                                    # the ping's OK arrives 0.3 s from now
        res = link.command({"v": 2, "type": "page", "p": "dash"}, 2.0)
        self.assertEqual((res.ok, res.err), (False, "busy"))     # its own ERR, not the ping's late OK

    def test_unanswered_earlier_line_costs_at_most_the_idle_wait(self):
        ser = DelayedSerial(delays=["drop"])              # the ping is never answered
        link = new_link(self, ser)
        link.connect()
        t = time.monotonic()
        with patch.object(host, "V2_ACK_WAIT", 0.2):
            res = link.command({"v": 2, "type": "ping"}, 1.0)
        self.assertTrue(res.ok)
        self.assertLess(time.monotonic() - t, 1.5)

    def test_reply_age_and_silent(self):
        ser = V2Serial(ready=READY_V2)
        link = new_link(self, ser)
        self.assertIsNone(link.reply_age())                      # not connected
        link.connect()
        self.assertTrue(link.wait_handshake(1))
        self.assertLess(link.reply_age(), 1.0)
        self.assertFalse(link.silent)
        self.assertGreater(link.reply_age(link.last_response + 100), host.SILENT_AFTER)
        link.last_response = time.monotonic() - 100
        self.assertTrue(link.silent)

    def test_ready_count_counts_reboots(self):
        ser = V2Serial(ready=READY_V2)
        link = new_link(self, ser)
        link.connect()
        self.assertTrue(link.wait_handshake(1))
        self.assertEqual(link.ready_count, 1)
        link.display_fault = "DIAG epd fault x"
        ser.rx.put(READY_V2)
        self.assertTrue(wait_for(lambda: link.ready_count == 2, 2))
        self.assertIsNone(link.display_fault)                    # a reboot clears the fault
        link.connect()
        self.assertEqual(link.ready_count, 0)


class OtaFinalReplyTests(unittest.TestCase):
    def test_an_ok_before_the_whole_image_went_out_is_not_success(self):
        class EarlyOk(FakeSerial):
            """OK for every line, and another OK as soon as the first image bytes arrive (before the last one)."""

            def write(self, data):
                try:
                    json.loads(data)
                except ValueError:
                    if not getattr(self, "early", False):
                        self.early = True
                        self.rx.put(b"OK\n")
                    return len(data)
                self.rx.put(b"OK\n")
                return len(data)
        link = new_link(self, EarlyOk())
        link.connect()
        self.assertTrue(link.wait_handshake(1))
        link.transport = "tcp"               # ota() is TCP-only; the fake serial stands in for the socket
        res = link.ota(bytes([0xE9]) + bytes(100000), "tok", begin_timeout=2, end_timeout=0.5)
        self.assertFalse(res.ok)
        self.assertIn(res.err, ("ota_incomplete", "timeout"))


class TcpReadTests(unittest.TestCase):
    def test_a_burst_of_lines_arrives_whole(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        self.addCleanup(srv.close)

        def serve():
            conn, _ = srv.accept()
            conn.sendall(READY_V2 + b"".join(b"OK\n" for _ in range(50)) + b"DIAG fw version=1.7.0 x=1\n")
            time.sleep(1.0)
            conn.close()
        threading.Thread(target=serve, daemon=True).start()
        link = host.Link(f"tcp://127.0.0.1:{srv.getsockname()[1]}", 115200)
        self.addCleanup(link.close)
        link.connect()
        self.assertTrue(wait_for(lambda: link.fw_version == "1.7.0", 3))
        self.assertGreaterEqual(link.ok_count, 50)


# ---------------------------------------------------------------------------
# H2 / H10: config
# ---------------------------------------------------------------------------
class ConfigTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.dir = Path(d.name)
        self.path = self.dir / "config.json"

    def test_bom_is_tolerated_on_load_and_on_save(self):
        self.path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"port": "COM9", "interval": 12}).encode())
        cfg = host.load_config(self.path)
        self.assertEqual((cfg["port"], cfg["interval"]), ("COM9", 12))
        host.save_config(self.path, {"wifi_addr": "10.0.0.2"})
        raw = self.path.read_bytes()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))        # written as plain UTF-8
        self.assertEqual(json.loads(raw), {"port": "COM9", "interval": 12, "wifi_addr": "10.0.0.2"})

    def test_replace_is_retried_on_permission_error(self):
        real = os.replace
        calls = []

        def flaky(a, b):
            calls.append(1)
            if len(calls) < 3:
                raise PermissionError("locked")
            return real(a, b)
        with patch.object(host.os, "replace", flaky), patch.object(host.time, "sleep"):
            host.save_config(self.path, {"interval": 5})
        self.assertEqual(len(calls), 3)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"interval": 5})
        self.assertEqual(list(self.dir.iterdir()), [self.path])           # no temp file left

    def test_persistent_lock_gives_an_error_result_and_leaves_the_old_file(self):
        self.path.write_text('{"interval": 1}', encoding="utf-8")
        with patch.object(host.os, "replace", side_effect=PermissionError("locked")), \
                patch.object(host.time, "sleep"), self.assertLogs("epaper_host", "ERROR"):
            err = host.save_config_checked(self.path, {"interval": 5})
        self.assertIn("PermissionError", err)
        self.assertEqual(self.path.read_text(encoding="utf-8"), '{"interval": 1}')
        self.assertEqual(list(self.dir.iterdir()), [self.path])
        self.assertEqual(host.save_config_checked(self.path, {"interval": 6}), "")
        self.assertEqual(host.load_config(self.path)["interval"], 6)

    def test_file_is_fsynced_and_temp_names_are_unique(self):
        names, synced = [], []
        real_replace, real_fsync = os.replace, os.fsync

        def replace(a, b):
            names.append(Path(a).name)
            return real_replace(a, b)
        with patch.object(host.os, "replace", replace), \
                patch.object(host.os, "fsync", lambda fd: synced.append(fd) or real_fsync(fd)):
            host.save_config(self.path, {"a": 1})
            host.save_config(self.path, {"a": 2})
        self.assertEqual(len(synced), 2)
        self.assertEqual(len(set(names)), 2)
        self.assertTrue(all(n.startswith("config.json.") and n.endswith(".tmp") for n in names))

    def test_concurrent_saves_never_corrupt_the_file(self):
        errors = []

        def spam(key):
            for i in range(20):
                try:
                    host.save_config(self.path, {key: i})
                except Exception as e:  # noqa: BLE001
                    errors.append(e)
        threads = [threading.Thread(target=spam, args=(f"k{n}",)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual([e for e in errors if not isinstance(e, PermissionError)], [])


class OptionTests(unittest.TestCase):
    def test_interval_is_clamped_to_the_keepalive_window(self):
        self.assertEqual(host.clamp_interval(500), 60.0)
        self.assertEqual(host.clamp_interval(0.1), 1.0)
        self.assertEqual(host.clamp_interval("30"), 30.0)

    def test_reconnect_pacing_uses_the_shorter_of_interval_and_retry(self):
        self.assertEqual(host.loop_wait(True, 30, 10), 30)
        self.assertEqual(host.loop_wait(False, 30, 10), 10)
        self.assertEqual(host.loop_wait(False, 5, 10), 5)
        self.assertEqual(host.loop_wait(True, 30, 10, elapsed=40), 0.0)

    def test_bad_v2_values_are_logged_once(self):
        host._coerced_logged.clear()
        cfg = {"v2": {"primary": "bogus", "heat_weeks": 99, "calendar": "yes"}}
        with self.assertLogs("epaper_host", "WARNING") as cm:
            opts = host.v2_options(cfg)
            host.v2_options(cfg)
        self.assertEqual((opts["primary"], opts["heat_weeks"], opts["calendar"]), ("today", 10, False))
        self.assertEqual(len(cm.records), 3)

    def test_broken_config_reload_is_logged_once_per_distinct_error(self):
        errors = [ValueError("a"), ValueError("a"), OSError("b"), OSError("b")]

        def loader():
            raise errors.pop(0)
        p = host.V2Pusher(FakeTm(snapshot()), options_loader=loader, reload_every=0.0)
        with self.assertLogs("epaper_host", "WARNING") as cm:
            for i in range(4):
                p.due(float(i), NOW)
        self.assertEqual(len(cm.records), 2)


# ---------------------------------------------------------------------------
# H8: push_v2
# ---------------------------------------------------------------------------
class RejectedBlockTests(unittest.TestCase):
    def test_a_block_the_board_answers_with_err_is_sent_again(self):
        rejected = []

        class Picky(V2Serial):
            def write(self, data):
                msg = json.loads(data)
                self.writes.append(msg)
                if msg.get("type") == "quota" and not rejected:
                    rejected.append(1)
                    self.rx.put(b"ERR busy\n")
                else:
                    self.rx.put(b"OK\n")
                return len(data)
        ser = Picky(ready=READY_V2)
        link = new_link(self, ser)
        link.connect()
        link.wait_handshake(1)
        sent = []
        pusher = worker.ObservedPusher(FakeTm(snapshot()), FakeMouse(reading()), on_sent=lambda k, m: sent.append(k))
        self.assertEqual(host.push_v2(link, pusher, 0.0, NOW), 4)
        self.assertNotIn("quota", sent)
        self.assertNotIn("quota", pusher._last)
        self.assertEqual(host.push_v2(link, pusher, 1.0, NOW), 1)         # next round: only the rejected block
        self.assertEqual(sent[-1], "quota")
        self.assertEqual(sum(m.get("type") == "quota" for m in ser.writes), 2)

    def test_ping_marks_keepalive(self):
        p = host.V2Pusher(FakeTm(snapshot()))
        p.mark_ping(5.0)
        self.assertFalse(p.keepalive_due(6.0))
        self.assertTrue(p.keepalive_due(36.0))


# ---------------------------------------------------------------------------
# H5: V2Session
# ---------------------------------------------------------------------------
class SessionTests(unittest.TestCase):
    def test_board_reboot_resets_the_pusher_and_the_weather_sync(self):
        ser = V2Serial(ready=READY_V2)
        link = new_link(self, ser)
        link.connect()
        link.wait_handshake(1)
        pusher = host.V2Pusher(FakeTm(snapshot()), FakeMouse(reading()))
        wx = weather.WxSync(lambda: {"on": True, "place": "上海", "lat": 31.2, "lon": 121.5, "after_min": 10})
        reboots = []
        session = worker.V2Session(link, pusher, wx, on_reboot=lambda: reboots.append(1))
        self.assertEqual(session.step(0.0, NOW), "v2")
        types = [m["type"] for m in ser.writes]
        self.assertEqual(sum(t == "dash" for t in types), 1)
        self.assertEqual(types.count("wx_cfg"), 1)
        session.step(5.0, NOW)
        self.assertEqual([m["type"] for m in ser.writes].count("dash"), 1)       # nothing changed, nothing resent
        ser.rx.put(READY_V2)                                                     # the board restarted
        self.assertTrue(wait_for(lambda: link.ready_count == 2, 2))
        session.step(10.0, NOW)
        types = [m["type"] for m in ser.writes]
        self.assertEqual(reboots, [1])
        self.assertEqual(types.count("dash"), 2)                                 # everything resent
        self.assertEqual(types.count("quota"), 2)
        self.assertEqual(types.count("wx_cfg"), 2)                               # weather settings again

    def test_new_connection_resets_without_calling_it_a_reboot(self):
        ser = V2Serial(ready=READY_V2)
        link = new_link(self, ser)
        link.connect()
        link.wait_handshake(1)
        pusher = host.V2Pusher(FakeTm(snapshot()), FakeMouse(reading()))
        reboots, ready = [], []
        session = worker.V2Session(link, pusher, None, on_reboot=lambda: reboots.append(1),
                                   on_v2_ready=lambda _l: ready.append(1))
        session.step(0.0, NOW)
        link.connect()
        link.wait_handshake(1)
        ser.rx.put(READY_V2)
        session.step(1.0, NOW)
        self.assertEqual((reboots, ready), ([], [1, 1]))
        self.assertEqual([m["type"] for m in ser.writes].count("dash"), 2)

    def test_disconnected_link_does_nothing(self):
        link = new_link(self, V2Serial(ready=READY_V2))
        session = worker.V2Session(link, host.V2Pusher(FakeTm(snapshot())), None)
        self.assertIsNone(session.step(0.0, NOW))

    def test_backoff_grows_and_resets(self):
        b = worker.Backoff(2, 30)
        self.assertEqual([b.next() for _ in range(6)], [2, 4, 8, 16, 30, 30])
        b.reset()
        self.assertEqual(b.next(), 2)


# ---------------------------------------------------------------------------
# H9: TODO page
# ---------------------------------------------------------------------------
def item(text, done=False, important=False):
    return {"text": text, "done": done, "important": important}


class TodoImageTests(unittest.TestCase):
    def test_footer_has_no_clock_but_still_shows_the_date_and_progress(self):
        items = [item("写周报"), item("买菜", done=True)]
        a = tp.render_todo(items, datetime(2026, 10, 28, 9, 5))
        b = tp.render_todo(items, datetime(2026, 10, 28, 22, 41))
        self.assertEqual([x.tobytes() for x in a], [x.tobytes() for x in b])          # time of day no longer in the image
        c = tp.render_todo(items, datetime(2026, 10, 29, 9, 5))
        self.assertNotEqual(a[0].tobytes(), c[0].tobytes())                           # the MM-DD date still is

    def test_frames_without_refresh(self):
        frames, _ = tp.push_frames([item("x")], T0, refresh=False)
        self.assertEqual(frames[-1]["type"], "img_end")
        self.assertEqual(tp.push_frames([item("x")], T0)[0][-1]["type"], "refresh_now")

    def test_unsupported_marks_are_shared(self):
        self.assertIs(tp.UNSUPPORTED_MARKS, weather.UNSUPPORTED_MARKS)
        self.assertTrue(weather.is_unsupported("unknown_type"))
        self.assertFalse(weather.is_unsupported("too_big"))


# ---------------------------------------------------------------------------
# HostWorker end to end (fake serial)
# ---------------------------------------------------------------------------
class WorkerTestBase(unittest.TestCase):
    def setUp(self):
        self.events = queue.Queue()
        self.cfg = dict(host.DEFAULT_CONFIG, interval=1, baud=115200)
        self.clock = [NOW]

    def start(self, ser, tm=None, port="COM_TEST", backoff=None, **kw):
        p = patch.object(host, "serial", serial_ns(ser))
        p.start()
        self.addCleanup(p.stop)
        opts = host.v2_options(self.cfg)
        opts["mouse"] = False
        self.w = worker.HostWorker(self.cfg, port, self.events, tm_factory=lambda _p: tm or FakeTm(snapshot()),
                                   now_fn=lambda: self.clock[0], options=opts, **kw)
        if backoff:
            self.w._backoff = backoff
        self.addCleanup(lambda: (self.w.stop(), self.w.join(10)))
        self.w.start()

    def pushed(self, n=5):
        return drain_until(self.events, lambda ev: sum(e[0] == "msg" for e in ev) >= n)


class FlakyTm(FakeTm):
    def __init__(self, snap, failures):
        super().__init__(snap)
        self.failures = failures

    def snapshot(self, now):
        if self.failures:
            self.failures -= 1
            raise RuntimeError("boom")
        return super().snapshot(now)


class WorkerLoopTests(WorkerTestBase):
    def test_an_exception_in_an_iteration_does_not_kill_the_worker(self):
        ser = V2Serial(ready=READY_V2)
        self.start(ser, tm=FlakyTm(snapshot(), failures=2), backoff=worker.Backoff(0.05, 0.1))
        with self.assertLogs("epaper_host.worker", "ERROR"):
            seen = self.pushed(5)
        self.assertEqual(sum(e[0] == "msg" for e in seen), 5)             # recovered and pushed everything
        self.assertTrue(any(e[:2] == ("status", "error") for e in seen))  # the failure was reported
        self.assertTrue(self.w.is_alive())

    def test_commands_run_before_the_tick_starts(self):
        ser = V2Serial(ready=READY_V2)
        self.start(ser)
        self.pushed()
        self.w.request_command("page", [{"v": 2, "type": "page", "p": "dash"}], 5)
        seen = drain_until(self.events, lambda ev: any(e[:2] == ("cmd", "page") for e in ev), 8)
        self.assertTrue(next(e[2] for e in seen if e[:2] == ("cmd", "page"))["ok"])


class WorkerSilenceTests(WorkerTestBase):
    def test_silent_event_when_the_board_stops_answering(self):
        ser = V2Serial(ready=READY_V2, ok_limit=1)                     # only the connect ping is answered
        with patch.object(host, "SILENT_AFTER", 0.3), patch.object(host, "V2_ACK_WAIT", 0.1), \
                patch.object(worker, "WIFI_PROBE_S", 0.1), patch.object(worker, "WIFI_PROBE_RETRY_S", 0.1):
            self.start(ser)
            seen = drain_until(self.events, lambda ev: any(e[0] == "silent" and e[1] for e in ev), 8)
        ev = next(e for e in seen if e[0] == "silent")
        self.assertTrue(ev[1])
        self.assertGreater(ev[2], 0.3)


class WorkerPortTests(WorkerTestBase):
    def test_vanished_port_falls_back_to_the_detected_board(self):
        class PortSerial(V2Serial):
            port = None

            def open(self):
                if self.port == "COM3":
                    raise OSError("gone")
                super().open()
        ser = PortSerial(ready=READY_V2)
        board = SimpleNamespace(device="COM7", vid=0x1A86, pid=0x7523, description="USB-SERIAL CH340", manufacturer="")
        with patch.object(worker, "RETRY_MIN_S", 0.05):
            self.cfg["reconnect_interval"] = 0.05
            self.start(ser, port="COM3", ports_fn=lambda: [board])
            seen = drain_until(self.events, lambda ev: any(e[0] == "link" and e[2] == "v2" for e in ev), 10)
        self.assertIn(("port", "COM7"), seen)
        self.assertEqual(self.w.port, "COM7")
        self.assertTrue(any(e[0] == "link" and e[2] == "v2" for e in seen))

    def test_a_port_that_still_exists_is_kept(self):
        class Busy(V2Serial):
            def open(self):
                raise OSError("busy")
        ser = Busy(ready=READY_V2)
        here = SimpleNamespace(device="COM3", vid=0x1A86, pid=0x7523, description="USB-SERIAL CH340", manufacturer="")
        with patch.object(worker, "RETRY_MIN_S", 0.05):
            self.cfg["reconnect_interval"] = 0.05
            self.start(ser, port="COM3", ports_fn=lambda: [here])
            wait_for(lambda: False, 1.0)
        self.assertEqual(self.w.port, "COM3")


class WorkerTodoTests(WorkerTestBase):
    def todo_types(self, ser, since=0):
        return [m["type"] for m in ser.writes[since:] if m.get("type") in ("img_begin", "img_chunk", "img_end", "refresh_now")]

    def test_date_change_repushes_the_image_without_refresh_now(self):
        ser = V2Serial(ready=READY_V2)
        self.cfg["v2"] = {"todo": tp.todo_config([item("写周报")])}
        self.start(ser)
        self.pushed()
        frames, _ = tp.push_frames([item("写周报")], NOW)
        self.w.request_command("todo", frames, 5)
        drain_until(self.events, lambda ev: any(e[:2] == ("cmd", "todo") for e in ev), 8)
        n = len(ser.writes)
        wait_for(lambda: False, 1.3)
        self.assertEqual(self.todo_types(ser, n), [])                      # same day: nothing resent
        self.clock[0] = NOW + timedelta(days=1)
        seen = drain_until(self.events, lambda ev: any(e[0] == "todo_auto" for e in ev), 8)
        res = next(e[1] for e in seen if e[0] == "todo_auto")
        self.assertTrue(res["ok"])
        types = self.todo_types(ser, n)
        self.assertEqual(types[0], "img_begin")
        self.assertEqual(types[-1], "img_end")
        self.assertNotIn("refresh_now", types)
        n2 = len(ser.writes)
        wait_for(lambda: False, 1.3)
        self.assertEqual(self.todo_types(ser, n2), [])                     # once per new date

    def test_nothing_is_pushed_automatically_before_the_first_push(self):
        ser = V2Serial(ready=READY_V2)
        self.cfg["v2"] = {"todo": tp.todo_config([item("写周报")])}
        self.start(ser)
        self.pushed()
        self.clock[0] = NOW + timedelta(days=2)
        wait_for(lambda: False, 1.4)
        self.assertEqual(self.todo_types(ser), [])

    def test_failed_push_is_reported_with_progress_and_failed_stage(self):
        class Rejecting(V2Serial):
            def write(self, data):
                msg = json.loads(data)
                self.writes.append(msg)
                self.rx.put(b"ERR too_big\n" if msg.get("type") == "img_end" else b"OK\n")
                return len(data)
        ser = Rejecting(ready=READY_V2)
        self.start(ser)
        self.pushed()
        frames, _ = tp.push_frames([item("写周报")], NOW)
        self.w.request_command("todo", frames, 5)
        seen = drain_until(self.events, lambda ev: any(e[:2] == ("cmd", "todo") for e in ev), 8)
        res = next(e[2] for e in seen if e[:2] == ("cmd", "todo"))
        self.assertEqual((res["ok"], res["err"], res["failed"]), (False, "too_big", "img_end"))
        self.assertEqual((res["sent"], res["total"]), (len(frames) - 2, len(frames)))


class UploadRetryTests(unittest.TestCase):
    def test_chunk_that_gets_no_reply_is_sent_once_more(self):
        ser = DelayedSerial(delays=[None, None, "drop"])      # ping, img_begin ok, first img_chunk lost
        link = new_link(self, ser)
        link.connect()
        link.wait_handshake(1)
        self.assertTrue(link.command({"v": 2, "type": "img_begin", "id": "todo", "size": 4, "crc": 1}, 1).ok)
        chunk = {"v": 2, "type": "img_chunk", "id": "todo", "off": 0, "d": "AAAA"}
        with patch.object(host, "LATE_REPLY_WAIT", 0.2):
            res = link.command(chunk, 0.4, retry_on_timeout=True)
        self.assertTrue(res.ok)
        self.assertEqual([m["type"] for m in ser.writes].count("img_chunk"), 2)

    def test_late_reply_is_used_instead_of_resending(self):
        ser = DelayedSerial(delays=[None, 0.5])               # the chunk's OK comes after the command timeout
        link = new_link(self, ser)
        link.connect()
        link.wait_handshake(1)
        chunk = {"v": 2, "type": "img_chunk", "id": "todo", "off": 0, "d": "AAAA"}
        with patch.object(host, "LATE_REPLY_WAIT", 1.0):
            res = link.command(chunk, 0.3, retry_on_timeout=True)
        self.assertTrue(res.ok)
        self.assertEqual([m["type"] for m in ser.writes].count("img_chunk"), 1)

    def test_without_retry_a_timeout_stays_a_timeout(self):
        ser = DelayedSerial(delays=[None, "drop"])
        link = new_link(self, ser)
        link.connect()
        link.wait_handshake(1)
        res = link.command({"v": 2, "type": "img_end", "id": "todo"}, 0.3)
        self.assertEqual((res.ok, res.err), (False, "timeout"))


class ProbeRetryTests(WorkerTestBase):
    def test_wifi_probe_is_asked_again_with_a_longer_timeout(self):
        asked = []

        class Slow(V2Serial):
            def write(self, data):
                msg = json.loads(data)
                self.writes.append(msg)
                if msg.get("type") == "wifi_status":
                    asked.append(1)
                    if len(asked) == 1:
                        return len(data)               # first probe goes unanswered
                self.rx.put(b"OK\n")
                return len(data)
        ser = Slow(ready=READY_V2)
        with patch.object(worker, "WIFI_PROBE_S", 0.3), patch.object(host, "V2_ACK_WAIT", 0.3):
            self.start(ser)
            seen = drain_until(self.events, lambda ev: any(e[:2] == ("cmd", "auto_status") for e in ev), 10)
        res = next(e[2] for e in seen if e[:2] == ("cmd", "auto_status"))
        self.assertTrue(res["ok"])
        self.assertEqual(len(asked), 2)


class CliLoopTests(unittest.TestCase):
    def test_cli_loop_survives_an_exception_in_an_iteration(self):
        from test_dashboard_v2 import MainV2Tests
        case = MainV2Tests("cfg")
        case.setUp()
        self.addCleanup(case.doCleanups)
        real_due = host.V2Pusher.due
        calls = []

        def flaky_due(self_, mono, now):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
            return real_due(self_, mono, now)
        ser = V2Serial(ready=READY_V2)
        with patch.object(host.V2Pusher, "due", flaky_due), self.assertLogs("epaper_host", "ERROR"):
            code, *_ = case.run_main(["--port", "COM_TEST"], ser, case.cfg(), ticks=4)
        self.assertEqual(code, 0)
        self.assertGreaterEqual(len(calls), 2)
        self.assertIn("dash", [m["type"] for m in ser.writes])


if __name__ == "__main__":
    unittest.main()
