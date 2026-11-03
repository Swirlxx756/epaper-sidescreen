"""仪表盘 v2：消息构造（纯函数）+ 主机接入（协商 / 节奏 / 去重）测试。全部用合成 fixture 和模拟串口。"""
import contextlib
import copy
import io
import json
import re
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dashboard_v2 as dv
import epaper_host as host
import tm_source
from test_host import FakeSerial

real_sleep = time.sleep
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "hub-devices.synthetic.json"
ADELAIDE = timezone(timedelta(hours=10, minutes=30))
NOW = datetime(2026, 10, 28, 19, 40, tzinfo=ADELAIDE)   # = 09:10 UTC，晚于 fixture 里所有 updatedAt


def raw():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def snapshot(now=NOW, mutate=None):
    data = raw()
    if mutate:
        mutate(data)
    return tm_source.build_snapshot(data, now)


def reading(pct=68, charging=False, age=60, status="ok", now=NOW):
    return SimpleNamespace(pct=pct, charging=charging, observed_at=now.timestamp() - age, status=status)


def wire(msg):
    return host.encode(msg)


class FormatTests(unittest.TestCase):
    def test_tokens_three_significant_digits(self):
        cases = {0: "0", 512: "512", 999: "999", 1000: "1.00K", 9996: "10.0K", 45_200: "45.2K",
                 999_600: "1.00M", 969_000_000: "969M", 7_130_000_000: "7.13B", 1_190_000_000: "1.19B",
                 999_960_000_000: "1.00T", 12_345_678_901_234: "12.3T"}
        for n, text in cases.items():
            with self.subTest(n=n):
                self.assertEqual(dv.fmt_tokens(n), text)

    def test_cost(self):
        self.assertEqual(dv.fmt_cost(2292.7), "$2,292.70")
        self.assertEqual(dv.fmt_cost(0), "$0.00")
        self.assertEqual(dv.fmt_cost(311.234), "$311.23")

    def test_countdown(self):
        cases = {117 * 60: "1h57m", (5 * 24 + 8) * 3600: "5d8h", 42 * 60: "42m", 0: "0m", -5: "0m",
                 59: "0m", 3600 + 5 * 60: "1h05m", 24 * 3600: "1d0h", 23 * 3600 + 59 * 60: "23h59m"}
        for sec, text in cases.items():
            with self.subTest(sec=sec):
                self.assertEqual(dv.fmt_countdown(sec), text)


class DashTests(unittest.TestCase):
    def test_exact_message_today_primary(self):
        self.assertEqual(dv.build_dash(snapshot(), NOW), {
            "v": 2, "type": "dash", "st": "ok", "time": "10-28 19:39",   # time = 设备 updatedAt 的本地时间
            "main": {"l": "TODAY", "tok": "1,130,000,000", "cost": "$311.50"},
            "alt": {"l": "TOTAL", "tok": "6.50B"}})

    def test_total_primary_swaps_main_and_alt(self):
        msg = dv.build_dash(snapshot(), NOW, "total")
        self.assertEqual(msg["main"], {"l": "TOTAL", "tok": "6,500,000,000", "cost": "$2,100.00"})
        self.assertEqual(msg["alt"], {"l": "TODAY", "tok": "1.13B"})
        self.assertNotIn("cost", msg["alt"])

    def test_time_is_newest_device_update_not_the_clock(self):
        msg = dv.build_dash(snapshot(), NOW + timedelta(minutes=7))
        self.assertEqual(msg["time"], "10-28 19:39")

    def test_freshness_thresholds(self):
        snap = snapshot()
        obs = snap.observed_at
        cases = [(timedelta(minutes=15), "ok"), (timedelta(minutes=15, seconds=1), "stale"),
                 (timedelta(hours=2), "stale"), (timedelta(hours=2, seconds=1), "na")]
        for delta, st in cases:
            with self.subTest(delta=delta):
                msg = dv.build_dash(snap, obs + delta)
                self.assertEqual(msg["st"], st)
                self.assertEqual("main" in msg and "alt" in msg, st != "na")  # na 不画旧值
                self.assertIn("time", msg)

    def test_no_data_is_na(self):
        self.assertEqual(dv.build_dash(None, NOW), {"v": 2, "type": "dash", "st": "na"})

    def test_naive_now_is_taken_as_local(self):
        snap = snapshot()
        msg = dv.build_dash(snap, NOW.replace(tzinfo=None))
        self.assertEqual(msg["time"], snap.observed_at.astimezone().strftime("%m-%d %H:%M"))


