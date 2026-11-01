"""离线天气：设置规整、城市搜索（假 HTTP）、wx_cfg 同步（假 link / 假串口）、GUI 工作线程。
不联网、不开串口。"""
import json
import logging
import queue
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import epaper_host as host
import gui
import weather
from test_dashboard_v2 import NOW, FakeMouse, FakeTm, V2Serial, reading, snapshot
from test_gui import READY_V2, drain_until

GEO_BODY = {"results": [
    {"id": 1796236, "name": "上海", "latitude": 31.22222, "longitude": 121.45806, "country": "中国", "admin1": "上海市"},
    {"name": "Shanghai", "latitude": 31.2, "longitude": 121.5, "country": "China", "admin1": "Shanghai"},
    {"name": "bad", "latitude": 99, "longitude": 0},
    {"name": "bad2", "latitude": "x", "longitude": 0},
]}
SH = {"on": True, "place": "上海", "lat": 31.2222, "lon": 121.4581, "after_min": 10}


class OptionsTests(unittest.TestCase):
    def test_missing_section_is_none(self):
        self.assertIsNone(weather.weather_options({}))
        self.assertIsNone(weather.weather_options({"v2": None}))
        self.assertIsNone(weather.weather_options({"v2": {"weather": "x"}}))
        self.assertIsNone(weather.wx_cfg_msg(None))

    def test_full_config_message(self):
        o = weather.weather_options({"v2": {"weather": {"on": True, "place": "上海", "lat": 31.23, "lon": 121.47,
                                                        "after_min": 10}}})
        self.assertEqual(weather.wx_cfg_msg(o), {"v": 2, "type": "wx_cfg", "on": True, "place": "上海",
                                                 "lat": 31.23, "lon": 121.47, "after_min": 10})

    def test_off_or_no_location_sends_only_off(self):
        off = {"v": 2, "type": "wx_cfg", "on": False}
        self.assertEqual(weather.wx_cfg_msg(weather.weather_options(
            {"v2": {"weather": {"on": False, "place": "x", "lat": 1, "lon": 2}}})), off)
        self.assertEqual(weather.wx_cfg_msg(weather.weather_options({"v2": {"weather": {"on": True}}})), off)
        self.assertEqual(weather.wx_cfg_msg(weather.weather_options(
            {"v2": {"weather": {"on": True, "lat": 95, "lon": 0}}})), off)

    def test_after_min_clamped_and_place_truncated(self):
        self.assertEqual(weather.clamp_after(0), 1)
        self.assertEqual(weather.clamp_after(999), 240)
        self.assertEqual(weather.clamp_after("abc"), 10)
        place = weather.truncate_place("乌鲁木齐乌鲁木齐乌鲁木齐")
        self.assertLessEqual(len(place.encode("utf-8")), 23)
        self.assertEqual(place, "乌鲁木齐乌鲁木")        # 7 个字 = 21 字节（第 8 个字会到 24）


class GeocodeTests(unittest.TestCase):
    def test_url_has_expected_query(self):
        url = weather.geocode_url("上海")
        self.assertTrue(url.startswith("https://geocoding-api.open-meteo.com/v1/search?"))
        for part in ("count=5", "language=zh", "format=json", "name=%E4%B8%8A%E6%B5%B7"):
            self.assertIn(part, url)

    def test_search_parses_and_filters(self):
        seen = []

        def fetch(url, timeout):
            seen.append((url, timeout))
            if "/v1/get?" in url:
                return json.dumps({"id": 1796236, "name": "Shanghai"}).encode()
            return json.dumps(GEO_BODY).encode()
        res = weather.search_city(" 上海 ", fetch=fetch)
        # 中文名换成英文名给板子（板子字库缺字），标签仍是中文
        self.assertEqual([r["place"] for r in res], ["Shanghai", "Shanghai"])
        self.assertEqual((res[0]["lat"], res[0]["lon"]), (31.2222, 121.4581))
        self.assertIn("上海 · 上海市 · 中国", res[0]["label"])
        self.assertEqual(len(seen), 2)

    def test_search_keeps_chinese_name_when_lookup_fails(self):
        def fetch(url, timeout):
            if "/v1/get?" in url:
                raise OSError("offline")
            return json.dumps(GEO_BODY).encode()
        self.assertEqual(weather.search_city("上海", fetch=fetch)[0]["place"], "上海")

    def test_no_results_key_and_blank_name(self):
        self.assertEqual(weather.search_city("zzzz", fetch=lambda u, t: b'{"generationtime_ms":0.3}'), [])
        self.assertEqual(weather.search_city("  ", fetch=lambda u, t: self.fail("no request")), [])

    def test_failures_raise(self):
        with self.assertRaises(ValueError):
            weather.search_city("x", fetch=lambda u, t: b"<html>")
        with self.assertRaises(OSError):
            weather.search_city("x", fetch=lambda u, t: (_ for _ in ()).throw(OSError("offline")))

    def test_default_fetch_is_mockable(self):
        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps(GEO_BODY).encode()
        with patch("urllib.request.urlopen", return_value=Resp()) as m:
            self.assertEqual(len(weather.search_city("上海")), 2)
        self.assertIn("geocoding-api.open-meteo.com", m.call_args[0][0].full_url)


