#!/usr/bin/env python3
"""待办页的电脑端部分（不依赖 tkinter，GUI 共用）。

板子上的中文字库只有几百个字，所以待办页由电脑用 Pillow 整页画好（400x300，黑 / 红两个位平面，
每像素 1 位、按行、高位在前、1 = 有墨，两个平面都有墨时红色优先），压缩后用 img_begin / img_chunk / img_end
发给板子。协议和 PackBits 压缩的精确格式见 docs/dashboard-v2-protocol.md。

  1. 设置：config.json 的 `v2.todo` = {"items": [{"text", "done", "important"}]}
  2. 渲染：和仪表盘同一种样子（粗体大写标题、实线 / 点线分隔、方框复选框、完成项划线 + 灰点阵、重要项红色），
     不抗锯齿（先二值化再出图），所以电子墨水屏上边缘清晰
  3. 压缩 + 分块：PackBits、base64，每行不超过协议的 1024 字节
"""
from __future__ import annotations

import base64
import logging
import zlib
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont
from weather import UNSUPPORTED_MARKS, is_unsupported  # noqa: F401 - one list of "firmware lacks this message" ERRs

log = logging.getLogger("epaper_host.todo")

W, H = 400, 300
PLANE_BYTES = W // 8 * H            # 15000
IMG_ID = "todo"
MAX_COMPRESSED = 12 * 1024          # 固件接受的压缩后最大字节数（超过 ERR too_big）
CHUNK_RAW = 600                     # 每块原始字节数 -> base64 800 字符，整行约 870 字节
TEXT_MAX_CHARS = 120                # 一条待办最多存多少个字
ITEMS_MAX = 60                      # 最多存多少条
MAX_LINES_PER_ITEM = 2              # 超长的待办最多画两行（第二行末尾加省略号）

# 版面（像素）
MARGIN = 10
HEADER_H = 34                       # 标题行（含下面的实线）
FOOTER_H = 22                       # 底部状态行
FONT_PX = 16
LINE_H = 19
ROW_PAD = 4                         # 每条待办上下各留的空白
BOX = 13                            # 复选框边长
TEXT_X = MARGIN + BOX + 8

FONT_DIRS = (Path("C:/Windows/Fonts"), Path("/usr/share/fonts"), Path("/System/Library/Fonts"))
# 中文字体按「二值化后笔画最清晰」排：黑体在 16 px 上几乎是点阵字的效果
TEXT_FONTS = ("msyh.ttc", "simhei.ttf", "Deng.ttf", "NotoSansCJK-Regular.ttc", "PingFang.ttc")
BOLD_FONTS = ("msyhbd.ttc", "Dengb.ttf", "simhei.ttf", "arialbd.ttf")


# ---------------------------------------------------------------------------
# 1. 设置
# ---------------------------------------------------------------------------
def clean_text(text) -> str:
    return " ".join(str(text).split())[:TEXT_MAX_CHARS]


def normalize_items(raw) -> list[dict]:
    """任意（可能被手改坏的）配置 -> [{"text","done","important"}]；空文本、非对象的条目丢掉。"""
    out = []
    for it in raw if isinstance(raw, list) else []:
        if isinstance(it, str):
            it = {"text": it}
        if not isinstance(it, dict):
            continue
        text = clean_text(it.get("text") or "")
        if text:
            out.append({"text": text, "done": it.get("done") is True, "important": it.get("important") is True})
    return out[:ITEMS_MAX]


def todo_items(cfg: dict) -> list[dict]:
    v2 = cfg.get("v2")
    todo = v2.get("todo") if isinstance(v2, dict) else None
    return normalize_items(todo.get("items") if isinstance(todo, dict) else None)


def todo_config(items: list[dict]) -> dict:
    """要写进 `v2.todo` 的对象。"""
    return {"items": normalize_items(items)}


# ---------------------------------------------------------------------------
# 2. 渲染
# ---------------------------------------------------------------------------
def _find_font(names) -> Path | None:
    for d in FONT_DIRS:
        for n in names:
            p = d / n
            if p.is_file():
                return p
    return None