class QuotaTests(unittest.TestCase):
    def test_exact_message(self):
        self.assertEqual(dv.build_quota(snapshot(), NOW), {"v": 2, "type": "quota", "st": "ok", "p": [
            {"n": "Claude", "plan": "Max 5x", "i": [
                {"l": "5h", "left": 60, "rst": "1h57m"}, {"l": "Wk", "left": 63, "rst": "5d8h"}],
             "txt": [{"s": "Fable 94%"}]},
            {"n": "Codex", "plan": "Plus", "i": [
                {"l": "5h", "left": 53, "rst": "2h46m"}, {"l": "Wk", "left": 93, "rst": "6d21h"}]},
            {"n": "Antigrav", "plan": "Pro",
             "i": [{"l": "5h", "left": 100, "rst": "4h59m"}, {"l": "Wk", "left": 97, "rst": "1d21h"}],
             "txt": [{"s": "4 Argon 0%", "w": 1}, {"s": "C/G 72%/64%"}]},
            {"n": "DeepSeek", "plan": "PAYG", "bal": "¥38.66", "txt": [{"s": "Today ¥7.42"}]}]})

    def antigravity(self, windows, now=NOW):
        def mutate(data):
            ag = next(x for x in data["devices"]["dev-a"]["limits"]["providers"] if x["provider"] == "antigravity")
            ag["windows"] = windows
        return dv.build_quota(snapshot(mutate=mutate), now)["p"][2]

    def test_text_segments_missing_values_and_warning_flag(self):
        entry = self.antigravity([
            {"kind": "weekly", "label": "Claude/GPT weekly", "usedPercent": 91.4},   # 剩 8.6 -> 9 -> 告警
            {"kind": "session", "label": "Gemini 5-hour", "usedPercent": 10}])
        self.assertEqual(entry["txt"], [{"s": "C/G --/9%", "w": 1}])
        self.assertEqual(entry["i"], [{"l": "5h", "left": 90}])      # 没有 resetsAt：不写 rst
        edge = self.antigravity([{"kind": "session", "label": "Claude/GPT 5-hour", "usedPercent": 90},
                                 {"kind": "weekly", "label": "Claude/GPT weekly", "usedPercent": 50}])
        self.assertEqual(edge["txt"], [{"s": "C/G 10%/50%", "w": 1}])   # 剩 10 也算告警
        self.assertNotIn("i", edge)
        ok = self.antigravity([{"kind": "weekly", "label": "Gemini 4 Argon weekly", "usedPercent": 89}])
        self.assertEqual(ok["txt"], [{"s": "4 Argon 11%"}])             # 剩 11：不告警

    def test_text_segment_with_passed_reset_shows_dashes(self):
        entry = self.antigravity([   # Argon 周窗口的重置时刻（09:05）已过，Claude/GPT 的还没到
            {"kind": "weekly", "label": "Gemini 4 Argon weekly", "usedPercent": 100, "resetsAt": "2026-10-28T09:05:00.000Z"},
            {"kind": "weekly", "label": "Claude/GPT weekly", "usedPercent": 50, "resetsAt": "2026-10-28T12:00:00.000Z"}])
        self.assertEqual(entry["txt"], [{"s": "4 Argon --"}, {"s": "C/G --/50%"}])

    def test_reset_time_passed_means_unknown_not_full(self):
        snap = snapshot()
        later = datetime(2026, 10, 28, 11, 8, tzinfo=timezone.utc)  # Claude 5h 在 11:07 已重置，没有新读数
        msg = dv.build_quota(snap, later)
        claude = msg["p"][0]
        self.assertEqual(claude["i"][0], {"l": "5h", "left": None})
        self.assertEqual(claude["i"][1]["left"], 63)
        self.assertEqual(len(claude["i"]), 2)
        self.assertEqual(msg["st"], "stale")           # 设备数据已 2 小时不到：过期
        self.assertEqual(claude["st"], "stale")        # 该家读数 09:09:50，距今 1h58m
        self.assertEqual(msg["p"][3]["st"], "stale")   # DeepSeek 读数 09:09:00，距今 1h59m

    def test_quota_na_when_hub_data_is_old(self):
        snap = snapshot()
        msg = dv.build_quota(snap, snap.observed_at + timedelta(hours=3))
        self.assertEqual(msg, {"v": 2, "type": "quota", "st": "na"})

    def test_provider_flagged_stale_or_unavailable(self):
        def mutate(data):
            for p in data["devices"]["dev-a"]["limits"]["providers"]:
                if p["provider"] == "codex":
                    p["stale"] = True
            for dev in data["devices"].values():
                for p in dev["limits"]["providers"]:
                    if p["provider"] == "antigravity":
                        p["status"] = "unavailable"
        msg = dv.build_quota(snapshot(mutate=mutate), NOW)
        by_name = {p["n"]: p for p in msg["p"]}
        self.assertEqual(by_name["Codex"]["st"], "stale")
        self.assertEqual(by_name["Codex"]["i"][0]["left"], 53)   # 过期但仍显示旧值
        self.assertEqual(by_name["Antigrav"], {"n": "Antigrav", "st": "na"})  # 取不到：不画数

    def test_placeholder_provider_has_no_plan_key_when_empty(self):
        msg = dv.build_quota(snapshot(mutate=lambda d: d["devices"]["dev-a"]["limits"]["providers"][1].update(accountLabel="")), NOW)
        self.assertNotIn("plan", msg["p"][1])


class ModelsTests(unittest.TestCase):
    def test_today_scope_uses_today_models_and_today_total(self):
        self.assertEqual(dv.build_models(snapshot(), NOW), {"v": 2, "type": "models", "st": "ok", "scope": "TODAY", "m": [
            ["claude-opus-5-5", "730M", 65], ["claude-sonnet-5-5", "300M", 27], ["deepseek-flash", "100M", 9]]})

    def test_total_scope_uses_all_time(self):
        self.assertEqual(dv.build_models(snapshot(), NOW, "total"), {"v": 2, "type": "models", "st": "ok", "scope": "TOTAL", "m": [
            ["claude-opus-5-5", "4.20B", 65], ["gpt-6.1-sol", "1.00B", 15], ["claude-sonnet-5-5", "500M", 8],
            ["deepseek-flash", "300M", 5], ["gpt-5.6-sol", "300M", 5]]})

    def test_na_has_no_rows(self):
        self.assertEqual(dv.build_models(None, NOW), {"v": 2, "type": "models", "st": "na", "scope": "TODAY"})
        self.assertEqual(dv.build_models(None, NOW, "total")["scope"], "TOTAL")


