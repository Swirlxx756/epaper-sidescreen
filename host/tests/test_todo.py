"""待办页 / 页面切换 / 立即刷新：渲染、PackBits、分块、配置、工作线程命令序列、窗口逻辑。
不开串口、不联网；配置只写临时文件副本。"""
import base64
import json
import queue
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dashboard_v2 as dv
import epaper_host as host
import gui
import todo_page as tp
from test_dashboard_v2 import NOW, FakeTm, V2Serial, snapshot
from test_gui import READY_V2, AppTestBase, FakeWorker, drain_until, wait_for

VECTORS = Path(__file__).parent / "fixtures" / "packbits_vectors.json"
T0 = datetime(2026, 11, 2, 20, 20)


def item(text, done=False, important=False):
    return {"text": text, "done": done, "important": important}


class PackBitsTests(unittest.TestCase):
    def test_shared_vectors(self):
        for v in json.loads(VECTORS.read_text(encoding="utf-8")):
            raw, enc = bytes.fromhex(v["raw"]), bytes.fromhex(v["enc"])
            with self.subTest(v["name"]):
                self.assertEqual(tp.packbits_encode(raw), enc)
                self.assertEqual(tp.packbits_decode(enc), raw)

    def test_roundtrip_random_and_runs(self):
        import random
        rng = random.Random(7)
        for n in (1, 2, 3, 127, 128, 129, 300, 5000):
            for mode in range(3):
                if mode == 0:
                    data = bytes(rng.randrange(256) for _ in range(n))
                elif mode == 1:
                    data = bytes(rng.choice((0, 0, 0, 255)) for _ in range(n))
                else:
                    data = bytes([rng.randrange(256)]) * n
                self.assertEqual(tp.packbits_decode(tp.packbits_encode(data)), data)

    def test_decoder_rejects_truncated_streams(self):
        for bad in (b"\x05ab", b"\xfe"):
            with self.assertRaises(ValueError):
                tp.packbits_decode(bad)
        with self.assertRaises(ValueError):
            tp.packbits_decode(b"\x00a", expected=2)