class FakeLink:
    """只实现 WxSync 用到的：opened_at / command / wx_status。replies 里按顺序给每条命令的结果。"""

    def __init__(self, replies):
        self.opened_at = 1.0
        self.wx_status = None
        self.sent = []
        self.replies = list(replies)

    def command(self, msg, timeout=5.0):
        self.sent.append(msg)
        res = self.replies.pop(0) if self.replies else host.CommandResult(True, "")
        if msg["type"] == "wx_status" and res.ok:
            self.wx_status = {"type": "status", "on": True, "place": "上海", "mode": "dash", "last": None,
                              "err": None, "after_min": 10}
        return res


OK = host.CommandResult(True, "")


def err(reason):
    return host.CommandResult(False, reason)


class SyncTests(unittest.TestCase):
    def make(self, opts=SH):
        self.opts = dict(opts) if opts else None
        self.events = []
        self.sync = weather.WxSync(lambda: self.opts, lambda k, d: self.events.append((k, d)))

    def test_sends_once_then_status_and_not_again(self):
        self.make()
        link = FakeLink([OK, OK])
        self.assertTrue(self.sync.sync(link))
        self.assertEqual([m["type"] for m in link.sent], ["wx_cfg", "wx_status"])
        self.assertEqual(link.sent[0], {"v": 2, "type": "wx_cfg", "on": True, "place": "上海", "lat": 31.2222,
                                        "lon": 121.4581, "after_min": 10})
        self.assertEqual(self.events[0][0], "status")
        for _ in range(3):
            self.assertFalse(self.sync.sync(link))
        self.assertEqual(len(link.sent), 2)

    def test_resends_on_reconnect_and_on_change(self):
        self.make()
        link = FakeLink([])
        self.sync.sync(link)
        link.opened_at = 2.0                                  # 重连
        self.assertTrue(self.sync.sync(link))
        self.opts["after_min"] = 20                           # 设置改了
        self.assertTrue(self.sync.sync(link))
        self.assertEqual([m["type"] for m in link.sent].count("wx_cfg"), 3)
        self.assertEqual(link.sent[-2]["after_min"], 20)

    def test_unconfigured_sends_nothing(self):
        self.make(None)
        link = FakeLink([])
        self.assertFalse(self.sync.sync(link))
        self.assertEqual(link.sent, [])

    def test_old_firmware_err_logged_once_no_retry(self):
        self.make()
        link = FakeLink([err("unknown_type")])
        with self.assertLogs("epaper_host.weather", level="INFO") as cm:
            for _ in range(4):
                self.sync.sync(link)
            link.opened_at = 2.0                              # 重连后再试一次，日志不再重复
            link.replies.append(err("unknown_type"))
            self.sync.sync(link)
        self.assertEqual([m["type"] for m in link.sent], ["wx_cfg", "wx_cfg"])
        self.assertEqual(len(cm.records), 1)
        self.assertEqual(cm.records[0].levelno, logging.INFO)
        self.assertEqual(self.events[0][0], "unsupported")

    def test_other_err_reported_without_retry(self):
        self.make()
        link = FakeLink([err("bad_lat")])
        self.sync.sync(link)
        self.sync.sync(link)
        self.assertEqual(len(link.sent), 1)
        self.assertEqual(self.events, [("error", "bad_lat")])

    def test_timeout_retries_once_then_gives_up(self):
        self.make()
        link = FakeLink([err("timeout"), err("timeout")])
        for _ in range(5):
            self.sync.sync(link)
        self.assertEqual(len(link.sent), 2)

    def test_listener_exception_is_swallowed(self):
        self.opts = dict(SH)
        sync = weather.WxSync(lambda: self.opts, lambda k, d: 1 / 0)
        with self.assertLogs("epaper_host.weather", level="WARNING"):
            sync.sync(FakeLink([OK, OK]))


class WxLineTests(unittest.TestCase):
    def test_parse_and_format(self):
        st = weather.parse_wx_status('{"type":"status","on":true,"place":"上海","lat":31.2,"lon":121.5,'
                                     '"after_min":10,"mode":"weather","last":"11-02 09:30","err":null}')
        text = weather.format_wx_status(st)
        for bit in ("上海", "10", "天气页", "11-02 09:30"):
            self.assertIn(bit, text)
        self.assertIsNone(weather.parse_wx_status("{nope"))
        self.assertIn("不支持", weather.format_wx_status({"unsupported": True}))
        self.assertIn("关闭", weather.format_wx_status({"on": False}))