class HeatTests(unittest.TestCase):
    def setUp(self):
        self.msg = dv.build_heat(snapshot(), NOW)

    def test_range_rows_and_lengths(self):
        m = self.msg
        # 10-28 是周三：15 周 = 最早一周的周日 07-19 起 14*7 + 4 = 102 天
        self.assertEqual((m["start"], m["end"], m["rows"]), ("2026-07-19", "2026-10-28", 7))
        self.assertEqual((len(m["d"]), len(m["t"])), (102, 102))
        self.assertEqual(datetime.fromisoformat(m["start"]).weekday(), 6)
        self.assertEqual((m["peak"], m["active"]), ("1.13B", 6))
        self.assertNotIn("active30", m)

    def test_rows_option_allows_5_or_7_only(self):
        self.assertEqual(dv.build_heat(snapshot(), NOW, 5)["rows"], 5)
        self.assertEqual(dv.build_heat(snapshot(), NOW, 6)["rows"], 7)
        self.assertEqual(dv.build_heat(None, NOW, 5)["rows"], 5)

    def test_window_length_is_whole_weeks_ending_today_for_every_weekday(self):
        for offset in range(7):                      # 2026-10-26 周一 .. 10-32 = 11-01 周日
            today = datetime(2026, 10, 26, 12, tzinfo=ADELAIDE) + timedelta(days=offset)
            for weeks in (4, 15, 26):
                with self.subTest(today=today.date(), weeks=weeks):
                    snap = tm_source.TmSnapshot(observed_at=today.astimezone(timezone.utc))
                    m = dv.build_heat(snap, today, 7, weeks)
                    self.assertEqual(len(m["d"]), (weeks - 1) * 7 + (offset + 1) % 7 + 1)   # 周日 = 本周第 1 天
                    self.assertEqual(datetime.fromisoformat(m["start"]).weekday(), 6)
                    self.assertEqual(m["end"], today.date().isoformat())

    def test_weeks_option_changes_the_window(self):
        m = dv.build_heat(snapshot(), NOW, 7, 4)     # 周三：3*7 + 4 = 25 天，从 10-04（周日）起
        self.assertEqual((m["start"], len(m["d"])), ("2026-10-04", 25))
        self.assertEqual(m["d"][:16], "-" * 16)      # 10-04..10-19 还没有记录
        self.assertEqual(m["d"][16:], "23460006C")

    def test_density_levels_unknown_and_zero_days(self):
        d = self.msg["d"]
        # 10-20 起有记录；10-24..26 所有设备都已在记录却没数据 = 真 0；10-20 之前没记录 = 未知
        self.assertEqual(d[:93], "-" * 93)
        self.assertEqual(d[93:], "23460006C")   # 10-20..10-28：ceil(v/1B*12)，封顶 12，10–12 档写作 A–C
        self.assertEqual(set(d), set("-02346C"))

    def test_trend_heights_are_base64_chars_relative_to_peak(self):
        t = self.msg["t"]
        self.assertEqual(t[:93], "-" * 93)
        self.assertEqual(t[93:], "GLRcAAAc/")    # 100M->6, 200M->11, 300M->17, 500M->28, 0->0, 1.13B->63

    def test_boundaries_exactly_on_a_level_edge(self):
        snap = tm_source.TmSnapshot(observed_at=NOW.astimezone(timezone.utc), daily_known_from="2026-10-22",
                                    daily={"2026-10-22": 1, "2026-10-23": 250_000_000, "2026-10-24": 250_000_001,
                                           "2026-10-25": 916_666_666, "2026-10-26": 1_000_000_000,
                                           "2026-10-27": 3_000_000_000})
        d = dv.build_heat(snap, NOW)["d"]
        # 10-22..28：刻度固定 1B（每档 1/12 = 83.3M）；正好 3/12 = 250M 属于第 3 档，多 1 就是第 4 档；
        # 1B 及以上都是第 12 档；10-28 没记录但各设备都已在记录 = 0
        self.assertEqual(d[-7:], "134BCC0")

    def test_levels_use_the_fixed_scale_not_the_window_peak(self):
        snap = tm_source.TmSnapshot(observed_at=NOW.astimezone(timezone.utc), daily_known_from="2026-10-27",
                                    daily={"2026-10-27": 100_000_000, "2026-10-28": 200_000_000})
        m = dv.build_heat(snap, NOW)
        self.assertEqual(m["d"][-2:], "23")                       # 峰值只有 200M，也不会画成最深
        self.assertEqual(m["peak"], "200M")

    def test_peak_and_active_cover_the_whole_window(self):
        snap = tm_source.TmSnapshot(observed_at=NOW.astimezone(timezone.utc), daily_known_from="2026-07-19",
                                    daily={"2026-07-19": 5, "2026-09-10": 1, "2026-10-28": 10})
        m = dv.build_heat(snap, NOW)
        self.assertEqual((m["d"][0], m["d"][-1]), ("1", "1"))   # 几个 token 也算第 1 档
        self.assertEqual((m["active"], m["peak"]), (3, "10"))   # 最早一天（07-19 周日）也在窗口里
        snap.daily["2026-07-18"] = 999                           # 前一天（周六）落在窗口外
        self.assertEqual(dv.build_heat(snap, NOW)["peak"], "10")

    def test_all_unknown_and_na(self):
        snap = tm_source.TmSnapshot(observed_at=NOW.astimezone(timezone.utc))
        m = dv.build_heat(snap, NOW)
        self.assertEqual((m["d"], m["t"], m["active"]), ("-" * 102, "-" * 102, 0))
        self.assertNotIn("peak", m)
        self.assertEqual(dv.build_heat(None, NOW), {"v": 2, "type": "heat", "st": "na", "rows": 7,
                                                   "start": "2026-07-19", "end": "2026-10-28"})

    def test_all_zero_known_days(self):
        snap = tm_source.TmSnapshot(observed_at=NOW.astimezone(timezone.utc), daily_known_from="2026-01-01")
        m = dv.build_heat(snap, NOW)
        self.assertEqual((m["d"], m["t"], m["peak"], m["active"]), ("0" * 102, "A" * 102, "0", 0))