class RenderTests(unittest.TestCase):
    def planes(self, items):
        return tp.render_todo(items, T0)

    def test_planes_are_400x300_one_bit(self):
        b, r = self.planes([item("写周报")])
        for p in (b, r):
            self.assertEqual((p.size, p.mode), ((400, 300), "1"))
            self.assertEqual(len(tp.pack_plane(p)), 15000)

    def test_no_antialiasing_only_pure_black_and_red_in_preview(self):
        b, r = self.planes([item("写周报，买牛奶"), item("Review PR", important=True), item("done", done=True)])
        img = tp.to_rgb(b, r)
        self.assertEqual({c for _, c in img.getcolors()}, {(255, 255, 255), (0, 0, 0), (200, 20, 20)})

    def test_chinese_text_actually_draws(self):
        box = (tp.TEXT_X, 40, 300, 60)
        self.assertIsNone(self.planes([])[0].crop(box).getbbox())
        self.assertIsNotNone(self.planes([item("写周报")])[0].crop(box).getbbox())
        # 汉字不是方框：同样长度的两个不同汉字串画出来不同
        self.assertNotEqual(tp.pack_plane(self.planes([item("写周报")])[0]), tp.pack_plane(self.planes([item("买牛奶")])[0]))

    def test_important_open_items_use_red_only(self):
        b, r = self.planes([item("普通")])
        self.assertEqual(sum(tp.pack_plane(r)), 0)
        b2, r2 = self.planes([item("普通"), item("重要的事", important=True)])
        self.assertGreater(sum(tp.pack_plane(r2)), 0)
        # 红色那一行里黑平面没有墨（红色条目不同时画黑）
        row_y0 = 34 + 4 + 19 + 4 + 4
        rows = b2.crop((10, row_y0, 380, row_y0 + 19))
        self.assertEqual(rows.getbbox(), None)

    def test_done_items_are_dithered_and_struck_through(self):
        _, r = self.planes([item("重要但完成", done=True, important=True)])
        self.assertEqual(sum(tp.pack_plane(r)), 0)            # 完成的项不再红
        b_open, _ = self.planes([item("重要但完成")])
        b_done, _ = self.planes([item("重要但完成", done=True)])
        self.assertNotEqual(tp.pack_plane(b_open), tp.pack_plane(b_done))

    def test_long_item_wraps_and_ellipsizes(self):
        font = tp._font(False, tp.FONT_PX)
        lines = tp.wrap_text("很长" * 60, font, 340)
        self.assertEqual(len(lines), tp.MAX_LINES_PER_ITEM)
        self.assertTrue(lines[-1].endswith("…"))
        for ln in lines:
            self.assertLessEqual(tp._text_width(font, ln), 340)
        words = tp.wrap_text("alpha beta gamma delta epsilon zeta eta theta iota", font, 200)
        self.assertEqual(" ".join(words).replace("…", "")[:5], "alpha")

    def test_more_line_appears_when_list_overflows(self):
        font = tp._font(False, tp.FONT_PX)
        items = [item(f"事项 {i}") for i in range(30)]
        placed, hidden = tp.layout_items(items, font, 300 - tp.HEADER_H - tp.FOOTER_H - 4)
        self.assertGreater(hidden, 0)
        self.assertEqual(len(placed) + hidden, 30)
        short, none_hidden = tp.layout_items(items[:3], font, 200)
        self.assertEqual((len(short), none_hidden), (3, 0))

    def test_more_line_never_overlaps_the_footer(self):
        font = tp._font(False, tp.FONT_PX)
        avail = 300 - tp.HEADER_H - tp.FOOTER_H - 4
        for n in range(1, 40):
            placed, hidden = tp.layout_items([item("x" * (n % 50 + 1)) for _ in range(n)], font, avail)
            used = sum(len(ls) * tp.LINE_H + 2 * tp.ROW_PAD for _, ls in placed)
            self.assertLessEqual(used + (tp.LINE_H + tp.ROW_PAD if hidden else 0), avail)

    def test_empty_state(self):
        b, r = self.planes([])
        self.assertGreater(sum(tp.pack_plane(b)), 0)           # 标题 + NO TODOS + 状态行

    def test_normalize_items(self):
        raw = [{"text": "  a   b "}, "str", {"text": ""}, 5, None, {"text": "x", "done": "yes", "important": True}]
        self.assertEqual(tp.normalize_items(raw), [item("a b"), item("str"), item("x", False, True)])
        self.assertEqual(tp.normalize_items("junk"), [])
        self.assertEqual(len(tp.normalize_items([{"text": "t"}] * 100)), tp.ITEMS_MAX)
        self.assertEqual(len(tp.clean_text("字" * 500)), tp.TEXT_MAX_CHARS)


class TransferTests(unittest.TestCase):
    def test_full_page_fits_and_frames_are_valid(self):
        items = [item(f"事项 {i} 写点比较长的中文内容测试压缩大小", i % 3 == 0, i % 4 == 0) for i in range(60)]
        frames, img = tp.push_frames(items, T0)
        begin, chunks, end, refresh = frames[0], frames[1:-2], frames[-2], frames[-1]
        self.assertEqual((begin["type"], begin["id"]), ("img_begin", "todo"))
        self.assertEqual(end, {"v": 2, "type": "img_end", "id": "todo"})
        self.assertEqual(refresh, {"v": 2, "type": "refresh_now"})
        data = b""
        for c in chunks:
            self.assertEqual(c["off"], len(data))                  # 顺序、连续
            data += base64.b64decode(c["d"])
        self.assertEqual(len(data), begin["size"])
        self.assertEqual(tp.crc32(data), begin["crc"])
        self.assertLessEqual(len(data), tp.MAX_COMPRESSED)
        for f in frames:
            self.assertLessEqual(len(host.encode(f)), 900)         # 比协议的 1024 字节上限留足余量
        # 解压回去就是两个平面
        black, red = tp.render_todo(items, T0)
        raw = tp.packbits_decode(data)
        self.assertEqual(len(raw), 2 * tp.PLANE_BYTES)
        self.assertEqual(raw[:tp.PLANE_BYTES], tp.pack_plane(black))
        self.assertEqual(raw[tp.PLANE_BYTES:], tp.pack_plane(red))
        self.assertEqual(img.size, (400, 300))

    def test_typical_page_is_a_few_kb(self):
        frames, _ = tp.push_frames([item("写周报"), item("买牛奶", True), item("打电话", False, True)], T0)
        self.assertLess(frames[0]["size"], 4096)

    def test_too_big_raises(self):
        with patch.object(tp, "MAX_COMPRESSED", 100):
            with self.assertRaises(ValueError):
                tp.push_frames([item("写周报")], T0)

    def test_chunk_boundaries(self):
        comp = bytes(range(256)) * 5
        frames = tp.build_frames(comp, chunk=600)
        self.assertEqual([f["off"] for f in frames[1:-1]], [0, 600, 1200])
        self.assertEqual(frames[0]["size"], 1280)

    def test_messages(self):
        self.assertEqual(tp.page_msg("todo"), {"v": 2, "type": "page", "p": "todo"})
        self.assertEqual(tp.refresh_now_msg(), {"v": 2, "type": "refresh_now"})
        with self.assertRaises(ValueError):
            tp.page_msg("nope")
        self.assertTrue(tp.is_unsupported("unknown_type"))
        self.assertFalse(tp.is_unsupported("too_big"))


