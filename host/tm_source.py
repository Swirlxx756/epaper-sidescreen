"""Token Monitor Hub 本地文件 -> TmSnapshot（只读）。

Token Monitor 桌面程序是 Hub，约每分钟把所有设备的数据写进
`%APPDATA%\\Token Monitor\\hub-devices.json`。本模块只读这个文件，不调用任何 CLI。
文件可能正在被写（JSON 不完整）：解析失败就沿用上次的好结果。

TmSnapshot 只放「原始事实」（数字、datetime）；倒计时、新鲜度、格式化都在
dashboard_v2.py 里按「当前时间」算，这样同一份快照可以反复用。
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("epaper_host.tm")

# 额度只显示这四家，顺序固定；值 = 屏幕上的名字
PROVIDERS = {"claude": "Claude", "codex": "Codex", "antigravity": "Antigrav", "deepseek": "DeepSeek"}
MAX_MODELS = 5
MAX_ITEMS = 2  # 每家最多 2 个额度条：5h、Wk

# Antigravity 的窗口标签是 "<模型组> 5-hour" / "<模型组> weekly"
_WINDOW_SUFFIX = re.compile(r"\s*(?:\d+-hour|weekly|daily|monthly)\s*$", re.IGNORECASE)
_CURRENCY = {"CNY": "¥", "USD": "$"}
_PLAN_ABBR = {"pay-as-you-go": "PAYG"}


def default_tm_file() -> Path:
    return Path(os.environ.get("APPDATA", "")) / "Token Monitor" / "hub-devices.json"


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class QuotaItem:
    l: str                          # 屏幕标签：5h / Wk
    left: int | None                # 剩余百分比 0-100，None = 未知
    resets_at: datetime | None      # 重置时刻（aware）；倒计时由 dashboard_v2 按当前时间算


@dataclass(frozen=True)
class QuotaText:
    """不画条、只写一行字的额度项（Antigravity 的 Claude/GPT 组、Argon，Claude 的 Fable，DeepSeek 今日花费）。"""
    name: str                       # "C/G" / "4 Argon" / "Fable" / "Today"
    lefts: tuple[int | None, ...]   # 剩余百分比，一个或 5h/周两个
    resets: tuple[datetime | None, ...] = ()   # 与 lefts 对应的重置时刻（过期的读数显示 --）
    value: str | None = None        # 非百分比的值（如 "¥7.42"）；有它就忽略 lefts


@dataclass(frozen=True)
class QuotaProvider:
    key: str                        # claude / codex / antigravity / deepseek
    name: str                       # 显示名
    plan: str                       # accountLabel（DeepSeek 的 Pay-as-you-go 缩成 PAYG）
    items: tuple[QuotaItem, ...] = ()
    txt: tuple[QuotaText, ...] = ()
    bal: str | None = None          # 余额类："¥38.66"
    updated_at: datetime | None = None
    stale: bool = False             # Token Monitor 自己标的 stale
    ok: bool = True                 # False = 这家有记录但没有 status=ok 的读数


@dataclass(frozen=True)
class ModelShare:
    name: str
    tokens: int
    pct: int                        # 占对应范围（累计 / 今日）总量的整数百分比


@dataclass
class TmSnapshot:
    total_tokens: int = 0
    total_cost: float = 0.0
    today_tokens: int = 0
    today_cost: float = 0.0
    models: tuple[ModelShare, ...] = ()        # 累计（allTime）前 5
    today_models: tuple[ModelShare, ...] = ()  # 今日前 5（今天已过期的设备不计入）
    quota: tuple[QuotaProvider, ...] = ()
    daily: dict[str, int] = field(default_factory=dict)         # 日期 -> token（各设备相加）
    daily_cost: dict[str, float] = field(default_factory=dict)
    # 各设备都已开始记录的最早日期：这一天起，没有记录 = 真的 0；之前没记录 = 未知
    daily_known_from: str | None = None
    observed_at: datetime | None = None                         # 各设备 updatedAt 里最新的（aware, UTC）
    device_updated_at: dict[str, datetime] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _num(x) -> float:
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
        return 0.0
    return float(x)


def parse_iso(s) -> datetime | None:
    """ISO 8601（含 Z）-> aware datetime；无时区当 UTC；坏值 -> None。"""
    if not isinstance(s, str) or not s:
        return None
    try:
        t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _dict(x) -> dict:
    return x if isinstance(x, dict) else {}


def _list(x) -> list:
    return x if isinstance(x, list) else []


def _round_half_up(x: float) -> int:
    return math.floor(x + 0.5)


def _money(amount, currency) -> str | None:
    if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(amount):
        return None
    cur = str(currency or "").upper()
    sym = _CURRENCY.get(cur)
    return f"{sym}{amount:,.2f}" if sym else f"{amount:,.2f} {cur}".strip()


# ---------------------------------------------------------------------------
# 额度
# ---------------------------------------------------------------------------
def _left(window: dict) -> int | None:
    used = window.get("usedPercent")
    if isinstance(used, bool) or not isinstance(used, (int, float)) or not math.isfinite(used):
        return None
    return max(0, min(100, _round_half_up(100 - used)))


def _bars(windows: list) -> list[QuotaItem]:
    """条形项：session -> 5h，weekly -> Wk（无标签的窗口；带标签的如 Fable 不要）。"""
    items = []
    for w in windows:
        kind = w.get("kind")
        if str(w.get("label") or "").strip() or kind not in ("session", "weekly"):
            continue
        left = _left(w)
        if left is not None:
            items.append(QuotaItem("5h" if kind == "session" else "Wk", left, parse_iso(w.get("resetsAt"))))
    return items[:MAX_ITEMS]


def _antigravity(windows: list) -> tuple[list[QuotaItem], list[QuotaText]]:
    """Antigravity 按标签关键词分组：Gemini -> 5h / Wk 两条；Claude/GPT -> `C/G 5h%/周%`；
    Argon -> `4 Argon 周%`。缺哪个就少哪个。"""
    gemini: dict[str, QuotaItem] = {}
    cg: dict[str, tuple[int | None, datetime | None]] = {}
    argon: tuple[str, int | None, datetime | None] | None = None
    for w in windows:
        kind, label = w.get("kind"), str(w.get("label") or "").strip()
        low = label.lower()
        left, rst = _left(w), parse_iso(w.get("resetsAt"))
        which = "5h" if kind == "session" else "Wk" if kind == "weekly" else None
        if which is None:
            continue
        if "argon" in low:
            name = _WINDOW_SUFFIX.sub("", label)
            name = re.sub(r"^gemini\s+", "", name, flags=re.IGNORECASE).strip() or "Argon"
            argon = (name, left, rst)
        elif "claude" in low or "gpt" in low:
            cg[which] = (left, rst)
        elif "gemini" in low and left is not None:
            gemini[which] = QuotaItem(which, left, rst)
    items = [gemini[k] for k in ("5h", "Wk") if k in gemini]
    txt = []  # 顺序：Argon 在前（靠左），C/G 在后（固件把最后一段贴右边缘）
    if argon:
        txt.append(QuotaText(argon[0], (argon[1],), (argon[2],)))
    if cg:
        pair = [cg.get("5h", (None, None)), cg.get("Wk", (None, None))]
        txt.append(QuotaText("C/G", tuple(x[0] for x in pair), tuple(x[1] for x in pair)))
    return items, txt


def _convert_provider(key: str, p: dict) -> QuotaProvider:
    windows = [w for w in _list(p.get("windows")) if isinstance(w, dict)]
    txt: list[QuotaText] = []
    if key == "antigravity":
        items, txt = _antigravity(windows)
    else:
        items = _bars(windows)
        # 带标签的周窗口（Claude 的 Fable）不画条，写成一行字 `Fable 94%`
        for w in windows:
            label = str(w.get("label") or "").strip()
            if label and w.get("kind") == "weekly":
                txt.append(QuotaText(_WINDOW_SUFFIX.sub("", label).strip() or label, (_left(w),),
                                     (parse_iso(w.get("resetsAt")),)))
    bal = None
    b = _dict(p.get("balance"))
    if not items and b:  # 余额类（DeepSeek）：今日花费写成一行字 `Today ¥7.42`
        spend = _money(b.get("todaySpend"), b.get("currency"))
        if spend is not None:
            txt.append(QuotaText("Today", (), value=spend))
    if not items and not any(t.lefts for t in txt):
        bal = _money(b.get("amount"), b.get("currency"))
        if bal is None:  # 退而求其次：billing 窗口的 remaining
            for w in windows:
                if w.get("kind") == "billing":
                    bal = _money(w.get("remaining"), w.get("currency"))
                    if bal:
                        break
    plan = str(p.get("accountLabel") or p.get("planLabel") or "").strip()
    plan = _PLAN_ABBR.get(plan.lower(), plan)
    return QuotaProvider(key=key, name=PROVIDERS[key], plan=plan, items=tuple(items), txt=tuple(txt),
                         bal=bal, updated_at=parse_iso(p.get("updatedAt")),
                         stale=bool(p.get("stale")), ok=True)


def _pick_quota(devices: list[dict]) -> tuple[QuotaProvider, ...]:
    """每家选「status=ok 且 updatedAt 最新」的一条（跨设备）。"""
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    out = []
    for key, name in PROVIDERS.items():
        ok_best, ok_ts = None, epoch
        other = None
        for dev in devices:
            for p in _list(_dict(dev.get("limits")).get("providers")):
                if not isinstance(p, dict) or str(p.get("provider", "")).lower() != key:
                    continue
                status = p.get("status")
                if status == "ok":
                    ts = parse_iso(p.get("updatedAt")) or epoch
                    if ok_best is None or ts > ok_ts:
                        ok_best, ok_ts = p, ts
                elif status != "notConfigured":
                    other = p
        if ok_best is not None:
            out.append(_convert_provider(key, ok_best))
        elif other is not None:  # 有记录但取不到：占位，屏幕显示 --
            out.append(QuotaProvider(key=key, name=name, plan="", ok=False,
                                     updated_at=parse_iso(other.get("updatedAt"))))
    return tuple(out)


# ---------------------------------------------------------------------------
# 快照
# ---------------------------------------------------------------------------
def _rank(sums: dict[str, float], total: int) -> tuple[ModelShare, ...]:
    """按 token 降序取前 5，占比的分母是该范围的总量。"""
    ranked = sorted(((n, int(t)) for n, t in sums.items() if t > 0), key=lambda kv: (-kv[1], kv[0]))
    denom = total or sum(t for _, t in ranked)
    return tuple(ModelShare(n, t, _round_half_up(t * 100 / denom) if denom else 0)
                 for n, t in ranked[:MAX_MODELS])


def build_snapshot(raw: dict, now: datetime | None = None) -> TmSnapshot:
    """hub-devices.json 的内容 -> TmSnapshot。`now` 只用来判断设备的「今天」是否已过期。"""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.astimezone()
    snap = TmSnapshot()
    devices = {did: d for did, d in _dict(_dict(raw).get("devices")).items() if isinstance(d, dict)}
    model_sum: dict[str, float] = {}
    today_sum: dict[str, float] = {}
    first_dates: list[str] = []

    for did, dev in devices.items():
        periods = _dict(dev.get("periods"))
        all_time, today = _dict(periods.get("allTime")), _dict(periods.get("today"))
        snap.total_tokens += int(_num(all_time.get("totalTokens")))
        snap.total_cost += _num(all_time.get("costUsd"))
        # 设备的「今天」已过（那台没同步、跨天了）就不计入今日
        ends = parse_iso(_dict(_dict(dev.get("periodWindows")).get("today")).get("endsAt"))
        if ends is None or ends > now:
            snap.today_tokens += int(_num(today.get("totalTokens")))
            snap.today_cost += _num(today.get("costUsd"))
            for name, tokens in _dict(today.get("models")).items():
                today_sum[str(name)] = today_sum.get(str(name), 0.0) + _num(tokens)
        for name, tokens in _dict(all_time.get("models")).items():
            model_sum[str(name)] = model_sum.get(str(name), 0.0) + _num(tokens)

        daily = [r for r in _list(_dict(dev.get("history")).get("daily")) if isinstance(r, dict)]
        dates = [r["date"] for r in daily if isinstance(r.get("date"), str)]
        if dates:
            first_dates.append(min(dates))
        for r in daily:
            date = r.get("date")
            if isinstance(date, str):
                snap.daily[date] = snap.daily.get(date, 0) + int(_num(r.get("tokens")))
                snap.daily_cost[date] = snap.daily_cost.get(date, 0.0) + _num(r.get("cost"))

        upd = parse_iso(dev.get("updatedAt"))
        if upd is not None:
            snap.device_updated_at[did] = upd
            if snap.observed_at is None or upd > snap.observed_at:
                snap.observed_at = upd

    snap.daily_known_from = max(first_dates) if first_dates else None
    snap.models = _rank(model_sum, snap.total_tokens)
    snap.today_models = _rank(today_sum, snap.today_tokens)
    snap.quota = _pick_quota(list(devices.values()))
    return snap


class TmSource:
    """读 hub-devices.json；文件没变就不重读，读坏了就沿用上次的好结果。"""

    def __init__(self, path=None):
        self.path = Path(os.path.expandvars(str(path))) if path else default_tm_file()
        self._raw: dict | None = None
        self._key: tuple[int, int] | None = None
        self.last_error: str | None = None

    def refresh(self) -> bool:
        """必要时重读文件；返回当前是否有可用数据。"""
        try:
            st = self.path.stat()
            key = (st.st_mtime_ns, st.st_size)
            if key == self._key and self._raw is not None:
                return True
            data = json.loads(self.path.read_bytes().decode("utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("devices"), dict):
                raise ValueError("no 'devices' object")
        except (OSError, ValueError) as e:  # JSONDecodeError / UnicodeDecodeError 都是 ValueError
            msg = f"{type(e).__name__}: {e}"
            if msg != self.last_error:  # 同一个错误只记一次，避免每 10 秒刷屏
                log.warning("Token Monitor file %s unreadable, keeping last good data: %s", self.path, msg)
            self.last_error = msg
            return self._raw is not None
        self._raw, self._key, self.last_error = data, key, None
        return True

    def snapshot(self, now: datetime | None = None) -> TmSnapshot | None:
        """没有任何好结果（从没读成功）时返回 None。"""
        if not self.refresh():
            return None
        return build_snapshot(self._raw, now)