class MouseTests(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(dv.build_mouse(reading(), NOW),
                         {"v": 2, "type": "mouse", "st": "ok", "pct": 68, "chg": False, "at": "19:39"})

    def test_freshness_thresholds(self):
        for age, st in ((600, "ok"), (601, "stale"), (3600, "stale"), (3601, "na")):
            with self.subTest(age=age):
                self.assertEqual(dv.build_mouse(reading(age=age), NOW)["st"], st)
        stale = dv.build_mouse(reading(age=1200), NOW)
        self.assertEqual((stale["pct"], stale["at"]), (68, "19:20"))   # 过期仍显示旧值
        self.assertEqual(dv.build_mouse(reading(age=7200), NOW), {"v": 2, "type": "mouse", "st": "na"})

    def test_off_unknown_pct_and_charging_omitted(self):
        off = dv.build_mouse(reading(status="off", charging=None), NOW)
        self.assertEqual((off["st"], off["pct"], "chg" in off), ("off", 68, False))
        self.assertEqual(dv.build_mouse(reading(pct=None, charging=True), NOW)["pct"], None)
        self.assertEqual(dv.build_mouse(reading(status="na"), NOW), {"v": 2, "type": "mouse", "st": "na"})

    def test_missing_module_or_reading_is_na(self):
        self.assertEqual(dv.build_mouse(None, NOW), {"v": 2, "type": "mouse", "st": "na"})
        no_time = SimpleNamespace(pct=5, charging=None, observed_at=None, status="ok")
        self.assertEqual(dv.build_mouse(no_time, NOW)["st"], "na")

    def test_pct_is_clamped_to_int_0_100(self):
        self.assertEqual(dv.build_mouse(reading(pct=140), NOW)["pct"], 100)
        self.assertEqual(dv.build_mouse(reading(pct=-3), NOW)["pct"], 0)

    def test_works_with_the_real_reading_type(self):
        try:
            import mouse_rapoo
        except ImportError:
            self.skipTest("mouse_rapoo not available")
        r = mouse_rapoo.MouseReading(pct=41, charging=None, observed_at=NOW.timestamp() - 5, status="ok")
        self.assertEqual(dv.build_mouse(r, NOW)["pct"], 41)


class FitTests(unittest.TestCase):
    def test_all_five_messages_fit_the_wire_limit(self):
        msgs = dv.build_all(snapshot(), reading(), NOW)
        self.assertEqual(list(msgs), ["dash", "mouse", "quota", "models", "heat"])
        for kind, msg in msgs.items():
            with self.subTest(kind=kind):
                self.assertEqual((msg["v"], msg["type"]), (2, kind))
                data = wire(msg)
                self.assertLessEqual(len(data), 1024)
                self.assertEqual(len(data), dv.wire_size(msg))
                self.assertTrue(data.endswith(b"\n") and data.count(b"\n") == 1)

    def test_models_with_huge_names_are_trimmed(self):
        def mutate(data):
            models = data["devices"]["dev-a"]["periods"]["allTime"]["models"]
            for i, n in enumerate(list(models)):
                models[("m%d" % i) + "x" * 400] = models.pop(n)
        msg = dv.build_models(snapshot(mutate=mutate), NOW, "total")
        self.assertLessEqual(len(wire(msg)), 1024)
        self.assertEqual(len(msg["m"]), 5)
        self.assertTrue(all(len(m[0]) <= 18 for m in msg["m"]))
        self.assertTrue(any(m[0].endswith("~") for m in msg["m"]))

    def test_quota_with_huge_text_is_trimmed_but_keeps_providers(self):
        def mutate(data):
            for p in data["devices"]["dev-a"]["limits"]["providers"]:
                p["accountLabel"] = "Plan" + "y" * 300 if p["provider"] != "deepseek" else "P" * 300
        snap = snapshot(mutate=mutate)
        big = dv.build_quota(snap, NOW)
        self.assertLessEqual(len(wire(big)), 1024)
        self.assertEqual([p["n"] for p in big["p"]], ["Claude", "Codex", "Antigrav", "DeepSeek"])
        self.assertTrue(all(len(p.get("plan", "")) <= 8 for p in big["p"]))

    def test_fit_drops_items_then_providers_when_still_too_big(self):
        msg = dv.build_quota(snapshot(), NOW)
        full = dv.wire_size(msg)
        slim = dv.fit(copy.deepcopy(msg), limit=full - 1)   # 先去掉 rst，仍然是四家
        self.assertEqual(len(slim["p"]), 4)
        self.assertFalse(any("rst" in it for p in slim["p"] for it in p.get("i", [])))
        tiny = dv.fit(copy.deepcopy(msg), limit=120)
        self.assertLessEqual(dv.wire_size(tiny), 120)
        self.assertTrue(0 < len(tiny["p"]) < 4)
        models = dv.build_models(snapshot(), NOW)
        self.assertLessEqual(len(dv.fit(copy.deepcopy(models), limit=100)["m"]), 2)

    def test_unfittable_message_raises(self):
        with self.assertRaises(ValueError):
            dv.fit(dv.build_dash(snapshot(), NOW), limit=20)

    def test_wire_size_counts_utf8_bytes(self):
        self.assertEqual(dv.wire_size({"b": "¥"}), len('{"b":"¥"}\n'.encode()))
        self.assertEqual(dv.wire_size({"b": "¥"}), 11)


# ---------------------------------------------------------------------------
# 主机接入
# ---------------------------------------------------------------------------
READY_V2 = b"READY epaper-sidescreen v2 GDEY042T81 400x300\n"
READY_V1 = b"READY epaper-sidescreen v1 GDEY042T81 400x300\n"


class V2Serial(FakeSerial):
    """按消息版本回复：reject_v2=True 时模拟 v1 固件（对 v2 消息回 ERR bad_version）。"""

    def __init__(self, reject_v2=False, ready=None, ok_limit=None):
        super().__init__(respond=False)
        self.reject_v2 = reject_v2
        self.ok_limit = ok_limit
        if ready:
            self.rx.put(ready)

    def write(self, data):
        msg = json.loads(data)
        self.writes.append(msg)
        if msg.get("v") == 2 and self.reject_v2:
            self.rx.put(b"ERR bad_version\n")
        elif self.ok_limit is None or len([1 for _ in self.writes]) <= self.ok_limit:
            self.rx.put(b"OK\n")
        return len(data)


class LinkTestCase(unittest.TestCase):
    def open(self, ser):
        self.ser = ser
        patcher = patch.object(host, "serial", SimpleNamespace(Serial=lambda: ser, SerialException=OSError))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.link = host.Link("COM_TEST", 115200, 120, 180)
        self.addCleanup(self.link.close)
        self.assertTrue(self.link.connect())
        self.assertTrue(self.link.wait_handshake(1))


class NegotiationTests(LinkTestCase):
    def test_parse_ready(self):
        self.assertEqual(host.parse_ready_proto("READY epaper-sidescreen v2 GDEY042T81 400x300"), "v2")
        self.assertEqual(host.parse_ready_proto("READY epaper-sidescreen v1 WS42TRI 400x300"), "v1")
        self.assertEqual(host.parse_ready_proto("READY epaper-sidescreen v3 X 1x1"), "v2")
        for bad in ("READY", "READY epaper-sidescreen", "READY epaper-sidescreen x2 a b"):
            self.assertIsNone(host.parse_ready_proto(bad))

    def test_ready_v2_selects_v2_without_probe(self):
        self.open(V2Serial(ready=READY_V2))
        self.assertEqual(self.link.negotiate(), "v2")
        self.assertEqual([m["v"] for m in self.ser.writes], [1])    # 只有握手时的 v1 ping

    def test_ready_v1_selects_v1_without_probe(self):
        self.open(V2Serial(ready=READY_V1))
        self.assertEqual(self.link.negotiate(), "v1")
        self.assertEqual(len(self.ser.writes), 1)

    def test_v2_ping_ok_selects_v2(self):
        self.open(V2Serial())
        self.assertEqual(self.link.negotiate(), "v2")
        self.assertEqual(self.ser.writes[1], {"v": 2, "type": "ping"})
        self.assertEqual(self.link.negotiate(), "v2")                # 已决定，不再探测
        self.assertEqual(len(self.ser.writes), 2)

    def test_bad_version_selects_v1(self):
        self.open(V2Serial(reject_v2=True))
        self.assertEqual(self.link.negotiate(), "v1")
        self.assertIn("bad_version", self.link.last_err)

    def test_silent_board_stays_undecided_and_sends_nothing(self):
        self.ser = ser = V2Serial()
        ser.respond = False
        with patch.object(host, "serial", SimpleNamespace(Serial=lambda: ser, SerialException=OSError)):
            ser.write = lambda data: ser.writes.append(json.loads(data)) or len(data)   # 一律不回
            link = host.Link("COM_TEST", 115200)
            self.addCleanup(link.close)
            link.connect()
            self.assertFalse(link.wait_handshake(0.1))
            self.assertIsNone(link.negotiate())
            self.assertFalse(link.has_response)
            self.assertEqual(len(ser.writes), 1)

    def test_unanswered_probe_is_retried_only_after_the_retry_delay(self):
        self.open(V2Serial(ok_limit=1))                                 # 只回应第一条（握手 ping）
        self.assertIsNone(self.link.negotiate(probe_timeout=0.05))
        self.assertEqual(len(self.ser.writes), 2)
        self.assertIsNone(self.link.negotiate(probe_timeout=0.05))      # 还没到重试时间
        self.assertEqual(len(self.ser.writes), 2)
        self.link._next_probe = 0.0
        self.ser.ok_limit = None
        self.assertEqual(self.link.negotiate(probe_timeout=1), "v2")

    def test_reconnect_forgets_the_previous_decision(self):
        self.open(V2Serial(ready=READY_V2))
        self.assertEqual(self.link.negotiate(), "v2")
        self.link.connect()
        self.assertIsNone(self.link.proto)
        self.assertIsNone(self.link.ready_proto)


class FakeTm:
    def __init__(self, snap):
        self.snap = snap
        self.calls = 0

    def snapshot(self, now):
        self.calls += 1
        return self.snap


class FakeMouse:
    def __init__(self, r):
        self.r = r
        self.stopped = False

    def snapshot(self):
        return self.r

    def stop(self):
        self.stopped = True


class PusherTests(unittest.TestCase):
    def setUp(self):
        self.mouse = FakeMouse(reading())
        self.p = host.V2Pusher(FakeTm(snapshot()), self.mouse)

    def kinds(self, mono, now=NOW, send=True):
        due = self.p.due(mono, now)
        if send:
            for kind, msg in due:
                self.p.mark_sent(kind, msg, mono)
        return [k for k, _ in due]

    def test_first_round_sends_all_five_in_order(self):
        self.assertEqual(self.kinds(0), ["dash", "mouse", "quota", "models", "heat"])

    def test_identical_content_is_not_resent(self):
        self.kinds(0)
        for t in (10, 20, 30, 61, 120, 700):
            self.assertEqual(self.kinds(t), [], t)

    def test_blocks_follow_content(self):
        self.kinds(0)
        # dash 的 time 是数据更新时间，不是时钟：时钟走了不会重发
        self.assertEqual(self.kinds(10, NOW + timedelta(minutes=1)), [])
        # 60 秒后重新评估 quota：倒计时变了所以重发；models / heat 没变不发
        self.assertEqual(self.kinds(61, NOW + timedelta(minutes=1)), ["quota"])
        # 数据文件更新了（新的 updatedAt）-> dash 重发
        newer = snapshot()
        newer.observed_at += timedelta(minutes=1)
        self.p.tm.snap = newer
        self.assertEqual(self.kinds(70, NOW + timedelta(minutes=1)), ["dash"])

    def test_set_options_resends_affected_blocks_immediately(self):
        self.kinds(0)
        self.p.set_options(primary="total")
        due = dict(self.p.due(5, NOW))
        self.assertEqual(sorted(due), ["dash", "models"])
        self.assertEqual((due["dash"]["main"]["l"], due["models"]["scope"]), ("TOTAL", "TOTAL"))
        self.p.set_options(heat_rows=5)
        self.assertEqual(dict(self.p.due(6, NOW))["heat"]["rows"], 5)
        self.p.set_options(heat_weeks=4)
        self.assertEqual(len(dict(self.p.due(7, NOW))["heat"]["d"]), 25)
        self.p.set_options(calendar=True)
        self.assertEqual(dict(self.p.due(8, NOW))["heat"]["view"], "cal")    # 日历：heat 立刻重发
        self.p.set_options(calendar=False)
        self.assertNotIn("view", dict(self.p.due(9, NOW))["heat"])
        self.p.set_options(refresh_min=15)                      # 刷新间隔：dash 立刻重发，带 every
        self.assertEqual(dict(self.p.due(10, NOW))["dash"]["every"], 900)
        self.p.set_options(refresh_min=3)                       # 最短间隔 = 不带 every
        self.assertNotIn("every", dict(self.p.due(11, NOW))["dash"])
        self.p.set_options(refresh_min=7)                       # 不在可选值里：忽略
        self.assertEqual(self.p.refresh_min, 3)

    def test_set_options_ignores_invalid_values(self):
        self.p.set_options(primary="weekly", heat_rows=6, heat_weeks=3)
        self.p.set_options(primary=None, heat_rows=True, heat_weeks=27)
        self.p.set_options(heat_weeks=False, calendar="yes")
        self.assertEqual((self.p.primary, self.p.heat_rows, self.p.heat_weeks), ("today", 7, 10))
        self.assertFalse(self.p.calendar)

    def test_options_are_reloaded_every_ten_seconds_and_failures_keep_old_values(self):
        values = [("total", 5, 8)]
        loader = Mock(side_effect=lambda: values[0] if values[0] != "boom" else (_ for _ in ()).throw(ValueError("half-written")))
        p = host.V2Pusher(FakeTm(snapshot()), FakeMouse(reading()), options_loader=loader)
        p.due(0, NOW)
        self.assertEqual((p.primary, p.heat_rows, p.heat_weeks), ("total", 5, 8))
        p.due(5, NOW)
        self.assertEqual(loader.call_count, 1)          # 10 秒内不重读
        values[0] = "boom"
        with self.assertLogs(host.log, "WARNING"):
            p.due(10, NOW)
        self.assertEqual((p.primary, p.heat_rows, p.heat_weeks), ("total", 5, 8))     # 读失败：沿用
        values[0] = (None, 7, 20)                        # None = 不改（--primary 锁定了）
        p.due(20, NOW)
        self.assertEqual((p.primary, p.heat_rows, p.heat_weeks), ("total", 7, 20))
        values[0] = (None, 7, 20, True)                  # 第 4 项：日历
        p.due(30, NOW)
        self.assertTrue(p.calendar)
        values[0] = (None, 7, 20, True, 30)              # 第 5 项：刷新间隔
        p.due(40, NOW)
        self.assertEqual(p.refresh_min, 30)

    def test_quota_models_heat_are_not_even_built_between_evaluations(self):
        self.kinds(0)
        with patch.object(dv, "build", wraps=dv.build) as build:
            self.p.due(30, NOW)
        self.assertEqual([c.args[0] for c in build.call_args_list], ["dash", "mouse"])
        with patch.object(dv, "build", wraps=dv.build) as build:
            self.p.due(61, NOW)
        self.assertEqual([c.args[0] for c in build.call_args_list], ["dash", "mouse", "quota", "models", "heat"])

    def test_mouse_change_goes_out_next_tick(self):
        self.kinds(0)
        self.mouse.r = reading(pct=67)
        self.assertEqual(self.kinds(10), ["mouse"])

    def test_unsent_messages_are_retried(self):
        self.assertEqual(self.kinds(0, send=False), ["dash", "mouse", "quota", "models", "heat"])
        self.assertEqual(self.kinds(10, send=False), ["dash", "mouse", "quota", "models", "heat"])

    def test_reset_resends_everything(self):
        self.kinds(0)
        self.p.reset()
        self.assertEqual(len(self.kinds(1)), 5)

    def test_keepalive(self):
        self.assertTrue(self.p.keepalive_due(0))
        self.kinds(100)
        self.assertFalse(self.p.keepalive_due(120))
        self.assertTrue(self.p.keepalive_due(130))

    def test_mouse_failures_and_missing_module_degrade_to_na(self):
        self.mouse.snapshot = Mock(side_effect=RuntimeError("hid gone"))
        with self.assertLogs(host.log, "WARNING"):
            msgs = dict(self.p.due(0, NOW))
        self.assertEqual(msgs["mouse"]["st"], "na")
        p = host.V2Pusher(FakeTm(snapshot()), None, mouse_factory=lambda: None)
        self.assertEqual(dict(p.due(0, NOW))["mouse"], {"v": 2, "type": "mouse", "st": "na"})

    def test_mouse_factory_is_called_lazily_once_and_closed(self):
        factory = Mock(return_value=self.mouse)
        p = host.V2Pusher(FakeTm(snapshot()), mouse_factory=factory)
        factory.assert_not_called()
        p.due(0, NOW)
        p.due(10, NOW)
        factory.assert_called_once()
        p.close()
        self.assertTrue(self.mouse.stopped)

    def test_missing_hub_file_reports_na_everywhere(self):
        p = host.V2Pusher(FakeTm(None), self.mouse)
        msgs = dict(p.due(0, NOW))
        self.assertEqual([msgs[k]["st"] for k in ("dash", "quota", "models", "heat")], ["na"] * 4)
        self.assertEqual(msgs["mouse"]["st"], "ok")


class PushV2Tests(LinkTestCase):
    def test_sends_five_lines_then_only_keepalive_pings(self):
        self.open(V2Serial(ready=READY_V2))
        pusher = host.V2Pusher(FakeTm(snapshot()), FakeMouse(reading()))
        self.assertEqual(host.push_v2(self.link, pusher, 0.0, NOW), 5)
        self.assertEqual([m["type"] for m in self.ser.writes[1:]], list(dv.KINDS))
        self.assertEqual(host.push_v2(self.link, pusher, 10.0, NOW), 0)
        self.assertEqual(len(self.ser.writes), 6)                         # 10 秒内没事可做，也不 ping
        self.assertEqual(host.push_v2(self.link, pusher, 31.0, NOW), 0)
        self.assertEqual(self.ser.writes[-1], {"v": 2, "type": "ping"})   # 超过 30 秒 -> 保活
        self.assertEqual(host.push_v2(self.link, pusher, 40.0, NOW), 0)
        self.assertEqual(len(self.ser.writes), 7)

    def test_unanswered_line_stops_the_round_and_rest_is_retried(self):
        self.open(V2Serial(ready=READY_V2, ok_limit=2))        # 握手 ping + 第 1 条回 OK，之后不回
        pusher = host.V2Pusher(FakeTm(snapshot()), FakeMouse(reading()))
        with patch.object(host, "V2_ACK_WAIT", 0.05):
            self.assertEqual(host.push_v2(self.link, pusher, 0.0, NOW), 2)   # dash 回了，mouse 没回
            self.assertEqual([m["type"] for m in self.ser.writes[1:]], ["dash", "mouse"])
            self.ser.ok_limit = None
            self.assertEqual(host.push_v2(self.link, pusher, 1.0, NOW), 3)   # 剩下三条下一轮补发
        self.assertEqual([m["type"] for m in self.ser.writes[3:]], ["quota", "models", "heat"])


class MainV2Tests(unittest.TestCase):
    def setUp(self):
        # main() 用真实时钟：把夹具的 updatedAt 改成现在，数据才不会随时间变成 stale / na
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        text = FIXTURE.read_text(encoding="utf-8")
        text = re.sub(r'"updatedAt": "[^"]+"', f'"updatedAt": "{stamp}"', text)
        self.tm_file = Path(tmp.name) / "hub-devices.json"
        self.tm_file.write_text(text, encoding="utf-8")

    def cfg(self, **v2):
        return dict(host.DEFAULT_CONFIG, v2={"tm_file": str(self.tm_file), **v2})

    def run_main(self, args, ser, cfg, ticks=3):
        count = []

        def sleep(_):
            if "--once" in args:   # 单次模式靠真实的短轮询等应答
                return real_sleep(0.01)
            count.append(1)
            if len(count) >= ticks:
                raise KeyboardInterrupt

        mouse = FakeMouse(reading(now=datetime.now(timezone.utc)))
        tok = Mock()
        tok.get.return_value = dict(host.EMPTY_TOK)
        with patch.object(host, "serial", SimpleNamespace(Serial=lambda: ser, SerialException=OSError)), \
                patch.object(host, "load_config", return_value=cfg), \
                patch.object(host, "load_mouse_source", return_value=mouse), \
                patch.object(host, "SystemCollector") as sysc, \
                patch.object(host, "TokscaleSource", return_value=tok) as tokscale, \
                patch.object(host.time, "sleep", side_effect=sleep):
            code = host.main(args)
        return code, sysc, tokscale, mouse

    def test_v2_firmware_gets_v2_blocks_and_v1_collectors_are_never_created(self):
        ser = V2Serial(ready=READY_V2)
        code, sysc, tokscale, mouse = self.run_main(["--port", "COM_TEST"], ser, self.cfg())
        self.assertEqual(code, 0)
        sysc.assert_not_called()
        tokscale.assert_not_called()
        self.assertEqual([m["type"] for m in ser.writes], ["ping"] + list(dv.KINDS))
        self.assertEqual({m["v"] for m in ser.writes[1:]}, {2})
        self.assertTrue(mouse.stopped)
        self.assertFalse(ser.is_open)
        self.assertEqual(next(m for m in ser.writes if m["type"] == "mouse")["st"], "ok")

    def test_v2_ping_ok_without_ready_also_selects_v2(self):
        ser = V2Serial()
        code, sysc, tokscale, _ = self.run_main(["--port", "COM_TEST"], ser, self.cfg())
        self.assertEqual([m["type"] for m in ser.writes], ["ping", "ping"] + list(dv.KINDS))
        sysc.assert_not_called()
        tokscale.assert_not_called()

    def test_v1_firmware_falls_back_to_v1_stats(self):
        for ser in (V2Serial(reject_v2=True), V2Serial(ready=READY_V1)):
            with self.subTest(ready=bool(ser.rx.qsize())):
                code, sysc, tokscale, _ = self.run_main(["--port", "COM_TEST"], ser, self.cfg(), ticks=2)
                self.assertEqual(code, 0)
                types = [m["type"] for m in ser.writes]
                self.assertEqual(types.count("stats"), 2)
                self.assertTrue(set(types) <= {"ping", "stats"})
                tokscale.assert_called_once()

    def test_v2_disabled_in_config_never_probes(self):
        ser = V2Serial(ready=READY_V2)
        code, _, _, mouse = self.run_main(["--port", "COM_TEST"], ser, self.cfg(enabled=False), ticks=2)
        self.assertEqual({m["v"] for m in ser.writes}, {1})
        self.assertFalse(mouse.stopped)   # 鼠标从没被创建

    def test_once_stays_v1_unless_v2_is_forced(self):
        ser = V2Serial(ready=READY_V2)
        tok = Mock()
        tok.get.return_value = dict(host.EMPTY_TOK)
        with patch.object(host, "serial", SimpleNamespace(Serial=lambda: ser, SerialException=OSError)), \
                patch.object(host, "TokscaleSource", return_value=tok), patch.object(host.time, "sleep"):
            self.assertEqual(host.main(["--once", "--port", "COM_TEST"]), 0)
        self.assertEqual([m["type"] for m in ser.writes], ["ping", "stats"])

    def test_once_with_v2_flag_sends_one_v2_round(self):
        ser = V2Serial()
        code, sysc, tokscale, mouse = self.run_main(["--once", "--v2", "--port", "COM_TEST"], ser, self.cfg())
        self.assertEqual(code, 0)
        self.assertEqual([m["type"] for m in ser.writes], ["ping"] + list(dv.KINDS))
        sysc.assert_not_called()
        tokscale.assert_not_called()
        self.assertTrue(mouse.stopped)

    def test_once_with_v2_flag_fails_when_board_rejects(self):
        ser = V2Serial(reject_v2=True)
        with patch.object(host, "V2_ACK_WAIT", 0.05):
            code, *_ = self.run_main(["--once", "--v2", "--port", "COM_TEST"], ser, self.cfg())
        self.assertEqual(code, 2)

    def test_dry_run_v2_prints_five_valid_lines_without_serial_or_collectors(self):
        out = io.StringIO()
        with patch.object(host, "load_config", return_value=self.cfg()), \
                patch.object(host, "Link") as link, patch.object(host, "SystemCollector") as sysc, \
                patch.object(host, "TokscaleSource") as tokscale, patch.object(host, "load_mouse_source") as mouse, \
                contextlib.redirect_stdout(out), self.assertLogs(host.log, "INFO"):
            self.assertEqual(host.main(["--dry-run-v2"]), 0)
        lines = out.getvalue().splitlines()
        self.assertEqual([json.loads(l)["type"] for l in lines], list(dv.KINDS))
        self.assertTrue(all(len(l.encode()) + 1 <= 1024 for l in lines))
        for mock in (link, sysc, tokscale, mouse):
            mock.assert_not_called()
        self.assertEqual(json.loads(lines[1]), {"v": 2, "type": "mouse", "st": "na"})

    def test_dry_run_v2_with_unreadable_file_still_prints_na_blocks_and_fails(self):
        out = io.StringIO()
        cfg = self.cfg(tm_file=str(FIXTURE.with_name("does-not-exist.json")))
        with patch.object(host, "load_config", return_value=cfg), contextlib.redirect_stdout(out), \
                self.assertLogs("epaper_host", "WARNING"):
            self.assertEqual(host.main(["--dry-run-v2"]), 1)
        self.assertEqual([json.loads(l)["st"] for l in out.getvalue().splitlines()], ["na"] * 5)

    def test_v2_config_merges_over_defaults(self):
        self.assertEqual(host.v2_options({"v2": {"mouse": False}}),
                         {"enabled": True, "tm_file": None, "mouse": False, "primary": "today",
                          "heat_rows": 7, "heat_weeks": 10, "calendar": False, "refresh_min": 3, "weather": None})
        self.assertEqual(host.v2_options({}), host.DEFAULT_V2)
        self.assertEqual(host.v2_options({"v2": None}), host.DEFAULT_V2)
        self.assertEqual(host.v2_options({"v2": {"primary": "TOTAL", "heat_rows": 7}})["primary"], "total")
        bad = host.v2_options({"v2": {"primary": "week", "heat_rows": 6, "heat_weeks": 3}})
        self.assertEqual((bad["primary"], bad["heat_rows"], bad["heat_weeks"]), ("today", 7, 10))
        for weeks in (4, 26):
            self.assertEqual(host.v2_options({"v2": {"heat_weeks": weeks}})["heat_weeks"], weeks)
        self.assertEqual(host.v2_options({"v2": {"heat_weeks": 27}})["heat_weeks"], 10)
        self.assertTrue(host.v2_options({"v2": {"calendar": True}})["calendar"])
        self.assertFalse(host.v2_options({"v2": {"calendar": "true"}})["calendar"])   # 只认 JSON true
        self.assertEqual(host.v2_options({"v2": {"refresh_min": 10}})["refresh_min"], 10)
        for bad in (7, "10", True, 0):
            self.assertEqual(host.v2_options({"v2": {"refresh_min": bad}})["refresh_min"], 3)

    def test_primary_and_heat_rows_from_config_and_cli(self):
        for args, cfg_v2, main_l, scope, rows in (
                ([], {}, "TODAY", "TODAY", 7),
                ([], {"primary": "total", "heat_rows": 5}, "TOTAL", "TOTAL", 5),
                (["--primary", "total"], {"primary": "today"}, "TOTAL", "TOTAL", 7)):   # 命令行优先于配置
            with self.subTest(args=args, cfg=cfg_v2):
                ser = V2Serial(ready=READY_V2)
                self.run_main(["--port", "COM_TEST", *args], ser, self.cfg(**cfg_v2), ticks=2)
                by_type = {m["type"]: m for m in ser.writes}
                self.assertEqual((by_type["dash"]["main"]["l"], by_type["models"]["scope"], by_type["heat"]["rows"]),
                                 (main_l, scope, rows))

    def test_dry_run_v2_primary_flag(self):
        for args, l in ((["--primary", "total"], "TOTAL"), (["--primary", "today"], "TODAY"), ([], "TODAY")):
            out = io.StringIO()
            with patch.object(host, "load_config", return_value=self.cfg()), contextlib.redirect_stdout(out), \
                    self.assertLogs(host.log, "INFO"):
                self.assertEqual(host.main(["--dry-run-v2", *args]), 0)
            self.assertEqual(json.loads(out.getvalue().splitlines()[0])["main"]["l"], l)


class ConfigExampleTests(unittest.TestCase):
    def test_example_config_has_v2_section_matching_defaults(self):
        example = json.loads((Path(__file__).resolve().parents[1] / "config.example.json").read_text(encoding="utf-8"))
        self.assertEqual(set(example["v2"]), set(host.DEFAULT_V2))


if __name__ == "__main__":
    unittest.main()