@lru_cache(maxsize=16)
def _font(bold: bool, size: int):
    path = _find_font(BOLD_FONTS if bold else TEXT_FONTS)
    if path is not None:
        try:
            return ImageFont.truetype(str(path), size)
        except OSError:
            pass
    log.warning("没找到中文字体，待办页里的汉字会显示成方框")
    try:
        return ImageFont.load_default(size)
    except TypeError:       # 旧 Pillow
        return ImageFont.load_default()


class Canvas:
    """黑、红两个平面（"L" 图，255 = 有墨），文字不抗锯齿。"""

    def __init__(self):
        self.planes = {"black": Image.new("L", (W, H), 0), "red": Image.new("L", (W, H), 0)}
        self._draw = {k: ImageDraw.Draw(v) for k, v in self.planes.items()}
        for d in self._draw.values():
            d.fontmode = "1"

    def text(self, xy, s, font, color="black", gray=False):
        """gray = 灰色（只留棋盘格上的点）。"""
        if not gray:
            self._draw[color].text(xy, s, fill=255, font=font)
            return
        mask = Image.new("L", (W, H), 0)
        md = ImageDraw.Draw(mask)
        md.fontmode = "1"
        md.text(xy, s, fill=255, font=font)
        self.planes[color].paste(ImageChops.lighter(self.planes[color], ImageChops.multiply(mask, _dither_mask())))
        self._draw[color] = ImageDraw.Draw(self.planes[color])
        self._draw[color].fontmode = "1"

    def rect(self, box, color="black", fill=False, width=1):
        if fill:
            self._draw[color].rectangle(box, fill=255)
        else:
            self._draw[color].rectangle(box, outline=255, width=width)

    def line(self, xy, color="black", width=1):
        self._draw[color].line(xy, fill=255, width=width)

    def hline_dotted(self, y, x0, x1, color="black"):
        for x in range(x0, x1 + 1, 2):
            self._draw[color].point((x, y), fill=255)

    def planes_1bit(self) -> tuple[Image.Image, Image.Image]:
        black = self.planes["black"].point(lambda v: 255 if v >= 128 else 0).convert("1")
        red = self.planes["red"].point(lambda v: 255 if v >= 128 else 0).convert("1")
        return black, red


@lru_cache(maxsize=1)
def _dither_mask() -> Image.Image:
    m = Image.new("L", (W, H), 0)
    px = m.load()
    for y in range(H):
        for x in range(W):
            if (x + y) % 2 == 0:
                px[x, y] = 255
    return m


def _text_width(font, s: str) -> int:
    return int(round(font.getlength(s)))


def wrap_text(text: str, font, max_w: int, max_lines: int = MAX_LINES_PER_ITEM) -> list[str]:
    """按像素宽度折行：能在空格处断就在空格处断，否则逐字断；超过 max_lines 行时最后一行以「…」结尾。"""
    lines: list[str] = []
    cur = ""
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if _text_width(font, cur + ch) <= max_w or not cur:
            cur += ch
            i += 1
            continue
        cut = cur.rfind(" ") if ch != " " and ord(ch) < 0x2E80 else -1     # 西文单词尽量不拆开
        if cut > 0:
            lines.append(cur[:cut])
            cur = cur[cut + 1:]
        else:
            lines.append(cur)
            cur = ""
        if ch == " " and not cur:
            i += 1                                               # 行首不留空格
    if cur or not lines:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and _text_width(font, last + "…") > max_w:
            last = last[:-1]
        lines[-1] = last + "…"
    return lines


def _draw_checkbox(cv: Canvas, x: int, y: int, done: bool, important: bool) -> None:
    color = "red" if important and not done else "black"
    cv.rect((x, y, x + BOX - 1, y + BOX - 1), color, width=2 if important and not done else 1)
    if done:
        cv.line([(x + 3, y + 6), (x + 5, y + 9), (x + 10, y + 3)], "black", width=2)