class WxSerial(V2Serial):
    """V2Serial + 认识 wx_cfg / wx_status（known=False 时像旧固件：ERR unknown_type）。"""

    def __init__(self, known=True, **kw):
        super().__init__(**kw)
        self.known = known

    def write(self, data):
        msg = json.loads(data)
        if msg.get("type") in ("wx_cfg", "wx_status"):
            self.writes.append(msg)
            if not self.known:
                self.rx.put(b"ERR unknown_type\n")
            else:
                if msg["type"] == "wx_status":
                    self.rx.put(b'WX {"type":"status","on":true,"place":"\xe4\xb8\x8a\xe6\xb5\xb7","lat":31.2,'
                                b'"lon":121.5,"after_min":10,"mode":"dash","last":null,"err":null}\n')
                self.rx.put(b"OK\n")
            return len(data)
        return super().write(data)


class LinkWxTests(unittest.TestCase):
    def test_wx_line_is_parsed_not_flagged_unrecognized(self):
        ser = WxSerial(ready=READY_V2)
        with patch.object(host, "serial", SimpleNamespace(Serial=lambda: ser, SerialException=OSError)):
            link = host.Link("COM_TEST", 115200, 120, 180)
            self.addCleanup(link.close)
            link.connect()
            link.wait_handshake(1)
            deadline = time.monotonic() + 2
            while link.ok_count < 2 and time.monotonic() < deadline:   # 握手 ping 的 OK 先到齐，免得被当成回复
                time.sleep(0.01)
            res = link.command({"v": 2, "type": "wx_status"}, 2)
        self.assertTrue(res.ok)
        self.assertEqual(link.wx_status["place"], "上海")


class WorkerWeatherTests(unittest.TestCase):
    def setUp(self):
        self.events = queue.Queue()
        self.cfg = dict(host.DEFAULT_CONFIG, interval=1, baud=115200,
                        v2={"weather": dict(SH)})

    def start(self, ser):
        p = patch.object(host, "serial", SimpleNamespace(Serial=lambda: ser, SerialException=OSError))
        p.start()
        self.addCleanup(p.stop)
        opts = host.v2_options(self.cfg)
        opts["mouse"] = False
        self.w = gui.HostWorker(self.cfg, "COM_TEST", self.events, tm_factory=lambda _p: FakeTm(snapshot()),
                                mouse_factory=lambda: FakeMouse(reading()), now_fn=lambda: NOW, options=opts)
        self.addCleanup(lambda: (self.w.stop(), self.w.join(10)))
        self.w.start()

    def test_cfg_sent_on_connect_with_status_event(self):
        ser = WxSerial(ready=READY_V2)
        self.start(ser)
        evs = drain_until(self.events, lambda ev: any(e[0] == "wx" for e in ev))
        wx = [e for e in evs if e[0] == "wx"]
        self.assertEqual(wx[0][1], "status")
        self.assertEqual(wx[0][2]["place"], "上海")
        cfgs = [m for m in ser.writes if m.get("type") == "wx_cfg"]
        self.assertEqual(len(cfgs), 1)
        self.assertEqual(cfgs[0]["lat"], 31.2222)

    def test_old_firmware_is_quiet_and_not_retried(self):
        ser = WxSerial(known=False, ready=READY_V2)
        self.start(ser)
        evs = drain_until(self.events, lambda ev: any(e[0] == "wx" for e in ev))
        self.assertEqual([e[1] for e in evs if e[0] == "wx"], ["unsupported"])
        drain_until(self.events, lambda ev: sum(e[0] == "msg" for e in ev) >= 5)   # 数据照常推送
        time.sleep(1.5)                                                            # 再过一个周期
        self.assertEqual(len([m for m in ser.writes if m.get("type") == "wx_cfg"]), 1)

    def test_request_weather_resends(self):
        ser = WxSerial(ready=READY_V2)
        self.start(ser)
        drain_until(self.events, lambda ev: any(e[0] == "wx" for e in ev))
        self.w.request_weather(weather.weather_options({"v2": {"weather": {**SH, "after_min": 30}}}))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and len([m for m in ser.writes if m.get("type") == "wx_cfg"]) < 2:
            time.sleep(0.05)
        cfgs = [m for m in ser.writes if m.get("type") == "wx_cfg"]
        self.assertEqual([c["after_min"] for c in cfgs], [10, 30])


if __name__ == "__main__":
    unittest.main()