FW_FIXTURES = Path(__file__).resolve().parents[2] / "docs" / "todo-image-fixtures.json"


def expand(seq: str) -> bytes:
    """"00*2207 ff*1 ..." -> bytes（固件共享测试数据里平面的写法）。"""
    return b"".join(bytes.fromhex(tok.split("*")[0]) * int(tok.split("*")[1]) for tok in seq.split())


@unittest.skipUnless(FW_FIXTURES.exists(), "docs/todo-image-fixtures.json missing")
class FirmwareFixtureTests(unittest.TestCase):
    """主机编码器对固件共享测试数据逐字节一致。"""

    def setUp(self):
        self.fx = json.loads(FW_FIXTURES.read_text(encoding="utf-8"))

    def test_crc32(self):
        for v in self.fx["crc32_vectors"]:
            self.assertEqual(tp.crc32(v["ascii"].encode()), v["crc32"])

    def test_rle_cases_both_directions(self):
        for c in self.fx["rle_cases"]:
            with self.subTest(c["name"]):
                raw, enc = bytes.fromhex(c["raw_hex"]), bytes.fromhex(c["rle_hex"])
                self.assertEqual(tp.packbits_encode(raw), enc)
                self.assertEqual(tp.packbits_decode(enc), raw)

    def test_decode_only_and_invalid(self):
        for c in self.fx["rle_decode_only"]:
            self.assertEqual(tp.packbits_decode(bytes.fromhex(c["rle_hex"])), bytes.fromhex(c["raw_hex"]))
        for c in self.fx["rle_invalid"]:
            with self.subTest(c["name"]), self.assertRaises(ValueError):
                tp.packbits_decode(bytes.fromhex(c["rle_hex"]), expected=c["out_len"])

    def test_frames_payload_and_lines(self):
        for f in self.fx["frames"]:
            with self.subTest(f["name"]):
                black, red = expand(f["black"]), expand(f["red"])
                payload = tp.packbits_encode(black) + tp.packbits_encode(red)
                self.assertEqual(payload.hex(), f["payload_hex"])
                self.assertEqual((len(payload), tp.crc32(payload)), (f["payload_size"], f["payload_crc32"]))
                lines = [host.encode(m).decode("utf-8").rstrip() for m in tp.build_frames(payload, chunk=f["chunk_size"])]
                self.assertEqual(lines[:len(f["lines"])], f["lines"][:len(lines)])


class ConfigTests(unittest.TestCase):
    def test_todo_saved_in_a_copy_and_other_keys_kept(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            path.write_text(json.dumps({"ota_token": "T", "v2": {"weather": {"on": True, "place": "上海"}}},
                                       ensure_ascii=False), encoding="utf-8")
            host.save_config(path, v2={"todo": tp.todo_config([item("写周报", True, True), item("")])})
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["ota_token"], "T")
            self.assertEqual(data["v2"]["weather"]["place"], "上海")
            self.assertEqual(data["v2"]["todo"], {"items": [item("写周报", True, True)]})
            self.assertEqual(tp.todo_items(data), [item("写周报", True, True)])
        self.assertEqual(tp.todo_items({}), [])
        self.assertEqual(tp.todo_items({"v2": {"todo": []}}), [])


