"""仪表盘 v2 消息构造（纯函数）。契约见 docs/dashboard-v2-protocol.md。

输入 = TmSnapshot（tm_source.py）+ 鼠标读数 + 当前时间，输出 = dash / quota / models /
heat / mouse 五个 dict。所有数字在这里格式化成字符串，新鲜度、告警判断也在这里；固件只画。
每条消息编码后（含 `\\n`）保证 ≤ 1024 字节，超了就裁剪。
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta

from tm_source import TmSnapshot

MAX_LINE = 1024
HEAT_WEEKS = (4, 26)  # 热图整周数的允许范围（默认 10 周 = 7 行 × 10 列，见 DEFAULT_V2）
PRIMARIES = ("today", "total")
HEAT_ROWS = (5, 7)  # 热图每列几格；7 = 按星期对齐（默认），5 = 不按星期
REFRESH_MINUTES = (3, 5, 10, 15, 30, 60)  # 屏幕刷新间隔可选值；3 = 三色屏最短间隔（默认，不发 every）
HEAT_LEVELS = 12  # 热图密度档数（'1'–'9'、'A'–'C'；固件 1–8 档白→红，9–12 档红上加黑点）
HEAT_DIGITS = "0123456789ABC"
B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
MODEL_NAME_MAX = 18  # 固件显示上限；超过的部分固件也会截断，主机先截可省字节

# 新鲜度阈值（秒），见契约「状态字段 st」
TM_OK_S, TM_STALE_S = 15 * 60, 2 * 3600
MOUSE_OK_S, MOUSE_STALE_S = 10 * 60, 60 * 60

KINDS = ("dash", "mouse", "quota", "models", "heat")  # 发送顺序


# ---------------------------------------------------------------------------
# 格式化
# ---------------------------------------------------------------------------
def fmt_tokens(n) -> str:
    """3 位有效数字 + K/M/B/T：7_130_000_000 -> '7.13B'，969e6 -> '969M'，45_200 -> '45.2K'。"""
    n = float(n)
    if n < 1000:
        return str(int(n))
    units = "KMBT"
    v, i = n / 1000, 0
    while float(f"{v:.3g}") >= 1000 and i < len(units) - 1:  # 999.6K 先进位成 1000，再换单位
        v /= 1000
        i += 1
    v = float(f"{v:.3g}")
    return f"{v:.2f}{units[i]}" if v < 10 else f"{v:.1f}{units[i]}" if v < 100 else f"{v:.0f}{units[i]}"


def fmt_cost(x) -> str:
    return f"${float(x):,.2f}"


def fmt_countdown(seconds: float) -> str:
    """1h57m / 5d8h / 42m（向下取整到分钟）。"""
    minutes = max(0, int(seconds // 60))
    hours, mins = divmod(minutes, 60)
    if hours >= 24:
        return f"{hours // 24}d{hours % 24}h"
    return f"{hours}h{mins:02d}m" if hours else f"{mins}m"


def _aware(now: datetime) -> datetime:
    return now if now.tzinfo else now.astimezone()


def _hhmm(ts: datetime, now: datetime) -> str:
    return ts.astimezone(now.tzinfo).strftime("%H:%M")


def wire_size(msg: dict) -> int:
    """与 epaper_host.encode 同口径：紧凑 JSON + `\\n`，UTF-8 字节数。"""
    return len((json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))


# ---------------------------------------------------------------------------
# 新鲜度
# ---------------------------------------------------------------------------
def _bucket(age_s: float, ok_s: float, stale_s: float) -> str:
    return "ok" if age_s <= ok_s else "stale" if age_s <= stale_s else "na"


def tm_state(snap: TmSnapshot | None, now: datetime) -> str:
    if snap is None or snap.observed_at is None:
        return "na"
    return _bucket((now - snap.observed_at).total_seconds(), TM_OK_S, TM_STALE_S)


def mouse_state(reading, now: datetime) -> str:
    if reading is None:
        return "na"
    status = getattr(reading, "status", "na")
    if status == "off":
        return "off"
    if status != "ok":
        return "na"
    at = getattr(reading, "observed_at", None)
    if at is None:
        return "na"
    return _bucket(now.timestamp() - at, MOUSE_OK_S, MOUSE_STALE_S)


# ---------------------------------------------------------------------------
# 超长裁剪
# ---------------------------------------------------------------------------
def _shrink_models(msg: dict) -> bool:
    m = msg.get("m") or []
    long = [x for x in m if len(x[0]) > MODEL_NAME_MAX]
    if long:
        for x in long:
            x[0] = x[0][:MODEL_NAME_MAX - 1] + "~"
        return True
    if m:
        m.pop()
        return True
    return False


def _shrink_quota(msg: dict) -> bool:
    ps = msg.get("p") or []
    if any(len(p.get("plan", "")) > 8 for p in ps):
        for p in ps:
            if len(p.get("plan", "")) > 8:
                p["plan"] = p["plan"][:7] + "~"
        return True
    if any("rst" in it for p in ps for it in p.get("i", [])):
        for p in ps:
            for it in p.get("i", []):
                it.pop("rst", None)
        return True
    for p in reversed(ps):
        if p.get("txt"):
            p["txt"].pop()
            if not p["txt"]:
                del p["txt"]
            return True
    for p in reversed(ps):  # 先从最后一家删额度项，空了再删整家
        if p.get("i"):
            p["i"].pop()
            return True
    if ps:
        ps.pop()
        return True
    return False


_SHRINKERS = {"models": _shrink_models, "quota": _shrink_quota}


def fit(msg: dict, limit: int = MAX_LINE) -> dict:
    """裁剪到编码后 ≤ limit 字节。能裁的只有 models / quota；其余消息结构固定，不会超。"""
    shrink = _SHRINKERS.get(msg.get("type"))
    while wire_size(msg) > limit:
        if shrink is None or not shrink(msg):
            raise ValueError(f"{msg.get('type')} message cannot fit in {limit} bytes")
    return msg


# ---------------------------------------------------------------------------
# 五种消息
# ---------------------------------------------------------------------------
def build_dash(snap: TmSnapshot | None, now: datetime, primary: str = "today", refresh_min: int = 3) -> dict:
    """main = 展开的周期（带千分位全数字 + 费用），alt = 另一个周期（缩写小字）。
    time = Token Monitor 设备最新的 updatedAt（本地时间），不是当前时间。
    refresh_min 比最短间隔长时带 every（秒）：板子显示过数据画面后，最多这么久刷新一次。"""
    now = _aware(now)
    st = tm_state(snap, now)
    msg: dict = {"v": 2, "type": "dash", "st": st}
    if refresh_min in REFRESH_MINUTES and refresh_min > REFRESH_MINUTES[0]:
        msg["every"] = refresh_min * 60
    if snap is not None and snap.observed_at is not None:
        msg["time"] = snap.observed_at.astimezone(now.tzinfo).strftime("%m-%d %H:%M")
    if snap is not None and st != "na":
        today = ("TODAY", snap.today_tokens, snap.today_cost)
        total = ("TOTAL", snap.total_tokens, snap.total_cost)
        first, other = (total, today) if primary == "total" else (today, total)
        msg["main"] = {"l": first[0], "tok": f"{int(first[1]):,}", "cost": fmt_cost(first[2])}
        msg["alt"] = {"l": other[0], "tok": fmt_tokens(other[1])}
    return fit(msg)


def _provider_state(p, now: datetime) -> str:
    if not p.ok or p.updated_at is None:
        return "na"
    st = _bucket((now - p.updated_at).total_seconds(), TM_OK_S, TM_STALE_S)
    return "stale" if st == "ok" and p.stale else st


def _text_segment(t, now: datetime) -> dict:
    """`C/G 72%/64%`：缺的或重置时刻已过的读数写 `--`；任一读数 ≤10 就告警 w:1。
    非百分比的项（DeepSeek 今日花费）原样写 `Today ¥7.42`，不告警。"""
    if t.value is not None:
        return {"s": f"{t.name} {t.value}"}
    shown = []
    for i, left in enumerate(t.lefts):
        rst = t.resets[i] if i < len(t.resets) else None
        if left is not None and rst is not None and (rst - now).total_seconds() <= 0:
            left = None
        shown.append(left)
    seg: dict = {"s": f"{t.name} " + "/".join("--" if v is None else f"{v}%" for v in shown)}
    if any(v is not None and v <= 10 for v in shown):
        seg["w"] = 1
    return seg


def build_quota(snap: TmSnapshot | None, now: datetime) -> dict:
    now = _aware(now)
    st = tm_state(snap, now)
    msg: dict = {"v": 2, "type": "quota", "st": st}
    if snap is None or st == "na":
        return msg
    out = []
    for p in snap.quota:
        entry: dict = {"n": p.name}
        if p.plan:
            entry["plan"] = p.plan
        pst = _provider_state(p, now)
        if pst != "ok":
            entry["st"] = pst
        if pst != "na":
            if p.bal is not None:
                entry["bal"] = p.bal
                txt = [_text_segment(t, now) for t in p.txt]  # 余额类也可带一行字（今日花费）
                if txt:
                    entry["txt"] = txt
            else:
                items = []
                for it in p.items:
                    left, rst = it.left, None
                    if it.resets_at is not None:
                        remain = (it.resets_at - now).total_seconds()
                        if remain > 0:
                            rst = fmt_countdown(remain)
                        else:  # 重置时刻已过，但还没取得新读数：不假定变回 100
                            left = None
                    item: dict = {"l": it.l, "left": left}
                    if rst is not None:
                        item["rst"] = rst
                    items.append(item)
                if items:
                    entry["i"] = items
                txt = [_text_segment(t, now) for t in p.txt]
                if txt:
                    entry["txt"] = txt
        out.append(entry)
    msg["p"] = out
    return fit(msg)


def build_models(snap: TmSnapshot | None, now: datetime, primary: str = "today") -> dict:
    now = _aware(now)
    st = tm_state(snap, now)
    total = primary == "total"
    msg: dict = {"v": 2, "type": "models", "st": st, "scope": "TOTAL" if total else "TODAY"}
    if snap is not None and st != "na":
        rows = snap.models if total else snap.today_models
        msg["m"] = [[m.name, fmt_tokens(m.tokens), m.pct] for m in rows]
    return fit(msg)


def heat_window(today, weeks: int):
    """整周窗口（一周从周日开始）：最近 `weeks` 周，起点是最早那周的周日，终点是今天。返回 (start, 天数)。"""
    start = today - timedelta(days=(today.weekday() + 1) % 7 + 7 * (weeks - 1))   # weekday(): 周一 = 0，周日 = 6
    return start, (today - start).days + 1


def build_heat(snap: TmSnapshot | None, now: datetime, rows: int = 7, weeks: int = 15, calendar: bool = False) -> dict:
    """calendar=True：加 view:"cal"，板子把 ACTIVITY 块画成当月日历（标题 CALENDAR）。"""
    now = _aware(now)
    end = now.date()
    start, days = heat_window(end, weeks)
    st = tm_state(snap, now)
    msg: dict = {"v": 2, "type": "heat", "st": st, "start": start.isoformat(), "end": end.isoformat(),
                 "rows": rows if rows in HEAT_ROWS else 7}
    if calendar:
        msg["view"] = "cal"
    if snap is None or st == "na":
        return msg
    vals: list[int | None] = []
    for i in range(days):
        day = (start + timedelta(days=i)).isoformat()
        if day in snap.daily:
            vals.append(snap.daily[day])
        elif snap.daily_known_from is not None and day >= snap.daily_known_from:
            vals.append(0)  # 所有设备都已在记录，那天没有记录 = 没用
        else:
            vals.append(None)
    known = [v for v in vals if v is not None]
    peak = max(known, default=0)
    d, t = [], []
    for v in vals:
        if v is None:
            d.append("-")
            t.append("-")
        elif v <= 0:
            d.append("0")
            t.append(B64[0])
        else:
            d.append(HEAT_DIGITS[min(HEAT_LEVELS, -(-v * HEAT_LEVELS // peak))])  # ceil(v/peak*12)，整数运算避免浮点边界
            t.append(B64[min(63, math.floor(v * 63 / peak + 0.5))])
    msg.update(d="".join(d), t="".join(t))
    if known:
        msg["peak"] = fmt_tokens(peak)
    msg["active"] = sum(1 for v in vals if v)
    return fit(msg)


def build_mouse(reading, now: datetime) -> dict:
    """reading 为 None（鼠标模块不可用）-> st:"na"。字段见 mouse_rapoo.MouseReading。"""
    now = _aware(now)
    msg: dict = {"v": 2, "type": "mouse", "st": mouse_state(reading, now)}
    if reading is None:
        return msg
    pct = getattr(reading, "pct", None)
    if msg["st"] != "na":
        msg["pct"] = None if pct is None else max(0, min(100, int(pct)))
        chg = getattr(reading, "charging", None)
        if chg is not None:
            msg["chg"] = bool(chg)
        at = getattr(reading, "observed_at", None)
        if at is not None:
            msg["at"] = _hhmm(datetime.fromtimestamp(at, now.tzinfo), now)
    return fit(msg)


def build(kind: str, snap: TmSnapshot | None, reading, now: datetime,
          primary: str = "today", heat_rows: int = 7, heat_weeks: int = 10, calendar: bool = False,
          refresh_min: int = 3) -> dict:
    if kind == "mouse":
        return build_mouse(reading, now)
    if kind == "heat":
        return build_heat(snap, now, heat_rows, heat_weeks, calendar)
    if kind == "quota":
        return build_quota(snap, now)
    if kind == "dash":
        return build_dash(snap, now, primary, refresh_min)
    return build_models(snap, now, primary)


def build_all(snap: TmSnapshot | None, reading, now: datetime,
              primary: str = "today", heat_rows: int = 7, heat_weeks: int = 10,
              calendar: bool = False, refresh_min: int = 3) -> dict[str, dict]:
    """五条消息，顺序 dash / mouse / quota / models / heat。"""
    return {kind: build(kind, snap, reading, now, primary, heat_rows, heat_weeks, calendar, refresh_min)
            for kind in KINDS}
