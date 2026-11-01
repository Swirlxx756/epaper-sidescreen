#!/usr/bin/env python3
"""离线天气页的电脑端部分（不依赖 tkinter，GUI 和命令行共用）。

板子在电脑离线一段时间后自己联网取 Open-Meteo 天气并显示；电脑端只负责两件事：
  1. 城市搜索：Open-Meteo 地理编码（无需 key），在后台线程里调用，返回 经纬度 + 显示用地名
  2. 把设置 `{"v":2,"type":"wx_cfg",...}` 发给板子（每次连接 / 重连都发一次，设置改了也发），
     再问一次 `wx_status` 显示板子那边的状态

设置存在 config.json 的 `v2.weather`：{"on", "place", "lat", "lon", "after_min"}。
旧固件不认识 wx_cfg 会回 `ERR`：只记一条日志，这次连接里不再重试，不弹窗。
协议见 docs/dashboard-v2-protocol.md。
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.parse
import urllib.request

log = logging.getLogger("epaper_host.weather")

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
GEOCODE_TIMEOUT_S = 8.0
GEOCODE_COUNT = 5
PLACE_MAX_BYTES = 23
AFTER_MIN_RANGE = (1, 240)
AFTER_MIN_DEFAULT = 10
WX_CMD_TIMEOUT_S = 5.0
UNSUPPORTED_MARKS = ("unknown", "bad_type", "unsupported")   # ERR texts of a firmware that lacks a message type
USER_AGENT = "epaper-sidescreen-host"


def is_unsupported(err: str) -> bool:
    """An ERR reason that means "this firmware does not know that message type" (weather, page, image ...)."""
    return any(m in (err or "") for m in UNSUPPORTED_MARKS)


# ---------------------------------------------------------------------------
# 设置
# ---------------------------------------------------------------------------
def _num(x) -> float | None:
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def truncate_place(name: str, limit: int = PLACE_MAX_BYTES) -> str:
    """地名截到 limit 个 UTF-8 字节（不切断字符）。"""
    name = " ".join(str(name).split())
    while len(name.encode("utf-8")) > limit:
        name = name[:-1]
    return name


def clamp_after(x) -> int:
    lo, hi = AFTER_MIN_RANGE
    try:
        v = int(x)
    except (TypeError, ValueError):
        return AFTER_MIN_DEFAULT
    return min(max(v, lo), hi)


def weather_options(cfg: dict) -> dict | None:
    """config 里的 `v2.weather` -> 规整后的 dict；没配过（或不是对象）-> None。
    位置缺失 / 越界时 lat、lon 为 None（此时只会发 on 开关）。"""
    v2 = cfg.get("v2")
    raw = v2.get("weather") if isinstance(v2, dict) else None
    if not isinstance(raw, dict):
        return None
    lat, lon = _num(raw.get("lat")), _num(raw.get("lon"))
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        lat = lon = None
    return {"on": raw.get("on") is True, "place": truncate_place(raw.get("place") or ""),
            "lat": lat, "lon": lon, "after_min": clamp_after(raw.get("after_min", AFTER_MIN_DEFAULT))}


def wx_cfg_msg(opts: dict | None) -> dict | None:
    """规整后的设置 -> wx_cfg 消息；没配过 -> None（不打扰板子上已有的设置）。
    关闭或没选城市时只发 on:false（板子保留原来的位置）。"""
    if opts is None:
        return None
    if not opts["on"] or opts["lat"] is None:
        return {"v": 2, "type": "wx_cfg", "on": False}
    return {"v": 2, "type": "wx_cfg", "on": True, "place": opts["place"], "lat": round(opts["lat"], 4),
            "lon": round(opts["lon"], 4), "after_min": opts["after_min"]}


def weather_config(on: bool, place: str, lat, lon, after_min) -> dict:
    """要写进 `v2.weather` 的对象。"""
    return {"on": bool(on), "place": truncate_place(place), "lat": lat, "lon": lon,
            "after_min": clamp_after(after_min)}


# ---------------------------------------------------------------------------
# 天气页预览：上位机自己拉一份和板子同样的 Open-Meteo 预报（交给固件的解析 / 绘图代码出图，见 gui.py）
# ---------------------------------------------------------------------------
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
FORECAST_TIMEOUT_S = 10.0
FORECAST_CACHE_S = 600.0


def forecast_url(lat: float, lon: float) -> str:
    """和固件 wxb::requestUrl（firmware/src/weather_parse.cpp）一字不差：同样的字段、同样的 %.4f。"""
    return (f"{FORECAST_URL}?latitude={lat:.4f}&longitude={lon:.4f}&timezone=auto"
            "&forecast_days=4&forecast_hours=12"
            "&current=temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m,is_day"
            "&hourly=temperature_2m,precipitation_probability"
            "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset")


def fetch_forecast(lat: float, lon: float, fetch=None, timeout: float = FORECAST_TIMEOUT_S) -> bytes:
    """同步拉一份预报原文（调用方放到后台线程）。网络失败抛 OSError。"""
    return (fetch or _http_get)(forecast_url(lat, lon), timeout)


class ForecastCache:
    """按 (lat, lon) 缓存预报原文 FORECAST_CACHE_S 秒；换了城市就重新拉。不依赖 tkinter，线程安全。"""

    def __init__(self, fetch=None, clock=time.monotonic, ttl: float = FORECAST_CACHE_S):
        self._fetch, self._clock, self._ttl = fetch, clock, ttl
        self._lock = threading.Lock()
        self._key: tuple[float, float] | None = None
        self._body = b""
        self._at = 0.0

    def get(self, lat: float, lon: float, force: bool = False) -> bytes:
        key = (round(lat, 4), round(lon, 4))
        with self._lock:
            if not force and key == self._key and self._clock() - self._at < self._ttl:
                return self._body
        body = fetch_forecast(lat, lon, self._fetch)
        with self._lock:
            self._key, self._body, self._at = key, body, self._clock()
        return body


# ---------------------------------------------------------------------------
# 城市搜索（Open-Meteo 地理编码）
# ---------------------------------------------------------------------------
def _http_get(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:   # noqa: S310 - 固定的 https 地址
        return r.read()


def geocode_url(name: str, count: int = GEOCODE_COUNT, language: str = "zh") -> str:
    q = urllib.parse.urlencode({"name": name, "count": count, "language": language, "format": "json"})
    return f"{GEOCODE_URL}?{q}"


def geocode_get_url(place_id, language: str = "en") -> str:
    q = urllib.parse.urlencode({"id": place_id, "language": language})
    return f"{GEOCODE_URL.rsplit('/', 1)[0]}/get?{q}"


def parse_geocode(payload: dict) -> list[dict]:
    """地理编码响应 -> [{"place","lat","lon","label"}]；坐标不合法的条目丢掉。没有结果时响应里没有 results。"""
    out = []
    for r in (payload.get("results") if isinstance(payload, dict) else None) or []:
        if not isinstance(r, dict) or not isinstance(r.get("name"), str):
            continue
        lat, lon = _num(r.get("latitude")), _num(r.get("longitude"))
        if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        parts = [r["name"]] + [r[k] for k in ("admin1", "country") if isinstance(r.get(k), str) and r[k]]
        seen: list[str] = []
        for p in parts:
            if p not in seen:
                seen.append(p)
        out.append({"id": r.get("id"), "place": truncate_place(r["name"]), "lat": round(lat, 4), "lon": round(lon, 4),
                    "label": " · ".join(seen) + f"  ({lat:.2f}, {lon:.2f})"})
    return out


def search_city(name: str, fetch=_http_get, timeout: float = GEOCODE_TIMEOUT_S) -> list[dict]:
    """同步搜索（调用方放到后台线程）。网络 / 解析失败抛 OSError / ValueError。"""
    name = name.strip()
    if not name:
        return []
    found = parse_geocode(json.loads(fetch(geocode_url(name), timeout).decode("utf-8")))
    for r in found:
        # 板子的中文字库只有几百个常用字（「阿德莱德」会缺字），屏幕上的地名用英文名；列表里仍显示中文。
        if not r["place"].isascii() and r.get("id") is not None:
            try:
                en = json.loads(fetch(geocode_get_url(r["id"], "en"), timeout).decode("utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(en, dict) and isinstance(en.get("name"), str) and en["name"].isascii() and en["name"]:
                r["place"] = truncate_place(en["name"])
    return found


# ---------------------------------------------------------------------------
# 把设置同步给板子
# ---------------------------------------------------------------------------
def parse_wx_status(line: str) -> dict | None:
    """`WX {json}` 的 JSON 部分 -> dict；坏了 -> None。"""
    try:
        data = json.loads(line)
    except ValueError:
        return None
    return data if isinstance(data, dict) and data.get("type") == "status" else None


def format_wx_status(st: dict | None) -> str:
    """板子的 wx_status -> 一句中文。"""
    if not st:
        return "—"
    if st.get("unsupported"):
        return "板子固件还不支持天气页（升级固件后会自动同步）"
    if not st.get("on"):
        return "天气页：关闭"
    bits = ["天气页：开", f"{st.get('place') or '未设城市'}", f"离线 {st.get('after_min', '?')} 分钟后显示"]
    bits.append("当前在天气页" if st.get("mode") == "weather" else "当前在仪表盘")
    bits.append(f"上次更新 {st['last']}" if st.get("last") else "还没取过天气")
    if st.get("err"):
        bits.append(f"错误：{st['err']}")
    return " · ".join(bits)


class WxSync:
    """每次连接（link.opened_at 变了）和设置改变后，向板子发一次 wx_cfg，成功后问一次 wx_status。
    只能在拥有 link 的线程里调用（和 Link.command 一样）。失败不重试：ERR（旧固件不认识）记一条日志，
    超时 / 断线最多再试一次，之后要等下次连接或设置改变。
    on_result(kind, data)：kind = "status"（data = wx_status dict）| "unsupported" | "error"（data = 原因）。"""

    MAX_ATTEMPTS = 2

    def __init__(self, get_opts, on_result=None):
        self._get_opts = get_opts
        self._on_result = on_result
        self._key = None
        self._attempts = 0
        self._logged: set = set()

    def _emit(self, kind: str, data) -> None:
        if self._on_result is not None:
            try:
                self._on_result(kind, data)
            except Exception as e:  # noqa: BLE001 - 回调不能拖垮推送
                log.warning("weather listener failed: %s", e)

    def resync(self) -> None:
        """The board forgot its settings (it rebooted): send them again on the next sync()."""
        self._key = None

    def sync(self, link) -> bool:
        """需要时发一次；返回 True = 这次调用向板子发了命令。"""
        msg = wx_cfg_msg(self._get_opts())
        if msg is None:
            return False
        key = (link.opened_at, json.dumps(msg, sort_keys=True))
        if key != self._key:
            self._key, self._attempts = key, 0
        if self._attempts >= self.MAX_ATTEMPTS or self._attempts < 0:
            return False
        self._attempts += 1
        res = link.command(msg, WX_CMD_TIMEOUT_S)
        if res.ok:
            self._attempts = -1                       # 这个连接 + 这份设置：完成
            self._ask_status(link)
            return True
        err = res.err or "error"
        if err in ("timeout", "disconnected"):
            if self._attempts < self.MAX_ATTEMPTS:
                return True                           # 下一轮再试一次
        else:
            self._attempts = -1                       # ERR：板子明确拒绝，不再试
        if is_unsupported(err):
            self._log_once("unsupported", "board firmware has no weather page yet (wx_cfg: %s); "
                           "it will sync after a firmware upgrade", err)
            self._emit("unsupported", err)
        else:
            self._log_once(("err", err), "board did not accept the weather settings: %s", err)
            self._emit("error", err)
        return True

    def _ask_status(self, link) -> None:
        res = link.command({"v": 2, "type": "wx_status"}, WX_CMD_TIMEOUT_S)
        status = getattr(link, "wx_status", None) if res.ok else None
        deadline = time.monotonic() + 1.0
        while res.ok and status is None and time.monotonic() < deadline:
            time.sleep(0.05)      # OK can be counted off a late earlier reply; the WX line follows closely
            status = getattr(link, "wx_status", None)
        if status:
            self._emit("status", dict(status))

    def _log_once(self, key, fmt: str, *args) -> None:
        if key not in self._logged:
            self._logged.add(key)
            log.info(fmt, *args)