class WorkerSequenceTests(unittest.TestCase):
    def setUp(self):
        self.events = queue.Queue()
        self.cfg = dict(host.DEFAULT_CONFIG, interval=1, baud=115200)

    def start(self, ser):
        p = patch.object(host, "serial", SimpleNamespace(Serial=lambda: ser, SerialException=OSError))
        p.start()
        self.addCleanup(p.stop)
        opts = host.v2_options(self.cfg)
        opts["mouse"] = False
        self.w = gui.HostWorker(self.cfg, "COM_TEST", self.events, tm_factory=lambda _p: FakeTm(snapshot()),
                                now_fn=lambda: NOW, options=opts)
        self.addCleanup(lambda: (self.w.stop(), self.w.join(10)))
        self.w.start()
        drain_until(self.events, lambda ev: sum(e[0] == "msg" for e in ev) >= 5)

    def result(self, tag, timeout=8):
        seen = drain_until(self.events, lambda ev: any(e[:2] == ("cmd", tag) for e in ev), timeout)
        return next(e[2] for e in seen if e[:2] == ("cmd", tag))

    def test_todo_sequence_goes_out_in_order(self):
        ser = V2Serial(ready=READY_V2)
        self.start(ser)
        n = len(ser.writes)
        frames, _ = tp.push_frames([item("写周报")], T0)
        self.w.request_command("todo", frames, 5)
        res = self.result("todo")
        self.assertEqual((res["ok"], res["sent"]), (True, len(frames)))
        got = [m for m in ser.writes[n:] if m.get("type") in ("img_begin", "img_chunk", "img_end", "refresh_now")]
        self.assertEqual(got, frames)

    def test_page_then_refresh_now(self):
        ser = V2Serial(ready=READY_V2)
        self.start(ser)
        n = len(ser.writes)
        self.w.request_command("page", [tp.page_msg("weather"), tp.refresh_now_msg()], 5)
        self.assertTrue(self.result("page")["ok"])
        self.assertEqual([m["type"] for m in ser.writes[n:] if m["type"] in ("page", "refresh_now")],
                         ["page", "refresh_now"])

    def test_old_firmware_stops_after_first_error_and_does_not_retry(self):
        class OldFw(V2Serial):
            def write(self, data):
                msg = json.loads(data)
                self.writes.append(msg)
                if msg.get("type") in ("img_begin", "img_chunk", "img_end", "page", "refresh_now"):
                    self.rx.put(b"ERR unknown_type\n")
                else:
                    self.rx.put(b"OK\n")
                return len(data)
        ser = OldFw(ready=READY_V2)
        self.start(ser)
        n = len(ser.writes)
        frames, _ = tp.push_frames([item("写周报")], T0)
        self.w.request_command("todo", frames, 5)
        res = self.result("todo")
        self.assertEqual((res["ok"], res["err"], res["sent"]), (False, "unknown_type", 0))
        self.assertEqual(gui.page_error_text(res["err"]), gui.OLD_FW_TEXT)
        wait_for(lambda: False, 1.2)                      # 又过了一轮推送：没有再发 img_*
        self.assertEqual(sum(m.get("type") == "img_begin" for m in ser.writes[n:]), 1)