def layout_items(items: list[dict], font, avail_h: int) -> tuple[list[tuple[dict, list[str]]], int]:
    """从上往下放，放不下的留给「+N more」。返回 ([(item, 行列表)], 没放下的条数)。
    没放下时要给「+N more」那一行留位置。"""
    max_w = W - MARGIN - TEXT_X
    placed: list[tuple[dict, list[str]]] = []
    used = 0
    more_h = LINE_H + ROW_PAD
    for idx, it in enumerate(items):
        lines = wrap_text(it["text"], font, max_w)
        h = len(lines) * LINE_H + 2 * ROW_PAD
        remaining_after = len(items) - idx - 1
        reserve = more_h if remaining_after else 0
        if used + h + reserve > avail_h:
            # 这条放不下；若连同「+N more」的位置都不够，再把最后放下的那条也让出去
            while placed and used + more_h > avail_h:
                _, ls = placed.pop()
                used -= len(ls) * LINE_H + 2 * ROW_PAD
            return placed, len(items) - len(placed)
        placed.append((it, lines))
        used += h
    return placed, 0


def render_todo(items: list[dict], now: datetime | None = None) -> tuple[Image.Image, Image.Image]:
    """-> (黑平面, 红平面)，都是 400x300 的 "1" 图，1 = 有墨（红色优先）。"""
    now = now or datetime.now()
    items = normalize_items(items)
    cv = Canvas()
    f_title, f_text = _font(True, 22), _font(False, FONT_PX)
    f_small, f_status = _font(False, 12), _font(True, 15)

    # 标题行：TODO（粗体大写）、右上角日期，下面一条实线
    cv.text((MARGIN, 4), "TODO", f_title)
    date = now.strftime("%m-%d")
    cv.text((W - MARGIN - _text_width(f_title, date), 4), date, f_title)
    cv.line([(MARGIN, HEADER_H - 3), (W - MARGIN - 1, HEADER_H - 3)], width=2)

    done_n = sum(it["done"] for it in items)
    body_top, body_bot = HEADER_H, H - FOOTER_H - 4
    if not items:
        msg = "NO TODOS"
        cv.text(((W - _text_width(f_title, msg)) // 2, 110), msg, f_title)
        sub = "在电脑上的上位机里添加待办"
        cv.text(((W - _text_width(f_text, sub)) // 2, 145), sub, f_text, gray=True)
    else:
        placed, hidden = layout_items(items, f_text, body_bot - body_top)
        y = body_top
        for n, (it, lines) in enumerate(placed):
            if n:
                cv.hline_dotted(y, MARGIN, W - MARGIN - 1)
            y += ROW_PAD
            done, imp = it["done"], it["important"]
            _draw_checkbox(cv, MARGIN, y + (LINE_H - BOX) // 2 + 1, done, imp)
            color = "red" if imp and not done else "black"
            for k, ln in enumerate(lines):
                ty = y + k * LINE_H
                cv.text((TEXT_X, ty), ln, f_text, color)  # 已完成：实心字 + 删除线（网点灰字在墨水屏上看不清）
                if done:
                    sy = ty + LINE_H // 2 + 1
                    cv.line([(TEXT_X - 2, sy), (TEXT_X + _text_width(f_text, ln) + 1, sy)])
            if imp and not done:                       # 红色感叹号
                ex = W - MARGIN - 4
                cv.rect((ex, y + 2, ex + 2, y + 10), "red", fill=True)
                cv.rect((ex, y + 13, ex + 2, y + 15), "red", fill=True)
            y += len(lines) * LINE_H + ROW_PAD
        if hidden:
            cv.hline_dotted(y, MARGIN, W - MARGIN - 1)
            cv.text((TEXT_X, y + ROW_PAD), f"+{hidden} more", f_text, "black")

    # 底部状态行：只放完成进度。不画时钟：图片是推一次画一次的，画进去的 HH:MM 一会儿就过期了
    fy = H - FOOTER_H
    cv.line([(MARGIN, fy - 2), (W - MARGIN - 1, fy - 2)])
    right = f"{done_n}/{len(items)} DONE" if items else "0 ITEMS"
    cv.text((W - MARGIN - _text_width(f_status, right), fy + 1), right, f_status)
    return cv.planes_1bit()


def to_rgb(black: Image.Image, red: Image.Image) -> Image.Image:
    """两个平面 -> 预览用的 RGB 图（白底、黑、红；红色优先）。"""
    img = Image.new("RGB", (W, H), (255, 255, 255))
    img.paste((0, 0, 0), mask=black)
    img.paste((200, 20, 20), mask=red)
    return img


def to_ppm(img: Image.Image) -> bytes:
    rgb = img.convert("RGB")
    return b"P6\n%d %d\n255\n" % rgb.size + rgb.tobytes()


def pack_plane(img: Image.Image) -> bytes:
    """"1" 图 -> 每像素 1 位、按行、高位在前、1 = 有墨（Pillow 的 "1" 模式里白 = 1，正好是有墨）。"""
    data = img.tobytes()
    if len(data) != PLANE_BYTES:
        raise ValueError(f"plane is {len(data)} bytes, expected {PLANE_BYTES}")
    return data


# ---------------------------------------------------------------------------
# 3. PackBits 压缩 + 分块
# ---------------------------------------------------------------------------
def packbits_encode(data: bytes) -> bytes:
    """标准 PackBits：控制字节 n；0..127 = 后面 n+1 个原样字节；129..255 = 把后面 1 个字节重复 257-n 次（2..128 次）；
    128 不用。3 个及以上相同字节才编成重复，较短的相同字节混在原样字节里（和大多数实现一样）。"""
    out = bytearray()
    i, n = 0, len(data)
    lit_start = 0

    def flush(end: int) -> None:
        s = lit_start
        while s < end:
            k = min(128, end - s)
            out.append(k - 1)
            out.extend(data[s:s + k])
            s += k

    while i < n:
        j = i + 1
        while j < n and data[j] == data[i] and j - i < 128:
            j += 1
        run = j - i
        if run >= 3:
            flush(i)
            out.append(257 - run)
            out.append(data[i])
            i = lit_start = j
        else:
            i = j
    flush(n)
    return bytes(out)


def packbits_decode(data: bytes, expected: int | None = None) -> bytes:
    """packbits_encode 的逆运算（测试和自检用；格式不对抛 ValueError）。"""
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        c = data[i]
        i += 1
        if c < 128:
            if i + c + 1 > n:
                raise ValueError("truncated literal run")
            out += data[i:i + c + 1]
            i += c + 1
        elif c > 128:
            if i >= n:
                raise ValueError("truncated repeat run")
            out += bytes([data[i]]) * (257 - c)
            i += 1
    if expected is not None and len(out) != expected:
        raise ValueError(f"decoded {len(out)} bytes, expected {expected}")
    return bytes(out)


def compress_planes(black: Image.Image, red: Image.Image) -> bytes:
    """黑平面的 PackBits 流紧接着红平面的 PackBits 流（各自独立，各自解出 15000 字节）。"""
    comp = packbits_encode(pack_plane(black)) + packbits_encode(pack_plane(red))
    if len(comp) > MAX_COMPRESSED:
        raise ValueError(f"compressed image too big ({len(comp)} > {MAX_COMPRESSED})")
    return comp


def crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def build_frames(comp: bytes, img_id: str = IMG_ID, chunk: int = CHUNK_RAW) -> list[dict]:
    """压缩后的字节 -> [img_begin, img_chunk..., img_end]。"""
    frames = [{"v": 2, "type": "img_begin", "id": img_id, "size": len(comp), "crc": crc32(comp)}]
    for off in range(0, len(comp), chunk):
        frames.append({"v": 2, "type": "img_chunk", "id": img_id, "off": off,
                       "d": base64.b64encode(comp[off:off + chunk]).decode("ascii")})
    frames.append({"v": 2, "type": "img_end", "id": img_id})
    return frames


def push_frames(items: list[dict], now: datetime | None = None, refresh: bool = True) -> tuple[list[dict], Image.Image]:
    """待办 -> (要依次发给板子的消息，refresh 时含最后的 refresh_now；预览图)。"""
    black, red = render_todo(items, now)
    frames = build_frames(compress_planes(black, red))
    return (frames + [refresh_now_msg()] if refresh else frames), to_rgb(black, red)


# ---------------------------------------------------------------------------
# 页面切换 / 立即刷新
# ---------------------------------------------------------------------------
PAGES = (("auto", "自动"), ("dash", "仪表盘"), ("weather", "天气"), ("todo", "待办"))
PAGE_KEYS = tuple(k for k, _ in PAGES)


def page_msg(page: str) -> dict:
    if page not in PAGE_KEYS:
        raise ValueError(f"unknown page {page!r}")
    return {"v": 2, "type": "page", "p": page}


def refresh_now_msg() -> dict:
    return {"v": 2, "type": "refresh_now"}
