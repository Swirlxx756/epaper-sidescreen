"""上位机 GUI 的非界面逻辑测试：配置保存、摘要格式化、日志队列、后台会话（假串口）。
不导入 tkinter，不打开真实串口，不碰鼠标 HID。"""
import io
import json
import logging
import queue
import re
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dashboard_v2 as dv
import epaper_host as host
import gui
import weather
from test_dashboard_v2 import NOW, FakeMouse, FakeTm, V2Serial, reading, snapshot
from test_wifi import FAULT_LINES, FW_LINE, OTA_TOKEN, REFRESH_LINE, SECRET, FakeBoard, WifiSerial, fw_image

READY_V2 = b"READY epaper-sidescreen v2 GDEY042T81 400x300\n"
READY_V1 = b"READY epaper-sidescreen v1 WS42TRI 400x300\n"


class SaveConfigTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "config.json"

    def read(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_creates_missing_file(self):
        gui.save_config(self.path, v2=dict(primary="total"))
        self.assertEqual(self.read(), {"v2": {"primary": "total"}})

    def test_keeps_other_keys(self):
        self.path.write_text(json.dumps({
            "_comment": "说明 中文", "port": "COM9", "interval": 7,
            "v2": {"enabled": True, "tm_file": "x.json", "mouse": True, "primary": "today", "heat_weeks": 5},
        }, ensure_ascii=False), encoding="utf-8")
        gui.save_config(self.path, v2=dict(primary="total", heat_weeks=8, mouse=False))
        data = self.read()
        self.assertEqual(data["_comment"], "说明 中文")
        self.assertEqual((data["port"], data["interval"]), ("COM9", 7))
        self.assertEqual(data["v2"], {"enabled": True, "tm_file": "x.json", "mouse": False,
                                      "primary": "total", "heat_weeks": 8})
        self.assertIn("说明 中文", self.path.read_text(encoding="utf-8"))   # UTF-8 原样，不转义
        self.assertFalse(self.path.with_name("config.json.tmp").exists())

    def test_null_v2_becomes_object(self):
        self.path.write_text('{"v2": null, "port": null}', encoding="utf-8")
        gui.save_config(self.path, v2=dict(heat_weeks=6))
        self.assertEqual(self.read(), {"v2": {"heat_weeks": 6}, "port": None})

    def test_broken_file_is_not_overwritten(self):
        self.path.write_text("{ not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            gui.save_config(self.path, v2=dict(primary="total"))
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{ not json")

    def test_top_level_keys_keep_everything_else(self):
        self.path.write_text(json.dumps({
            "_comment": "说明", "port": "COM9", "interval": 7,
            "v2": {"enabled": True, "primary": "today"},
        }, ensure_ascii=False), encoding="utf-8")
        gui.save_config(self.path, {"transport": "wifi", "wifi_addr": "192.168.1.50"})
        data = self.read()
        self.assertEqual((data["transport"], data["wifi_addr"]), ("wifi", "192.168.1.50"))
        self.assertEqual((data["_comment"], data["port"], data["interval"]), ("说明", "COM9", 7))
        self.assertEqual(data["v2"], {"enabled": True, "primary": "today"})     # 没动 v2
        gui.save_config(self.path, {"wifi_addr": "10.0.0.2"}, v2={"mouse": False})
        data = self.read()
        self.assertEqual(data["wifi_addr"], "10.0.0.2")
        self.assertEqual(data["transport"], "wifi")
        self.assertEqual(data["v2"], {"enabled": True, "primary": "today", "mouse": False})

    def test_top_level_only_does_not_create_v2_section(self):
        gui.save_config(self.path, {"transport": "serial"})
        self.assertEqual(self.read(), {"transport": "serial"})

    def test_broken_file_not_overwritten_by_top_level_save_either(self):
        self.path.write_text("[1, 2", encoding="utf-8")
        with self.assertRaises(ValueError):
            gui.save_config(self.path, {"transport": "wifi"})
        self.path.write_text("[1, 2]", encoding="utf-8")     # 合法 JSON 但不是对象
        with self.assertRaises(ValueError):
            gui.save_config(self.path, {"transport": "wifi"})
        self.assertEqual(self.path.read_text(encoding="utf-8"), "[1, 2]")

    def test_ota_token_is_a_normal_top_level_key(self):
        self.assertNotIn("ota_token", gui.SECRET_KEYS)
        gui.save_config(self.path, {"ota_token": OTA_TOKEN})
        self.assertEqual(host.load_config(self.path)["ota_token"], OTA_TOKEN)

    def test_password_keys_are_refused(self):
        for key in gui.SECRET_KEYS:
            with self.subTest(key=key), self.assertRaises(ValueError):
                gui.save_config(self.path, {key: SECRET})
        with self.assertRaises(ValueError):
            gui.save_config(self.path, v2={"pass": SECRET})
        self.assertFalse(self.path.exists())

    def test_new_keys_have_defaults_and_load_back(self):
        self.assertEqual((host.DEFAULT_CONFIG["transport"], host.DEFAULT_CONFIG["wifi_addr"]), ("serial", None))
        gui.save_config(self.path, {"transport": "wifi", "wifi_addr": "192.168.1.50"})
        cfg = host.load_config(self.path)
        self.assertEqual((cfg["transport"], cfg["wifi_addr"], cfg["interval"]), ("wifi", "192.168.1.50", 30))

    def test_saved_values_load_back_through_host(self):
        gui.save_config(self.path, v2=dict(primary="total", heat_weeks=9))
        opts = host.v2_options(host.load_config(self.path))
        self.assertEqual((opts["primary"], opts["heat_weeks"]), ("total", 9))


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.msgs = dv.build_all(snapshot(), reading(pct=68, charging=True), NOW)
        self.text = "\n".join(gui.format_summary(self.msgs))

    def test_empty_says_not_pushed(self):
        text = "\n".join(gui.format_summary({}))
        self.assertEqual(text.count("尚未推送"), 5)

    def test_token_lines(self):
        dash = self.msgs["dash"]
        self.assertIn(f"今日（主）  {dash['main']['tok']} tokens  {dash['main']['cost']}", self.text)
        self.assertIn(f"累计（副）  {dash['alt']['tok']} tokens", self.text)
        self.assertIn(dash["time"], self.text)

    def test_primary_total_swaps_roles(self):
        msgs = dv.build_all(snapshot(), reading(), NOW, primary="total")
        text = "\n".join(gui.format_summary(msgs))
        self.assertIn("累计（主）", text)
        self.assertIn("今日（副）", text)
        self.assertIn("（累计）", text)   # 模型段标题

    def test_mouse_line(self):
        self.assertIn("电量 68% · 充电中", self.text)
        off = {"dash": {}, "mouse": {"v": 2, "type": "mouse", "st": "off"}}
        self.assertIn("鼠标已关机", "\n".join(gui.format_summary(off)))

    def test_quota_line_per_provider(self):
        for p in self.msgs["quota"]["p"]:
            line = next(ln for ln in self.text.splitlines() if ln.strip().startswith(p["n"]))
            for it in p.get("i", []):
                self.assertIn(f"{it['l']} {it['left']}%", line)
            if p.get("bal"):
                self.assertIn(f"余额 {p['bal']}", line)
            for t in p.get("txt", []):
                self.assertIn(t["s"], line)

    def test_quota_format_example(self):
        quota = {"p": [{"n": "Claude", "plan": "Max", "i": [{"l": "5h", "left": 62}, {"l": "Wk", "left": None}],
                        "txt": [{"s": "C/G 5%/64%", "w": 1}]},
                       {"n": "DeepSeek", "plan": "PAYG", "bal": "¥38.66", "st": "stale"}]}
        lines = gui.format_summary({"quota": quota})
        joined = "\n".join(lines)
        self.assertIn("Claude (Max)    5h 62% / Wk --    C/G 5%/64% !", joined)
        self.assertIn("DeepSeek (PAYG)    余额 ¥38.66    [数据偏旧]", joined)

    def test_models_and_heat(self):
        for name, tok, pct in self.msgs["models"]["m"]:
            self.assertIn(f"{name}    {tok}    {pct}%", self.text)
        heat = self.msgs["heat"]
        self.assertIn(f"{heat['start']} ~ {heat['end']} · 活跃 {heat['active']} 天", self.text)

    def test_no_data_state(self):
        msgs = dv.build_all(None, None, NOW)
        text = "\n".join(gui.format_summary(msgs))
        self.assertIn("无数据", text)


class LogHandlerTests(unittest.TestCase):
    def test_collects_records_from_other_threads_and_detaches(self):
        h = gui.QueueLogHandler()
        gui.attach_log_handler(h)
        self.addCleanup(gui.detach_log_handler, h)
        import threading
        t = threading.Thread(target=lambda: logging.getLogger("epaper_host").info("hello 你好"))
        t.start()
        t.join()
        logging.getLogger("mouse_rapoo").warning("mouse warn")
        logging.getLogger("epaper_host.tm").error("tm err")
        logging.getLogger("unrelated").error("ignored")
        items = h.drain()
        self.assertEqual([lv for lv, _ in items], [logging.INFO, logging.WARNING, logging.ERROR])
        self.assertIn("hello 你好", items[0][1])
        self.assertEqual(h.drain(), [])
        gui.detach_log_handler(h)
        logging.getLogger("epaper_host").warning("after detach")
        self.assertEqual(h.drain(), [])


def drain_until(q, predicate, timeout=6.0):
    """收集 worker 事件直到 predicate(已收集事件列表) 为真；超时返回现有列表。"""
    seen, deadline = [], time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            seen.append(q.get(timeout=0.05))
        except queue.Empty:
            pass
        if predicate(seen):
            break
    return seen


def wait_for(cond, timeout=6.0):
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)
    return True


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.events = queue.Queue()
        self.cfg = dict(host.DEFAULT_CONFIG, interval=1, baud=115200)
        self.mouse = FakeMouse(reading())

    def start(self, ser, mouse=True, open_error=None):
        def make_serial():
            if open_error:
                raise open_error
            return ser
        p = patch.object(host, "serial", SimpleNamespace(Serial=make_serial, SerialException=OSError))
        p.start()
        self.addCleanup(p.stop)
        opts = host.v2_options(self.cfg)
        opts["mouse"] = mouse
        self.w = gui.HostWorker(self.cfg, "COM_TEST", self.events, tm_factory=lambda _p: FakeTm(snapshot()),
                                mouse_factory=lambda: self.mouse, now_fn=lambda: NOW, options=opts)
        self.addCleanup(self.stop_worker)
        self.w.start()

    def stop_worker(self):
        self.w.stop()
        self.w.join(10)

    def kinds_sent(self, ser, from_index=0):
        return [m["type"] for m in ser.writes[from_index:] if m.get("type") in dv.KINDS]

    def test_v2_session_pushes_all_blocks_and_stops_cleanly(self):
        ser = V2Serial(ready=READY_V2)
        self.start(ser)
        events = drain_until(self.events, lambda ev: sum(e[0] == "msg" for e in ev) >= 5)
        self.assertEqual([e[1] for e in events if e[0] == "msg"], list(dv.KINDS))
        self.assertIn(("status", "v2", "已连接 v2"), events)
        self.assertEqual(self.kinds_sent(ser), list(dv.KINDS))
        self.w.stop()
        self.w.join(10)
        self.assertFalse(self.w.is_alive())
        rest = drain_until(self.events, lambda ev: any(e[0] == "done" for e in ev), timeout=2)
        self.assertIn(("done",), rest)
        self.assertGreaterEqual(ser.closed, 1)
        self.assertFalse(ser.is_open)
        self.assertTrue(self.mouse.stopped)

    def test_reset_resends_everything(self):
        ser = V2Serial(ready=READY_V2)
        self.start(ser)
        drain_until(self.events, lambda ev: sum(e[0] == "msg" for e in ev) >= 5)
        n = len(ser.writes)
        self.w.request_reset()
        self.assertTrue(wait_for(lambda: len(self.kinds_sent(ser, n)) >= 5))
        self.assertEqual(self.kinds_sent(ser, n)[:5], list(dv.KINDS))

    def test_set_options_applies_live(self):
        ser = V2Serial(ready=READY_V2)
        self.start(ser)
        drain_until(self.events, lambda ev: sum(e[0] == "msg" for e in ev) >= 5)
        n = len(ser.writes)
        self.w.request_options(primary="total", heat_weeks=8)
        self.assertTrue(wait_for(lambda: {"dash", "models", "heat"} <= set(self.kinds_sent(ser, n))))
        sent = ser.writes[n:]
        dash = next(m for m in sent if m.get("type") == "dash")
        self.assertEqual(dash["main"]["l"], "TOTAL")
        heat = next(m for m in sent if m.get("type") == "heat")
        self.assertEqual(heat["start"], dv.heat_window(NOW.date(), 8)[0].isoformat())
        self.assertNotIn("view", heat)
        n = len(ser.writes)
        self.w.request_options(calendar=True)
        self.assertTrue(wait_for(lambda: "heat" in self.kinds_sent(ser, n)))
        heat = next(m for m in ser.writes[n:] if m.get("type") == "heat")
        self.assertEqual(heat["view"], "cal")

    def test_mouse_disabled_never_touches_mouse_source(self):
        ser = V2Serial(ready=READY_V2)
        self.start(ser, mouse=False)
        events = drain_until(self.events, lambda ev: sum(e[0] == "msg" for e in ev) >= 5)
        mouse = next(e[2] for e in events if e[0] == "msg" and e[1] == "mouse")
        self.assertEqual(mouse["st"], "na")

    def test_v1_board_reports_status_and_pushes_nothing(self):
        ser = V2Serial(reject_v2=True, ready=READY_V1)
        self.start(ser)
        events = drain_until(self.events, lambda ev: ("status", "v1", "已连接 v1（不支持）") in ev)
        self.assertIn(("status", "v1", "已连接 v1（不支持）"), events)
        time.sleep(1.3)   # 再过一个 tick，确认仍然不推送
        self.assertEqual(self.kinds_sent(ser), [])
        self.assertFalse([e for e in drain_until(self.events, lambda ev: False, 0.2) if e[0] == "msg"])

    def test_open_failure_reports_error_and_stop_is_clean(self):
        self.start(V2Serial(), open_error=OSError("port busy"))
        events = drain_until(self.events, lambda ev: any(e[:2] == ("status", "error") for e in ev))
        self.assertTrue(any(e[:2] == ("status", "error") for e in events))
        self.w.stop()
        self.w.join(10)
        self.assertFalse(self.w.is_alive())


class WifiHelperTests(unittest.TestCase):
    def test_format_wifi_status(self):
        f = gui.format_wifi_status
        self.assertEqual(f({"st": "connected", "ssid": "Home-2.4G", "ip": "192.168.1.50"}), "已连接 Home-2.4G · 192.168.1.50")
        self.assertEqual(f({"st": "off"}), "未配置")
        self.assertEqual(f({"st": "connecting", "ssid": "Home-2.4G"}), "连接中 Home-2.4G")
        self.assertIn("连接失败", f({"st": "failed", "ssid": "Home-2.4G"}))
        self.assertEqual(f(None), "未知")

    def test_wifi_url_and_addresses(self):
        self.assertEqual(gui.wifi_url(" 192.168.1.50 "), "tcp://192.168.1.50")
        self.assertEqual(gui.wifi_url("tcp://h:9"), "tcp://h:9")
        self.assertEqual(gui.addr_for_display("tcp://192.168.1.50"), "192.168.1.50")
        board = {"name": "epaper-sidescreen", "id": "a1b2c3", "ip": "192.168.1.50", "port": 7330, "fw": "1.3.0", "busy": False}
        self.assertEqual(gui.board_label(board), "epaper-sidescreen (a1b2c3) 192.168.1.50")
        self.assertEqual(gui.board_addr({**board, "port": 9000}), "192.168.1.50:9000")
        self.assertIn("[占用]", gui.board_label({**board, "busy": True}))

    def test_signal_and_error_text(self):
        self.assertEqual(gui.signal_text(-52), "-52 dBm（强）")
        self.assertEqual(gui.signal_text(-90), "-90 dBm（弱）")
        self.assertEqual(gui.signal_text(None), "--")
        self.assertIn("超时", gui.cmd_error_text("timeout"))
        self.assertIn("bad_field ssid", gui.cmd_error_text("bad_field ssid"))

    def test_format_screen(self):
        f = gui.format_screen
        self.assertEqual(f(False, "DIAG display stopped", None), ("屏幕：—", False))
        text, bad = f(True, "DIAG display stopped", datetime(2026, 10, 29, 23, 48, 5))
        self.assertTrue(bad)
        self.assertIn("EN（RST）复位键", text)
        self.assertEqual(f(True, None, datetime(2026, 10, 29, 23, 48, 5)), ("屏幕：上次刷新完成 23:48:05", False))
        self.assertIn("还没收到刷新记录", f(True, None, None)[0])


FAKE_RENDER = Path(__file__).resolve().parent / "fixtures" / "fake_render.py"
RENDER_WAIT_S = 10.0     # 等渲染子进程的上限（假渲染器起一个 Python 约 0.1 秒）


def fake_render_cmd(record: Path, mode: str = "ok") -> list[str]:
    return [sys.executable, str(FAKE_RENDER), "--record", str(record), "--mode", mode]


def read_records(record: Path) -> list[dict]:
    if not record.exists():
        return []
    return [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()]


def ppm_size(ppm: bytes) -> tuple[int, int]:
    _magic, w, h, _max = ppm.split(maxsplit=4)[:4]
    return int(w), int(h)


class PreviewHelperTests(unittest.TestCase):
    """预览的非界面部分：喂给渲染器的字节、调用约定（假渲染器子进程）、后台合并请求。"""

    def setUp(self):
        self.msgs = dv.build_all(snapshot(), reading(pct=68), NOW)
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.record = Path(self.dir.name) / "record.jsonl"

    def test_input_is_the_wire_bytes_in_send_order(self):
        data = gui.preview_input(self.msgs)
        self.assertEqual(data, b"".join(host.encode(self.msgs[k]) for k in dv.KINDS))
        partial = gui.preview_input({"heat": self.msgs["heat"], "dash": self.msgs["dash"]})
        self.assertEqual(partial, host.encode(self.msgs["dash"]) + host.encode(self.msgs["heat"]))
        self.assertEqual(gui.preview_input({}), b"")

    def test_too_long_message_is_skipped_like_the_pusher(self):
        huge = {"v": 2, "type": "models", "m": [["x" * 2000, "1M", 1]]}
        self.assertEqual(gui.preview_input({"models": huge, "dash": self.msgs["dash"]}),
                         host.encode(self.msgs["dash"]))

    def test_render_ok_feeds_stdin_and_returns_ppm(self):
        data = gui.preview_input(self.msgs)
        ppm, note = gui.render_preview(fake_render_cmd(self.record), data, online=True, timeout=RENDER_WAIT_S)
        self.assertEqual(note, "")
        self.assertEqual(ppm_size(ppm), (400, 300))
        rec = read_records(self.record)[0]
        self.assertEqual(rec["stdin"].encode("utf-8"), data)
        self.assertNotIn("--offline", rec["args"])

    def test_offline_flag(self):
        gui.render_preview(fake_render_cmd(self.record), b"", online=False, timeout=RENDER_WAIT_S)
        self.assertIn("--offline", read_records(self.record)[0]["args"])

    def test_rejected_lines_still_give_an_image_with_a_note(self):
        ppm, note = gui.render_preview(fake_render_cmd(self.record, "reject"), b"{}\n", True, RENDER_WAIT_S)
        self.assertIsNotNone(ppm)
        self.assertIn("固件会拒收", note)
        self.assertIn("bad_field p", note)

    def test_failure_timeout_and_missing_exe(self):
        ppm, note = gui.render_preview(fake_render_cmd(self.record, "fail"), b"", True, RENDER_WAIT_S)
        self.assertIsNone(ppm)
        self.assertIn("退出码 2", note)
        t0 = time.monotonic()
        ppm, note = gui.render_preview(fake_render_cmd(self.record, "hang"), b"", True, timeout=1.0)
        self.assertIsNone(ppm)
        self.assertIn("超时", note)
        self.assertLess(time.monotonic() - t0, RENDER_WAIT_S)
        ppm, note = gui.render_preview([str(Path(self.dir.name) / "nope.exe")], b"", True, 1.0)
        self.assertIsNone(ppm)
        self.assertIn("无法运行渲染器", note)

    def test_find_renderer_and_staleness(self):
        d = Path(self.dir.name)
        exe, src = d / gui.RENDER_EXE, d / "src"
        self.assertIsNone(gui.find_renderer([exe]))
        exe.write_bytes(b"x")
        self.assertEqual(gui.find_renderer([d / "missing.exe", exe]), exe)
        src.mkdir()
        (src / "dashboard_v2.cpp").write_text("x")
        (src / "main.cpp").write_text("x")
        now = time.time()
        import os
        os.utime(exe, (now, now))
        os.utime(src / "dashboard_v2.cpp", (now - 60, now - 60))
        os.utime(src / "main.cpp", (now + 60, now + 60))          # 渲染器不编 main.cpp
        self.assertFalse(gui.renderer_stale(exe, src))
        os.utime(src / "dashboard_v2.cpp", (now + 60, now + 60))
        self.assertTrue(gui.renderer_stale(exe, src))
        self.assertFalse(gui.renderer_stale(d / "missing.exe", src))

    def test_background_renderer_coalesces_and_skips_repeats(self):
        gate, calls, events = __import__("threading").Event(), [], queue.Queue()

        def render(_cmd, data, online):
            calls.append((data, online))
            gate.wait(RENDER_WAIT_S)
            return b"P6 " + data, ""
        r = gui.PreviewRenderer(["x"], events, render_fn=render)
        self.assertTrue(r.submit(b"A", True))
        self.assertTrue(wait_for(lambda: calls))
        r.submit(b"B", True)                 # 第一次还在渲染：B 被 C 顶掉
        r.submit(b"C", False)
        gate.set()
        got = drain_until(events, lambda ev: len(ev) >= 2, RENDER_WAIT_S)
        self.assertEqual([e[1] for e in got], [b"P6 A", b"P6 C"])
        self.assertEqual(calls, [(b"A", True), (b"C", False)])
        self.assertTrue(wait_for(lambda: not r._busy))
        self.assertFalse(r.submit(b"C", False))   # 和上一次一样，不再渲染
        self.assertTrue(r.submit(b"C", True))

    def test_failed_render_can_be_retried(self):
        events = queue.Queue()
        r = gui.PreviewRenderer(["x"], events, render_fn=lambda *_a: (None, "坏了"))
        r.submit(b"A", True)
        self.assertEqual(drain_until(events, lambda ev: ev, RENDER_WAIT_S)[0], ("preview", None, "坏了"))
        self.assertTrue(wait_for(lambda: not r._busy))
        self.assertTrue(r.submit(b"A", True))
        drain_until(events, lambda ev: ev, RENDER_WAIT_S)


class RealRendererTests(unittest.TestCase):
    """编好的 epaper_render（firmware/test/preview/build.sh）在就测：和 preview.exe 对同样的行画出一样的图。"""

    def setUp(self):
        self.exe = gui.find_renderer()
        if self.exe is None:
            self.skipTest("epaper_render 没编译（firmware 目录运行 bash test/preview/build.sh）")
        self.msgs = dv.build_all(snapshot(), reading(pct=68), NOW)

    def test_renders_a_400x300_tricolour_screen(self):
        ppm, note = gui.render_preview([str(self.exe)], gui.preview_input(self.msgs), True, RENDER_WAIT_S)
        self.assertEqual(note, "")
        self.assertEqual(ppm_size(ppm), (400, 300))
        pixels = ppm[-400 * 300 * 3:]
        colours = {pixels[i:i + 3] for i in range(0, len(pixels), 3)}
        self.assertEqual(colours, {b"\xff\xff\xff", b"\x00\x00\x00", b"\xc8\x14\x14"})
        offline, _ = gui.render_preview([str(self.exe)], gui.preview_input(self.msgs), False, RENDER_WAIT_S)
        self.assertNotEqual(ppm, offline)                  # 离线图标

    def test_weather_page_from_open_meteo_body(self):
        body = adelaide_body()
        ppm, note = gui.render_weather([str(self.exe)], body, "上海", RENDER_WAIT_S)
        if ppm is None and "太旧" in note:
            self.skipTest("epaper_render 还不支持 --weather，请重新编译")
        self.assertEqual(note, "")
        self.assertEqual(ppm_size(ppm), (400, 300))
        pixels = ppm[-400 * 300 * 3:]
        self.assertEqual({pixels[i:i + 3] for i in range(0, len(pixels), 3)}, {b"\xff\xff\xff", b"\x00\x00\x00", b"\xc8\x14\x14"})
        again, _ = gui.render_weather([str(self.exe)], body, "上海", RENDER_WAIT_S)
        self.assertEqual(ppm, again)
        other, _ = gui.render_weather([str(self.exe)], body, "Adelaide", RENDER_WAIT_S)
        self.assertNotEqual(ppm, other)                              # 地名画在左上角
        ppm, note = gui.render_weather([str(self.exe)], b'{"current": {}}', "x", RENDER_WAIT_S)
        self.assertIsNone(ppm)
        self.assertIn("解析失败", note)

    def test_same_image_as_the_offline_preview_tool(self):
        preview_exe = self.exe.with_name("preview.exe")
        if not preview_exe.is_file():
            self.skipTest("preview.exe 没编译")
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            lines = Path(d) / "gui_lines.txt"
            lines.write_bytes(gui.preview_input(self.msgs))
            subprocess.run([str(preview_exe), d, "1", str(lines)], capture_output=True, timeout=60,
                           cwd=str(gui.FIRMWARE_DIR))
            want = (Path(d) / "gui_lines.ppm").read_bytes()
        ppm, _ = gui.render_preview([str(self.exe)], gui.preview_input(self.msgs), True, RENDER_WAIT_S)
        self.assertEqual(ppm, want)


class WorkerWifiTests(unittest.TestCase):
    """HostWorker 的 WiFi 部分：USB 串口上的命令流、WIFI 行事件、tcp 会话。"""

    def setUp(self):
        self.events = queue.Queue()
        self.cfg = dict(host.DEFAULT_CONFIG, interval=1, baud=115200)
        self.ser = WifiSerial()

    def start(self, port="COM_TEST", ser=None, open_error=None, real_serial=False):
        ser = ser or self.ser
        if not real_serial:
            def make_serial():
                if open_error:
                    raise open_error
                return ser
            p = patch.object(host, "serial", SimpleNamespace(Serial=make_serial, SerialException=OSError))
            p.start()
            self.addCleanup(p.stop)
        opts = host.v2_options(self.cfg)
        opts["mouse"] = False
        self.w = gui.HostWorker(self.cfg, port, self.events, tm_factory=lambda _p: FakeTm(snapshot()),
                                now_fn=lambda: NOW, options=opts)
        self.addCleanup(self.stop_worker)
        self.w.start()

    def stop_worker(self):
        self.w.stop()
        self.w.join(10)

    def cmd_event(self, tag, timeout=8):
        seen = drain_until(self.events, lambda ev: any(e[:2] == ("cmd", tag) for e in ev), timeout)
        return next((e[2] for e in seen if e[:2] == ("cmd", tag)), None), seen

    def test_serial_v2_reports_link_and_probes_wifi_status_once(self):
        self.start()
        res, seen = self.cmd_event("auto_status")
        self.assertTrue(res["ok"])
        self.assertEqual(res["status"]["ip"], "192.168.1.50")
        self.assertIn(("link", "serial", "v2"), seen)
        self.assertTrue(any(e[:3] == ("wifi", "status", WifiSerial.STATUS) for e in seen))
        drain_until(self.events, lambda ev: sum(e[0] == "msg" for e in ev) >= 5)
        time.sleep(1.2)     # another tick: no second probe
        self.assertEqual(sum(m.get("type") == "wifi_status" for m in self.ser.writes), 1)

    def test_scan_result_event_and_pushes_continue(self):
        self.start()
        drain_until(self.events, lambda ev: sum(e[0] == "msg" for e in ev) >= 5)
        n = len(self.ser.writes)
        self.w.request_command("scan", {"v": 2, "type": "wifi_scan"}, 10)
        res, _ = self.cmd_event("scan")
        self.assertTrue(res["ok"])
        self.assertEqual([x["ssid"] for x in res["scan"]], ["Home-2.4G", "Cafe"])
        self.assertEqual(self.ser.writes[n]["type"], "wifi_scan")
        self.w.request_reset()
        self.assertTrue(wait_for(lambda: len([m for m in self.ser.writes[n:] if m.get("type") in dv.KINDS]) >= 5))

    def test_set_and_clear_commands(self):
        self.start()
        drain_until(self.events, lambda ev: sum(e[0] == "msg" for e in ev) >= 5)
        self.w.request_command("set", {"v": 2, "type": "wifi_set", "ssid": "Home-2.4G", "pass": SECRET}, 5)
        res, _ = self.cmd_event("set")
        self.assertTrue(res["ok"])
        sent = next(m for m in self.ser.writes if m.get("type") == "wifi_set")
        self.assertEqual((sent["ssid"], sent["pass"]), ("Home-2.4G", SECRET))
        self.w.request_command("clear", {"v": 2, "type": "wifi_clear"}, 5)
        res, _ = self.cmd_event("clear")
        self.assertTrue(res["ok"])
        self.ser.mode["reset_settings"] = "err:bad_field x"
        self.w.request_command("reset", {"v": 2, "type": "reset_settings"}, 5)
        res, _ = self.cmd_event("reset")
        self.assertEqual((res["ok"], res["err"]), (False, "bad_field x"))

    def test_unanswered_command_times_out_without_stalling_pushes(self):
        self.start()
        drain_until(self.events, lambda ev: sum(e[0] == "msg" for e in ev) >= 5)
        n = len(self.ser.writes)
        self.ser.mode["wifi_scan"] = "silent"
        t0 = time.monotonic()
        self.w.request_command("scan", {"v": 2, "type": "wifi_scan"}, 0.5)
        res, _ = self.cmd_event("scan", timeout=4)
        self.assertEqual((res["ok"], res["err"]), (False, "timeout"))
        self.assertLess(time.monotonic() - t0, 3)
        self.w.request_reset()
        self.assertTrue(wait_for(lambda: len([m for m in self.ser.writes[n:] if m.get("type") in dv.KINDS]) >= 5))

    def test_command_without_connection_reports_not_connected(self):
        self.start(open_error=OSError("port busy"))
        drain_until(self.events, lambda ev: any(e[:2] == ("status", "error") for e in ev))
        self.w.request_command("scan", {"v": 2, "type": "wifi_scan"}, 2)
        res, _ = self.cmd_event("scan")
        self.assertEqual((res["ok"], res["err"]), (False, "not_connected"))

    def test_password_never_logged_by_the_worker(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setLevel(logging.DEBUG)
        root = logging.getLogger()
        old = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.removeHandler, handler)
        self.addCleanup(root.setLevel, old)
        qh = gui.QueueLogHandler()
        gui.attach_log_handler(qh)
        self.addCleanup(gui.detach_log_handler, qh)
        self.start()
        drain_until(self.events, lambda ev: sum(e[0] == "msg" for e in ev) >= 5)
        self.w.request_command("set", {"v": 2, "type": "wifi_set", "ssid": "Home-2.4G", "pass": SECRET}, 5)
        self.cmd_event("set")
        self.ser.mode["wifi_set"] = "err:bad_field pass"
        self.w.request_command("set", {"v": 2, "type": "wifi_set", "ssid": "Home-2.4G", "pass": SECRET}, 5)
        self.cmd_event("set")
        self.assertNotIn(SECRET, stream.getvalue())
        self.assertFalse([text for _lv, text in qh.drain() if SECRET in text])
        self.assertIn("wifi_set", stream.getvalue())      # 记了命令类型，没记内容

    def test_tcp_session_has_no_wifi_probe_and_reports_tcp_link(self):
        board = FakeBoard()
        self.addCleanup(board.close)
        self.start(port=board.url, real_serial=True)
        events = drain_until(self.events, lambda ev: sum(e[0] == "msg" for e in ev) >= 5, timeout=10)
        self.assertIn(("status", "v2", "已连接 v2"), events)
        self.assertIn(("link", "tcp", "v2"), events)
        # "msg" 在发出时就报，假板子读到最后一行可能稍晚一点
        self.assertTrue(wait_for(lambda: len([m for m in board.received if m["type"] in dv.KINDS]) >= 5, 3))
        self.assertEqual([m["type"] for m in board.received if m["type"] in dv.KINDS], list(dv.KINDS))
        self.assertFalse(any(m["type"] == "wifi_status" for m in board.received))

    def test_serial_v2_auto_pairs_once_without_logging_the_token(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setLevel(logging.DEBUG)
        root = logging.getLogger()
        old = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.removeHandler, handler)
        self.addCleanup(root.setLevel, old)
        self.start()
        res, seen = self.cmd_event("auto_pair")
        self.assertTrue(res["ok"])
        self.assertEqual(res["pair"], {"type": "pair", "token": OTA_TOKEN, "id": "a1b2c3"})
        drain_until(self.events, lambda ev: sum(e[0] == "msg" for e in ev) >= 5)
        time.sleep(1.2)     # another tick: no second pair
        self.assertEqual(sum(m.get("type") == "pair" for m in self.ser.writes), 1)
        self.assertNotIn(OTA_TOKEN, stream.getvalue())

    def test_tcp_ota_reports_progress_and_nothing_else_is_sent_during_transfer(self):
        board = FakeBoard(ready=READY_V2 + FW_LINE)
        self.addCleanup(board.close)
        self.start(port=board.url, real_serial=True)
        events = drain_until(self.events, lambda ev: sum(e[0] == "msg" for e in ev) >= 5 and ("fw", "1.3.1") in ev,
                             timeout=10)
        self.assertIn(("fw", "1.3.1"), events)
        image = fw_image(200000)
        self.w.request_ota(image, OTA_TOKEN)
        events = drain_until(self.events, lambda ev: any(e[:2] == ("ota", "done") for e in ev), timeout=15)
        done = next(e[2] for e in events if e[:2] == ("ota", "done"))
        self.assertTrue(done["ok"], done)
        self.assertEqual(board.image, image)
        self.assertEqual(board.junk, 0)
        types = [m["type"] for m in board.received]
        self.assertEqual(types[-1], "ota_begin")          # 传输期间没有推送、ping 或命令
        progress = [e[2:] for e in events if e[:2] == ("ota", "progress")]
        self.assertEqual(progress[-1], (200000, 200000))
        self.assertLessEqual(len(progress), 22)            # 约每 5% 一次
        self.assertGreaterEqual(len(progress), 10)
        rest = drain_until(self.events, lambda ev: ("status", "connecting", "板子重启中，稍后自动重连…") in ev, timeout=5)
        self.assertIn(("fw", None), rest)
        self.assertIn(("status", "connecting", "板子重启中，稍后自动重连…"), rest)

    def test_ota_with_bad_file_or_no_connection(self):
        board = FakeBoard()
        self.addCleanup(board.close)
        self.start(port=board.url, real_serial=True)
        drain_until(self.events, lambda ev: sum(e[0] == "msg" for e in ev) >= 5, timeout=10)
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        junk = Path(d.name) / "notes.bin"
        junk.write_bytes(b"not firmware")
        self.w.request_ota(str(junk), OTA_TOKEN)
        events = drain_until(self.events, lambda ev: any(e[:2] == ("ota", "done") for e in ev))
        self.assertEqual(next(e[2] for e in events if e[:2] == ("ota", "done"))["err"], "bad_image")
        self.assertFalse(any(m["type"] == "ota_begin" for m in board.received))

    def test_ota_on_usb_serial_is_refused(self):
        self.start()
        self.cmd_event("auto_pair")
        self.w.request_ota(fw_image(), OTA_TOKEN)
        events = drain_until(self.events, lambda ev: any(e[:2] == ("ota", "done") for e in ev))
        self.assertEqual(next(e[2] for e in events if e[:2] == ("ota", "done"))["err"], "tcp_only")
        self.assertFalse(any(m.get("type") == "ota_begin" for m in self.ser.writes))

    def test_tcp_unreachable_reports_connect_error(self):
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        self.start(port=f"tcp://127.0.0.1:{port}", real_serial=True)
        events = drain_until(self.events, lambda ev: any(e[:2] == ("status", "error") for e in ev), timeout=10)
        err = next(e for e in events if e[:2] == ("status", "error"))
        self.assertIn("无法连接 tcp://127.0.0.1", err[2])

    def test_tcp_fault_and_refresh_lines_become_events(self):
        board = FakeBoard(ready=READY_V2 + FAULT_LINES + REFRESH_LINE.encode() + b"\n")
        self.addCleanup(board.close)
        self.start(port=board.url, real_serial=True)
        events = drain_until(self.events, lambda ev: any(e[0] == "fault" and "display stopped" in e[1] for e in ev)
                             and any(e[0] == "refreshed" for e in ev), timeout=10)   # 先到的是 "DIAG epd fault ..." 那一行
        fault = next(e for e in events if e[0] == "fault" and "display stopped" in e[1])
        self.assertIn("display stopped", fault[1])
        self.assertIsInstance(next(e for e in events if e[0] == "refreshed")[1], datetime)
        board.drop_clients()        # 板子重启：连接断开，故障随连接清掉
        events = drain_until(self.events, lambda ev: ("fault", None) in ev, timeout=10)
        self.assertIn(("fault", None), events)


class FakeWorker:
    """App 测试用的假后台会话：记录请求，stop() 立刻发 done。"""
    instances: list = []

    def __init__(self, cfg, port, events, options=None, **_kw):
        self.cfg, self.port, self.events, self.options = cfg, port, events, options
        self.commands: list = []
        self.otas: list = []
        self.started = False
        FakeWorker.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.events.put(("done",))

    def join(self, _t=None):
        pass

    def request_command(self, tag, msg, timeout=5.0):
        self.commands.append((tag, msg, timeout))

    def request_options(self, **_o):
        pass

    def request_reset(self):
        self.resets = getattr(self, "resets", 0) + 1

    def request_ota(self, image, token):
        self.otas.append((image, token))

    def request_weather(self, opts):
        self.weather = getattr(self, "weather", []) + [opts]


BOARD = {"name": "epaper-sidescreen", "id": "a1b2c3", "ip": "192.168.1.50", "port": 7330, "fw": "1.3.0", "busy": False}


class AppTestBase(unittest.TestCase):
    """真实 tkinter 窗口（withdraw，不显示），假 worker / 假发现 / 空串口列表：不开任何串口或网络。"""

    def setUp(self):
        try:
            import tkinter
            self.root = tkinter.Tk()
        except Exception as e:  # noqa: BLE001 - 没有显示环境时跳过
            self.skipTest(f"tkinter 不可用：{e}")
        self.root.withdraw()
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "config.json"
        FakeWorker.instances = []
        self.boards = [BOARD]
        self.closed = False
        self.addCleanup(self.shutdown)

    @staticmethod
    def no_network(url, timeout):
        raise OSError("no network in tests")

    def shutdown(self):
        if not self.closed and hasattr(self, "app"):
            self.app.on_close()
        elif not hasattr(self, "app"):
            self.root.destroy()

    def make(self, file_dialog=None, renderer=None, geocode_fn=None, weather_fetch=None, **cfg_extra):
        cfg = dict(host.DEFAULT_CONFIG, **cfg_extra)
        self.app = gui.App(self.root, cfg=cfg, config_path=self.path, ports_fn=lambda: [],
                           geocode_fn=geocode_fn or (lambda name: []),
                           weather_fetch=weather_fetch or self.no_network,
                           worker_factory=FakeWorker, discover_fn=lambda: list(self.boards),
                           file_dialog=file_dialog, renderer=renderer)
        self.root.update()
        return self.app

    def pump(self, cond=lambda: False, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.root.update()
            self.app._drain_events()
            if cond():
                return True
            time.sleep(0.01)
        return cond()

    def feed(self, *events):
        for ev in events:
            self.app.events.put(ev)
        self.app._drain_events()
        self.root.update()

    def read(self):
        return json.loads(self.path.read_text(encoding="utf-8"))


class AppTests(AppTestBase):
    def test_builds_headlessly_in_serial_mode_by_default(self):
        app = self.make()
        self.assertEqual(app.transport(), "serial")
        self.assertEqual(app.port_box.winfo_manager(), "grid")
        self.assertEqual(app.addr_box.winfo_manager(), "")
        self.assertEqual(str(app.wifi_btn.cget("state")), "disabled")
        self.assertEqual(app.wifi_var.get(), "Wi-Fi：—")
        self.assertEqual(FakeWorker.instances, [])

    def test_wifi_mode_shows_address_row_and_persists_choice(self):
        self.path.write_text(json.dumps({"port": "COM9", "v2": {"mouse": True}}), encoding="utf-8")
        app = self.make()
        app.transport_var.set("wifi")
        app.on_transport()
        self.root.update()
        self.assertEqual(app.addr_box.winfo_manager(), "grid")
        self.assertEqual(app.port_box.winfo_manager(), "")
        self.assertEqual(app.search_btn.winfo_manager(), "grid")
        self.assertEqual(app.refresh_btn.winfo_manager(), "")
        data = self.read()
        self.assertEqual((data["transport"], data["port"], data["v2"]), ("wifi", "COM9", {"mouse": True}))
        app.transport_var.set("serial")
        app.on_transport()
        self.assertEqual(self.read()["transport"], "serial")

    def test_saved_wifi_settings_drive_auto_connect(self):
        app = self.make(transport="wifi", wifi_addr="192.168.1.50")
        self.assertEqual(app.transport(), "wifi")
        self.assertEqual(app.addr_var.get(), "192.168.1.50")
        app.auto_connect()
        worker = FakeWorker.instances[0]
        self.assertEqual(worker.port, "tcp://192.168.1.50")
        self.assertTrue(worker.started)

    def test_serial_stays_default_and_uses_serial_port(self):
        app = self.make(port="COM_TEST")
        app.auto_connect()
        self.assertEqual(FakeWorker.instances[0].port, "COM_TEST")

    def test_wifi_without_address_searches_instead_of_connecting(self):
        app = self.make(transport="wifi")
        app.auto_connect()
        self.assertEqual(FakeWorker.instances, [])
        self.assertTrue(self.pump(lambda: app._found))
        self.assertEqual(app.selected_addr(), "192.168.1.50")       # 只有一块，自动选中
        self.assertEqual(app.addr_var.get(), "epaper-sidescreen (a1b2c3) 192.168.1.50")

    def test_connect_without_address_complains(self):
        app = self.make(transport="wifi")
        app.toggle_connection()
        self.assertEqual(FakeWorker.instances, [])
        self.assertIn("搜索", app.status_var.get())

    def test_search_fills_combobox_with_boards_and_last_used(self):
        self.boards = [BOARD, {**BOARD, "id": "ffeedd", "ip": "192.168.1.60", "busy": True}]
        app = self.make(transport="wifi", wifi_addr="192.168.1.99")
        app.search_boards()
        self.assertEqual(str(app.search_btn.cget("state")), "disabled")
        self.assertTrue(self.pump(lambda: app._found))
        values = list(app.addr_box["values"])
        self.assertEqual(values[0], "epaper-sidescreen (a1b2c3) 192.168.1.50")
        self.assertIn("[占用]", values[1])
        self.assertEqual(values[-1], "192.168.1.99")
        self.assertEqual(str(app.search_btn.cget("state")), "normal")
        app.addr_var.set(values[1])
        self.assertEqual(app.selected_addr(), "192.168.1.60")
        app.addr_var.set("  10.1.1.1  ")                              # 手填
        self.assertEqual(app.selected_addr(), "10.1.1.1")

    def test_search_with_no_boards_and_with_failure(self):
        app = self.make(transport="wifi")
        self.boards = []
        app.search_boards()
        self.assertTrue(self.pump(lambda: not app._searching))
        self.assertIn("没找到", app.status_var.get())
        app._discover_fn = lambda: (_ for _ in ()).throw(OSError("no network"))
        app.search_boards()
        self.assertTrue(self.pump(lambda: not app._searching))
        self.assertIn("搜索失败", app.status_var.get())

    def test_connecting_in_wifi_mode_remembers_the_address_and_locks_inputs(self):
        app = self.make(transport="wifi")
        app.addr_var.set("192.168.1.77")
        app.toggle_connection()
        self.assertEqual(self.read()["wifi_addr"], "192.168.1.77")
        self.assertEqual(str(app.addr_box.cget("state")), "disabled")
        self.assertEqual(str(app.mode_radios[0].cget("state")), "disabled")
        app.toggle_connection()                                      # 断开
        self.pump(lambda: app.worker is None)
        self.assertEqual(str(app.addr_box.cget("state")), "normal")
        self.assertEqual(str(app.conn_btn.cget("text")), "连接")

    def test_wifi_label_and_provision_button_follow_link_and_status(self):
        app = self.make(port="COM_TEST")
        app.toggle_connection()
        self.feed(("link", "serial", "v2"))
        self.assertEqual(app.wifi_var.get(), "Wi-Fi：读取中…")
        self.assertEqual(str(app.wifi_btn.cget("state")), "normal")
        for st, text in (({"type": "status", "st": "connected", "ssid": "Home-2.4G", "ip": "192.168.1.50"},
                          "Wi-Fi：已连接 Home-2.4G · 192.168.1.50"),
                         ({"type": "status", "st": "off"}, "Wi-Fi：未配置"),
                         ({"type": "status", "st": "connecting", "ssid": "Home-2.4G"}, "Wi-Fi：连接中 Home-2.4G"),
                         ({"type": "status", "st": "failed"}, "Wi-Fi：连接失败（密码错或找不到网络）")):
            self.feed(("wifi", "status", st))
            self.assertEqual(app.wifi_var.get(), text)
        self.feed(("link", "serial", "v1"))
        self.assertEqual(str(app.wifi_btn.cget("state")), "disabled")
        self.feed(("link", "serial", "v2"))
        self.feed(("link", "tcp", "v2"))
        self.assertEqual(str(app.wifi_btn.cget("state")), "disabled")      # WiFi 连接时不能配网
        self.assertIn("已通过 Wi-Fi 连接", app.wifi_var.get())

    def test_tcp_label_shows_address_in_use(self):
        app = self.make(transport="wifi", wifi_addr="192.168.1.50")
        app.toggle_connection()
        self.feed(("link", "tcp", "v2"))
        self.assertEqual(app.wifi_var.get(), "Wi-Fi：已通过 Wi-Fi 连接 192.168.1.50")

    def test_old_firmware_marks_wifi_unsupported(self):
        app = self.make(port="COM_TEST")
        app.toggle_connection()
        self.feed(("link", "serial", "v2"),
                  ("cmd", "auto_status", {"ok": False, "err": "unknown_type", "scan": None, "status": None}))
        self.assertIn("固件不支持", app.wifi_var.get())
        self.assertEqual(str(app.wifi_btn.cget("state")), "disabled")

    def test_disconnect_clears_wifi_state_and_closes_gate(self):
        app = self.make(port="COM_TEST")
        app.toggle_connection()
        self.feed(("link", "serial", "v2"), ("wifi", "status", {"type": "status", "st": "off"}))
        app.toggle_connection()
        self.pump(lambda: app.worker is None)
        self.assertEqual(app.wifi_var.get(), "Wi-Fi：—")
        self.assertEqual(str(app.wifi_btn.cget("state")), "disabled")

    def test_screen_line_shows_fault_in_red_and_last_refresh(self):
        app = self.make(transport="wifi", wifi_addr="192.168.1.50")
        self.assertEqual(app.screen_var.get(), "屏幕：—")
        app.toggle_connection()
        self.feed(("link", "tcp", "v2"))
        self.assertIn("还没收到刷新记录", app.screen_var.get())
        self.feed(("refreshed", datetime(2026, 10, 29, 23, 48, 5)))
        self.assertEqual(app.screen_var.get(), "屏幕：上次刷新完成 23:48:05")
        self.feed(("fault", "DIAG display stopped (driver fault); reboot to retry"))
        self.assertIn("复位键", app.screen_var.get())
        self.assertEqual(str(app.screen_label.cget("foreground")), gui.STATE_STYLE["error"][0])
        self.feed(("fault", None))
        self.assertEqual(app.screen_var.get(), "屏幕：上次刷新完成 23:48:05")
        self.assertEqual(str(app.screen_label.cget("foreground")), "")
        app.toggle_connection()
        self.pump(lambda: app.worker is None)
        self.assertEqual(app.screen_var.get(), "屏幕：—")
        self.assertIsNone(app.last_refresh)

    # -- 升级固件 -------------------------------------------------------------
    def ota_app(self, **kw):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.bin = Path(d.name) / "firmware.bin"
        self.bin.write_bytes(fw_image(30000))
        self.dialog_calls = []
        self.pick = str(self.bin)
        app = self.make(file_dialog=lambda **k: self.dialog_calls.append(k) or self.pick,
                        transport="wifi", wifi_addr="192.168.1.50", **{"ota_token": OTA_TOKEN, **kw})
        app.toggle_connection()
        self.feed(("link", "tcp", "v2"))
        return app, FakeWorker.instances[0]

    def test_ota_button_enabled_only_over_wifi_v2_with_token(self):
        app = self.make(transport="wifi", wifi_addr="192.168.1.50", ota_token=OTA_TOKEN)
        self.assertEqual(str(app.ota_btn.cget("state")), "disabled")         # 没连接
        app.toggle_connection()
        self.feed(("link", "tcp", "v2"))
        self.assertEqual(str(app.ota_btn.cget("state")), "normal")
        self.feed(("link", "tcp", "v1"))
        self.assertEqual(str(app.ota_btn.cget("state")), "disabled")
        self.feed(("link", "serial", "v2"))
        self.assertEqual(str(app.ota_btn.cget("state")), "disabled")         # USB 时用 esptool
        self.feed(("link", "tcp", "v2"))
        app.cfg["ota_token"] = None
        app._update_wifi_ui()
        self.assertEqual(str(app.ota_btn.cget("state")), "disabled")         # 没配对

    def test_no_token_logs_a_pairing_hint_once(self):
        app = self.make(transport="wifi", wifi_addr="192.168.1.50")
        app.toggle_connection()
        with self.assertLogs(gui.log, level="INFO") as logs:
            self.feed(("link", "tcp", "v2"), ("link", None, None), ("link", "tcp", "v2"))
        self.assertEqual(sum("还没和板子配对" in line for line in logs.output), 1)

    def test_auto_pair_saves_token_only_when_it_changes(self):
        self.path.write_text(json.dumps({"port": "COM9", "v2": {"mouse": True}}), encoding="utf-8")
        app = self.make(port="COM_TEST")
        app.toggle_connection()
        res = {"ok": True, "err": "", "scan": None, "status": None,
               "pair": {"type": "pair", "token": OTA_TOKEN, "id": "a1b2c3"}}
        with self.assertLogs(gui.log, level="INFO") as logs:
            self.feed(("link", "serial", "v2"), ("cmd", "auto_pair", res))
        self.assertIn("已和板子配对", " ".join(logs.output))
        self.assertNotIn(OTA_TOKEN, " ".join(logs.output))
        self.assertEqual(self.read(), {"port": "COM9", "v2": {"mouse": True}, "ota_token": OTA_TOKEN})
        self.assertEqual(app.cfg["ota_token"], OTA_TOKEN)
        mtime = self.path.stat().st_mtime_ns
        with self.assertNoLogs(gui.log, level="INFO"):
            self.feed(("cmd", "auto_pair", res))                           # 同一个码：不写、不提示
        self.assertEqual(self.path.stat().st_mtime_ns, mtime)

    def test_auto_pair_on_old_firmware_is_ignored(self):
        app = self.make(port="COM_TEST")
        app.toggle_connection()
        with self.assertLogs(gui.log, level="INFO") as logs:
            self.feed(("link", "serial", "v2"),
                      ("cmd", "auto_pair", {"ok": False, "err": "unknown_type", "scan": None, "status": None, "pair": None}))
        self.assertEqual([r.levelno for r in logs.records], [logging.INFO])
        self.assertFalse(self.path.exists())
        self.assertNotIn("固件不支持", app.wifi_var.get())                    # WiFi 本身照常

    def test_ota_flow_progress_and_done(self):
        app, worker = self.ota_app()
        with patch.object(app, "ask_yes_no", return_value=True) as ask:
            app.start_ota()
        self.assertIn("大约 1 分钟", ask.call_args[0][1])
        self.assertIn("自动回到旧固件", ask.call_args[0][1])
        self.assertEqual(worker.otas, [(fw_image(30000), OTA_TOKEN)])
        self.assertEqual(self.dialog_calls[0]["filetypes"][0][1], "*.bin")
        self.assertEqual(str(app.ota_btn.cget("state")), "disabled")
        self.assertEqual(app.status_var.get(), "升级固件中 0%")
        self.feed(("ota", "progress", 13500, 30000))
        self.assertEqual(app.status_var.get(), "升级固件中 45%")
        with self.assertLogs(gui.log, level="INFO") as logs:
            self.feed(("ota", "done", {"ok": True, "err": ""}))
            self.feed(("link", None, None), ("fw", None), ("link", "tcp", "v2"), ("fw", "1.3.2"))
        self.assertIn("升级完成，板子正在重启", " ".join(logs.output))
        self.assertIn("板子固件版本 1.3.2", " ".join(logs.output))
        self.assertEqual(app.fw_version, "1.3.2")
        self.assertEqual(str(app.ota_btn.cget("state")), "normal")

    def test_ota_error_texts(self):
        app, worker = self.ota_app()
        for err, text in (("ota_token", "用 USB 线连一次上位机"), ("ota_too_big", "固件太大"),
                          ("ota_md5", "校验失败，没有写入，板子仍是旧固件"), ("ota_timeout", "传输中断，板子仍是旧固件"),
                          ("disconnected", "传输中断"), ("bad_image", "这不是 ESP32 固件文件"),
                          ("tcp_only", "只能在 Wi-Fi 连接下升级"), ("ota_write", "ota_write")):
            with self.subTest(err=err):
                app.ota_running = True
                self.feed(("ota", "done", {"ok": False, "err": err}))
                self.assertIn(text, app.status_var.get())
                self.assertEqual(app.dot.cget("fg"), gui.STATE_STYLE["error"][0])
                self.assertFalse(app.ota_running)

    def test_ota_cancel_bad_file_and_declined_confirm_send_nothing(self):
        app, worker = self.ota_app()
        self.pick = ""                                                       # 取消选择
        app.start_ota()
        junk = self.bin.with_name("notes.bin")
        junk.write_bytes(b"\x00not firmware")
        self.pick = str(junk)
        with patch.object(app, "ask_yes_no", return_value=True) as ask:
            app.start_ota()
        ask.assert_not_called()
        self.assertIn("这不是 ESP32 固件文件", app.status_var.get())
        self.pick = str(self.bin)
        with patch.object(app, "ask_yes_no", return_value=False):
            app.start_ota()
        self.assertEqual(worker.otas, [])
        self.assertFalse(app.ota_running)

    def test_disconnect_resets_ota_state(self):
        app, worker = self.ota_app()
        with patch.object(app, "ask_yes_no", return_value=True):
            app.start_ota()
        self.assertEqual(app.ota_bar.winfo_manager(), "grid")               # 升级期间显示进度条
        self.feed(("ota", "progress", 15000, 30000))
        self.assertEqual(int(float(app.ota_bar.cget("value"))), 50)
        with patch.object(app, "ask_yes_no", return_value=False) as ask:   # 升级中断开要确认
            app.toggle_connection()
        ask.assert_called_once()
        self.assertIsNotNone(app.worker)
        self.assertTrue(app.ota_running)
        with patch.object(app, "ask_yes_no", return_value=False) as ask:   # 升级中退出同样要确认
            self.assertFalse(app.request_quit())
        self.assertFalse(app._closing)
        with patch.object(app, "ask_yes_no", return_value=True):
            app.toggle_connection()
        self.pump(lambda: app.worker is None)
        self.assertFalse(app.ota_running)
        self.assertEqual(app.ota_bar.winfo_manager(), "")
        self.assertEqual(str(app.ota_btn.cget("state")), "disabled")

    def open_dialog(self):
        app = self.make(port="COM_TEST")
        app.toggle_connection()
        self.feed(("link", "serial", "v2"), ("wifi", "status", {"type": "status", "st": "off"}))
        app.open_wifi_dialog()
        self.root.update()
        self.assertIsNotNone(app.wifi_dialog)
        return app, app.wifi_dialog, FakeWorker.instances[0]

    def test_dialog_only_opens_over_usb_serial_v2(self):
        app = self.make(transport="wifi", wifi_addr="192.168.1.50")
        app.toggle_connection()
        self.feed(("link", "tcp", "v2"))
        app.open_wifi_dialog()
        self.assertIsNone(app.wifi_dialog)

    def test_dialog_scan_lists_networks_and_selection_fills_ssid(self):
        app, dlg, worker = self.open_dialog()
        dlg.scan()
        self.assertEqual(worker.commands[-1][:2], ("scan", {"v": 2, "type": "wifi_scan"}))
        self.assertEqual(worker.commands[-1][2], gui.SCAN_TIMEOUT_S)
        self.assertEqual(str(dlg.scan_btn.cget("state")), "disabled")      # 扫描期间不能再点
        self.feed(("cmd", "scan", {"ok": True, "err": "", "status": None, "scan": [
            {"ssid": "Home-2.4G", "rssi": -52, "sec": True}, {"ssid": "Cafe", "rssi": -80, "sec": False}]}))
        rows = [dlg.tree.item(i, "values") for i in dlg.tree.get_children()]
        self.assertEqual(rows, [("Home-2.4G", "-52 dBm（强）", "是"), ("Cafe", "-80 dBm（弱）", "否（开放）")])
        self.assertEqual(str(dlg.scan_btn.cget("state")), "normal")
        first = dlg.tree.get_children()[0]
        dlg.tree.selection_set(first)
        self.root.update()
        self.assertEqual(dlg.ssid_var.get(), "Home-2.4G")
        dlg.ssid_var.set("Hidden-Net")                                     # 隐藏网络：直接改
        self.assertEqual(dlg.ssid_var.get(), "Hidden-Net")

    def test_dialog_scan_failure_shows_text(self):
        app, dlg, worker = self.open_dialog()
        dlg.scan()
        self.feed(("cmd", "scan", {"ok": False, "err": "timeout", "status": None, "scan": None}))
        self.assertIn("超时", dlg.status_var.get())
        self.assertEqual(str(dlg.scan_btn.cget("state")), "normal")

    def test_dialog_write_sends_wifi_set_then_offers_wifi_mode_and_prefills_address(self):
        app, dlg, worker = self.open_dialog()
        dlg.ssid_var.set("Home-2.4G")
        dlg.pass_var.set(SECRET)
        dlg.show_var.set(True)
        dlg._toggle_show()
        self.assertEqual(str(dlg.pass_entry.cget("show")), "")
        dlg.write()
        tag, msg, _timeout = worker.commands[-1]
        self.assertEqual((tag, msg), ("set", {"v": 2, "type": "wifi_set", "ssid": "Home-2.4G", "pass": SECRET}))
        self.feed(("cmd", "set", {"ok": True, "err": "", "status": None, "scan": None}))
        self.assertIn("正在连接 Home-2.4G", dlg.status_var.get())
        self.feed(("wifi", "status", {"type": "status", "st": "connecting", "ssid": "Home-2.4G"}))
        self.assertEqual(dlg.hint_var.get(), "")
        self.feed(("wifi", "status", {"type": "status", "st": "connected", "ssid": "Home-2.4G",
                                      "ip": "192.168.1.50", "port": 7330}))
        self.assertIn("已连接 Home-2.4G · 192.168.1.50", dlg.status_var.get())
        self.assertIn("切到 Wi-Fi", dlg.hint_var.get())
        self.assertIn("192.168.1.50", dlg.hint_var.get())
        self.assertEqual(app.addr_var.get(), "192.168.1.50")
        data = self.read()
        self.assertEqual(data["wifi_addr"], "192.168.1.50")
        self.assertNotIn(SECRET, self.path.read_text(encoding="utf-8"))     # 密码绝不进配置
        self.assertEqual(app.transport(), "serial")                          # 只预填，不自动切

    def test_dialog_ignores_stale_status_of_previous_network(self):
        app, dlg, worker = self.open_dialog()
        dlg.ssid_var.set("New-Net")
        dlg.write()
        self.feed(("cmd", "set", {"ok": True, "err": "", "status": None, "scan": None}),
                  ("wifi", "status", {"type": "status", "st": "connected", "ssid": "Old-Net", "ip": "10.0.0.9"}))
        self.assertEqual(dlg.hint_var.get(), "")
        self.assertNotIn("wifi_addr", self.read() if self.path.exists() else {})

    def test_dialog_failed_connection_hint_and_write_error(self):
        app, dlg, worker = self.open_dialog()
        dlg.ssid_var.set("Home-2.4G")
        dlg.write()
        self.feed(("cmd", "set", {"ok": False, "err": "bad_field pass", "status": None, "scan": None}))
        self.assertIn("bad_field pass", dlg.status_var.get())
        dlg.write()
        self.feed(("cmd", "set", {"ok": True, "err": "", "status": None, "scan": None}),
                  ("wifi", "status", {"type": "status", "st": "failed", "ssid": "Home-2.4G"}))
        self.assertIn("检查密码", dlg.hint_var.get())

    def test_dialog_validates_before_sending(self):
        app, dlg, worker = self.open_dialog()
        dlg.write()
        self.assertIn("请先选", dlg.status_var.get())
        dlg.ssid_var.set("x" * 33)
        dlg.write()
        self.assertIn("名称太长", dlg.status_var.get())
        dlg.ssid_var.set("ok")
        dlg.pass_var.set("p" * 64)
        dlg.write()
        self.assertIn("密码太长", dlg.status_var.get())
        self.assertEqual(worker.commands, [])

    def test_dialog_clear_and_factory_reset_confirm(self):
        app, dlg, worker = self.open_dialog()
        dlg.clear()
        self.assertEqual(worker.commands[-1][:2], ("clear", {"v": 2, "type": "wifi_clear"}))
        self.feed(("cmd", "clear", {"ok": True, "err": "", "status": None, "scan": None}))
        self.assertIn("已清除", dlg.status_var.get())
        asked = []
        with patch.object(app, "ask_yes_no", side_effect=lambda *a, **k: asked.append(a) or False):
            dlg.reset()
        self.assertEqual(len(asked), 1)
        self.assertEqual(len(worker.commands), 1)                           # 没确认就不发
        with patch.object(app, "ask_yes_no", return_value=True):
            dlg.reset()
        self.assertEqual(worker.commands[-1][:2], ("reset", {"v": 2, "type": "reset_settings"}))
        self.feed(("cmd", "reset", {"ok": True, "err": "", "status": None, "scan": None}))
        self.assertIn("恢复出厂", dlg.status_var.get())

    def test_dialog_status_line_tracks_board_and_close_clears_password(self):
        app, dlg, worker = self.open_dialog()
        self.assertIn("未配置", dlg.status_var.get())
        self.feed(("wifi", "status", {"type": "status", "st": "connecting", "ssid": "X"}))
        self.assertIn("连接中", dlg.status_var.get())
        dlg.pass_var.set(SECRET)
        dlg.close()
        self.root.update()
        self.assertEqual(dlg.pass_var.get(), "")
        self.assertEqual(dlg.pass_entry.get() if dlg.top.winfo_exists() else "", "")
        self.assertIsNone(app.wifi_dialog)

    def test_dialog_disables_actions_when_usb_goes_away(self):
        app, dlg, worker = self.open_dialog()
        self.feed(("link", "tcp", "v2"))
        for b in (dlg.scan_btn, dlg.write_btn, dlg.clear_btn, dlg.reset_btn):
            self.assertEqual(str(b.cget("state")), "disabled")
        self.assertIn("已断开", dlg.status_var.get())

    def test_closing_the_app_closes_the_dialog_and_clears_password(self):
        app, dlg, worker = self.open_dialog()
        dlg.pass_var.set(SECRET)
        self.closed = True
        app.on_close()
        self.assertIsNone(app.wifi_dialog)
        self.assertEqual(dlg.pass_var.get(), "")


class AppWeatherTests(AppTestBase):
    RESULTS = [{"place": "上海", "lat": 31.2222, "lon": 121.4581, "label": "上海 · 中国  (31.22, 121.46)"},
               {"place": "Shanghai", "lat": 31.2, "lon": 121.5, "label": "Shanghai"}]

    def test_search_off_tk_thread_pick_saves_and_syncs(self):
        import threading
        threads = []
        app = self.make(geocode_fn=lambda name: threads.append((name, threading.current_thread())) or self.RESULTS)
        app.toggle_connection()
        app.wx_city_var.set("上海")
        app.search_city()
        self.assertTrue(self.pump(lambda: app._wx_results))
        self.assertEqual(threads[0][0], "上海")
        self.assertIsNot(threads[0][1], threading.main_thread())
        self.assertEqual(str(app.wx_search_btn.cget("state")), "normal")
        app.wx_pick_box.current(0)
        app.on_wx_pick()
        saved = self.read()["v2"]["weather"]
        self.assertEqual(saved, {"on": False, "place": "上海", "lat": 31.2222, "lon": 121.4581, "after_min": 10})
        app.wx_on_var.set(True)
        app.on_wx_toggle()
        app.wx_after_var.set("30")
        app.on_wx_after()
        self.assertEqual(self.read()["v2"]["weather"]["after_min"], 30)
        self.assertTrue(self.read()["v2"]["weather"]["on"])
        sent = FakeWorker.instances[0].weather
        self.assertEqual(sent[-1]["after_min"], 30)
        self.assertTrue(sent[-1]["on"])
        self.assertIn("上海", app.wx_place_var.get())

    def test_search_failure_is_quiet(self):
        def boom(name):
            raise OSError("offline")
        app = self.make(geocode_fn=boom)
        app.wx_city_var.set("x")
        app.search_city()
        self.assertTrue(self.pump(lambda: "失败" in app.wx_status_var.get()))
        self.assertEqual(str(app.wx_search_btn.cget("state")), "normal")

    def test_loads_saved_config_and_shows_board_status(self):
        app = self.make(v2={"weather": {"on": True, "place": "北京", "lat": 39.9, "lon": 116.4, "after_min": 5}})
        self.assertTrue(app.wx_on_var.get())
        self.assertEqual(app.wx_after_var.get(), "5")
        self.assertIn("北京", app.wx_place_var.get())
        self.feed(("wx", "unsupported", "unknown_type"))
        self.assertIn("不支持", app.wx_status_var.get())


class AppPreviewTests(AppTestBase):
    """窗口里的屏幕预览：假渲染器子进程，等它时都有超时。"""

    def setUp(self):
        super().setUp()
        p = patch.object(gui, "PREVIEW_DEBOUNCE_MS", 0)
        p.start()
        self.addCleanup(p.stop)
        self.record = Path(self.dir.name) / "record.jsonl"
        self.msgs = dv.build_all(snapshot(), reading(pct=68), NOW)

    def push_all(self):
        self.feed(*[("msg", k, m, datetime(2026, 10, 29, 20, 20)) for k, m in self.msgs.items()])

    def test_without_renderer_shows_build_hint(self):
        app = self.make()
        self.assertTrue(self.pump(lambda: "build.sh" in str(app.preview_label.cget("text"))))
        self.push_all()
        self.assertTrue(self.pump(lambda: "build.sh" in str(app.preview_label.cget("text"))))
        self.assertIsNone(app.preview_ppm)

    def test_no_data_yet_placeholder(self):
        app = self.make(renderer=fake_render_cmd(self.record))
        self.assertTrue(self.pump(lambda: "还没有推送过数据" in str(app.preview_label.cget("text"))))
        self.assertEqual(read_records(self.record), [])          # 没数据不跑渲染器
        self.assertEqual(app._preview_img.width(), 400 * app.preview_scale)

    def test_pushed_messages_are_rendered_and_follow_online_state(self):
        app = self.make(renderer=fake_render_cmd(self.record))
        self.feed(("link", "serial", "v2"))
        self.push_all()
        self.assertTrue(self.pump(lambda: app.preview_ppm is not None, RENDER_WAIT_S))
        rec = read_records(self.record)
        self.assertEqual(len(rec), 1)                              # 五条消息只渲染一次
        self.assertEqual(rec[0]["stdin"].encode("utf-8"), gui.preview_input(self.msgs))
        self.assertNotIn("--offline", rec[0]["args"])
        self.assertEqual(str(app.preview_label.cget("text")), "")
        self.assertEqual(app._preview_img.width(), 400 * app.preview_scale)
        self.assertIn("按固件绘图代码渲染", app.preview_note_var.get())
        # 断开：同样的数据，板子离线时的样子
        self.feed(("link", None, None))
        self.assertTrue(self.pump(lambda: len(read_records(self.record)) == 2 and
                                  app.preview_ppm.endswith(b"\x00\x00\x00"), RENDER_WAIT_S))
        self.assertIn("--offline", read_records(self.record)[1]["args"])
        self.assertIn("离线", app.preview_note_var.get())

    def test_render_failure_shows_reason(self):
        app = self.make(renderer=fake_render_cmd(self.record, "fail"))
        self.push_all()
        self.assertTrue(self.pump(lambda: "渲染失败" in app.preview_note_var.get(), RENDER_WAIT_S))
        self.assertIsNone(app.preview_ppm)

    def test_scale_switch_zooms_and_is_saved(self):
        app = self.make(renderer=fake_render_cmd(self.record), preview_scale=1)
        self.assertEqual(app.preview_scale, 1)
        self.push_all()
        self.assertTrue(self.pump(lambda: app.preview_ppm is not None, RENDER_WAIT_S))
        app.scale_var.set(2)
        app.on_preview_scale()
        self.assertEqual((app._preview_img.width(), app._preview_img.height()), (800, 600))
        self.assertEqual(self.read()["preview_scale"], 2)
        self.assertEqual(len(read_records(self.record)), 1)          # 缩放不用重新渲染

    def test_saved_scale_is_used(self):
        app = self.make(preview_scale=2)
        self.assertEqual((app.preview_scale, app.scale_var.get()), (2, 2))

    def test_summary_tab_marks_headings(self):
        app = self.make()
        self.push_all()
        heads = app.summary.tag_ranges("h")
        self.assertEqual(len(heads), 2 * 5)
        self.assertEqual(app.summary.get(heads[0], heads[1]), "【Token】")


FIXTURES = Path(__file__).resolve().parent / "fixtures"
ADELAIDE = {"on": True, "place": "Adelaide", "lat": -34.9287, "lon": 138.5986, "after_min": 10}


def adelaide_body() -> bytes:
    return (FIXTURES / "open_meteo_adelaide.json").read_bytes()


class WeatherPreviewHelperTests(unittest.TestCase):
    """天气页预览的非界面部分：请求地址、10 分钟缓存、假渲染器、后台线程（不联网）。"""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.record = Path(self.dir.name) / "record.jsonl"

    def test_forecast_url_is_the_one_the_firmware_builds(self):
        src = gui.FIRMWARE_DIR / "src" / "weather_parse.cpp"
        if not src.is_file():
            self.skipTest("没有固件源码")
        text = src.read_text(encoding="utf-8")
        body = text[text.index("bool requestUrl"):]
        fmt = "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', body[:body.index("lat, lon")]))
        self.assertEqual(weather.forecast_url(-34.9287, 138.5986), fmt.replace("%.4f", "{}").format("-34.9287", "138.5986"))

    def test_forecast_cache_is_per_city_and_expires(self):
        urls, now = [], [100.0]

        def fetch(url, timeout):
            urls.append(url)
            return b"{}"

        cache = weather.ForecastCache(fetch, clock=lambda: now[0], ttl=600)
        cache.get(-34.9287, 138.5986)
        cache.get(-34.9287, 138.5986)
        self.assertEqual(len(urls), 1)
        cache.get(31.23, 121.47)                                    # 换城市：重新拉
        self.assertEqual(len(urls), 2)
        cache.get(31.23, 121.47)
        now[0] += 599
        cache.get(31.23, 121.47)
        self.assertEqual(len(urls), 2)
        now[0] += 2                                                 # 超过 10 分钟
        cache.get(31.23, 121.47)
        self.assertEqual(len(urls), 3)
        cache.get(31.23, 121.47, force=True)
        self.assertEqual(len(urls), 4)

    def test_render_weather_feeds_body_and_place(self):
        ppm, note = gui.render_weather(fake_render_cmd(self.record), b'{"a":1}', "上海", RENDER_WAIT_S)
        self.assertEqual((note, ppm_size(ppm)), ("", (400, 300)))
        rec = read_records(self.record)[0]
        self.assertEqual(rec["args"][-2:], ["--weather", "上海"])
        self.assertEqual(rec["stdin"], '{"a":1}')

    def test_render_weather_failures(self):
        ppm, note = gui.render_weather(fake_render_cmd(self.record, "usage"), b"{}", "x", RENDER_WAIT_S)
        self.assertIsNone(ppm)
        self.assertIn("太旧", note)
        self.assertIn("build.sh", note)
        ppm, note = gui.render_weather(fake_render_cmd(self.record, "parse"), b"{}", "x", RENDER_WAIT_S)
        self.assertIsNone(ppm)
        self.assertIn("解析失败", note)
        ppm, note = gui.render_weather(fake_render_cmd(self.record, "fail"), b"{}", "x", RENDER_WAIT_S)
        self.assertIn("退出码 2", note)

    def wait_event(self, q, timeout=RENDER_WAIT_S):
        return q.get(timeout=timeout)

    def test_previewer_fetches_once_per_city_and_reports(self):
        q, urls = queue.Queue(), []

        def fetch(url, timeout):
            urls.append(url)
            return b"{}"

        pv = gui.WeatherPreviewer(fake_render_cmd(self.record), q, fetch=fetch)
        pv.request(ADELAIDE)
        ev = self.wait_event(q)
        self.assertEqual((ev[0], ppm_size(ev[1]), ev[2]), ("wxpreview", (400, 300), ""))
        pv.request(ADELAIDE)
        self.assertIsNotNone(self.wait_event(q)[1])
        self.assertEqual(len(urls), 1)                              # 第二次用缓存
        self.assertIn("latitude=-34.9287&longitude=138.5986", urls[0])
        self.assertEqual(len(read_records(self.record)), 2)

    def test_previewer_no_city_failure_and_missing_renderer(self):
        q = queue.Queue()
        pv = gui.WeatherPreviewer(fake_render_cmd(self.record), q, fetch=lambda u, t: b"{}")
        pv.request(None)
        self.assertEqual(q.get_nowait(), ("wxpreview", None, gui.WX_PREVIEW_NO_CITY))
        pv.request({**ADELAIDE, "lat": None, "lon": None})
        self.assertIn("没有设置城市", q.get_nowait()[2])

        def boom(url, timeout):
            raise OSError("timed out")

        pv = gui.WeatherPreviewer(fake_render_cmd(self.record), q, fetch=boom)
        pv.request(ADELAIDE)
        ev = self.wait_event(q)
        self.assertIsNone(ev[1])
        self.assertIn("拉取天气失败", ev[2])
        self.assertIn("timed out", ev[2])
        self.assertEqual(read_records(self.record), [])             # 没拿到数据就不跑渲染器
        gui.WeatherPreviewer(None, q).request(ADELAIDE)
        self.assertIn("build.sh", q.get_nowait()[2])


class AppPageViewTests(AppTestBase):
    """页面单选框只切换屏幕预览标签（不发给板子、不写 config）；天气页用假 HTTP + 假渲染器。"""

    def setUp(self):
        super().setUp()
        p = patch.object(gui, "PREVIEW_DEBOUNCE_MS", 0)
        p.start()
        self.addCleanup(p.stop)
        self.record = Path(self.dir.name) / "record.jsonl"
        self.urls = []

    def fetch(self, url, timeout):
        self.urls.append(url)
        return adelaide_body()

    def select(self, app, page):
        app.page_var.set(page)
        app.on_page()

    def test_weather_radio_fetches_renders_and_shows_in_preview_tab_without_sending(self):
        app = self.make(renderer=fake_render_cmd(self.record), weather_fetch=self.fetch, v2={"weather": ADELAIDE})
        app.toggle_connection()
        self.feed(("link", "serial", "v2"))
        w = FakeWorker.instances[-1]
        app.data_tabs.select(app.data_tabs.tabs()[1])                # 先停在别的标签
        self.select(app, "weather")
        self.assertEqual(str(app.data_tabs.select()), str(app.preview_tab))
        self.assertTrue(self.pump(lambda: app.wx_ppm is not None, RENDER_WAIT_S))
        self.assertEqual(str(app.preview_label.cget("text")), "")
        self.assertEqual(app._preview_img.width(), 400 * app.preview_scale)
        self.assertIn("天气页", app.preview_note_var.get())
        self.assertEqual(app.page_status_var.get(), "")                # 只是预览：没有状态文字
        self.assertEqual(w.commands, [])                              # 什么也没发
        self.assertFalse(self.path.exists())                          # 也没写 config
        self.assertEqual(len(self.urls), 1)
        self.assertIn("latitude=-34.9287&longitude=138.5986", self.urls[0])
        rec = read_records(self.record)[-1]
        self.assertEqual(rec["args"][-2:], ["--weather", "Adelaide"])
        self.assertEqual(rec["stdin"].encode("utf-8"), adelaide_body())
        self.select(app, "dash")                                      # 切回去再切过来：缓存，不再联网
        self.select(app, "weather")
        self.assertTrue(self.pump(lambda: len(read_records(self.record)) == 2, RENDER_WAIT_S))
        self.assertEqual(len(self.urls), 1)
        app.push_page()                                               # 推送才发 page + refresh_now
        self.assertEqual(w.commands[-1][:2], ("page", [gui.todo_page.page_msg("weather"), gui.todo_page.refresh_now_msg()]))

    def test_dashboard_radios_show_the_live_preview(self):
        msgs = dv.build_all(snapshot(), reading(pct=68), NOW)
        app = self.make(renderer=fake_render_cmd(self.record), weather_fetch=self.fetch, v2={"weather": ADELAIDE})
        self.feed(*[("msg", k, m, datetime(2026, 10, 29, 20, 20)) for k, m in msgs.items()])
        self.assertTrue(self.pump(lambda: app.preview_ppm is not None, RENDER_WAIT_S))
        self.select(app, "weather")
        self.assertTrue(self.pump(lambda: app.wx_ppm is not None, RENDER_WAIT_S))
        self.assertIsNot(app._shown_ppm(), app.preview_ppm)
        for page in ("auto", "dash"):
            self.select(app, page)
            self.assertIs(app._shown_ppm(), app.preview_ppm)
            self.assertIn("按固件绘图代码渲染", app.preview_note_var.get())
        self.assertEqual(len(read_records(self.record)), 2)           # 一次仪表盘，一次天气；切换不重画仪表盘

    def test_dashboard_updates_while_looking_at_weather_do_not_overwrite_it(self):
        app = self.make(renderer=fake_render_cmd(self.record), weather_fetch=self.fetch, v2={"weather": ADELAIDE})
        self.select(app, "weather")
        self.assertTrue(self.pump(lambda: app.wx_ppm is not None, RENDER_WAIT_S))
        shown, note = app._shown_ppm(), app.preview_note_var.get()
        app._on_preview(b"P6\n400 300\n255\n" + b"\xff" * 360000, "")
        self.assertIs(app._shown_ppm(), shown)
        self.assertEqual(app.preview_note_var.get(), note)

    def test_todo_radio_shows_the_rendered_todo_page(self):
        items = [{"text": "写周报", "done": False, "important": True}]
        app = self.make(renderer=fake_render_cmd(self.record), v2={"todo": {"items": items}})
        self.select(app, "todo")
        self.assertTrue(app._todo_view_ppm.startswith(b"P6\n400 300\n"))
        self.assertIs(app._shown_ppm(), app._todo_view_ppm)
        self.assertEqual(str(app.preview_label.cget("text")), "")
        self.assertIn("待办页", app.preview_note_var.get())
        self.assertEqual(read_records(self.record), [])               # 待办页不经过渲染器
        app.scale_var.set(2)
        app.on_preview_scale()
        self.assertEqual(app._preview_img.width(), 800)

    def test_weather_without_city_and_with_failed_fetch_show_text_in_preview_area(self):
        app = self.make(renderer=fake_render_cmd(self.record))
        self.select(app, "weather")
        self.assertTrue(self.pump(lambda: "没有设置城市" in str(app.preview_label.cget("text"))))
        self.assertIsNone(app.wx_ppm)
        self.assertEqual(read_records(self.record), [])

    def test_weather_fetch_failure_text(self):
        app = self.make(renderer=fake_render_cmd(self.record), v2={"weather": ADELAIDE})   # 假 HTTP 抛 OSError
        self.select(app, "weather")
        self.assertTrue(self.pump(lambda: "拉取天气失败" in str(app.preview_label.cget("text")), RENDER_WAIT_S))
        self.assertIn("no network", str(app.preview_label.cget("text")))
        self.assertEqual(read_records(self.record), [])

    def test_weather_without_renderer_shows_build_hint(self):
        app = self.make(weather_fetch=self.fetch, v2={"weather": ADELAIDE})
        self.select(app, "weather")
        self.assertTrue(self.pump(lambda: "build.sh" in str(app.preview_label.cget("text"))))

    def test_old_renderer_without_weather_mode_says_rebuild(self):
        app = self.make(renderer=fake_render_cmd(self.record, "usage"), weather_fetch=self.fetch,
                        v2={"weather": ADELAIDE})
        self.select(app, "weather")
        self.assertTrue(self.pump(lambda: "太旧" in str(app.preview_label.cget("text")), RENDER_WAIT_S))

    def test_picking_another_city_updates_the_weather_preview(self):
        app = self.make(renderer=fake_render_cmd(self.record), weather_fetch=self.fetch, v2={"weather": ADELAIDE})
        self.select(app, "weather")
        self.assertTrue(self.pump(lambda: len(read_records(self.record)) == 1, RENDER_WAIT_S))
        app._save_wx(place="Perth", lat=-31.95, lon=115.86)
        self.assertTrue(self.pump(lambda: len(read_records(self.record)) == 2, RENDER_WAIT_S))
        self.assertEqual(read_records(self.record)[-1]["args"][-1], "Perth")
        self.assertIn("latitude=-31.9500&longitude=115.8600", self.urls[-1])

    def test_saved_page_is_the_board_page_and_the_starting_preview(self):
        items = [{"text": "写周报", "done": False, "important": False}]
        app = self.make(v2={"page": "todo", "todo": {"items": items}})
        self.assertEqual((app.page, app.view_page, app.page_var.get()), ("todo", "todo", "todo"))
        self.assertIs(app._shown_ppm(), app._todo_view_ppm)
        self.assertIsNotNone(app._todo_view_ppm)
        self.assertEqual(str(app.push_page_btn.cget("state")), "disabled")          # 没连板子不能推送


class FakeReg:
    """winreg 的替身：一个 dict 当 Run 键。"""
    HKEY_CURRENT_USER = "HKCU"
    REG_SZ = 1

    def __init__(self, fail=False):
        self.values: dict[str, str] = {}
        self.fail = fail

    class _Key:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def OpenKey(self, root, path):
        return self._Key()

    def CreateKey(self, root, path):
        if self.fail:
            raise PermissionError("denied")
        return self._Key()

    def QueryValueEx(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name], self.REG_SZ

    def SetValueEx(self, key, name, _r, _t, value):
        self.values[name] = value

    def DeleteValue(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        del self.values[name]


class AutostartTests(unittest.TestCase):
    def test_command_uses_pythonw_and_tray_flag(self):
        with tempfile.TemporaryDirectory() as d:
            py = Path(d) / "python.exe"
            py.write_bytes(b"")
            (Path(d) / "pythonw.exe").write_bytes(b"")
            cmd = gui.autostart_command(py, Path(r"C:\x\host\gui.py"))
        self.assertEqual(cmd, f'"{Path(d) / "pythonw.exe"}" "C:\\x\\host\\gui.py" --tray')
        # 没有 pythonw.exe 时照用原来的解释器
        self.assertTrue(gui.autostart_command(Path(r"C:\nope\python.exe"), Path("g.py")).startswith('"C:\\nope\\python.exe"'))

    def test_registry_value_on_and_off(self):
        reg = FakeReg()
        a = gui.RegistryAutostart(reg, command_fn=lambda: "CMD")
        self.assertFalse(a.enabled())
        a.set(True)
        self.assertEqual(reg.values, {gui.AUTOSTART_NAME: "CMD"})
        self.assertTrue(a.enabled())
        a.set(False)
        a.set(False)          # 已经没有了也不报错
        self.assertFalse(a.enabled())


class FakeTray:
    instances: list = []

    def __init__(self, events):
        self.events = events
        self.titles, self.notes, self.stopped = [], [], False
        FakeTray.instances.append(self)

    def set_title(self, text):
        self.titles.append(text)

    def notify(self, text):
        self.notes.append(text)

    def stop(self):
        self.stopped = True


class AppTrayTests(AppTestBase):
    def make_tray(self, autostart=None, start_hidden=False, tray_factory=FakeTray, renderer=None):
        FakeTray.instances = []
        self.app = gui.App(self.root, cfg=dict(host.DEFAULT_CONFIG), config_path=self.path, ports_fn=lambda: [],
                           worker_factory=FakeWorker, discover_fn=lambda: [], renderer=renderer,
                           tray_factory=tray_factory, autostart=autostart, start_hidden=start_hidden)
        self.app._tray_note_dialog = lambda: False        # 真实对话框会 wait_window 阻塞测试
        self.root.update()
        return self.app

    def test_window_close_hides_to_tray_and_keeps_worker(self):
        self.root.deiconify()
        app = self.make_tray()
        self.note_calls = []
        app._tray_note_dialog = lambda: self.note_calls.append(True) or False
        tray = FakeTray.instances[0]
        app.on_window_close()
        self.root.update()
        self.assertEqual(self.root.state(), "withdrawn")
        self.assertFalse(app._closing)              # 只是藏起来，轮询和推送照常
        self.assertEqual(self.note_calls, [True])   # 第一次关窗口：弹一次性提示
        app.on_window_close()                       # 提示只出一次
        self.assertEqual(self.note_calls, [True])
        self.feed(("tray", "show"))
        self.assertEqual(self.root.state(), "normal")
        self.closed = True
        app.events.put(("tray", "quit"))
        app._drain_events()
        self.assertTrue(app._closing)
        self.assertTrue(tray.stopped)

    def test_preview_paused_in_tray_and_rendered_on_show(self):
        self.root.deiconify()
        with patch.object(gui, "PREVIEW_DEBOUNCE_MS", 0):
            record = Path(self.dir.name) / "record.jsonl"
            app = self.make_tray(renderer=fake_render_cmd(record))
            app.on_window_close()
            msgs = dv.build_all(snapshot(), reading(pct=68), NOW)
            self.feed(*[("msg", k, m, datetime(2026, 10, 29, 20, 20)) for k, m in msgs.items()])
            self.pump(lambda: False, 0.5)
            self.assertEqual(read_records(record), [])           # 藏在托盘：不跑渲染器
            self.assertEqual(set(app.msgs), set(msgs))           # 消息照常保留
            self.feed(("tray", "show"))
            self.assertTrue(self.pump(lambda: app.preview_ppm is not None, RENDER_WAIT_S))
            self.assertEqual(len(read_records(record)), 1)       # 显示时补渲染一次
        self.closed = True
        app.events.put(("tray", "quit"))
        app._drain_events()

    def test_status_goes_to_tray_tooltip_and_start_hidden(self):
        self.root.deiconify()
        app = self.make_tray(start_hidden=True)
        self.assertEqual(self.root.state(), "withdrawn")
        app._set_status("v2", "已连接")
        self.assertEqual(FakeTray.instances[0].titles[-1], "墨水屏副屏 · 已连接")

    def test_without_tray_close_quits_as_before(self):
        def broken(_events):
            raise ImportError("no pystray")
        with self.assertLogs(gui.log, "WARNING"):
            app = self.make_tray(tray_factory=broken)
        self.assertIsNone(app.tray)
        self.closed = True
        app.on_window_close()
        self.assertTrue(app._closing)

    def test_autostart_checkbox_writes_and_reverts_on_error(self):
        reg = FakeReg()
        app = self.make_tray(autostart=gui.RegistryAutostart(reg, command_fn=lambda: "CMD"))
        self.assertFalse(app.autostart_var.get())
        app.autostart_var.set(True)
        app.on_autostart()
        self.assertIn(gui.AUTOSTART_NAME, reg.values)
        reg.fail = True
        app.autostart_var.set(False)
        with self.assertLogs(gui.log, "ERROR"):
            app.on_autostart()
        self.assertTrue(app.autostart_var.get())     # 失败：勾选框退回去
        self.assertIn(gui.AUTOSTART_NAME, reg.values)

    def test_refresh_interval_box_saves_and_requests(self):
        app = self.make_tray()
        self.assertEqual(app.refresh_var.get(), "每 3 分钟（最快）")
        self.assertEqual([gui.refresh_minutes(v) for v in app.refresh_box.cget("values")], list(dv.REFRESH_MINUTES))
        app.refresh_var.set(gui.refresh_label(10))
        app.on_refresh()
        self.assertEqual(app.opts["refresh_min"], 10)
        self.assertEqual(self.read()["v2"]["refresh_min"], 10)

    def test_no_autostart_backend_no_checkbox(self):
        app = self.make_tray()
        self.assertFalse(hasattr(app, "autostart_var"))

    def test_tray_note_dont_show_again_is_saved(self):
        self.root.deiconify()
        app = self.make_tray()
        app._tray_note_dialog = lambda: True
        app.on_window_close()
        self.assertTrue(self.read()["tray_note_off"])
        self.assertTrue(app.cfg["tray_note_off"])
        self.assertEqual(self.root.state(), "withdrawn")

    def test_tray_note_skipped_when_turned_off_and_dialog_has_checkbox(self):
        self.root.deiconify()
        calls = []
        FakeTray.instances = []
        app = gui.App(self.root, cfg=dict(host.DEFAULT_CONFIG, tray_note_off=True), config_path=self.path,
                      ports_fn=lambda: [], worker_factory=FakeWorker, discover_fn=lambda: [], renderer=None,
                      tray_factory=FakeTray)
        self.app = app
        app._tray_note_dialog = lambda: calls.append(1) or False
        app.on_window_close()
        self.assertEqual(calls, [])
        self.assertEqual(self.root.state(), "withdrawn")
        top, dont, ok = app._build_tray_note()                 # 对话框本身：「不再提示」默认不勾
        self.assertFalse(dont.get())
        top.destroy()


class AppLayoutTests(AppTestBase):
    """布局 / 首次运行 / 反馈 / 键盘。"""

    def test_two_columns_and_fits_default_window(self):
        app = self.make()
        self.root.update_idletasks()
        self.assertEqual(int(app.left_canvas.master.grid_info()["column"]), 0)            # 左列（可滚动）
        for w in (app.conn_btn, app.page_radios[0], app.todo_tree, app.refresh_box, app.wx_city_entry):
            self.assertTrue(str(w).startswith(str(app.left_canvas)), w)                   # 连接 / 页面 / 待办 / 设置
        rows = [int(f.grid_info()["row"]) for f in (app.conn_btn.master, app.refresh_box.master,
                                                     app.page_radios[0].master.master, app.todo_tree.master.master)]
        self.assertEqual(rows, sorted(rows))                                              # 连接 / 显示设置 / 页面 / 待办
        for w in (app.refresh_box, app.wx_after_spin, app.wx_pick_box, app.addr_box):
            self.assertTrue(w.bind("<MouseWheel>"), w)                                    # 滚轮不改值，改成滚左列
        self.assertLessEqual(self.root.winfo_reqheight(), app.S(gui.DEFAULT_H) + 40)       # 不是 900 像素的单列
        self.assertLessEqual(self.root.winfo_reqwidth(), app.S(gui.DEFAULT_W) + 40)
        self.assertEqual(int(app.data_tabs.master.grid_info()["column"]), 1)               # 右列：预览 + 日志
        self.assertEqual([app.data_tabs.tab(t, "text") for t in app.data_tabs.tabs()], ["屏幕预览", "数据明细"])

    def test_empty_port_list_shows_hint_and_auto_entry(self):
        app = self.make()
        self.assertEqual(app.conn_hint_var.get(), gui.NO_PORT_HINT)
        self.assertEqual(str(app.refresh_btn.cget("text")), "重新扫描")
        self.assertEqual(list(app.port_box["values"])[0], gui.AUTO_PORT)
        self.assertEqual(app.port_var.get(), gui.AUTO_PORT)
        self.assertIsNone(app.selected_port())

    def test_auto_port_passes_none_and_ports_fn_to_worker(self):
        ports = [SimpleNamespace(device="COM7", vid=0x303A, pid=0x1001, description="USB JTAG")]
        app = self.make()
        app._ports_fn = lambda: ports
        app.refresh_ports()
        self.assertEqual(app.conn_hint_var.get(), "")
        values = list(app.port_box["values"])
        self.assertEqual((values[0], len(values)), (gui.AUTO_PORT, 2))
        app.toggle_connection()
        worker = FakeWorker.instances[0]
        self.assertIsNone(worker.port)
        app.toggle_connection()
        self.pump(lambda: app.worker is None)
        app.port_var.set(values[1])
        self.assertEqual(app.selected_port(), "COM7")

    def test_retry_error_keeps_inputs_editable(self):
        app = self.make(port="COM_TEST")
        app.toggle_connection()
        self.assertEqual(str(app.port_box.cget("state")), "disabled")
        self.feed(("status", "error", "无法打开串口 COM_TEST，稍后重试（详见日志）"))
        self.assertEqual(str(app.port_box.cget("state")), "normal")
        self.assertIn("断开", app.conn_hint_var.get())
        self.feed(("status", "connecting", "连接中…"))
        self.assertEqual(str(app.port_box.cget("state")), "disabled")

    def test_silent_event_shows_unresponsive_and_clears(self):
        app = self.make(port="COM_TEST")
        app.toggle_connection()
        self.feed(("link", "serial", "v2"), ("status", "v2", "已连接 v2"))
        self.feed(("silent", True, 120.0))
        self.assertIn("无响应", app.status_var.get())
        self.feed(("silent", False, 3.0))
        self.assertEqual(app.status_var.get(), "已连接 v2")

    def test_fw_reboot_port_and_todo_auto_events(self):
        app = self.make(port="COM_TEST")
        app.toggle_connection()
        self.feed(("link", "serial", "v2"), ("fw", "1.6.0"))
        self.assertEqual(app.fw_var.get(), "固件：1.6.0")
        self.feed(("refreshed", datetime(2026, 10, 29, 23, 48, 5)))
        with self.assertLogs(gui.log, "INFO"):
            self.feed(("reboot",))
        self.assertIsNone(app.last_refresh)
        app._port_map = {"COM9  desc": "COM9"}
        app.port_var.set("COM_TEST")
        self.feed(("port", "COM9"))
        self.assertEqual(app.port_var.get(), "COM9  desc")
        self.feed(("todo_auto", {"ok": True, "err": "", "sent": 5, "total": 5}))
        self.assertIn("自动重新推送", app.todo_status_var.get())

    def test_save_error_is_shown(self):
        self.path.write_text("{ not json", encoding="utf-8")
        app = self.make()
        app.primary_var.set("total")
        app.on_primary()
        self.assertIn("保存设置失败", app.save_error_var.get())
        self.path.write_text("{}", encoding="utf-8")
        app.primary_var.set("today")
        app.on_primary()
        self.assertEqual(app.save_error_var.get(), "")

    def test_weather_minutes_validated_and_city_placeholder_and_saved_label(self):
        app = self.make()
        self.assertEqual(app.wx_city_var.get(), gui.WX_PLACEHOLDER)
        app.search_city()                                              # 占位文字不当城市名
        self.assertIn("先输入城市名", app.wx_status_var.get())
        app.wx_after_var.set("999")
        app.on_wx_after(revert=True)
        self.assertIn("1–240", app.wx_status_var.get())
        self.assertEqual(app.wx_after_var.get(), "10")
        self.assertFalse(self.path.exists())
        app.wx_after_var.set("abc")
        app.on_wx_after()
        self.assertIn("整数", app.wx_status_var.get())
        app._wx_results = [{"place": "上海", "lat": 31.2, "lon": 121.5, "label": "上海"}]
        app.wx_pick_box["values"] = ["上海"]
        app.wx_pick_box.current(0)
        app.on_wx_pick()
        self.assertTrue(app.wx_place_var.get().startswith("已保存：上海"))

    def test_refresh_data_rebuilds_preview_data_without_sending(self):
        fixture = Path(__file__).with_name("fixtures") / "hub-devices.synthetic.json"
        tm = Path(self.dir.name) / "hub-devices.json"
        tm.write_bytes(fixture.read_bytes())
        app = self.make(v2={"tm_file": str(tm)})
        app.toggle_connection()
        self.feed(("link", "serial", "v2"))
        w = FakeWorker.instances[-1]
        app.msgs["mouse"] = {"v": 2, "type": "mouse", "st": "ok", "pct": 57}
        app.refresh_data()
        self.assertEqual(str(app.refresh_data_btn.cget("state")), "disabled")
        self.assertTrue(self.pump(lambda: "heat" in app.msgs))
        self.assertEqual(set(app.msgs), set(dv.KINDS))
        self.assertEqual(app.msgs["mouse"]["pct"], 57)                     # 鼠标沿用最近一次推送的
        self.assertEqual(str(app.refresh_data_btn.cget("state")), "normal")
        self.assertEqual(app.push_var.get(), "上次推送 --:--:--")          # 不算一次推送
        self.assertEqual(w.commands, [])                                   # 什么也没发给板子

    def test_log_menu_copy_and_clear(self):
        app = self.make()
        app.log_handler.queue.put((logging.INFO, "第一行日志"))
        app._drain_log()
        self.assertIn("第一行日志", app.log_text.get("1.0", "end"))
        app.log_copy()
        self.assertIn("第一行日志", self.root.clipboard_get())
        app.log_clear()
        self.assertEqual(app.log_text.get("1.0", "end-1c"), "")

    def test_text_widgets_use_theme_colours(self):
        app = self.make()
        self.assertEqual(str(app.log_text.cget("background")), app.colors["field"])
        self.assertEqual(str(app.log_text.cget("foreground")), app.colors["fg"])
        self.assertNotIn("#fbfbfb", (str(app.log_text.cget("background")).lower(), str(app.summary.cget("background")).lower()))

    def test_dark_mode_follows_system_and_native_fallback_still_builds(self):
        try:
            import sv_ttk  # noqa: F401
        except ImportError:
            self.skipTest("没装 sv-ttk")
        with patch.object(gui, "system_dark", return_value=True):
            app = self.make()
        self.assertEqual((app.theme, app.dark), ("dark", True))
        self.assertEqual(app.colors["bg"], "#1c1c1c")
        self.assertEqual(app.colors["err"], "#ff6b6b")
        self.closed = True
        app.on_close()

    def test_without_sv_ttk_falls_back_to_native_theme(self):
        with patch.object(gui, "apply_theme", return_value=""):
            app = self.make()
        self.assertEqual(app.theme, "")
        self.assertTrue(app.colors["fg"])
        self.assertEqual(app.conn_btn.winfo_manager(), "grid")

    def test_wrap_lengths_follow_layout_not_fixed_pixels(self):
        app = self.make()
        self.root.deiconify()
        self.root.geometry("1300x700")
        self.pump(lambda: False, 0.3)
        wide = int(str(app.conn_hint.cget("wraplength")))
        self.root.geometry("900x700")
        self.pump(lambda: False, 0.3)
        narrow = int(str(app.conn_hint.cget("wraplength")))
        self.assertLessEqual(narrow, wide)
        self.assertGreaterEqual(narrow, app.S(240))

    def test_wifi_dialog_keys_grab_and_centre(self):
        app = self.make(port="COM_TEST")
        app.toggle_connection()
        self.feed(("link", "serial", "v2"), ("wifi", "status", {"type": "status", "st": "off"}))
        app.open_wifi_dialog()
        dlg = app.wifi_dialog
        self.root.update()
        dlg.ssid_var.set("Home-2.4G")
        self.assertTrue(dlg.top.bind("<Return>"))
        self.assertTrue(dlg.top.bind("<Escape>"))
        dlg._on_return()                                   # Return = 写入
        self.assertEqual(FakeWorker.instances[0].commands[-1][0], "set")
        self.feed(("cmd", "set", {"ok": True, "err": "", "status": None, "scan": None}))
        dlg.close()                                        # Esc 绑定的就是 close
        self.assertIsNone(app.wifi_dialog)

    def test_wifi_dialog_is_modal_once_visible(self):
        self.root.deiconify()
        app = self.make(port="COM_TEST")
        app.toggle_connection()
        self.feed(("link", "serial", "v2"), ("wifi", "status", {"type": "status", "st": "off"}))
        app.open_wifi_dialog()
        dlg = app.wifi_dialog
        self.pump(lambda: self.root.grab_current() is not None, 1.0)
        self.assertEqual(self.root.grab_current(), dlg.top)
        dlg.close()


if __name__ == "__main__":
    unittest.main()