class AppTodoTests(AppTestBase):
    def connect(self, app):
        app.toggle_connection()
        self.feed(("link", "serial", "v2"))
        return FakeWorker.instances[-1]

    def test_buttons_disabled_until_v2_link(self):
        app = self.make()
        self.assertEqual(str(app.refresh_now_btn.cget("state")), "disabled")
        self.assertEqual(str(app.todo_push_btn.cget("state")), "disabled")
        self.assertEqual(str(app.push_page_btn.cget("state")), "disabled")
        self.connect(app)
        self.assertEqual(str(app.refresh_now_btn.cget("state")), "normal")
        self.assertEqual(str(app.todo_push_btn.cget("state")), "normal")

    def saved_page(self):
        if not self.path.exists():
            return None
        return (self.read().get("v2") or {}).get("page")

    def test_radio_only_previews_and_push_button_sends_page_then_refresh_now_and_saves(self):
        app = self.make()
        w = self.connect(app)
        app.page_var.set("dash")
        app.on_page()
        self.assertEqual(w.commands, [])                              # 点单选框什么也不发
        self.assertIsNone(self.saved_page())                          # 也不保存
        self.assertEqual(app.view_page, "dash")
        self.assertEqual(app.page_status_var.get(), "预览：仪表盘 · 还没推送到屏幕")
        self.assertEqual(app.page_board_var.get(), "屏幕当前：自动")
        self.assertEqual(str(app.data_tabs.select()), str(app.preview_tab))      # 切到屏幕预览标签
        app.push_page()
        tag, msgs, _ = w.commands[-1]
        self.assertEqual((tag, msgs), ("page", [tp.page_msg("dash"), tp.refresh_now_msg()]))
        self.assertIsNone(self.saved_page())                          # 板子确认后才保存
        self.feed(("cmd", "page", {"ok": True, "err": ""}))
        self.assertEqual(self.saved_page(), "dash")
        self.assertEqual(app.page, "dash")
        self.assertEqual(app.page_board_var.get(), "屏幕当前：仪表盘")
        self.assertEqual(app.page_status_var.get(), gui.REFRESHING_TEXT)       # 固件没有开始事件：自己进入刷新中
        self.assertTrue(app.refreshing)
        for w in (app.push_page_btn, app.refresh_now_btn, app.todo_push_btn):
            self.assertEqual(str(w.cget("state")), "disabled")
        for r in app.page_radios:                                     # 单选框只是预览，刷新中也能点
            self.assertEqual(str(r.cget("state")), "normal")
        self.feed(("refreshed", datetime(2026, 10, 29, 23, 48, 5)))
        self.assertEqual(app.page_status_var.get(), "屏幕已刷新 23:48:05")
        self.assertFalse(app.refreshing)
        for w in (app.push_page_btn, app.refresh_now_btn, app.todo_push_btn):
            self.assertEqual(str(w.cget("state")), "normal")

    def test_failed_page_push_is_not_saved(self):
        app = self.make()
        self.connect(app)
        app.page_var.set("dash")
        app.on_page()
        app.push_page()
        self.feed(("cmd", "page", {"ok": False, "err": "timeout"}))
        self.assertIsNone(self.saved_page())
        self.assertEqual(app.page, "auto")
        self.assertFalse(app.refreshing)

    def test_push_todo_page_sends_image_first_only_when_edited_since_last_push(self):
        app = self.make(v2={"todo": {"items": [item("写周报")]}})
        w = self.connect(app)
        app.page_var.set("todo")
        app.on_page()
        self.assertEqual(w.commands, [])
        self.assertTrue(app._todo_view_ppm.startswith(b"P6\n400 300\n"))        # 预览标签里是待办页
        app.push_page()
        tag, msgs, _ = w.commands[-1]
        types = [m["type"] for m in msgs]
        self.assertEqual(tag, "todo")
        self.assertEqual((types[0], types[-3], types[-2], types[-1]), ("img_begin", "img_end", "page", "refresh_now"))
        self.assertEqual(types.count("refresh_now"), 1)               # 只刷新一次
        self.assertEqual(msgs[-2], tp.page_msg("todo"))
        self.assertEqual(str(app.push_page_btn.cget("state")), "disabled")
        self.feed(("cmd", "todo", {"ok": True, "err": "", "sent": len(msgs)}))
        self.assertEqual(self.saved_page(), "todo")
        self.feed(("refreshed", datetime(2026, 10, 29, 23, 48, 5)))
        app.push_page()                                               # 没改过：只推页面
        self.assertEqual(w.commands[-1][:2], ("page", [tp.page_msg("todo"), tp.refresh_now_msg()]))
        self.feed(("cmd", "page", {"ok": True, "err": ""}), ("refreshed", datetime(2026, 10, 29, 23, 50, 5)))
        app.todo_text_var.set("买牛奶")                               # 改了待办：预览标签跟着变，下次推送重新带图
        app.todo_add()
        self.assertIsNotNone(app._todo_view_ppm)
        app.push_page()
        self.assertEqual(w.commands[-1][0], "todo")

    def test_refreshing_ends_by_timeout(self):
        with patch.object(gui, "REFRESH_TIMEOUT_MS", 30):
            app = self.make()
            self.connect(app)
            app.refresh_now()
            self.feed(("cmd", "refresh", {"ok": True, "err": ""}))
            self.assertTrue(app.refreshing)
            self.assertTrue(self.pump(lambda: not app.refreshing))
        self.assertIn("没有收到刷新完成", app.page_status_var.get())
        self.assertEqual(str(app.refresh_now_btn.cget("state")), "normal")

    def test_ctrl_r_refreshes_only_when_free(self):
        app = self.make()
        self.assertEqual(app._on_ctrl_r(), "break")                   # 没连接：什么也不发
        w = self.connect(app)
        app._on_ctrl_r()
        self.assertEqual(w.commands[-1][:2], ("refresh", [tp.refresh_now_msg()]))
        self.feed(("cmd", "refresh", {"ok": True, "err": ""}))
        app._on_ctrl_r()
        self.assertEqual(len(w.commands), 1)                          # 刷新中不重复发

    def test_refresh_now_button(self):
        app = self.make()
        w = self.connect(app)
        app.refresh_now()
        self.assertEqual(w.commands[-1][:2], ("refresh", [tp.refresh_now_msg()]))

    def test_page_without_connection_previews_but_sends_nothing(self):
        app = self.make()
        app.page_var.set("dash")
        app.on_page()
        self.assertEqual(app.view_page, "dash")
        self.assertEqual(app.page_status_var.get(), "预览：仪表盘 · 还没推送到屏幕\n" + gui.DISCONNECTED_PAGE_TEXT)
        self.assertEqual(str(app.push_page_btn.cget("state")), "disabled")
        app.push_page()
        self.assertEqual(FakeWorker.instances, [])
        self.assertIsNone(self.saved_page())                          # 没推送就没有保存
        self.assertEqual(str(app.page_radios[0].cget("state")), "normal")   # 没有悄悄灰掉
        w = self.connect(app)                                          # 连上后不会补发
        self.assertEqual(w.commands, [])
        self.assertIsNone(self.saved_page())
        self.assertEqual(str(app.push_page_btn.cget("state")), "normal")

    def test_edit_list_saves_to_config(self):
        self.path.write_text(json.dumps({"ota_token": "T", "v2": {"weather": {"on": True}}}), encoding="utf-8")
        app = self.make()
        for text in ("写周报", "买牛奶", "打电话"):
            app.todo_text_var.set(text)
            app.todo_add()
        self.assertEqual([i["text"] for i in app.todo_items], ["写周报", "买牛奶", "打电话"])
        app.todo_tree.selection_set("2")
        self.root.update()
        app.todo_move(-1)
        self.assertEqual([i["text"] for i in app.todo_items], ["写周报", "打电话", "买牛奶"])
        app.todo_imp_var.set(True)
        app.on_todo_flags()
        app.todo_done_var.set(True)
        app.on_todo_flags()
        app.todo_text_var.set("买牛奶和鸡蛋")
        app.todo_update()
        app.todo_tree.selection_set("0")
        self.root.update()
        app.todo_delete()
        data = self.read()
        self.assertEqual(data["ota_token"], "T")
        self.assertTrue(data["v2"]["weather"]["on"])
        self.assertEqual(data["v2"]["todo"]["items"], [item("买牛奶和鸡蛋", True, True), item("买牛奶")])
        self.assertEqual(len(app.todo_tree.get_children()), 2)

    def test_push_sends_frames_and_shows_preview(self):
        self.path.write_text(json.dumps({"v2": {"todo": {"items": [item("写周报"), item("紧急", False, True)]}}}),
                             encoding="utf-8")
        app = self.make(v2={"todo": {"items": [item("写周报"), item("紧急", False, True)]}})
        w = self.connect(app)
        app.push_todo()
        tag, frames, _ = w.commands[-1]
        self.assertEqual(tag, "todo")
        self.assertEqual([f["type"] for f in frames][0::len(frames) - 1], ["img_begin", "refresh_now"])
        self.assertTrue(app.todo_ppm.startswith(b"P6\n400 300\n"))
        self.assertIsNone(app.preview_ppm)                                           # 主预览不被待办占用
        self.assertEqual(str(app.todo_push_btn.cget("state")), "disabled")          # 传输中不能再点
        self.feed(("cmd", "todo", {"ok": True, "err": "", "sent": len(frames)}))
        self.assertEqual(app.todo_status_var.get(), gui.REFRESHING_TEXT)
        self.assertEqual(str(app.todo_push_btn.cget("state")), "disabled")          # 刷新中也不能点
        self.feed(("refreshed", datetime(2026, 10, 29, 23, 48, 5)))
        self.assertEqual(app.todo_status_var.get(), "屏幕已刷新 23:48:05")
        self.assertEqual(str(app.todo_push_btn.cget("state")), "normal")

    def test_failed_push_marks_thumbnail_as_not_delivered(self):
        app = self.make(v2={"todo": {"items": [item("写周报")]}})
        self.connect(app)
        app.push_todo()
        self.feed(("cmd", "todo", {"ok": False, "err": "timeout", "sent": 1}))
        self.assertIn("没有送达", app.todo_preview_note_var.get())
        self.assertFalse(app.refreshing)
        self.assertEqual(str(app.todo_push_btn.cget("state")), "normal")

    def test_old_firmware_shows_one_line_note_without_dialog(self):
        app = self.make()
        self.connect(app)
        app.push_todo()
        with patch("tkinter.messagebox.showerror") as dlg, patch("tkinter.messagebox.showwarning") as dlg2:
            self.feed(("cmd", "todo", {"ok": False, "err": "unknown_type", "sent": 0}))
            self.assertEqual(app.todo_status_var.get(), gui.OLD_FW_TEXT)
            app.page_var.set("todo")
            app.on_page()
            app.push_page()
            self.feed(("cmd", "page", {"ok": False, "err": "unknown_type", "sent": 0}))
            self.assertEqual(app.page_status_var.get(), gui.OLD_FW_TEXT)
        dlg.assert_not_called()
        dlg2.assert_not_called()
        self.assertEqual(len(FakeWorker.instances[-1].commands), 2)                  # 没有重试

    def test_preview_button_does_not_need_a_connection_and_uses_thumbnail_not_main_preview(self):
        app = self.make(v2={"todo": {"items": [item("写周报")]}})
        app.preview_todo()
        self.assertTrue(app.todo_ppm.startswith(b"P6\n400 300\n"))
        self.assertIsNone(app.preview_ppm)                                              # 主预览不被占用
        self.assertEqual((app._todo_thumb.width(), app._todo_thumb.height()), (gui.THUMB_W, gui.THUMB_H))
        self.assertIn("还没有推送", app.todo_preview_note_var.get())
        dash = b"P6\n400 300\n255\n" + b"\xff" * 360000
        app._on_preview(dash, "")
        self.assertIs(app.preview_ppm, dash)                                            # 仪表盘预览照常更新

    def test_enter_updates_selected_row_else_adds_and_mode_label_follows(self):
        app = self.make()
        self.assertIn("新增事项", app.todo_mode_var.get())
        self.assertEqual(str(app.todo_update_btn.cget("state")), "disabled")
        self.assertNotEqual(app.todo_empty.place_info(), {})                            # 空状态提示
        app.todo_text_var.set("写周报")
        app.todo_commit()
        self.assertEqual(app.todo_empty.place_info(), {})
        self.assertEqual([i["text"] for i in app.todo_items], ["写周报"])
        self.assertIsNone(app._selected_todo())                                         # 添加后回到「新增」
        app.todo_text_var.set("买牛奶")
        app.todo_commit()
        self.assertEqual(len(app.todo_items), 2)
        app.todo_tree.selection_set("0")
        self.root.update()
        self.assertIn("正在编辑第 1 条", app.todo_mode_var.get())
        self.assertEqual(app.todo_text_var.get(), "写周报")
        app.todo_text_var.set("写月报")
        app.todo_commit()                                                               # 选中 = 修改
        self.assertEqual([i["text"] for i in app.todo_items], ["写月报", "买牛奶"])
        app.todo_tree.selection_set("1")
        self.root.update()
        app.todo_cancel_edit()                                                          # Esc：回到新增
        self.assertIsNone(app._selected_todo())
        self.assertEqual(app.todo_text_var.get(), "")

    def test_delete_key_and_limit_message(self):
        app = self.make(v2={"todo": {"items": [item("a"), item("b")]}})
        app.todo_tree.selection_set("0")
        self.root.update()
        self.assertTrue(app.todo_tree.bind("<Delete>"))                 # Delete 键绑定的就是 todo_delete
        app.todo_delete()
        self.assertEqual([i["text"] for i in app.todo_items], ["b"])
        with patch.object(tp, "ITEMS_MAX", 1):
            app.todo_text_var.set("c")
            app.todo_add()
            self.assertEqual([i["text"] for i in app.todo_items], ["b"])
            self.assertEqual(app.todo_status_var.get(), "最多 1 条，先删掉一些")
            app._update_todo_mode()
            self.assertEqual(str(app.todo_add_btn.cget("state")), "disabled")


if __name__ == "__main__":
    unittest.main()
