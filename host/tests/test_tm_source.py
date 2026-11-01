"""Token Monitor Hub 文件读取测试。数据全是缩小的合成 fixture，不含真实账号 / 项目。"""
import copy
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tm_source
from tm_source import build_snapshot

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "hub-devices.synthetic.json"
NOW = datetime(2026, 10, 28, 9, 10, tzinfo=timezone.utc)


def load_raw():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def provider(snap, key):
    return next(p for p in snap.quota if p.key == key)


def items(p):
    return [(i.l, i.left) for i in p.items]


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.raw = load_raw()
        self.snap = build_snapshot(self.raw, NOW)

    def test_totals_and_today_sum_over_devices(self):
        self.assertEqual(self.snap.total_tokens, 6_500_000_000)
        self.assertAlmostEqual(self.snap.total_cost, 2100.0)
        self.assertEqual(self.snap.today_tokens, 1_130_000_000)
        self.assertAlmostEqual(self.snap.today_cost, 311.5)

    def test_device_whose_today_has_ended_is_left_out_of_today(self):
        self.raw["devices"]["dev-b"]["periodWindows"]["today"]["endsAt"] = "2026-10-28T08:00:00.000Z"
        snap = build_snapshot(self.raw, NOW)
        self.assertEqual(snap.today_tokens, 1_000_000_000)
        self.assertAlmostEqual(snap.today_cost, 300.0)
        self.assertEqual(snap.total_tokens, 6_500_000_000)  # 累计不受影响

    def test_models_top5_by_tokens_with_share_of_total(self):
        self.assertEqual([(m.name, m.tokens, m.pct) for m in self.snap.models], [
            ("claude-opus-5-5", 4_200_000_000, 65),
            ("gpt-6.1-sol", 1_000_000_000, 15),
            ("claude-sonnet-5-5", 500_000_000, 8),
            ("deepseek-flash", 300_000_000, 5),   # 与 gpt-5.6-sol 同量，按名字排
            ("gpt-5.6-sol", 300_000_000, 5),
        ])

    def test_today_models_sum_live_devices_with_today_total_as_denominator(self):
        self.assertEqual([(m.name, m.tokens, m.pct) for m in self.snap.today_models], [
            ("claude-opus-5-5", 730_000_000, 65), ("claude-sonnet-5-5", 300_000_000, 27),
            ("deepseek-flash", 100_000_000, 9)])
        self.raw["devices"]["dev-b"]["periodWindows"]["today"]["endsAt"] = "2026-10-28T08:00:00.000Z"
        snap = build_snapshot(self.raw, NOW)   # dev-b 的今天已过：不计入今日模型
        self.assertEqual([(m.name, m.pct) for m in snap.today_models],
                         [("claude-opus-5-5", 60), ("claude-sonnet-5-5", 30), ("deepseek-flash", 10)])

    def test_quota_has_only_the_four_providers_in_fixed_order(self):
        self.assertEqual([p.name for p in self.snap.quota], ["Claude", "Codex", "Antigrav", "DeepSeek"])

    def test_quota_picks_newest_ok_reading_across_devices(self):
        claude = provider(self.snap, "claude")  # dev-b 的 ok 读数（09:09:50）比 dev-a（09:09:00）新；unavailable 被忽略
        self.assertTrue(claude.ok)
        self.assertEqual(claude.plan, "Max 5x")
        self.assertEqual(items(claude), [("5h", 60), ("Wk", 63)])   # 带标签的 Fable 窗口不要
        codex = provider(self.snap, "codex")      # dev-a 更新（09:09:40 vs 09:05:00）
        self.assertEqual(items(codex), [("5h", 53), ("Wk", 93)])
        self.assertEqual(codex.items[0].resets_at, datetime(2026, 10, 28, 11, 56, tzinfo=timezone.utc))

    def test_antigravity_gemini_is_two_bars_and_other_groups_are_text(self):
        p = provider(self.snap, "antigravity")
        # Gemini 5h 100% / 周 96.8% 各一条（不再合并）；Claude/GPT 5h 71.7% / 周 63.6%；Argon 周 0%
        self.assertEqual(items(p), [("5h", 100), ("Wk", 97)])
        self.assertEqual(p.items[0].resets_at, datetime(2026, 10, 28, 14, 9, tzinfo=timezone.utc))
        self.assertEqual([(t.name, t.lefts) for t in p.txt], [("4 Argon", (0,)), ("C/G", (72, 64))])
        self.assertEqual(p.txt[1].resets[1], datetime(2026, 10, 30, 6, 14, 46, tzinfo=timezone.utc))

    def test_antigravity_tolerates_missing_groups_and_windows(self):
        dev = self.raw["devices"]["dev-a"]
        ag = next(x for x in dev["limits"]["providers"] if x["provider"] == "antigravity")
        ag["windows"] = [
            {"kind": "weekly", "label": "Claude/GPT weekly", "usedPercent": 95, "resetsAt": "2026-10-30T06:00:00.000Z"},
            {"kind": "session", "label": "Gemini 5-hour", "usedPercent": 10, "resetsAt": "2026-10-28T12:00:00.000Z"},
            {"kind": "weekly", "label": "Brand New Group weekly", "usedPercent": 1},
        ]
        p = provider(build_snapshot(self.raw, NOW), "antigravity")
        self.assertEqual(items(p), [("5h", 90)])                    # 没有 Gemini 周窗口：只有一条
        self.assertEqual([(t.name, t.lefts) for t in p.txt], [("C/G", (None, 5))])   # 缺 5h：写 --
        ag["windows"] = []
        p = provider(build_snapshot(self.raw, NOW), "antigravity")
        self.assertEqual((p.items, p.txt, p.bal), ((), (), None))

    def test_argon_name_comes_from_the_label(self):
        dev = self.raw["devices"]["dev-a"]
        ag = next(x for x in dev["limits"]["providers"] if x["provider"] == "antigravity")
        ag["windows"] = [{"kind": "weekly", "label": "Gemini 5 Argon weekly", "usedPercent": 40}]
        self.assertEqual(provider(build_snapshot(self.raw, NOW), "antigravity").txt[0].name, "5 Argon")

    def test_at_most_two_bars_per_provider(self):
        dev = self.raw["devices"]["dev-a"]
        cl = next(x for x in dev["limits"]["providers"] if x["provider"] == "claude")
        cl["updatedAt"] = "2026-10-28T09:10:00.000Z"
        cl["windows"] += [{"kind": "weekly", "usedPercent": 1}, {"kind": "session", "usedPercent": 2}]
        self.assertEqual(len(provider(build_snapshot(self.raw, NOW), "claude").items), 2)

    def test_deepseek_balance_and_plan_abbreviation(self):
        p = provider(self.snap, "deepseek")
        self.assertEqual((p.plan, p.bal, p.items), ("PAYG", "¥38.66", ()))
        self.raw["devices"]["dev-a"]["limits"]["providers"][3]["balance"]["currency"] = "USD"
        self.assertEqual(provider(build_snapshot(self.raw, NOW), "deepseek").bal, "$38.66")

    def test_provider_without_any_ok_reading_becomes_placeholder(self):
        for dev in self.raw["devices"].values():
            for p in dev["limits"]["providers"]:
                if p["provider"] == "codex":
                    p["status"] = "unavailable"
        codex = provider(build_snapshot(self.raw, NOW), "codex")
        self.assertFalse(codex.ok)
        self.assertEqual(codex.items, ())
        # notConfigured 的不显示
        for dev in self.raw["devices"].values():
            dev["limits"]["providers"] = [p for p in dev["limits"]["providers"] if p["provider"] != "deepseek"]
        self.assertNotIn("deepseek", [p.key for p in build_snapshot(self.raw, NOW).quota])

    def test_daily_is_summed_by_date_and_known_from_is_latest_first_date(self):
        d = self.snap.daily
        self.assertEqual(d["2026-10-22"], 300_000_000)   # a:0 + b:300M
        self.assertEqual(d["2026-10-23"], 500_000_000)
        self.assertEqual(d["2026-10-28"], 1_130_000_000)
        self.assertNotIn("2026-10-24", d)
        self.assertAlmostEqual(self.snap.daily_cost["2026-10-28"], 311.5)
        self.assertEqual(self.snap.daily_known_from, "2026-10-22")  # dev-b 从 10-22 才开始记录

    def test_observed_at_is_newest_device_update(self):
        self.assertEqual(self.snap.observed_at, datetime(2026, 10, 28, 9, 9, 30, tzinfo=timezone.utc))
        self.assertEqual(set(self.snap.device_updated_at), {"dev-a", "dev-b"})
        self.assertEqual(self.snap.device_updated_at["dev-b"].minute, 0)

    def test_tolerates_missing_and_garbage_fields(self):
        for raw in ({}, {"devices": None}, {"devices": {"x": 5}}, {"devices": {"x": {"periods": []}}}):
            snap = build_snapshot(raw, NOW)
            self.assertEqual((snap.total_tokens, snap.today_tokens, snap.models, snap.quota), (0, 0, (), ()))
            self.assertIsNone(snap.observed_at)
        weird = copy.deepcopy(self.raw)
        weird["devices"]["dev-a"]["periods"]["allTime"]["totalTokens"] = "n/a"
        weird["devices"]["dev-a"]["limits"]["providers"][0]["windows"][0]["usedPercent"] = None
        weird["devices"]["dev-a"]["updatedAt"] = "yesterday"
        snap = build_snapshot(weird, NOW)
        self.assertEqual(snap.total_tokens, 1_500_000_000)
        self.assertEqual(snap.observed_at.minute, 0)  # 只剩 dev-b 的 09:00

    def test_left_rounds_half_up_and_clamps(self):
        dev = self.raw["devices"]["dev-a"]
        cl = dev["limits"]["providers"][0]
        cl["windows"] = [{"kind": "session", "usedPercent": 41.5}, {"kind": "weekly", "usedPercent": 130}]
        dev["limits"]["providers"][0]["updatedAt"] = "2026-10-28T09:10:00.000Z"  # 比 dev-b 新
        self.assertEqual(items(provider(build_snapshot(self.raw, NOW), "claude")),
                         [("5h", 59), ("Wk", 0)])


class TmSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "hub-devices.json"
        self.src = tm_source.TmSource(self.path)

    def write(self, text, mtime):
        self.path.write_text(text, encoding="utf-8")
        os.utime(self.path, ns=(mtime, mtime))

    def test_missing_file_gives_none_without_raising(self):
        self.assertIsNone(self.src.snapshot(NOW))
        self.assertIn("FileNotFoundError", self.src.last_error)

    def test_reads_fixture_and_picks_up_changes(self):
        text = FIXTURE.read_text(encoding="utf-8")
        self.write(text, 1_000_000_000)
        self.assertEqual(self.src.snapshot(NOW).total_tokens, 6_500_000_000)
        raw = json.loads(text)
        raw["devices"]["dev-b"]["periods"]["allTime"]["totalTokens"] = 2_500_000_000
        self.write(json.dumps(raw), 2_000_000_000)
        self.assertEqual(self.src.snapshot(NOW).total_tokens, 7_500_000_000)

    def test_half_written_file_keeps_last_good_result(self):
        text = FIXTURE.read_text(encoding="utf-8")
        self.write(text, 1_000_000_000)
        self.assertIsNotNone(self.src.snapshot(NOW))
        self.write(text[: len(text) // 2], 2_000_000_000)  # 写到一半
        snap = self.src.snapshot(NOW)
        self.assertEqual(snap.total_tokens, 6_500_000_000)
        self.assertIn("JSONDecodeError", self.src.last_error)
        self.write(text, 3_000_000_000)  # 写完 -> 恢复
        self.assertIsNotNone(self.src.snapshot(NOW))
        self.assertIsNone(self.src.last_error)

    def test_bad_first_read_then_good(self):
        self.write("{ not json", 1)
        self.assertIsNone(self.src.snapshot(NOW))
        self.write(FIXTURE.read_text(encoding="utf-8"), 2)
        self.assertIsNotNone(self.src.snapshot(NOW))

    def test_unchanged_file_is_not_reparsed(self):
        self.write(FIXTURE.read_text(encoding="utf-8"), 1_000_000_000)
        self.src.snapshot(NOW)
        raw = self.src._raw
        self.src.snapshot(NOW)
        self.assertIs(self.src._raw, raw)

    def test_default_path_is_under_appdata(self):
        old = os.environ.get("APPDATA")
        os.environ["APPDATA"] = r"C:\Fake\Roaming"
        try:
            self.assertEqual(tm_source.TmSource().path.name, "hub-devices.json")
            self.assertEqual(tm_source.TmSource().path.parent.name, "Token Monitor")
            self.assertEqual(tm_source.TmSource("%APPDATA%/x.json").path.parts[-1], "x.json")
        finally:
            if old is None:
                del os.environ["APPDATA"]
            else:
                os.environ["APPDATA"] = old


if __name__ == "__main__":
    unittest.main()
