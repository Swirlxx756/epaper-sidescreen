#!/usr/bin/env python3
"""墨水屏副屏 · 上位机小窗口（tkinter）。

把 epaper_host.py 的 v2 推送会话包进一个窗口：选串口或 Wi-Fi 板子、连接 / 断开、改显示设置、
看最近推送的数据（墨水屏预览 + 明细）和日志；USB 连着时还能给板子配 Wi-Fi（配网…）。协议逻辑全部复用
epaper_host（Link / V2Pusher / push_v2），这里不重写。屏幕预览由固件自己的绘图代码离线编出的
epaper_render.exe 画（firmware/test/preview/build.sh），所以和真屏不会走样。

  py -3.14 host/gui.py      # 带控制台
  pyw -3.14 host/gui.py     # 无控制台
  pyw -3.14 host/gui.py --tray   # 不弹窗口，直接在托盘里运行（开机自启用的就是这个）

关窗口 = 收进托盘，继续推送；托盘图标右键「退出」才真正退出。同一时间只能开一个。

只支持 v2 固件；板子是 v1 固件时只显示状态，不推送（v1 统计请用 epaper_host.py）。
文件分三层，前两层不依赖 tkinter，可无界面测试：
  1. 纯函数 / 小类：配置保存、摘要格式化、预览渲染、日志队列
  2. HostWorker：后台线程里跑一个 v2 会话（等价于 main() 的 v2 循环）
  3. App / WifiDialog：窗口，只管控件和轮询队列

窗口分两列：左 = 连接 / 板子 / 页面，右 = 标签页（屏幕预览 / 数据明细 / 待办 / 设置）+ 日志。装了 sv-ttk 就用
Windows 11 风格并跟随系统明暗，没装用原生主题；颜色全部从主题取，不写死。
"""
from __future__ import annotations

import logging
import queue
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dashboard_v2  # noqa: E402
import discovery  # noqa: E402
import epaper_host as host  # noqa: E402
import tm_source  # noqa: E402
import todo_page  # noqa: E402
import weather  # noqa: E402
from worker import COMMAND_MAX_S, WIFI_PROBE_S, HostWorker, ObservedPusher  # noqa: E402,F401

log = logging.getLogger("epaper_host.gui")

LOG_LOGGERS = ("epaper_host", "mouse_rapoo")     # tm_source / weather / todo 的日志器是 epaper_host 的子日志器
LOG_MAX_LINES = 500
POLL_MS = 200
SPIN_DEBOUNCE_MS = 400
CLOSE_JOIN_S = 8.0
SCAN_TIMEOUT_S = 10.0   # 板子扫描 WiFi 约 2–4 秒
PREVIEW_DEBOUNCE_MS = 500   # 一轮推送的几条消息陆续到，攒一下再渲染
PREVIEW_TIMEOUT_S = 10.0    # 渲染一屏通常 < 0.2 秒

LABELS_CN = {"TODAY": "今日", "TOTAL": "累计"}
PRIMARY_CN = {"today": "今日", "total": "累计"}


def refresh_label(minutes: int) -> str:
    return f"每 {minutes} 分钟" + ("（最快）" if minutes == dashboard_v2.REFRESH_MINUTES[0] else "")


def refresh_minutes(label: str) -> int | None:
    return next((m for m in dashboard_v2.REFRESH_MINUTES if refresh_label(m) == label), None)


# ---------------------------------------------------------------------------
# 1. 配置保存
# ---------------------------------------------------------------------------
default_config_path = host.default_config_path   # 脚本（或打包后的 exe）旁的 config.json，和 load_config(None) 同一个位置


# 实现在 epaper_host（命令行 --pair 也要写配置）：`updates` 改顶层键，`v2` 改 `v2` 下的键，其余原样保留；
# JSON 坏了抛 ValueError、不覆盖；WiFi 密码（SECRET_KEYS）绝不写进配置文件。ota_token 是配对码，可以存。
SECRET_KEYS = host.SECRET_KEYS
save_config = host.save_config


# ---------------------------------------------------------------------------
# 2. 数据摘要（只从消息 dict 推导，不重读文件）
# ---------------------------------------------------------------------------
def _hhmm_ts(ts: datetime | None = None) -> str:
    return (ts or datetime.now()).strftime("%H:%M:%S")


def _provider_line(p: dict) -> str:
    head = p.get("n", "?") + (f" ({p['plan']})" if p.get("plan") else "")
    parts = []
    if p.get("i"):
        bars = " / ".join(
            f"{it.get('l', '?')} " + ("--" if it.get("left") is None else f"{it['left']}%") for it in p["i"])
        parts.append(bars)
    if p.get("bal"):
        parts.append(f"余额 {p['bal']}")
    for t in p.get("txt") or []:
        parts.append(t.get("s", "") + (" !" if t.get("w") else ""))
    st = p.get("st")
    if st in ("stale", "na"):
        parts.append("[数据偏旧]" if st == "stale" else "[无数据]")
    return head + "    " + "    ".join(parts) if parts else head


def format_summary(msgs: dict[str, dict]) -> list[str]:
    """把最近发送的几条消息整理成若干行可读文字。缺的消息显示「尚未推送」。"""
    lines: list[str] = []

    dash = msgs.get("dash")
    lines.append("【Token】")
    if dash is None:
        lines.append("  （尚未推送）")
    elif "main" not in dash:
        lines.append("  无数据（Token Monitor 文件不可读或已过期）")
    else:
        main, alt = dash["main"], dash.get("alt") or {}
        lines.append(f"  {LABELS_CN.get(main.get('l'), main.get('l'))}（主）  "
                     f"{main.get('tok', '--')} tokens  {main.get('cost', '')}".rstrip())
        if alt:
            lines.append(f"  {LABELS_CN.get(alt.get('l'), alt.get('l'))}（副）  {alt.get('tok', '--')} tokens")
        note = "（数据偏旧）" if dash.get("st") == "stale" else ""
        if dash.get("time"):
            lines.append(f"  数据更新于 {dash['time']}{note}")

    mouse = msgs.get("mouse")
    lines.append("【鼠标】")
    if mouse is None:
        lines.append("  （尚未推送）")
    elif mouse.get("st") == "off":
        lines.append("  鼠标已关机或休眠")
    elif mouse.get("st") == "na":
        lines.append("  无读数")
    else:
        pct = mouse.get("pct")
        bits = ["电量 " + ("--" if pct is None else f"{pct}%")]
        if mouse.get("chg"):
            bits.append("充电中")
        if mouse.get("at"):
            bits.append(f"读数 {mouse['at']}")
        if mouse.get("st") == "stale":
            bits.append("偏旧")
        lines.append("  " + " · ".join(bits))

    quota = msgs.get("quota")
    lines.append("【额度】")
    if quota is None:
        lines.append("  （尚未推送）")
    elif not quota.get("p"):
        lines.append("  无数据")
    else:
        lines.extend("  " + _provider_line(p) for p in quota["p"])

    models = msgs.get("models")
    lines.append("【模型】" + (f"（{PRIMARY_CN.get(str(models.get('scope')).lower(), models.get('scope'))}）"
                              if models and models.get("scope") else ""))
    if models is None:
        lines.append("  （尚未推送）")
    elif not models.get("m"):
        lines.append("  无数据")
    else:
        lines.extend(f"  {name}    {tok}    {pct}%" for name, tok, pct in models["m"])

    heat = msgs.get("heat")
    lines.append("【活动】")
    if heat is None:
        lines.append("  （尚未推送）")
    elif "active" not in heat:
        lines.append(f"  {heat.get('start', '?')} ~ {heat.get('end', '?')}  无数据")
    else:
        bits = [f"{heat.get('start')} ~ {heat.get('end')}", f"活跃 {heat['active']} 天"]
        if heat.get("peak"):
            bits.append(f"峰值 {heat['peak']}")
        lines.append("  " + " · ".join(bits))
    return lines


# ---------------------------------------------------------------------------
# 屏幕预览：把最近的消息喂给固件绘图代码编出的 epaper_render，拿回一张 400×300 的 PPM
# ---------------------------------------------------------------------------
RENDER_EXE = "epaper_render.exe" if sys.platform == "win32" else "epaper_render"
FIRMWARE_DIR = Path(__file__).resolve().parents[1] / "firmware"
RENDER_BUILD_CMD = "bash test/preview/build.sh"
RENDER_MISSING_TEXT = ("预览渲染器还没编译。\n在 firmware 目录运行：\n" + RENDER_BUILD_CMD +
                       "\n（需要 MSYS2 g++ 和 PlatformIO 下载的库，见 host/README.md）")
# 渲染器编进去的固件源码：这些文件比 exe 新，说明预览可能和真屏不一样了
RENDER_SOURCES = ("*.h", "dashboard_v2.cpp", "protocol.cpp", "weather_parse.cpp", "weather_page_b.cpp")


def renderer_candidates() -> list[Path]:
    """先找脚本（或打包后的 exe）旁边，再找 firmware/test/preview/build/。"""
    here = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
    return [here / RENDER_EXE, FIRMWARE_DIR / "test" / "preview" / "build" / RENDER_EXE]


def find_renderer(candidates: list[Path] | None = None) -> Path | None:
    return next((p for p in (candidates or renderer_candidates()) if p.is_file()), None)


def renderer_stale(exe: Path, src_dir: Path = FIRMWARE_DIR / "src") -> bool:
    """firmware/src 里渲染器用到的源码有比 exe 新的 -> True（该重新编译了）。读不到时当作不旧。"""
    try:
        built = exe.stat().st_mtime
        return any(p.stat().st_mtime > built for pat in RENDER_SOURCES for p in src_dir.glob(pat))
    except OSError:
        return False


def preview_input(msgs: dict[str, dict]) -> bytes:
    """按发送顺序把最近的消息编成协议行，和发给板子的字节完全一样；编不出来的（超长）跳过，pusher 也不会发它。"""
    out = []
    for kind in dashboard_v2.KINDS:
        if kind in msgs:
            try:
                out.append(host.encode(msgs[kind]))
            except (TypeError, ValueError):
                pass
    return b"".join(out)


def render_preview(cmd: list[str], data: bytes, online: bool,
                   timeout: float = PREVIEW_TIMEOUT_S) -> tuple[bytes | None, str]:
    """跑一次渲染器 -> (PPM 字节或 None, 提示)。约定见 firmware/test/preview/render.cpp：协议行从 stdin 进，
    PPM 从 stdout 出；--offline 画离线图标；退出码 3 = 图照出，但有行被固件拒收（stderr 写原因）。"""
    args = [*cmd, *([] if online else ["--offline"])]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)     # pyw 下不闪黑窗口
    try:
        r = subprocess.run(args, input=data, capture_output=True, timeout=timeout, creationflags=flags)
    except subprocess.TimeoutExpired:
        return None, f"渲染超时（超过 {timeout:g} 秒）"
    except OSError as e:
        return None, f"无法运行渲染器：{e}"
    err = r.stderr.decode("utf-8", "replace").strip()
    if r.returncode in (0, 3) and r.stdout.startswith(b"P6"):
        if r.returncode == 3:
            return r.stdout, "固件会拒收其中的消息：" + (err.splitlines()[0] if err else "原因不明")
        return r.stdout, ""
    return None, f"渲染失败（退出码 {r.returncode}）" + (f"：{err.splitlines()[0]}" if err else "")


def render_weather(cmd: list[str], body: bytes, place: str,
                   timeout: float = PREVIEW_TIMEOUT_S) -> tuple[bytes | None, str]:
    """天气页预览：Open-Meteo 响应原文从 stdin 进，渲染器用固件的解析 / 绘图代码出一张 PPM（--weather <地名>）。
    -> (PPM 字节或 None, 失败原因)。旧版渲染器不认 --weather（打印 usage、退出码 1）：提示重新编译。"""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        r = subprocess.run([*cmd, "--weather", place], input=body, capture_output=True, timeout=timeout,
                           creationflags=flags)
    except subprocess.TimeoutExpired:
        return None, f"渲染超时（超过 {timeout:g} 秒）"
    except OSError as e:
        return None, f"无法运行渲染器：{e}"
    err = r.stderr.decode("utf-8", "replace").strip()
    if r.returncode == 0 and r.stdout.startswith(b"P6"):
        return r.stdout, ""
    if r.returncode == 1 and "usage" in err:
        return None, "渲染器太旧，不支持天气预览；请重新编译（" + RENDER_BUILD_CMD + "）"
    if r.returncode == 4:
        return None, "天气数据解析失败（Open-Meteo 的回复不完整）"
    return None, f"渲染失败（退出码 {r.returncode}）" + (f"：{err.splitlines()[0]}" if err else "")


class WeatherPreviewer:
    """后台拉 Open-Meteo（缓存约 10 分钟）+ 渲染天气页，不依赖 tkinter。request() 立刻返回，同一时间最多一个
    线程；结果以 ("wxpreview", ppm 字节或 None, 提示) 放进 events。ppm 为 None 时提示就是要显示的文字。"""

    def __init__(self, cmd: list[str] | None, events: queue.Queue, fetch=None, render_fn=render_weather,
                 clock=time.monotonic):
        self.cmd = list(cmd) if cmd else None
        self.events = events
        self._render = render_fn
        self._cache = weather.ForecastCache(fetch, clock)
        self._lock = threading.Lock()
        self._want: tuple | None = None
        self._busy = False

    def request(self, opts: dict | None, force: bool = False) -> None:
        if opts is None or opts.get("lat") is None:
            self.events.put(("wxpreview", None, WX_PREVIEW_NO_CITY))
            return
        if self.cmd is None:
            self.events.put(("wxpreview", None, RENDER_MISSING_TEXT))
            return
        with self._lock:
            self._want = (opts["lat"], opts["lon"], opts["place"], force)
            if self._busy:
                return
            self._busy = True
        threading.Thread(target=self._run, daemon=True, name="wxpreview").start()

    def _run(self) -> None:
        while True:
            with self._lock:
                req, self._want = self._want, None
                if req is None:
                    self._busy = False
                    return
            lat, lon, place, force = req
            try:
                body = self._cache.get(lat, lon, force)
            except Exception as e:  # noqa: BLE001 - 网络 / 超时 / 坏地址：只显示一句话
                self.events.put(("wxpreview", None, f"拉取天气失败：{e}"))
                continue
            try:
                ppm, note = self._render(self.cmd, body, place)
            except Exception as e:  # noqa: BLE001
                ppm, note = None, f"{type(e).__name__}: {e}"
            self.events.put(("wxpreview", ppm, note))


class PreviewRenderer:
    """后台渲染，不依赖 tkinter。submit() 只记下最新一份输入；同一时间最多一个渲染进程，跑完发现又有
    新输入就再跑一次（中间的旧输入直接丢掉）。和上一次相同的输入不重复渲染。
    结果以 ("preview", ppm 字节或 None, 提示) 放进 events。"""

    def __init__(self, cmd: list[str], events: queue.Queue, render_fn=render_preview):
        self.cmd = list(cmd)
        self.events = events
        self._render = render_fn
        self._lock = threading.Lock()
        self._want: tuple | None = None
        self._last: tuple | None = None
        self._busy = False

    def submit(self, data: bytes, online: bool) -> bool:
        """返回 False = 和上一次一样，没有安排渲染。"""
        req = (data, online)
        with self._lock:
            if req == self._last and self._want is None:
                return False
            self._want = req
            if self._busy:
                return True
            self._busy = True
        threading.Thread(target=self._run, daemon=True, name="preview").start()
        return True

    def _run(self) -> None:
        while True:
            with self._lock:
                req, self._want = self._want, None
                if req is None:
                    self._busy = False
                    return
                self._last = req
            try:
                ppm, note = self._render(self.cmd, *req)
            except Exception as e:  # noqa: BLE001 - 预览坏了不能拖垮窗口
                ppm, note = None, f"{type(e).__name__}: {e}"
            if ppm is None:
                with self._lock:
                    self._last = None          # 失败了，同样的输入下次还可以再试
            self.events.put(("preview", ppm, note))


# ---------------------------------------------------------------------------
# WiFi 辅助（纯函数）
# ---------------------------------------------------------------------------
WIFI_ST_CN = {"off": "未配置", "connecting": "连接中", "failed": "连接失败（密码错或找不到网络）"}
CMD_ERR_CN = {
    "timeout": "板子没有回应（超时）",
    "usb_only": "这条命令只能走 USB 串口",
    "disconnected": "连接已断开",
    "not_connected": "没有连接到板子",
    "stopped": "已断开连接",
}
PASS_MAX_BYTES, SSID_MAX_BYTES = 63, 32


def format_wifi_status(st: dict | None) -> str:
    """`WIFI status` 的 dict -> 一句中文：已连接 Home-2.4G · 192.168.1.50 / 未配置 / 连接中 / 连接失败。"""
    if not st:
        return "未知"
    state = st.get("st")
    ssid = st.get("ssid") or ""
    if state == "connected":
        return " · ".join(["已连接" + (f" {ssid}" if ssid else ""), *([st["ip"]] if st.get("ip") else [])])
    if state == "connecting":
        return "连接中" + (f" {ssid}" if ssid else "")
    if state == "failed":
        return "连接失败" + (f" {ssid}" if ssid else "") + "（密码错或找不到网络）"
    return WIFI_ST_CN.get(state, str(state))


def format_screen(connected: bool, fault: str | None, last_refresh: datetime | None) -> tuple[str, bool]:
    """屏幕状态那一行 -> (文字, 是否故障)。刷新记录来自板子的 DIAG 行（固件 1.3.1 起 WiFi 上也有）。"""
    if not connected:
        return "屏幕：—", False
    if fault:
        return "屏幕：驱动故障，已停止刷新 —— 请按板子上的 EN（RST）复位键", True
    if last_refresh is not None:
        return "屏幕：上次刷新完成 " + _hhmm_ts(last_refresh), False
    return "屏幕：还没收到刷新记录（每次刷新至少间隔 3 分钟）", False


def cmd_error_text(err: str) -> str:
    return CMD_ERR_CN.get(err, f"板子返回错误：{err}")


OTA_ERR_CN = {
    "ota_token": "配对码不对：用 USB 线连一次上位机，会自动重新配对",
    "no_token": "还没和板子配对：用 USB 线连一次上位机，会自动配对",
    "ota_too_big": "固件太大",
    "too_big": "固件太大",
    "ota_md5": "校验失败，没有写入，板子仍是旧固件",
    "ota_timeout": "传输中断，板子仍是旧固件",
    "disconnected": "传输中断，板子仍是旧固件",
    "timeout": "板子没有回应（超时），重连后看日志里的固件版本",
    "bad_image": "这不是 ESP32 固件文件",
    "unreadable": "读不了这个文件",
    "tcp_only": "只能在 Wi-Fi 连接下升级",
    "not_connected": "没有连接到板子",
    "stopped": "已断开连接，升级没有进行",
}


def ota_error_text(err: str) -> str:
    return "升级失败：" + OTA_ERR_CN.get(err, f"板子返回错误 {err}，板子仍是旧固件")


PAGE_CN = dict(todo_page.PAGES)
TODO_LIST_ROWS = 6
OLD_FW_TEXT = "板子固件太旧，还不支持这个功能（升级到 1.6.0 以上）"


def page_error_text(err: str) -> str:
    """页面 / 待办 / 立即刷新命令失败时的一句话。旧固件回 unknown_type：只提示，不重试。"""
    if todo_page.is_unsupported(err):
        return OLD_FW_TEXT
    if "too_big" in err:
        return "待办内容太多，压缩后板子装不下：删掉几条或缩短文字"
    return cmd_error_text(err)


def todo_row(it: dict) -> tuple[str, str, str]:
    return ("☑" if it["done"] else "☐", "★" if it["important"] else "", it["text"])


def signal_text(rssi) -> str:
    if not isinstance(rssi, (int, float)) or isinstance(rssi, bool):
        return "--"
    return f"{int(rssi)} dBm（{'强' if rssi >= -60 else '中' if rssi >= -75 else '弱'}）"


def wifi_url(addr: str) -> str:
    """'192.168.1.50' -> 'tcp://192.168.1.50'（已带 tcp:// 的原样返回）。"""
    addr = addr.strip()
    return addr if host.is_tcp_port(addr) else f"tcp://{addr}"


def board_addr(board: dict) -> str:
    """发现到的板子 -> 地址串：默认端口只写 IP，其他端口写 ip:port。"""
    return board["ip"] if board["port"] == discovery.DEFAULT_TCP_PORT else f"{board['ip']}:{board['port']}"


def board_label(board: dict) -> str:
    """下拉框里显示的文字：name (id) ip，占用中的加后缀。"""
    return f"{board['name']} ({board['id']}) {board_addr(board)}" + ("  [占用]" if board.get("busy") else "")


def addr_for_display(addr: str) -> str:
    """配置里存的地址去掉 tcp:// 前缀，下拉框里好看。"""
    addr = (addr or "").strip()
    return addr.split("://", 1)[1] if host.is_tcp_port(addr) else addr


# ---------------------------------------------------------------------------
# 日志：logging.Handler -> 队列（线程安全），由窗口 after() 轮询取走
# ---------------------------------------------------------------------------
class QueueLogHandler(logging.Handler):
    """把日志记录格式化成 (levelno, 文本) 放进队列；任意线程都可以调用。"""

    def __init__(self, q: queue.Queue | None = None):
        super().__init__(logging.INFO)
        self.queue: queue.Queue = q or queue.Queue()
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait((record.levelno, self.format(record)))
        except Exception:  # noqa: BLE001 - 日志不能拖垮程序
            self.handleError(record)

    def drain(self, limit: int = 200) -> list[tuple[int, str]]:
        out = []
        while len(out) < limit:
            try:
                out.append(self.queue.get_nowait())
            except queue.Empty:
                break
        return out


def attach_log_handler(handler: logging.Handler, names=LOG_LOGGERS) -> None:
    for name in names:
        lg = logging.getLogger(name)
        lg.addHandler(handler)
        if lg.level == logging.NOTSET or lg.level > logging.INFO:
            lg.setLevel(logging.INFO)


def detach_log_handler(handler: logging.Handler, names=LOG_LOGGERS) -> None:
    for name in names:
        logging.getLogger(name).removeHandler(handler)


# ---------------------------------------------------------------------------
# 3. 后台会话（不依赖 tkinter）：HostWorker / ObservedPusher 在 worker.py，和命令行共用 V2Session
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 4. 窗口
# ---------------------------------------------------------------------------
STATE_STYLE = {   # state -> (圆点颜色, 默认文字)
    "idle": ("#888888", "未连接"),
    "connecting": ("#d98c00", "连接中…"),
    "v2": ("#1a9a3c", "已连接 v2"),
    "v1": ("#d98c00", "已连接 v1（不支持）"),
    "error": ("#cc2222", "错误"),
}
BEZEL = "#3c3f44"          # 预览外框（像屏幕的边框；墨水屏本身就是这个纸色，明暗主题下都不变）
PAPER = "#f4f4f1"          # 墨水屏的纸色
PAPER_TEXT = "#555555"     # 纸色底上的说明文字
PREVIEW_SCALES = (1, 2)
LOG_FONTS = ("Cascadia Mono", "Consolas")
UI_FONT = "Microsoft YaHei UI"
UI_PT = 10
GAP = 10                   # 分组之间的间距（96 DPI 下的像素）
DEFAULT_W, DEFAULT_H = 1000, 680    # 默认窗口大小（96 DPI；高分屏按缩放放大）
MIN_W, MIN_H = 900, 600
SCREEN_MARGIN = 80         # 任务栏 + 标题栏
THUMB_W, THUMB_H = 200, 150         # 待办页缩略图

AUTO_PORT = "自动"
NO_PORT_HINT = "没有找到串口：用数据线连接板子，然后点「重新扫描」"
DISCONNECTED_PAGE_TEXT = "连接板子后才能推送到屏幕，现在只能预览"
WX_PREVIEW_NO_CITY = "没有设置城市\n到「设置」标签的天气一栏搜索并选一个城市，这里就能预览天气页。"
REFRESHING_TEXT = "屏幕刷新中，约 20 秒…"
REFRESH_TIMEOUT_MS = 45000          # 固件没有「开始刷新」事件：最多等这么久再放开按钮
REFRESH_RULE_TEXT = "三色屏每次整屏刷新至少间隔 3 分钟，画面有变化才刷新；选项再小也不会更快。"
WX_PLACEHOLDER = "输入城市名（中文或英文）"


def system_dark() -> bool:
    """Windows 的「应用」明暗模式（AppsUseLightTheme = 0 为深色）；读不到当浅色。"""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


def apply_theme(root) -> str:
    """装了 sv-ttk（Sun Valley，Windows 11 风格）就用它并跟随系统明暗 -> "light" / "dark"；没装或出错 -> ""（用原生主题）。"""
    try:
        import sv_ttk
        sv_ttk.set_theme("dark" if system_dark() else "light", root)
        return sv_ttk.get_theme(root)
    except Exception:  # noqa: BLE001 - 没有 sv-ttk 也要能跑
        return ""


def enable_dpi_awareness() -> None:
    """每显示器 DPI 感知（2）。必须在创建 Tk 之前调用；失败（非 Windows / 旧系统）就算了。"""
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:  # noqa: BLE001
        pass


def apply_tk_scaling(root) -> None:
    """Tk 的缩放（像素 / 磅）跟着系统 DPI 走。"""
    try:
        root.tk.call("tk", "scaling", root.winfo_fpixels("1i") / 72.0)
    except Exception:  # noqa: BLE001
        pass


def mix_color(root, c1: str, c2: str, t: float) -> str:
    """c1 和 c2 按 t（0..1，越大越接近 c2）混色 -> #rrggbb。"""
    r1, g1, b1 = root.winfo_rgb(c1)
    r2, g2, b2 = root.winfo_rgb(c2)
    return "#%02x%02x%02x" % tuple(int((a * (1 - t) + b * t) / 257) for a, b in ((r1, r2), (g1, g2), (b1, b2)))


def _sv_color(root, theme: str, key: str) -> str:
    """sv-ttk 的主题色表里取一项（它的 TFrame 底色画在位图里，style.lookup 查不到）。"""
    try:
        return str(root.tk.call("set", f"ttk::theme::sv_{theme}::colors({key})"))
    except Exception:  # noqa: BLE001
        return ""


def theme_colors(root, style, theme: str) -> dict:
    """从当前主题里取底色 / 字色 / 输入框底色，再派生次要文字和边框色；告警 / 错误色按明暗各一套。
    theme = "light" / "dark"（sv-ttk）或 ""（原生主题）。"""
    dark = theme == "dark"
    bg = (_sv_color(root, theme, "-bg") if theme else "") or style.lookup("TFrame", "background")         or root.cget("background")
    fg = (_sv_color(root, theme, "-fg") if theme else "") or style.lookup("TLabel", "foreground")         or ("white" if dark else "black")
    try:
        root.winfo_rgb(bg), root.winfo_rgb(fg)
    except Exception:  # noqa: BLE001 - 主题给了认不出的颜色名
        bg, fg = ("#1c1c1c", "#fafafa") if dark else ("#f0f0f0", "#000000")
    field = bg if theme else (style.lookup("TEntry", "fieldbackground") or "white")
    return {"bg": bg, "fg": fg, "field": field,
            "muted": mix_color(root, fg, bg, 0.4), "border": mix_color(root, fg, bg, 0.75),
            "warn": "#e0a040" if dark else "#b36b00", "err": "#ff6b6b" if dark else "#cc2222"}


def center_over(top, parent) -> None:
    """把 top 摆在 parent 正中（parent 藏起来或没有位置时摆在屏幕中央）。"""
    top.update_idletasks()
    w, h = top.winfo_reqwidth(), top.winfo_reqheight()
    if parent.winfo_viewable():
        x = parent.winfo_rootx() + (parent.winfo_width() - w) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - h) // 2
    else:
        x, y = (top.winfo_screenwidth() - w) // 2, (top.winfo_screenheight() - h) // 3
    top.geometry(f"+{max(0, x)}+{max(0, y)}")


def default_preview_scale(root, width_1x: int, height_1x: int) -> int:
    """高分屏（约 150% 缩放起，Tk 报 143.9 DPI）且屏幕放得下时默认 2×，否则 1×。
    width_1x / height_1x = 预览按 1× 摆好时窗口要的宽高；2× 再宽 400、再高 300 像素。"""
    try:
        fits = (height_1x + 300 <= root.winfo_screenheight() - SCREEN_MARGIN
                and width_1x + 400 <= root.winfo_screenwidth())
        return 2 if root.winfo_fpixels("1i") >= 140 and fits else 1
    except Exception:  # noqa: BLE001
        return 1


def list_serial_ports() -> list:
    """pyserial 的端口对象列表（device / description / vid / pid ...）；只枚举，不打开任何串口。"""
    if host.list_ports is None:
        return []
    return list(host.list_ports.comports())


class WifiDialog:
    """配网窗口（只在 USB 串口连着 v2 板子时打开）。动作都经 app.send_command 交给工作线程，
    结果和板子的 `WIFI status` 行由 App 转进 handle()。密码只在本窗口的输入框里，窗口关闭时清空；
    不写日志、不写配置。"""

    def __init__(self, app):
        self.app = app
        tk, ttk = app.tk, app.ttk
        self.top = tk.Toplevel(app.root)
        self.top.title("给板子配 Wi-Fi")
        self.top.minsize(app.S(480), app.S(460))
        self.top.transient(app.root)
        self.top.protocol("WM_DELETE_WINDOW", self.close)
        self.top.bind("<Escape>", lambda _e: self.close())
        self.top.bind("<Return>", self._on_return)
        self.busy = False
        self.connected = True
        self.expect_connect = False       # 刚写入 WiFi，等 connected 状态
        self.pending_ssid = ""

        outer = ttk.Frame(self.top, padding=app.S(12))
        outer.pack(fill="both", expand=True)
        app._wrap(ttk.Label(outer, style="Muted.TLabel",
                            text="板子只支持 2.4 GHz Wi-Fi（电脑可以在同一路由器的 5 GHz 上）。访客网络 / AP 隔离会挡住连接。"
                            )).pack(fill="x")

        bar = ttk.Frame(outer)
        bar.pack(fill="x", pady=(8, 4))
        self.scan_btn = ttk.Button(bar, text="扫描", width=8, command=self.scan)
        self.scan_btn.pack(side="left")
        ttk.Label(bar, text="  选一个网络，或在下面直接填名称（隐藏网络）").pack(side="left")

        tree_frame = ttk.Frame(outer)
        tree_frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tree_frame, columns=("ssid", "rssi", "sec"), show="headings",
                                 height=8, selectmode="browse")
        for col, text, width in (("ssid", "名称", 220), ("rssi", "信号", 120), ("sec", "加密", 60)):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor="w")
        sb = ttk.Scrollbar(tree_frame, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        form = ttk.Frame(outer)
        form.pack(fill="x", pady=(8, 0))
        ttk.Label(form, text="名称 SSID").grid(row=0, column=0, sticky="w")
        self.ssid_var = tk.StringVar()
        self.ssid_entry = ttk.Entry(form, textvariable=self.ssid_var, width=36)
        self.ssid_entry.grid(row=0, column=1, sticky="ew", padx=6, pady=2)
        ttk.Label(form, text="密码").grid(row=1, column=0, sticky="w")
        self.pass_var = tk.StringVar()
        self.pass_entry = ttk.Entry(form, textvariable=self.pass_var, show="*", width=36)
        self.pass_entry.grid(row=1, column=1, sticky="ew", padx=6, pady=2)
        self.show_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(form, text="显示密码", variable=self.show_var,
                        command=self._toggle_show).grid(row=2, column=1, sticky="w", padx=6)
        form.columnconfigure(1, weight=1)

        btns = ttk.Frame(outer)
        btns.pack(fill="x", pady=(8, 0))
        self.write_btn = ttk.Button(btns, text="写入", width=8, command=self.write)
        self.write_btn.pack(side="left")
        self.clear_btn = ttk.Button(btns, text="清除 Wi-Fi", command=self.clear)
        self.clear_btn.pack(side="left", padx=6)
        self.reset_btn = ttk.Button(btns, text="恢复出厂", command=self.reset)
        self.reset_btn.pack(side="left")

        self.status_var = tk.StringVar(value="板子 Wi-Fi：" + app.wifi_text())
        app._wrap(ttk.Label(outer, textvariable=self.status_var)).pack(fill="x", pady=(10, 0))
        self.hint_var = tk.StringVar()
        app._wrap(ttk.Label(outer, textvariable=self.hint_var, style="Status.TLabel")).pack(fill="x", pady=(4, 0))
        self._buttons = (self.scan_btn, self.write_btn, self.clear_btn, self.reset_btn)
        center_over(self.top, app.root)
        self._grab_tries = 0
        self.top.after(0, self._grab)
        self.ssid_entry.focus_set()

    # -- 小工具 --------------------------------------------------------------
    def _grab(self) -> None:
        """模态：窗口显示出来之后才能 grab_set（还没映射会报错，隔一会儿再试几次）。"""
        try:
            if self.top.winfo_exists():
                self.top.grab_set()
                self.top.focus_set()
        except self.app.tk.TclError:
            self._grab_tries += 1
            if self._grab_tries < 10:
                self.top.after(50, self._grab)

    def _on_return(self, _event=None) -> None:
        if not self.busy and self.connected:
            self.write()

    def _toggle_show(self) -> None:
        self.pass_entry.configure(show="" if self.show_var.get() else "*")

    def _on_select(self, _event=None) -> None:
        sel = self.tree.selection()
        if sel:
            self.ssid_var.set(self.tree.set(sel[0], "ssid"))

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        state = "disabled" if busy or not self.connected else "normal"
        for b in self._buttons:
            b.configure(state=state)

    def set_connected(self, connected: bool) -> None:
        self.connected = connected
        if not connected:
            self.status_var.set("USB 串口已断开，无法继续配网")
        self._set_busy(self.busy)

    def _send(self, tag: str, msg: dict, timeout: float, doing: str) -> None:
        if self.busy:
            return
        if not self.app.send_command(tag, msg, timeout):
            self.status_var.set("还没有连上板子")
            return
        self._set_busy(True)
        self.status_var.set(doing)

    # -- 动作 ----------------------------------------------------------------
    def scan(self) -> None:
        self._send("scan", {"v": 2, "type": "wifi_scan"}, SCAN_TIMEOUT_S, "扫描中…（最多约 10 秒）")

    def write(self) -> None:
        ssid, password = self.ssid_var.get().strip("\r\n"), self.pass_var.get()
        if not ssid:
            self.status_var.set("请先选一个网络或填写名称")
            return
        if len(ssid.encode("utf-8")) > SSID_MAX_BYTES:
            self.status_var.set(f"名称太长（最多 {SSID_MAX_BYTES} 字节）")
            return
        if len(password.encode("utf-8")) > PASS_MAX_BYTES:
            self.status_var.set(f"密码太长（最多 {PASS_MAX_BYTES} 字节）")
            return
        self.pending_ssid = ssid
        self.expect_connect = True        # 板子会在 OK 之后马上发 status 行，先标记再发
        self.hint_var.set("")
        self._send("set", {"v": 2, "type": "wifi_set", "ssid": ssid, "pass": password}, 5.0, "写入中…")

    def clear(self) -> None:
        self.hint_var.set("")
        self._send("clear", {"v": 2, "type": "wifi_clear"}, 5.0, "清除中…")

    def reset(self) -> None:
        if not self.app.ask_yes_no("恢复出厂", "清掉板子上保存的 Wi-Fi 和最后一屏内容？\n此操作不能撤销。", self.top):
            return
        self.hint_var.set("")
        self._send("reset", {"v": 2, "type": "reset_settings"}, 5.0, "恢复出厂中…")

    # -- 结果 ----------------------------------------------------------------
    def fill_networks(self, nets: list) -> None:
        self.tree.delete(*self.tree.get_children())
        for n in nets:
            self.tree.insert("", "end", values=(n.get("ssid", ""), signal_text(n.get("rssi")),
                                                "是" if n.get("sec") else "否（开放）"))

    def handle(self, ev: tuple) -> None:
        """App 转进来的 ("wifi", kind, data) 和 ("cmd", tag, result) 事件。"""
        if ev[0] == "wifi" and ev[1] == "status":
            st = ev[2]
            self.status_var.set("板子 Wi-Fi：" + format_wifi_status(st))
            if self.expect_connect and st.get("ssid") not in (None, "", self.pending_ssid):
                return            # 上一个网络的旧状态，不是这次写入的结果
            if self.expect_connect and st.get("st") == "connected":
                self.expect_connect = False
                self.app.on_board_connected(st.get("ip"), st.get("port"))
                self.hint_var.set(f"已连上 Wi-Fi，板子地址 {st.get('ip', '?')}。现在可以关掉本窗口、断开 USB 后"
                                  "把「方式」切到 Wi-Fi，再点连接（地址已填好）。")
            elif self.expect_connect and st.get("st") == "failed":
                self.expect_connect = False
                self.hint_var.set("连接失败：检查密码，并确认这是 2.4 GHz 网络、不是访客网络。")
            return
        if ev[0] != "cmd" or ev[1] not in ("scan", "set", "clear", "reset"):
            return
        tag, res = ev[1], ev[2]
        self._set_busy(False)
        if not res.get("ok"):
            if tag == "set":
                self.expect_connect = False
            self.status_var.set(cmd_error_text(res.get("err", "")))
            return
        if tag == "scan":
            nets = res.get("scan") or []
            self.fill_networks(nets)
            self.status_var.set(f"找到 {len(nets)} 个网络" if nets else "没扫到网络（板子附近没有 2.4 GHz Wi-Fi？）")
        elif tag == "set":
            self.status_var.set(f"已写入，板子正在连接 {self.pending_ssid}…")
        elif tag == "clear":
            self.status_var.set("已清除板子上保存的 Wi-Fi")
        elif tag == "reset":
            self.status_var.set("已恢复出厂（Wi-Fi 和保存的最后一屏已清除）")

    def close(self) -> None:
        self.pass_var.set("")                      # 密码不留在界面里
        self.pass_entry.delete(0, "end")
        self.app.wifi_dialog = None
        self.top.destroy()


# ---------------------------------------------------------------------------
# 托盘图标和开机自启（main() 里接上；测试里不传，窗口行为照旧）
# ---------------------------------------------------------------------------
TRAY_TITLE = "墨水屏副屏"
AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_NAME = "EpaperSidescreenHost"
SINGLE_INSTANCE_MUTEX = "Local\\EpaperSidescreenHostGui"


def autostart_command(python: str | Path | None = None, script: Path | None = None) -> str:
    """登录时运行的命令：用无控制台的 pythonw 跑本文件，--tray 直接进托盘。"""
    exe = Path(python or sys.executable)
    if exe.name.lower() == "python.exe" and exe.with_name("pythonw.exe").exists():
        exe = exe.with_name("pythonw.exe")
    return f'"{exe}" "{script or Path(__file__).resolve()}" --tray'


class RegistryAutostart:
    """当前用户 Run 键下的一个值：有这个值 = 登录后自动启动。只动当前用户，不要管理员权限。"""

    def __init__(self, reg=None, name: str = AUTOSTART_NAME, command_fn=autostart_command):
        if reg is None:
            import winreg as reg
        self._reg, self.name, self._command_fn = reg, name, command_fn

    def enabled(self) -> bool:
        try:
            with self._reg.OpenKey(self._reg.HKEY_CURRENT_USER, AUTOSTART_KEY) as k:
                self._reg.QueryValueEx(k, self.name)
            return True
        except OSError:
            return False

    def set(self, on: bool) -> None:
        with self._reg.CreateKey(self._reg.HKEY_CURRENT_USER, AUTOSTART_KEY) as k:
            if on:
                self._reg.SetValueEx(k, self.name, 0, self._reg.REG_SZ, self._command_fn())
            else:
                try:
                    self._reg.DeleteValue(k, self.name)
                except FileNotFoundError:
                    pass


def tray_image(size: int = 64):
    """托盘图标：黑框白底的小屏幕，顶上一条红、下面两行黑字。"""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    u = size / 64
    d.rectangle([2 * u, 8 * u, 62 * u, 56 * u], fill="white", outline="black", width=max(1, round(5 * u)))
    d.rectangle([12 * u, 18 * u, 52 * u, 27 * u], fill=(200, 30, 30))
    d.rectangle([12 * u, 33 * u, 44 * u, 38 * u], fill="black")
    d.rectangle([12 * u, 43 * u, 36 * u, 48 * u], fill="black")
    return img


class Tray:
    """pystray 托盘图标，跑在自己的线程里。菜单只往窗口的事件队列放 ("tray", "show" / "quit")，
    tkinter 只在窗口线程里动。左键单击 = 显示窗口。"""

    def __init__(self, events: queue.Queue):
        import pystray
        menu = pystray.Menu(
            pystray.MenuItem("显示窗口", lambda icon, item: events.put(("tray", "show")), default=True),
            pystray.MenuItem("退出", lambda icon, item: events.put(("tray", "quit"))))
        self.icon = pystray.Icon("epaper-sidescreen", tray_image(), TRAY_TITLE, menu)
        self.icon.run_detached()

    def set_title(self, text: str) -> None:
        self.icon.title = text[:127]   # Windows 托盘提示最多 128 字符

    def notify(self, text: str) -> None:
        try:
            self.icon.notify(text, TRAY_TITLE)
        except Exception:  # noqa: BLE001 - 通知只是提示，失败不影响运行
            pass

    def stop(self) -> None:
        self.icon.stop()


def acquire_single_instance(name: str = SINGLE_INSTANCE_MUTEX):
    """Windows 命名互斥量：第一个实例拿到句柄（要一直留着）；已经有实例在跑时返回 None。非 Windows 不限制。"""
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (ImportError, OSError, AttributeError):
        return True
    handle = k32.CreateMutexW(None, False, name)
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        k32.CloseHandle(handle)
        return None
    return handle


class App:
    """主窗口。左列：连接 / 板子 / 页面 / 设置；右列：屏幕预览·数据明细·待办 + 日志。"""

    def __init__(self, root, cfg: dict | None = None, config_path: Path | None = None,
                 ports_fn=list_serial_ports, worker_factory=HostWorker, discover_fn=discovery.discover,
                 file_dialog=None, geocode_fn=weather.search_city, weather_fetch=None, renderer="auto", tray_factory=None, autostart=None, start_hidden=False):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk = tk, ttk
        self.root = root
        self.config_path = Path(config_path) if config_path else default_config_path()
        self._ports_fn = ports_fn
        self._worker_factory = worker_factory
        self._discover_fn = discover_fn
        self._geocode_fn = geocode_fn
        self._file_dialog = file_dialog          # (**askopenfilename 参数) -> 路径或 ""；测试里替换掉
        self.autostart = autostart               # RegistryAutostart 或测试替身；None = 不显示开机自启
        self.tray = None                         # 有托盘时关窗口 = 收进托盘
        self._tray_hint_shown = False
        # 预览渲染器命令："auto" = 找编好的 epaper_render；None = 没有（显示编译提示）；测试里传假渲染器
        self.renderer_note = ""
        if renderer == "auto":
            exe = find_renderer()
            renderer = [str(exe)] if exe else None
            if exe and renderer_stale(exe):
                self.renderer_note = "渲染器比固件源码旧，预览可能和真屏不同；请重新编译（" + RENDER_BUILD_CMD + "）"
        self.preview_ppm: bytes | None = None     # 最近一次渲染结果（1×）
        self._preview_img = None                  # PhotoImage 要留引用，否则被回收
        self._preview_job = None
        self.ota_running = False
        self.fw_version: str | None = None       # 本次连接上板子报的固件版本
        self._pair_hint_shown = False
        self.worker: HostWorker | None = None
        self.wifi_dialog: WifiDialog | None = None
        self.link_info: tuple = (None, None)     # (transport, proto) of the live connection
        self.wifi_status: dict | None = None     # last `WIFI status` of the board (USB serial only)
        self.wifi_note = ""                      # e.g. firmware too old for Wi-Fi
        self.display_fault: str | None = None    # board's display-fault DIAG line on this connection
        self.last_refresh: datetime | None = None  # last "refresh done" the board reported
        self.silent = False                      # 已连接但 > 90 秒没有回复
        self.silent_age: float | None = None
        self._status: tuple[str, str] = ("idle", STATE_STYLE["idle"][1])
        self._connected_addr = ""
        self._found: list[dict] = []
        self._addr_map: dict[str, str] = {}
        self._searching = False
        self.events: queue.Queue = queue.Queue()
        self.preview = PreviewRenderer(renderer, self.events) if renderer else None
        self.wx_previewer = WeatherPreviewer(renderer, self.events, fetch=weather_fetch)
        self.wx_ppm: bytes | None = None          # 天气页预览（1×）；wx_note = 没有图时要显示的文字
        self.wx_note = ""
        self.wx_text = ""                         # 没有图时预览区里的文字（没设置城市 / 拉取失败）
        self._dash_note = ""
        self._todo_view_ppm: bytes | None = None  # 屏幕预览标签里的待办页（待办改了就作废）
        self.msgs: dict[str, dict] = {}
        self._closing = False
        self._in_tray = False                     # 窗口收在托盘里：不渲染预览，显示时补渲染一次
        self._user_stop = False
        self._spin_job = None
        self._port_map: dict[str, str] = {}
        self._conn_error = False                  # 工作线程在重试连接
        self.refreshing = False                   # 屏幕正在刷新（命令成功后到 refreshed / 超时）
        self._refresh_job = None
        self._refresh_src = "page"
        self._push_page: str | None = None        # 「推送到屏幕」已交给工作线程、还没有结果的页面

        self.load_error = ""
        if cfg is None:
            try:
                cfg = host.load_config(self.config_path if self.config_path.exists() else None)
            except (OSError, ValueError) as e:
                self.load_error = f"读取 {self.config_path.name} 失败，使用默认值：{e}"
                cfg = dict(host.DEFAULT_CONFIG)
        self.cfg = cfg
        self.opts = host.v2_options(cfg)
        self.wx = weather.weather_options(cfg)        # 规整后的 v2.weather；没配过 = None
        self._wx_results: list[dict] = []
        self._wx_search_id = 0
        self.todo_items = todo_page.todo_items(cfg)
        v2cfg = cfg.get("v2") if isinstance(cfg.get("v2"), dict) else {}
        self.page = v2cfg.get("page") if v2cfg.get("page") in todo_page.PAGE_KEYS else "auto"   # 屏幕当前（上次推送 / 保存）
        self.view_page = self.page                # 屏幕预览标签正在看的页面（单选框）；点它不会发给板子
        self._todo_dirty = True                   # 待办改过（或这次运行还没推送过）：推送「待办」页要先推图
        self.todo_ppm: bytes | None = None        # 最近一次渲染出的待办页（1×）
        self._todo_thumb = None
        self._todo_busy = False
        self.transport_var_init = cfg.get("transport") if cfg.get("transport") in ("serial", "wifi") else "serial"

        self.log_handler = QueueLogHandler()
        attach_log_handler(self.log_handler)

        root.title("墨水屏副屏 · 上位机")
        scale = cfg.get("preview_scale")
        self.preview_scale = scale if scale in PREVIEW_SCALES else 1
        self._setup_style()
        root.minsize(self.S(MIN_W), self.S(MIN_H))
        self._build()
        self._show_placeholder("")
        root.update_idletasks()
        if scale not in PREVIEW_SCALES:
            self.preview_scale = default_preview_scale(root, root.winfo_reqwidth(), root.winfo_reqheight())
            self.scale_var.set(self.preview_scale)
            self._show_placeholder("")
            root.update_idletasks()
        root.protocol("WM_DELETE_WINDOW", self.on_window_close)
        root.bind("<Control-r>", self._on_ctrl_r)
        root.bind("<Control-R>", self._on_ctrl_r)
        if tray_factory is not None:
            try:
                self.tray = tray_factory(self.events)
            except Exception as e:  # noqa: BLE001 - 没有 pystray / Pillow 等：关窗口就是退出
                log.warning("托盘图标不可用，关闭窗口会直接退出：%s", e)
        if start_hidden and self.tray is not None:
            root.withdraw()
            self._in_tray = True
        self._initial_geometry()
        self.refresh_ports(initial=True)
        self._refresh_addr_values()
        self._show_transport()
        self._update_wifi_ui()
        self._update_wx_ui()
        self._render_summary()
        self._update_page_ui()
        if not self._dash_view():
            self._show_view()                  # 上次选的是待办 / 天气：预览标签直接显示它
        if self.load_error:
            log.warning("%s", self.load_error)
        if host.serial is None:
            log.error("未安装 pyserial：pip install -r requirements.txt")
        self._poll_job = root.after(POLL_MS, self._poll)

    # -- 样式 / 主题 -------------------------------------------------------
    def S(self, n: float) -> int:
        """按界面缩放换算像素（96 DPI = 1）。"""
        return int(round(n * self.ui_scale))

    def _setup_style(self) -> None:
        """主题（sv-ttk 跟随系统明暗，没装就用原生）、字体、颜色。所有颜色从主题里取，不写死。"""
        import tkinter.font as tkfont
        root = self.root
        try:
            self.ui_scale = max(1.0, root.winfo_fpixels("1i") / 96.0)
        except Exception:  # noqa: BLE001
            self.ui_scale = 1.0
        self.theme = apply_theme(root)               # "light" / "dark" / ""（没有 sv-ttk）
        self.dark = self.theme == "dark"
        families = set(tkfont.families(root))
        self.ui_family = UI_FONT if UI_FONT in families else tkfont.nametofont("TkDefaultFont", root=root).actual("family")
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkCaptionFont", "TkTooltipFont"):
            try:
                tkfont.nametofont(name, root=root).configure(family=self.ui_family, size=UI_PT)
            except Exception:  # noqa: BLE001
                pass
        for name, pt, bold in (("SunValleyBodyFont", UI_PT, False), ("SunValleyBodyStrongFont", UI_PT, True),
                               ("SunValleyCaptionFont", UI_PT - 1, False), ("SunValleyBodyLargeFont", UI_PT + 2, False),
                               ("SunValleySubtitleFont", UI_PT + 4, True), ("SunValleyTitleFont", UI_PT + 8, True)):
            try:   # sv-ttk 的字体是负数像素（不跟 DPI），换成磅数 + 雅黑
                tkfont.nametofont(name, root=root).configure(family=self.ui_family, size=pt,
                                                             weight="bold" if bold else "normal")
            except Exception:  # noqa: BLE001
                pass
        self.font_bold = tkfont.Font(root=root, family=self.ui_family, size=UI_PT, weight="bold")
        self.font_status = tkfont.Font(root=root, family=self.ui_family, size=UI_PT + 1, weight="bold")
        self.font_text = tkfont.Font(root=root, family=self.ui_family, size=UI_PT)
        mono = next((f for f in LOG_FONTS if f in families), None)
        self.font_mono = tkfont.Font(root=root, family=mono, size=UI_PT - 1) if mono else \
            tkfont.nametofont("TkFixedFont", root=root)

        style = self.ttk.Style(root)
        if not self.theme and sys.platform == "win32" and "vista" in style.theme_names():
            style.theme_use("vista")
        self.colors = theme_colors(root, style, self.theme)
        c = self.colors
        style.configure("TLabelframe.Label", font=self.font_bold)
        style.configure("Muted.TLabel", foreground=c["muted"])
        style.configure("Err.TLabel", foreground=c["err"])
        style.configure("Status.TLabel", font=self.font_status)
        style.configure("Placeholder.TEntry", foreground=c["muted"])
        style.configure("TNotebook.Tab", padding=(self.S(12), self.S(3)))
        style.configure("Treeview", rowheight=max(self.S(24), int(self.font_text.metrics("linespace") * 1.7)))
        root.configure(background=c["bg"])

    def _text_widget_colors(self) -> dict:
        c = self.colors
        return {"background": c["field"], "foreground": c["fg"], "insertbackground": c["fg"],
                "highlightthickness": 1, "highlightbackground": c["border"], "highlightcolor": c["border"],
                "relief": "flat"}

    def _wrap(self, label, width: int | None = None):
        """让标签的 wraplength 跟着它被分到的宽度走，不写死像素。起始值只是第一次布局用的上限。"""
        label.configure(wraplength=width or self.S(340))
        label.bind("<Configure>", lambda e, l=label: l.configure(wraplength=max(self.S(240), e.width - 2)))
        return label

    # -- 构建界面 ----------------------------------------------------------
    def _build(self) -> None:
        tk, ttk = self.tk, self.ttk
        root, S = self.root, self.S
        root.columnconfigure(0, weight=0)
        root.columnconfigure(1, weight=1)
        root.rowconfigure(0, weight=1)
        left = ttk.Frame(root, padding=(S(12), S(10), S(6), S(10)))
        left.grid(row=0, column=0, sticky="ns")
        left.columnconfigure(0, weight=1)
        right = ttk.Frame(root, padding=(S(6), S(10), S(12), S(10)))
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)
        gap = S(GAP)

        # ---- 连接 ----
        conn = ttk.LabelFrame(left, text="连接", padding=(S(10), S(6), S(10), S(8)))
        conn.grid(row=0, column=0, sticky="ew")
        conn.columnconfigure(1, weight=1)
        ttk.Label(conn, text="方式").grid(row=0, column=0, sticky="w")
        modes = ttk.Frame(conn)
        modes.grid(row=0, column=1, columnspan=2, sticky="w", padx=S(6))
        self.transport_var = tk.StringVar(value=self.transport_var_init)
        self.mode_radios = [
            ttk.Radiobutton(modes, text=text, value=key, variable=self.transport_var, command=self.on_transport)
            for text, key in (("USB 串口", "serial"), ("Wi-Fi", "wifi"))]
        for r in self.mode_radios:
            r.pack(side="left", padx=(0, S(14)))
        self.addr_label_var = tk.StringVar(value="USB 串口")
        ttk.Label(conn, textvariable=self.addr_label_var).grid(row=1, column=0, sticky="w")
        self.port_var = tk.StringVar()
        self.port_box = ttk.Combobox(conn, textvariable=self.port_var, width=20)
        self.port_box.grid(row=1, column=1, sticky="ew", padx=S(6), pady=S(4))
        self.addr_var = tk.StringVar(value=addr_for_display(self.cfg.get("wifi_addr") or ""))
        self.addr_box = ttk.Combobox(conn, textvariable=self.addr_var, width=20)
        self.addr_box.grid(row=1, column=1, sticky="ew", padx=S(6), pady=S(4))
        self.refresh_btn = ttk.Button(conn, text="重新扫描", command=self.refresh_ports)
        self.refresh_btn.grid(row=1, column=2, sticky="ew")
        self.search_btn = ttk.Button(conn, text="搜索", command=self.search_boards)
        self.search_btn.grid(row=1, column=2, sticky="ew")
        self.conn_hint_var = tk.StringVar()
        self.conn_hint = self._wrap(ttk.Label(conn, textvariable=self.conn_hint_var, style="Muted.TLabel"))
        self.conn_hint.grid(row=2, column=0, columnspan=3, sticky="ew")
        ttk.Separator(conn).grid(row=3, column=0, columnspan=3, sticky="ew", pady=(S(6), S(4)))
        self.dot = tk.Label(conn, text="●", fg=STATE_STYLE["idle"][0], bg=self.colors["bg"], bd=0,
                            font=(self.ui_family, UI_PT + 3))
        self.dot.grid(row=4, column=0, sticky="w")
        self.status_var = tk.StringVar(value=STATE_STYLE["idle"][1])
        self.status_label = self._wrap(ttk.Label(conn, textvariable=self.status_var, style="Status.TLabel"))
        self.status_label.grid(row=4, column=1, sticky="ew", padx=S(6))
        self.conn_btn = ttk.Button(conn, text="连接", command=self.toggle_connection)
        self.conn_btn.grid(row=4, column=2, sticky="ew")
        self.push_var = tk.StringVar(value="上次推送 --:--:--")
        ttk.Label(conn, textvariable=self.push_var, style="Muted.TLabel").grid(
            row=5, column=1, columnspan=2, sticky="w", padx=S(6))

        # ---- 板子 ----
        board = ttk.LabelFrame(left, text="板子", padding=(S(10), S(6), S(10), S(8)))
        board.grid(row=1, column=0, sticky="ew", pady=(gap, 0))
        board.columnconfigure(0, weight=1)
        self.fw_var = tk.StringVar()
        ttk.Label(board, textvariable=self.fw_var).grid(row=0, column=0, columnspan=2, sticky="w")
        self.wifi_var = tk.StringVar()
        self._wrap(ttk.Label(board, textvariable=self.wifi_var)).grid(row=1, column=0, columnspan=2, sticky="ew",
                                                                      pady=(S(2), 0))
        self.screen_var = tk.StringVar()
        self.screen_label = self._wrap(ttk.Label(board, textvariable=self.screen_var))
        self.screen_label.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(S(2), 0))
        self.ota_bar = ttk.Progressbar(board, mode="determinate", maximum=100)
        self.ota_bar.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(S(6), 0))
        self.ota_bar.grid_remove()
        btns = ttk.Frame(board)
        btns.grid(row=4, column=0, columnspan=2, sticky="w", pady=(S(6), 0))
        self.wifi_btn = ttk.Button(btns, text="配网…", command=self.open_wifi_dialog)
        self.wifi_btn.pack(side="left")
        self.ota_btn = ttk.Button(btns, text="升级固件…", command=self.start_ota)
        self.ota_btn.pack(side="left", padx=(S(8), 0))

        # ---- 页面 ----
        pg = ttk.LabelFrame(left, text="页面", padding=(S(10), S(6), S(10), S(8)))
        pg.grid(row=2, column=0, sticky="ew", pady=(gap, 0))
        pg.columnconfigure(0, weight=1)
        radios = ttk.Frame(pg)
        radios.grid(row=0, column=0, sticky="w")
        ttk.Label(radios, text="屏幕显示").pack(side="left", padx=(0, S(6)))
        self.page_var = tk.StringVar(value=self.page)
        self.page_radios = [
            ttk.Radiobutton(radios, text=text, value=key, variable=self.page_var, command=self.on_page)
            for key, text in todo_page.PAGES]
        for r in self.page_radios:
            r.pack(side="left", padx=(0, S(8)))
        acts = ttk.Frame(pg)
        acts.grid(row=1, column=0, sticky="w", pady=(S(6), 0))
        self.push_page_btn = ttk.Button(acts, text="推送到屏幕", style="Accent.TButton", command=self.push_page)
        self.push_page_btn.pack(side="left", padx=(0, S(8)))
        self.refresh_now_btn = ttk.Button(acts, text="立即刷新屏幕", command=self.refresh_now)
        self.refresh_now_btn.pack(side="left")
        self.resend_btn = ttk.Button(acts, text="重新推送数据", command=self.resend)
        self.resend_btn.pack(side="left", padx=(S(8), 0))
        ttk.Label(acts, text="Ctrl+R", style="Muted.TLabel").pack(side="left", padx=(S(8), 0))
        self.page_status_var = tk.StringVar()
        self._wrap(ttk.Label(pg, textvariable=self.page_status_var, style="Status.TLabel")).grid(
            row=2, column=0, sticky="ew", pady=(S(6), 0))
        self.page_board_var = tk.StringVar()
        self._wrap(ttk.Label(pg, textvariable=self.page_board_var, style="Muted.TLabel")).grid(
            row=3, column=0, sticky="ew", pady=(S(2), 0))
        self._wrap(ttk.Label(pg, style="Muted.TLabel",
                             text="点单选框只切换「屏幕预览」，不会动板子；满意了再点「推送到屏幕」。"
                                  "「立即刷新屏幕」马上整屏刷新，不受最短间隔限制，刷得太勤会缩短三色屏寿命。"
                                  "「重新推送数据」让上位机下一轮重发全部数据块。")
                   ).grid(row=4, column=0, sticky="ew", pady=(S(2), 0))

        self.save_error_var = tk.StringVar()
        self._wrap(ttk.Label(left, textvariable=self.save_error_var, style="Err.TLabel")).grid(
            row=3, column=0, sticky="ew", pady=(S(4), 0))
        left.rowconfigure(4, weight=1)

        # ---- 右列：预览 / 明细 / 待办 + 日志 ----
        self.data_tabs = ttk.Notebook(right)
        self.data_tabs.grid(row=0, column=0, sticky="ew")
        self._build_preview_tab()
        self._build_summary_tab()
        self._build_todo_tab()
        self._build_settings_tab()
        logf = ttk.LabelFrame(right, text="日志", padding=(S(8), S(4), S(8), S(6)))
        logf.grid(row=1, column=0, sticky="nsew", pady=(gap, 0))
        logf.columnconfigure(0, weight=1)
        logf.rowconfigure(0, weight=1)
        self.log_text = tk.Text(logf, height=6, width=50, wrap="none", state="disabled", font=self.font_mono,
                                padx=S(6), pady=S(4), **self._text_widget_colors())
        sb = ttk.Scrollbar(logf, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        self.log_text.tag_configure("warn", foreground=self.colors["warn"])
        self.log_text.tag_configure("err", foreground=self.colors["err"])
        self._build_log_menu()

    def _build_settings_tab(self) -> None:
        """右列的「设置」标签：显示设置 + 离线天气页两个分组。"""
        ttk, S = self.ttk, self.S
        tab = ttk.Frame(self.data_tabs, padding=(S(8), S(8), S(8), S(6)))
        self.data_tabs.add(tab, text="设置")
        tab.columnconfigure(0, weight=1)
        sets = ttk.LabelFrame(tab, text="显示设置", padding=(S(10), S(6), S(10), S(8)))
        sets.grid(row=0, column=0, sticky="ew")
        self._build_display_settings(sets)
        wx = ttk.LabelFrame(tab, text="离线天气页", padding=(S(10), S(6), S(10), S(8)))
        wx.grid(row=1, column=0, sticky="ew", pady=(S(GAP), 0))
        self._build_weather_settings(wx)

    def _build_display_settings(self, sets) -> None:
        tk, ttk, S = self.tk, self.ttk, self.S
        sets.columnconfigure(2, weight=1)
        ttk.Label(sets, text="主显示").grid(row=0, column=0, sticky="w")
        self.primary_var = tk.StringVar(value=self.opts["primary"])
        prim = ttk.Frame(sets)
        prim.grid(row=0, column=1, sticky="w", padx=S(6))
        for key in dashboard_v2.PRIMARIES:
            ttk.Radiobutton(prim, text=PRIMARY_CN.get(key, key), value=key, variable=self.primary_var,
                            command=self.on_primary).pack(side="left", padx=(0, S(10)))
        lo, hi = dashboard_v2.HEAT_WEEKS
        weeks = ttk.Frame(sets)
        weeks.grid(row=0, column=2, sticky="w", padx=(S(16), 0))
        ttk.Label(weeks, text="活动周数").pack(side="left")
        self.weeks_var = tk.StringVar(value=str(self.opts["heat_weeks"]))
        self.weeks_spin = ttk.Spinbox(weeks, from_=lo, to=hi, width=4, textvariable=self.weeks_var,
                                      command=self._schedule_weeks)
        self.weeks_spin.pack(side="left", padx=S(6))
        self.weeks_var.trace_add("write", lambda *_: self._schedule_weeks())
        self.weeks_spin.bind("<FocusOut>", lambda _e: self._weeks_focus_out())
        ttk.Label(weeks, text=f"{lo}–{hi}", style="Muted.TLabel").pack(side="left")
        checks = ttk.Frame(sets)
        checks.grid(row=1, column=0, columnspan=3, sticky="w", pady=(S(6), 0))
        self.calendar_var = tk.BooleanVar(value=bool(self.opts["calendar"]))
        ttk.Checkbutton(checks, text="活动显示为日历", variable=self.calendar_var,
                        command=self.on_calendar).pack(side="left", padx=(0, S(16)))
        self.mouse_var = tk.BooleanVar(value=bool(self.opts["mouse"]))
        ttk.Checkbutton(checks, text="读取鼠标电量（下次连接生效）", variable=self.mouse_var,
                        command=self.on_mouse).pack(side="left")
        ttk.Label(sets, text="屏幕刷新").grid(row=2, column=0, sticky="w", pady=(S(6), 0))
        self.refresh_var = tk.StringVar(value=refresh_label(self.opts["refresh_min"]))
        self.refresh_box = ttk.Combobox(sets, textvariable=self.refresh_var, state="readonly", width=14,
                                        values=[refresh_label(m) for m in dashboard_v2.REFRESH_MINUTES])
        self.refresh_box.grid(row=2, column=1, sticky="w", padx=S(6), pady=(S(6), 0))
        self.refresh_box.bind("<<ComboboxSelected>>", lambda _e: self.on_refresh())
        self._wrap(ttk.Label(sets, style="Muted.TLabel", text=REFRESH_RULE_TEXT), S(460)).grid(
            row=3, column=0, columnspan=3, sticky="ew", pady=(S(2), 0))
        if self.autostart is not None:
            self.autostart_var = tk.BooleanVar(value=self.autostart.enabled())
            ttk.Checkbutton(sets, text="开机自启（登录后在托盘里运行）", variable=self.autostart_var,
                            command=self.on_autostart).grid(row=4, column=0, columnspan=3, sticky="w", pady=(S(6), 0))
        self.settings_status_var = tk.StringVar()
        self._wrap(ttk.Label(sets, textvariable=self.settings_status_var, style="Err.TLabel"), S(460)).grid(
            row=5, column=0, columnspan=3, sticky="ew", pady=(S(2), 0))

    def _build_weather_settings(self, wx) -> None:
        tk, ttk, S = self.tk, self.ttk, self.S
        wx.columnconfigure(0, weight=1)
        cur = self.wx or {}
        sentence = ttk.Frame(wx)
        sentence.grid(row=0, column=0, columnspan=2, sticky="w")
        self.wx_on_var = tk.BooleanVar(value=bool(cur.get("on")))
        ttk.Checkbutton(sentence, text="电脑停止推送", variable=self.wx_on_var,
                        command=self.on_wx_toggle).pack(side="left")
        lo, hi = weather.AFTER_MIN_RANGE
        self.wx_after_var = tk.StringVar(value=str(cur.get("after_min", weather.AFTER_MIN_DEFAULT)))
        self.wx_after_spin = ttk.Spinbox(sentence, from_=lo, to=hi, width=5, textvariable=self.wx_after_var,
                                         command=self.on_wx_after)
        self.wx_after_spin.pack(side="left", padx=S(6))
        self.wx_after_spin.bind("<FocusOut>", lambda _e: self.on_wx_after(revert=True))
        self.wx_after_spin.bind("<Return>", lambda _e: self.on_wx_after())
        ttk.Label(sentence, text="分钟后，屏幕自动显示天气").pack(side="left")
        ttk.Label(wx, text="城市").grid(row=1, column=0, sticky="w", pady=(S(8), 0))
        self.wx_city_var = tk.StringVar(value=WX_PLACEHOLDER)
        self.wx_city_entry = ttk.Entry(wx, textvariable=self.wx_city_var, style="Placeholder.TEntry")
        self.wx_city_entry.grid(row=2, column=0, sticky="ew", padx=(0, S(6)))
        self.wx_city_entry.bind("<FocusIn>", lambda _e: self._city_focus(True))
        self.wx_city_entry.bind("<FocusOut>", lambda _e: self._city_focus(False))
        self.wx_city_entry.bind("<Return>", lambda _e: self.search_city())
        self.wx_search_btn = ttk.Button(wx, text="搜索", command=self.search_city)
        self.wx_search_btn.grid(row=2, column=1)
        self.wx_pick_var = tk.StringVar()
        self.wx_pick_box = ttk.Combobox(wx, textvariable=self.wx_pick_var, state="readonly", width=34)
        self.wx_pick_box.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(S(6), 0))
        self.wx_pick_box.bind("<<ComboboxSelected>>", lambda _e: self.on_wx_pick())
        self.wx_pick_box.bind("<Return>", lambda _e: self.on_wx_pick())
        self.wx_place_var = tk.StringVar()
        self._wrap(ttk.Label(wx, textvariable=self.wx_place_var), S(460)).grid(
            row=4, column=0, columnspan=2, sticky="ew", pady=(S(6), 0))
        self.wx_status_var = tk.StringVar()
        self._wrap(ttk.Label(wx, textvariable=self.wx_status_var, style="Muted.TLabel"), S(460)).grid(
            row=5, column=0, columnspan=2, sticky="ew", pady=(S(2), 0))

    def _build_preview_tab(self) -> None:
        tk, ttk, S = self.tk, self.ttk, self.S
        pv = ttk.Frame(self.data_tabs, padding=(S(8), S(10), S(8), S(6)))
        self.data_tabs.add(pv, text="屏幕预览")
        self.preview_tab = pv
        pv.columnconfigure(0, weight=1)
        bezel = tk.Frame(pv, bg=BEZEL, padx=S(7), pady=S(7))     # 深色边框衬出白色的墨水屏
        bezel.grid(row=0, column=0)
        self.preview_label = tk.Label(bezel, bd=0, highlightthickness=0, compound="center", justify="center",
                                      fg=PAPER_TEXT, bg=PAPER, font=self.font_text, anchor="n")  # 窗口太矮时先保住上半屏
        self.preview_label.pack()
        row = ttk.Frame(pv)
        row.grid(row=1, column=0, sticky="ew", pady=(S(8), 0))
        self.scale_var = tk.IntVar(value=self.preview_scale)
        for s in reversed(PREVIEW_SCALES):
            ttk.Radiobutton(row, text=f"{s}×", value=s, variable=self.scale_var,
                            command=self.on_preview_scale).pack(side="right", padx=(S(8), 0))
        ttk.Label(row, text="缩放", style="Muted.TLabel").pack(side="right")
        self.preview_note_var = tk.StringVar()
        self._wrap(ttk.Label(row, textvariable=self.preview_note_var, style="Muted.TLabel"), S(420)).pack(
            side="left", fill="x", expand=True)

    def _build_summary_tab(self) -> None:
        tk, ttk, S = self.tk, self.ttk, self.S
        ds = ttk.Frame(self.data_tabs, padding=(S(8), S(8), S(8), S(6)))
        self.data_tabs.add(ds, text="数据明细")
        ds.columnconfigure(0, weight=1)
        ds.rowconfigure(0, weight=1)
        colors = self._text_widget_colors()
        colors.update(background=self.colors["bg"], highlightthickness=0)
        self.summary = tk.Text(ds, height=12, width=48, wrap="word", state="disabled", cursor="arrow",
                               font=self.font_text, spacing1=1, spacing3=1, **colors)
        sb = ttk.Scrollbar(ds, command=self.summary.yview)
        self.summary.configure(yscrollcommand=sb.set)
        self.summary.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        self.summary.tag_configure("h", font=self.font_bold, spacing1=6)
        self.summary.tag_configure("muted", foreground=self.colors["muted"])

    def _build_todo_tab(self) -> None:
        tk, ttk, S = self.tk, self.ttk, self.S
        td = ttk.Frame(self.data_tabs, padding=(S(8), S(8), S(8), S(6)))
        self.data_tabs.add(td, text="待办")
        td.columnconfigure(0, weight=1)
        lf = ttk.Frame(td)
        lf.grid(row=0, column=0, sticky="nsew")
        lf.columnconfigure(0, weight=1)
        self.todo_tree = ttk.Treeview(lf, columns=("done", "imp", "text"), show="headings",
                                      height=TODO_LIST_ROWS, selectmode="browse")
        for col, text, width, anchor in (("done", "完成", 48, "center"), ("imp", "重要", 48, "center"),
                                         ("text", "内容", 190, "w")):
            self.todo_tree.heading(col, text=text)
            self.todo_tree.column(col, width=S(width), anchor=anchor, stretch=col == "text")
        tsb = ttk.Scrollbar(lf, command=self.todo_tree.yview)
        self.todo_tree.configure(yscrollcommand=tsb.set)
        self.todo_tree.grid(row=0, column=0, sticky="nsew")
        tsb.grid(row=0, column=1, sticky="ns")
        self.todo_tree.bind("<<TreeviewSelect>>", lambda _e: self._on_todo_select())
        self.todo_tree.bind("<Delete>", lambda _e: self.todo_delete())
        self.todo_tree.bind("<Double-1>", self._on_todo_double)
        self.todo_empty = tk.Label(self.todo_tree, text="还没有待办。\n在下面输入内容，按 Enter 添加。", justify="center",
                                   bg=self.colors["field"], fg=self.colors["muted"], font=self.font_text)
        side = ttk.Frame(td)
        side.grid(row=1, column=0, sticky="w", pady=(S(6), 0))
        for text, cmd in (("上移", lambda: self.todo_move(-1)), ("下移", lambda: self.todo_move(1)),
                          ("删除", self.todo_delete)):
            ttk.Button(side, text=text, width=4, command=cmd).pack(side="left", padx=(0, S(4)))
        self.todo_done_var = tk.BooleanVar(value=False)
        self.todo_imp_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(side, text="已完成", variable=self.todo_done_var, command=self.on_todo_flags).pack(
            side="left", padx=(S(4), 0))
        ttk.Checkbutton(side, text="重要", variable=self.todo_imp_var, command=self.on_todo_flags).pack(
            side="left", padx=(S(2), 0))
        self.todo_mode_var = tk.StringVar()
        self._wrap(ttk.Label(td, textvariable=self.todo_mode_var, style="Muted.TLabel"), S(380)).grid(
            row=2, column=0, sticky="ew", pady=(S(6), 0))
        edit = ttk.Frame(td)
        edit.grid(row=3, column=0, sticky="ew", pady=(S(2), 0))
        self.todo_text_var = tk.StringVar()
        self.todo_entry = ttk.Entry(edit, textvariable=self.todo_text_var)
        self.todo_entry.pack(side="left", fill="x", expand=True)
        self.todo_entry.bind("<Return>", lambda _e: self.todo_commit())
        self.todo_entry.bind("<Escape>", lambda _e: self.todo_cancel_edit())
        self.todo_add_btn = ttk.Button(edit, text="添加", width=5, command=self.todo_add)
        self.todo_add_btn.pack(side="left", padx=(S(6), 0))
        self.todo_update_btn = ttk.Button(edit, text="保存", command=self.todo_update)
        self.todo_update_btn.pack(side="left", padx=(S(4), 0))
        # 右侧：待办页缩略图 + 预览 / 推送（不占用主预览标签）
        pv = ttk.Frame(td)
        pv.grid(row=0, column=1, rowspan=4, sticky="n", padx=(S(12), 0))
        self.todo_thumb = tk.Label(pv, bd=1, relief="solid", compound="center", justify="center",
                                   fg=PAPER_TEXT, bg=PAPER, font=self.font_text,
                                   text="点「预览」看\n待办页的样子")
        self._set_todo_thumb_blank()
        self.todo_thumb.pack()
        act = ttk.Frame(pv)
        act.pack(fill="x", pady=(S(6), 0))
        ttk.Button(act, text="预览", command=self.preview_todo).pack(side="left")
        self.todo_push_btn = ttk.Button(act, text="推送待办", command=self.push_todo)
        self.todo_push_btn.pack(side="left", padx=(S(6), 0))
        self.todo_preview_note_var = tk.StringVar()
        self._wrap(ttk.Label(pv, textvariable=self.todo_preview_note_var, style="Muted.TLabel"), S(200)).pack(
            fill="x", pady=(S(4), 0))
        self.todo_status_var = tk.StringVar()
        self._wrap(ttk.Label(pv, textvariable=self.todo_status_var, style="Status.TLabel"), S(200)).pack(
            fill="x", pady=(S(2), 0))
        self._refresh_todo_tree()

    def _set_todo_thumb_blank(self) -> None:
        img = self.tk.PhotoImage(master=self.root, width=THUMB_W, height=THUMB_H)
        img.put(PAPER, to=(0, 0, THUMB_W, THUMB_H))
        self._todo_thumb = img
        self.todo_thumb.configure(image=img)

    def _build_log_menu(self) -> None:
        tk = self.tk
        c = self.colors
        self.log_menu = tk.Menu(self.root, tearoff=0, bg=c["field"], fg=c["fg"])
        self.log_menu.add_command(label="复制", command=self.log_copy)
        self.log_menu.add_command(label="清空", command=self.log_clear)
        self.log_text.bind("<Button-3>", self._on_log_menu)
        self.log_text.bind("<Control-c>", lambda _e: (self.log_copy(), "break")[1])

    def _on_log_menu(self, event) -> None:
        self.log_text.focus_set()
        try:
            self.log_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.log_menu.grab_release()

    def log_copy(self) -> None:
        """复制选中的日志；没选就复制全部。"""
        t = self.log_text
        try:
            text = t.get("sel.first", "sel.last")
        except self.tk.TclError:
            text = t.get("1.0", "end-1c")
        if text:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)

    def log_clear(self) -> None:
        t = self.log_text
        t.configure(state="normal")
        t.delete("1.0", "end")
        t.configure(state="disabled")

    # -- 窗口大小 ----------------------------------------------------------
    def _initial_geometry(self) -> None:
        """默认约 1000×680（按 DPI 缩放），不超出屏幕；内容更大时撑到内容大小。"""
        root = self.root
        visible = root.state() == "normal"
        if visible:
            root.withdraw()                    # 先别显示：update() 让换行标签按真实宽度重排，再量大小
        for _ in range(3):
            root.update()
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight() - SCREEN_MARGIN
        w = min(max(self.S(DEFAULT_W), root.winfo_reqwidth()), sw)
        h = min(max(self.S(DEFAULT_H), root.winfo_reqheight()), sh)
        root.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")
        if visible:
            root.deiconify()

    # -- 串口 --------------------------------------------------------------
    def refresh_ports(self, initial: bool = False) -> None:
        self._port_error = ""
        try:
            ports = self._ports_fn()
        except Exception as e:  # noqa: BLE001
            ports = []
            log.warning("枚举串口失败: %s", e)
            self._port_error = "枚举串口失败（见日志）"
        self._port_map = {host.describe_port(p): p.device for p in ports}
        self.port_box["values"] = [AUTO_PORT, *self._port_map]
        if initial or not self.port_var.get():
            want = self.cfg.get("port")
            want = None if not want or host.is_tcp_port(str(want)) else str(want)
            for text, dev in self._port_map.items():
                if dev == want:
                    self.port_var.set(text)
                    break
            else:
                self.port_var.set(want or AUTO_PORT)
        self._update_conn_hint()

    def selected_port(self) -> str | None:
        """None = 自动检测（交给工作线程）。"""
        text = self.port_var.get().strip()
        if not text or text == AUTO_PORT:
            return None
        return self._port_map.get(text) or text.split()[0]

    def _update_conn_hint(self) -> None:
        if self._conn_error and self.worker is not None:
            hint = "连不上，会自动重试。要改串口或地址：改好后先点「断开」再点「连接」。"
        elif self.transport() == "wifi":
            hint = "" if self.addr_var.get().strip() else "点「搜索」在局域网里找板子，或直接填板子的 IP。"
        elif self._port_error:
            hint = self._port_error
        elif not self._port_map:
            hint = NO_PORT_HINT
        else:
            sel = self.selected_port()
            hint = "" if sel is None or sel in self._port_map.values() else \
                f"没有找到 {sel}：确认数据线已连接，再点「重新扫描」。"
        self.conn_hint_var.set(hint)

    # -- 方式 / Wi-Fi 地址 / 搜索 ---------------------------------------------
    def transport(self) -> str:
        return "wifi" if self.transport_var.get() == "wifi" else "serial"

    def _show_transport(self) -> None:
        wifi = self.transport() == "wifi"
        self.addr_label_var.set("地址" if wifi else "USB 串口")
        for show, hide in ((self.addr_box, self.port_box), (self.search_btn, self.refresh_btn)) if wifi else \
                          ((self.port_box, self.addr_box), (self.refresh_btn, self.search_btn)):
            hide.grid_remove()
            show.grid()
        self._update_conn_hint()

    def on_transport(self) -> None:
        self._show_transport()
        self.cfg["transport"] = self.transport()
        self._save_top(transport=self.transport())

    def _refresh_addr_values(self) -> None:
        """下拉框内容：发现到的板子（name (id) ip）+ 上次用过的地址。"""
        self._addr_map = {board_label(b): board_addr(b) for b in self._found}
        last = addr_for_display(self.cfg.get("wifi_addr") or "")
        values = list(self._addr_map)
        if last and last not in self._addr_map.values():
            values.append(last)
            self._addr_map[last] = last
        self.addr_box["values"] = values

    def selected_addr(self) -> str | None:
        text = self.addr_var.get().strip()
        if not text:
            return None
        return self._addr_map.get(text) or text

    def search_boards(self) -> None:
        if self._searching or self.worker is not None:
            return
        self._searching = True
        self.search_btn.configure(state="disabled", text="搜索中…")
        self._set_status("connecting", "正在局域网里搜索板子…")

        def run():
            try:
                self.events.put(("found", self._discover_fn()))
            except Exception as e:  # noqa: BLE001 - 搜索失败只报告，不影响窗口
                self.events.put(("found_error", f"{type(e).__name__}: {e}"))
        threading.Thread(target=run, daemon=True, name="discover").start()

    def _on_found(self, boards: list[dict] | None, error: str = "") -> None:
        self._searching = False
        self.search_btn.configure(state="disabled" if self.worker is not None else "normal", text="搜索")
        idle = (lambda text: self._set_status("idle", text)) if self.worker is None else (lambda text: None)
        if boards is None:
            log.warning("搜索板子失败: %s", error)
            idle("搜索失败（见日志），可以手动填 IP")
            return
        self._found = boards
        self._refresh_addr_values()
        if not boards:
            log.warning("没有找到板子：确认板子已连上 Wi-Fi，或手动填它的 IP（广播被路由器拦住时常见）")
            idle("没找到板子，可以手动填 IP")
            return
        log.info("找到 %d 块板子：%s", len(boards), "；".join(board_label(b) for b in boards))
        idle(f"找到 {len(boards)} 块板子")
        saved = addr_for_display(self.cfg.get("wifi_addr") or "")
        pick = next((b for b in boards if board_addr(b) == saved), None) or \
            (boards[0] if len(boards) == 1 else None)
        if pick is not None and (not self.addr_var.get().strip() or self.selected_addr() == saved):
            self.addr_var.set(board_label(pick))
        self._update_conn_hint()

    def on_board_connected(self, ip, port=None) -> None:
        """配网成功：把板子的 IP 记成 wifi_addr（预填，不自动切方式）。"""
        if not isinstance(ip, str) or not ip:
            return
        addr = ip if port in (None, discovery.DEFAULT_TCP_PORT) else f"{ip}:{port}"
        self.cfg["wifi_addr"] = addr
        self._save_top(wifi_addr=addr)
        self._refresh_addr_values()
        self.addr_var.set(addr)
        log.info("板子已连上 Wi-Fi：%s（已填入 Wi-Fi 地址）", addr)

    def auto_connect(self) -> None:
        """启动时按保存的方式自动连接；Wi-Fi 方式还没有地址就先搜索。"""
        if self.worker is not None:
            return
        if self.transport() == "wifi" and not self.selected_addr():
            self.search_boards()
            return
        self.toggle_connection()

    # -- 配网 -----------------------------------------------------------------
    def can_provision(self) -> bool:
        transport, proto = self.link_info
        return self.worker is not None and transport == "serial" and proto == "v2" and not self.wifi_unsupported()

    def wifi_unsupported(self) -> bool:
        return self.wifi_note.startswith("固件不支持")

    def wifi_text(self) -> str:
        transport, proto = self.link_info
        if transport is None:
            return "—"
        if transport == "tcp":
            return f"已通过 Wi-Fi 连接 {self._connected_addr}"
        if proto == "v1":
            return "固件是 v1，不支持"
        if self.wifi_note:
            return self.wifi_note
        return format_wifi_status(self.wifi_status) if self.wifi_status else "读取中…"

    def _update_screen_ui(self) -> None:
        text, bad = format_screen(self.link_info[0] is not None, self.display_fault, self.last_refresh)
        self.screen_var.set(text)
        self.screen_label.configure(foreground=STATE_STYLE["error"][0] if bad else "")

    def _update_wifi_ui(self) -> None:
        self._update_screen_ui()
        self.fw_var.set("固件：" + (self.fw_version or "—"))
        self.wifi_var.set("Wi-Fi：" + self.wifi_text())
        self.wifi_btn.configure(state="normal" if self.can_provision() else "disabled")
        self.ota_btn.configure(state="normal" if self.can_ota() else "disabled")
        self.resend_btn.configure(state="normal" if self.worker is not None else "disabled")
        self._update_page_ui()
        if self.wifi_dialog is not None:
            self.wifi_dialog.set_connected(self.can_provision())

    # -- 升级固件（Wi-Fi） -----------------------------------------------------
    def can_ota(self) -> bool:
        return (self.worker is not None and self.link_info == ("tcp", "v2")
                and bool(self.cfg.get("ota_token")) and not self.ota_running)

    def _ask_firmware_file(self) -> str:
        build = Path(__file__).resolve().parents[1] / "firmware" / ".pio" / "build" / "waveshare_esp32_42tri"
        kwargs = {"title": "选择固件文件", "filetypes": [("固件", "*.bin"), ("所有文件", "*.*")],
                  "initialdir": str(build) if build.is_dir() else None, "parent": self.root}
        if self._file_dialog is not None:
            return self._file_dialog(**kwargs) or ""
        from tkinter import filedialog
        return filedialog.askopenfilename(**{k: v for k, v in kwargs.items() if v is not None}) or ""

    def start_ota(self) -> None:
        if not self.can_ota():
            return
        path = self._ask_firmware_file()
        if not path:
            return
        image, err = host.read_firmware(path)
        if err:
            self._set_status("error", ota_error_text(err))
            log.warning("%s：%s", Path(path).name, ota_error_text(err))
            return
        if not self.ask_yes_no("升级固件", f"把 {Path(path).name}（{len(image) // 1024} KB）写入板子？\n\n"
                               "升级期间屏幕不刷新，大约 1 分钟；完成后板子自动重启。"
                               "如果新固件起不来，板子会自动回到旧固件。"):
            return
        self.ota_running = True
        self.worker.request_ota(image, self.cfg["ota_token"])
        self._set_status("connecting", "升级固件中 0%")
        self._show_ota_bar(0)
        log.info("准备升级固件：%s", Path(path).name)
        self._update_wifi_ui()

    def _show_ota_bar(self, pct: int | None) -> None:
        if pct is None:
            self.ota_bar.grid_remove()
            return
        self.ota_bar.configure(value=pct)
        self.ota_bar.grid()

    def _on_ota(self, ev: tuple) -> None:
        if ev[1] == "progress":
            sent, total = ev[2], ev[3]
            pct = sent * 100 // max(1, total)
            self._set_status("connecting", f"升级固件中 {pct}%")
            self._show_ota_bar(pct)
            return
        res = ev[2]
        self.ota_running = False
        self._show_ota_bar(None)
        if res.get("ok"):
            self._set_status("connecting", "升级完成，板子正在重启…")
            log.info("升级完成，板子正在重启…（重连后会显示新的固件版本）")
        else:
            text = ota_error_text(res.get("err", ""))
            self._set_status("error", text)
            log.warning("%s", text)
        self._update_wifi_ui()

    def _on_auto_pair(self, res: dict) -> None:
        """连上 USB 后自动要的一次配对码：存进配置（只在变了时写），配对码不进日志。"""
        token = (res.get("pair") or {}).get("token") if res.get("ok") else None
        if token:
            if self.cfg.get("ota_token") != token:
                self.cfg["ota_token"] = token
                self._save_top(ota_token=token)
                log.info("已和板子配对，之后可以通过 Wi-Fi 升级固件")
        elif res.get("err") not in ("stopped", "not_connected"):
            log.info("板子没有给配对码（%s），这块固件不支持 Wi-Fi 升级", res.get("err") or "无回复")
        self._update_wifi_ui()

    def open_wifi_dialog(self) -> None:
        if not self.can_provision():
            return
        if self.wifi_dialog is not None:
            self.wifi_dialog.top.lift()
            return
        self.wifi_dialog = WifiDialog(self)

    def send_command(self, tag: str, msg: dict, timeout: float = 5.0) -> bool:
        if self.worker is None:
            return False
        self.worker.request_command(tag, msg, timeout)
        return True

    def ask_yes_no(self, title: str, message: str, parent=None) -> bool:
        from tkinter import messagebox
        return bool(messagebox.askyesno(title, message, parent=parent or self.root))

    # -- 连接 / 断开 -------------------------------------------------------
    def toggle_connection(self) -> None:
        if self.worker is not None:
            if self.ota_running and not self.ask_yes_no(
                    "正在升级固件", "固件还在写入板子。现在断开会中断升级（板子仍是旧固件，不会变砖）。\n\n仍要断开吗？"):
                return
            self._user_stop = True
            self.worker.stop()
            self.conn_btn.configure(state="disabled", text="断开中…")
            self._set_status("connecting", "正在断开…")
            return
        if host.serial is None:
            self._set_status("error", "未安装 pyserial")
            return
        if self.transport() == "wifi":
            addr = self.selected_addr()
            if not addr:
                self._set_status("error", "请先点「搜索」，或填写板子的 IP")
                return
            port = wifi_url(addr)
            self._connected_addr = addr_for_display(addr)
            if self.cfg.get("wifi_addr") != self._connected_addr:
                self.cfg["wifi_addr"] = self._connected_addr
                self._save_top(wifi_addr=self._connected_addr)
        else:
            port = self.selected_port()      # "自动" = None：工作线程自己检测
        self._user_stop = False
        self._conn_error = False
        self.worker = self._worker_factory(
            self.cfg, port, self.events, ports_fn=self._ports_fn,
            options={**self.opts, "primary": self.primary_var.get(), "mouse": self.mouse_var.get(),
                     "calendar": bool(self.calendar_var.get()),
                     "heat_weeks": self._weeks() or self.opts["heat_weeks"]})
        self.worker.start()
        self.conn_btn.configure(text="断开")
        self._lock_inputs(True)
        self._set_status("connecting", "连接中…")
        self._update_wifi_ui()

    def _lock_inputs(self, locked: bool) -> None:
        state = "disabled" if locked else "normal"
        for w in (self.port_box, self.addr_box, self.search_btn, self.refresh_btn, *self.mode_radios):
            w.configure(state=state)

    def _set_status(self, state: str, text: str | None = None) -> None:
        self._status = (state, text or STATE_STYLE[state][1])
        self._render_status()

    def _render_status(self) -> None:
        state, text = self._status
        color = STATE_STYLE[state][0]
        if self.silent and state in ("v2", "v1"):
            color = STATE_STYLE["connecting"][0]
            age = f"，已 {int(self.silent_age)} 秒没有回复" if self.silent_age else ""
            text = f"无响应{age}"
        self.dot.configure(fg=color)
        self.status_var.set(text)
        if self.tray is not None:
            self.tray.set_title(f"{TRAY_TITLE} · {text}")

    # -- 显示设置 ----------------------------------------------------------
    def _weeks(self) -> int | None:
        try:
            v = int(self.weeks_var.get())
        except ValueError:
            return None
        lo, hi = dashboard_v2.HEAT_WEEKS
        return v if lo <= v <= hi else None

    def _report_save(self, err: str) -> None:
        self.save_error_var.set(f"保存设置失败：{err}。改动只在本次运行里有效。" if err else "")

    def _save(self, **updates) -> None:
        self._report_save(host.save_config_checked(self.config_path, None, v2=updates))

    def _save_top(self, **updates) -> None:
        self._report_save(host.save_config_checked(self.config_path, updates))

    def on_primary(self) -> None:
        primary = self.primary_var.get()
        self.opts["primary"] = primary
        if self.worker is not None:
            self.worker.request_options(primary=primary)
        self._save(primary=primary)

    def on_refresh(self) -> None:
        minutes = refresh_minutes(self.refresh_var.get())
        if minutes is None or minutes == self.opts["refresh_min"]:
            return
        self.opts["refresh_min"] = minutes
        if self.worker is not None:
            self.worker.request_options(refresh_min=minutes)
        self._save(refresh_min=minutes)

    def on_autostart(self) -> None:
        on = bool(self.autostart_var.get())
        try:
            self.autostart.set(on)
        except OSError as e:
            log.error("设置开机自启失败: %s", e)
            self.settings_status_var.set(f"设置开机自启失败：{e}")
            self.autostart_var.set(not on)
            return
        self.settings_status_var.set("")
        log.info("已%s开机自启", "打开" if on else "关闭")

    def on_calendar(self) -> None:
        calendar = bool(self.calendar_var.get())
        self.opts["calendar"] = calendar
        if self.worker is not None:
            self.worker.request_options(calendar=calendar)
        self._save(calendar=calendar)

    def _schedule_weeks(self) -> None:
        if self._spin_job is not None:
            self.root.after_cancel(self._spin_job)
        self._spin_job = self.root.after(SPIN_DEBOUNCE_MS, self.on_weeks)

    def _weeks_focus_out(self) -> None:
        if self._weeks() is None:
            self.weeks_var.set(str(self.opts["heat_weeks"]))
            self.settings_status_var.set("")

    def on_weeks(self) -> None:
        if self._spin_job is not None:
            self.root.after_cancel(self._spin_job)       # 直接调用时别让排好的那次再来一遍
            self._spin_job = None
        weeks = self._weeks()
        lo, hi = dashboard_v2.HEAT_WEEKS
        if weeks is None:
            self.settings_status_var.set(f"活动周数要填 {lo}–{hi} 之间的整数（现在仍是 {self.opts['heat_weeks']}）")
            return
        self.settings_status_var.set("")
        if weeks == self.opts["heat_weeks"]:
            return
        self.opts["heat_weeks"] = weeks
        if self.worker is not None:
            self.worker.request_options(heat_weeks=weeks)
        self._save(heat_weeks=weeks)

    def on_mouse(self) -> None:
        self.opts["mouse"] = bool(self.mouse_var.get())
        self._save(mouse=self.opts["mouse"])
        if self.worker is not None:
            log.info("鼠标读取设置已保存，下次连接生效")

    # -- 离线天气 ----------------------------------------------------------
    def _wx_current(self) -> dict:
        return self.wx or {"on": False, "place": "", "lat": None, "lon": None,
                           "after_min": weather.AFTER_MIN_DEFAULT}

    def _update_wx_ui(self) -> None:
        cur = self._wx_current()
        if cur["lat"] is not None:
            self.wx_place_var.set(f"已保存：{cur['place']}（{cur['lat']:.2f}, {cur['lon']:.2f}）")
        else:
            self.wx_place_var.set("还没选城市：输入城市名（中文或英文），按 Enter 搜索，再从结果里选一个")

    def _city_focus(self, focus_in: bool) -> None:
        text = self.wx_city_var.get()
        if focus_in and text == WX_PLACEHOLDER:
            self.wx_city_var.set("")
            self.wx_city_entry.configure(style="TEntry")
        elif not focus_in and not text.strip():
            self.wx_city_var.set(WX_PLACEHOLDER)
            self.wx_city_entry.configure(style="Placeholder.TEntry")

    def _city_text(self) -> str:
        text = self.wx_city_var.get().strip()
        return "" if text == WX_PLACEHOLDER else text

    def _save_wx(self, **changes) -> None:
        """改 v2.weather 的几个字段：存 config.json、同步给工作线程（连着时发给板子）。"""
        cur = {**self._wx_current(), **changes}
        conf = weather.weather_config(cur["on"], cur["place"], cur["lat"], cur["lon"], cur["after_min"])
        v2 = self.cfg.get("v2")
        self.cfg["v2"] = v2 = dict(v2) if isinstance(v2, dict) else {}
        v2["weather"] = conf
        self.wx = weather.weather_options(self.cfg)
        self._save(weather=conf)
        if self.worker is not None:
            self.worker.request_weather(self.wx)
        self._update_wx_ui()
        if self.view_page == "weather" and ("lat" in changes or "lon" in changes):
            self._show_weather_view()                    # 换了城市：预览跟着换

    def on_wx_toggle(self) -> None:
        self._save_wx(on=bool(self.wx_on_var.get()))

    def on_wx_after(self, revert: bool = False) -> None:
        lo, hi = weather.AFTER_MIN_RANGE
        saved = self._wx_current()["after_min"]
        try:
            v = int(self.wx_after_var.get())
        except ValueError:
            v = None
        if v is None or not lo <= v <= hi:
            self.wx_status_var.set(f"分钟数要填 {lo}–{hi} 之间的整数（现在仍是 {saved}）")
            if revert:
                self.wx_after_var.set(str(saved))
            return
        self.wx_status_var.set("")
        if self.wx is not None and v == self.wx["after_min"]:
            return
        self._save_wx(after_min=v)

    def search_city(self) -> None:
        name = self._city_text()
        if not name:
            self.wx_status_var.set("先输入城市名（中文或英文）")
            return
        self._wx_search_id += 1
        sid = self._wx_search_id
        self.wx_search_btn.configure(state="disabled", text="搜索中…")

        def run():      # 不在 Tk 线程里联网
            try:
                self.events.put(("geo", sid, self._geocode_fn(name), ""))
            except Exception as e:  # noqa: BLE001 - 网络 / 解析失败只提示，不弹窗
                self.events.put(("geo", sid, None, f"{type(e).__name__}: {e}"))
        threading.Thread(target=run, daemon=True, name="geocode").start()

    def _on_geo(self, sid: int, results: list[dict] | None, error: str) -> None:
        if sid != self._wx_search_id:
            return                                    # 已有更新的搜索
        self.wx_search_btn.configure(state="normal", text="搜索")
        if results is None:
            log.warning("城市搜索失败: %s", error)
            self.wx_status_var.set("城市搜索失败（要联网；详见日志）")
            return
        self._wx_results = results
        self.wx_pick_box["values"] = [r["label"] for r in results]
        if not results:
            self.wx_pick_var.set("")
            self.wx_status_var.set("没找到这个城市，换个写法试试（比如英文名）")
            return
        self.wx_status_var.set(f"找到 {len(results)} 个：用 ↑↓ 选一个，按 Enter 确定" if len(results) > 1 else "")
        if len(results) == 1:
            self.wx_pick_box.current(0)
            self.on_wx_pick()
        else:
            self.wx_pick_box.current(0)
            self.wx_pick_box.focus_set()

    def on_wx_pick(self) -> None:
        i = self.wx_pick_box.current()
        if not 0 <= i < len(self._wx_results):
            return
        r = self._wx_results[i]
        self._save_wx(place=r["place"], lat=r["lat"], lon=r["lon"])
        self.wx_status_var.set("")
        log.info("天气城市已设为 %s", r["place"])

    def _on_wx(self, kind: str, data) -> None:
        if kind == "status":
            self.wx_status_var.set("板子：" + weather.format_wx_status(data))
        elif kind == "unsupported":
            self.wx_status_var.set("板子：" + weather.format_wx_status({"unsupported": True}))
        else:
            self.wx_status_var.set(f"板子没有接受天气设置（{cmd_error_text(str(data))}）")

    # -- 页面 / 立即刷新 / 待办 ----------------------------------------------------
    def can_send_page(self) -> bool:
        return self.worker is not None and self.link_info[1] == "v2"

    def _controls_free(self) -> bool:
        """能发页面 / 刷新命令：连着 v2、屏幕没在刷新、没在升级。"""
        return self.can_send_page() and not self.refreshing and not self.ota_running

    def _update_page_ui(self) -> None:
        free = self._controls_free()
        self.refresh_now_btn.configure(state="normal" if free else "disabled")
        self.push_page_btn.configure(state="normal" if free and not self._todo_busy else "disabled")
        self.todo_push_btn.configure(state="normal" if free and not self._todo_busy else "disabled")
        self.page_board_var.set(f"屏幕当前：{PAGE_CN[self.page]}")
        if not self.can_send_page() and not self.refreshing:
            self._set_view_status()                    # 预览那一行 + 断开时的提示
        elif DISCONNECTED_PAGE_TEXT in self.page_status_var.get():
            self._set_view_status()

    def _send_ui_command(self, tag: str, msg, timeout: float, doing: str) -> bool:
        if not self._controls_free():
            self.page_status_var.set(DISCONNECTED_PAGE_TEXT if not self.can_send_page() else "屏幕刷新中，稍等…")
            return False
        self.page_status_var.set(doing)
        return self.send_command(tag, msg, timeout)

    def _set_view_status(self) -> None:
        v = self.view_page
        text = f"预览：{PAGE_CN[v]} · " + ("就是屏幕当前的页面" if v == self.page else "还没推送到屏幕")
        if not self.can_send_page():
            text += "\n" + DISCONNECTED_PAGE_TEXT
        self.page_status_var.set(text)

    def on_page(self) -> None:
        """点单选框：只切换屏幕预览标签的内容，什么也不发给板子（要推送点「推送到屏幕」）。"""
        page = self.page_var.get()
        if page not in todo_page.PAGE_KEYS:
            return
        self.view_page = page
        self.data_tabs.select(self.preview_tab)
        self._show_view()
        if not self.refreshing:                        # 刷新中保留进度文字
            self._set_view_status()

    def push_page(self) -> None:
        """把选中的页面推给板子：page + refresh_now。待办页要是改过 / 还没推过，先带上待办图（一条命令，只刷新一次）。"""
        page = self.page_var.get()
        if page not in todo_page.PAGE_KEYS:
            return
        if not self._controls_free():
            self.page_status_var.set(DISCONNECTED_PAGE_TEXT if not self.can_send_page() else "屏幕刷新中，稍等…")
            return
        if page == "todo" and self._todo_dirty:
            try:
                frames, img = todo_page.push_frames(self.todo_items, refresh=False)
            except ValueError:
                self.page_status_var.set(page_error_text("too_big"))
                return
            except Exception as e:  # noqa: BLE001
                self.page_status_var.set(f"渲染失败：{type(e).__name__}: {e}")
                return
            self._show_todo_image(img, f"{_hhmm_ts()} 待办页，已交给板子（{len(frames)} 条消息）")
            self._todo_busy = True
            self._push_page = page
            self.page_status_var.set("推送待办图和页面…")
            self.send_command("todo", [*frames, todo_page.page_msg(page), todo_page.refresh_now_msg()], 8.0)
            self._update_page_ui()
            return
        if self._send_ui_command("page", [todo_page.page_msg(page), todo_page.refresh_now_msg()], 8.0,
                                 f"推送「{PAGE_CN[page]}」到屏幕…"):
            self._push_page = page

    def _page_pushed(self, page: str) -> None:
        """命令成功：这一页就是屏幕当前的页面，保存到 config。"""
        self.page = page
        self._save(page=page)
        self.page_board_var.set(f"屏幕当前：{PAGE_CN[page]}")

    def refresh_now(self) -> None:
        self._send_ui_command("refresh", [todo_page.refresh_now_msg()], 5.0, "已请求立即刷新屏幕…")

    def _on_ctrl_r(self, _event=None) -> str:
        if self._controls_free():
            self.refresh_now()
        return "break"

    def _begin_refresh(self, src: str) -> None:
        """命令成功后：固件没有「开始刷新」事件，自己进入「刷新中」，等 refreshed 或超时。"""
        self.refreshing = True
        self._refresh_src = src
        if self._refresh_job is not None:
            self.root.after_cancel(self._refresh_job)
        self._refresh_job = self.root.after(REFRESH_TIMEOUT_MS, lambda: self._end_refresh(None))
        self._set_refresh_text(REFRESHING_TEXT)
        self._update_page_ui()

    def _end_refresh(self, when: datetime | None) -> None:
        if not self.refreshing:
            return
        self.refreshing = False
        if self._refresh_job is not None:
            self.root.after_cancel(self._refresh_job)
            self._refresh_job = None
        self._set_refresh_text(f"屏幕已刷新 {_hhmm_ts(when)}" if when else
                               "没有收到刷新完成的回复（屏幕可能已经刷新，看一眼确认）")
        self._update_page_ui()

    def _set_refresh_text(self, text: str) -> None:
        if self._refresh_src == "todo":
            self.todo_status_var.set(text)
        self.page_status_var.set(text)

    def _on_ui_cmd(self, tag: str, res: dict) -> None:
        pushed = None
        if tag in ("page", "todo"):
            pushed, self._push_page = self._push_page, None
        if tag == "todo":
            self._todo_busy = False
            if res.get("ok"):
                self._todo_dirty = False
                if pushed:
                    self._page_pushed(pushed)
                self._begin_refresh("todo")
            else:
                self.todo_status_var.set(page_error_text(res.get("err", "")))
                if pushed:
                    self.page_status_var.set(page_error_text(res.get("err", "")))
                self.todo_preview_note_var.set("没有送达：屏幕仍是旧内容")
                log.warning("推送待办失败：%s", res.get("err") or "未知")
                self._update_page_ui()
            return
        if res.get("ok"):
            if tag == "page" and pushed:
                self._page_pushed(pushed)
            self._begin_refresh(tag)
        else:
            self.page_status_var.set(page_error_text(res.get("err", "")))
            log.info("%s 命令没有成功：%s", tag, res.get("err") or "未知")
            self._update_page_ui()

    # 待办编辑
    def _refresh_todo_tree(self, select: int | None = None) -> None:
        t = self.todo_tree
        t.delete(*t.get_children())
        for i, it in enumerate(self.todo_items):
            t.insert("", "end", iid=str(i), values=todo_row(it))
        if select is not None and 0 <= select < len(self.todo_items):
            t.selection_set(str(select))
            t.see(str(select))
        if self.todo_items:
            self.todo_empty.place_forget()
        else:
            self.todo_empty.place(relx=0.5, rely=0.5, anchor="center")
        self._on_todo_select()

    def _selected_todo(self) -> int | None:
        sel = self.todo_tree.selection()
        return int(sel[0]) if sel else None

    def _update_todo_mode(self) -> None:
        i = self._selected_todo()
        full = len(self.todo_items) >= todo_page.ITEMS_MAX
        if i is not None:
            self.todo_mode_var.set(f"正在编辑第 {i + 1} 条：Enter 保存修改，Esc 取消（回到新增）")
        elif full:
            self.todo_mode_var.set(f"已有 {todo_page.ITEMS_MAX} 条（最多 {todo_page.ITEMS_MAX} 条），删掉一些才能再添加")
        else:
            self.todo_mode_var.set("新增事项：输入内容后按 Enter 添加；选中一行可修改（双击或 Enter 编辑，Delete 删除）")
        self.todo_update_btn.configure(state="normal" if i is not None else "disabled")
        self.todo_add_btn.configure(state="disabled" if full else "normal")

    def _on_todo_select(self) -> None:
        i = self._selected_todo()
        if i is None or i >= len(self.todo_items):
            self.todo_done_var.set(False)
            self.todo_imp_var.set(False)
        else:
            it = self.todo_items[i]
            self.todo_text_var.set(it["text"])
            self.todo_done_var.set(it["done"])
            self.todo_imp_var.set(it["important"])
        self._update_todo_mode()

    def _on_todo_double(self, event) -> None:
        row = self.todo_tree.identify_row(event.y)
        if row:
            self.todo_tree.selection_set(row)
            self.todo_entry.focus_set()
            self.todo_entry.selection_range(0, "end")
            self.todo_entry.icursor("end")

    def _save_todo(self, select: int | None = None) -> None:
        conf = todo_page.todo_config(self.todo_items)
        v2 = self.cfg.get("v2")
        self.cfg["v2"] = v2 = dict(v2) if isinstance(v2, dict) else {}
        v2["todo"] = conf
        self._save(todo=conf)
        self._todo_dirty = True
        self._todo_view_ppm = None
        self._refresh_todo_tree(select)
        if self.view_page == "todo":
            self._show_todo_view()

    def todo_commit(self) -> None:
        """输入框里按 Enter：选中了一行 = 保存修改，否则 = 添加。"""
        if self._selected_todo() is not None:
            self.todo_update()
        else:
            self.todo_add()

    def todo_cancel_edit(self) -> None:
        self.todo_tree.selection_remove(*self.todo_tree.selection())
        self.todo_text_var.set("")
        self._on_todo_select()

    def todo_add(self) -> None:
        text = todo_page.clean_text(self.todo_text_var.get())
        if len(self.todo_items) >= todo_page.ITEMS_MAX:
            self.todo_status_var.set(f"最多 {todo_page.ITEMS_MAX} 条，先删掉一些")
            return
        if not text:
            return
        self.todo_items.append({"text": text, "done": False, "important": False})
        self._save_todo(None)                              # 不选中新行：下一次 Enter 还是添加
        self.todo_tree.see(str(len(self.todo_items) - 1))
        self.todo_text_var.set("")
        self.todo_status_var.set("")
        self.todo_entry.focus_set()

    def todo_update(self) -> None:
        i = self._selected_todo()
        text = todo_page.clean_text(self.todo_text_var.get())
        if i is None or not text:
            return
        self.todo_items[i]["text"] = text
        self._save_todo(None)
        self.todo_text_var.set("")
        self.todo_entry.focus_set()

    def todo_delete(self) -> None:
        i = self._selected_todo()
        if i is None:
            return
        del self.todo_items[i]
        self._save_todo(min(i, len(self.todo_items) - 1) if self.todo_items else None)
        if not self.todo_items:
            self.todo_text_var.set("")

    def todo_move(self, d: int) -> None:
        i = self._selected_todo()
        if i is None or not 0 <= i + d < len(self.todo_items):
            return
        items = self.todo_items
        items[i], items[i + d] = items[i + d], items[i]
        self._save_todo(i + d)

    def on_todo_flags(self) -> None:
        i = self._selected_todo()
        if i is None:
            return
        self.todo_items[i]["done"] = bool(self.todo_done_var.get())
        self.todo_items[i]["important"] = bool(self.todo_imp_var.get())
        self._save_todo(i)

    # 待办预览（缩略图在待办标签里，不占主预览）/ 推送
    def _show_todo_image(self, img, note: str) -> None:
        self.todo_ppm = todo_page.to_ppm(img)
        try:
            from PIL import Image
            thumb = todo_page.to_ppm(img.convert("RGB").resize((THUMB_W, THUMB_H), Image.LANCZOS))
            self._todo_thumb = self.tk.PhotoImage(master=self.root, data=thumb, format="ppm")
            self.todo_thumb.configure(image=self._todo_thumb, text="")
        except Exception:  # noqa: BLE001 - 缩略图失败不影响推送
            pass
        self.todo_preview_note_var.set(note)

    def preview_todo(self) -> None:
        try:
            black, red = todo_page.render_todo(self.todo_items)
        except Exception as e:  # noqa: BLE001 - 字体 / Pillow 出问题只提示
            self.todo_status_var.set(f"渲染失败：{type(e).__name__}: {e}")
            return
        self._show_todo_image(todo_page.to_rgb(black, red), f"{_hhmm_ts()} 待办页预览（还没有推送）")

    def push_todo(self) -> None:
        if not self._controls_free():
            self.todo_status_var.set(DISCONNECTED_PAGE_TEXT.replace("推送到屏幕", "推送待办")
                                     if not self.can_send_page() else "屏幕刷新中，稍等…")
            return
        try:
            frames, img = todo_page.push_frames(self.todo_items)
        except ValueError:
            self.todo_status_var.set(page_error_text("too_big"))
            return
        except Exception as e:  # noqa: BLE001
            self.todo_status_var.set(f"渲染失败：{type(e).__name__}: {e}")
            return
        self._show_todo_image(img, f"{_hhmm_ts()} 待办页，已交给板子（{len(frames) - 2} 条消息）")
        self._todo_busy = True
        self.todo_status_var.set("推送中…")
        self.send_command("todo", frames, 8.0)
        self._update_page_ui()

    def resend(self) -> None:
        if self.worker is None:
            log.info("未连接，无可重发的内容")
            return
        self.worker.request_reset()
        log.info("下一轮会重新推送全部数据")

    # -- 轮询 --------------------------------------------------------------
    def _poll(self) -> None:
        if self._closing:
            return
        try:
            self._drain_events()
            self._drain_log()
        finally:
            if not self._closing:
                self._poll_job = self.root.after(POLL_MS, self._poll)

    def _drain_events(self) -> None:
        changed = False
        while True:
            try:
                ev = self.events.get_nowait()
            except queue.Empty:
                break
            kind = ev[0]
            if kind == "status":
                self._set_status(ev[1], ev[2])
                if self.worker is not None:
                    self._conn_error = ev[1] == "error"
                    self._lock_inputs(not self._conn_error)      # 重试连接时输入框保持可改
                    self._update_conn_hint()
            elif kind == "msg":
                self.msgs[ev[1]] = ev[2]
                self.push_var.set("上次推送 " + _hhmm_ts(ev[3]))
                changed = True
            elif kind == "link":
                self.link_info = (ev[1], ev[2])
                self._schedule_preview()          # 在线 / 离线图标跟着变
                if ev[1] is None:
                    self.wifi_status, self.wifi_note = None, ""
                elif self.link_info == ("tcp", "v2") and not self.cfg.get("ota_token") and not self._pair_hint_shown:
                    self._pair_hint_shown = True
                    log.info("还没和板子配对：用 USB 线连一次上位机后，就能通过 Wi-Fi 升级固件")
                self._update_wifi_ui()
            elif kind == "wifi":
                if ev[1] == "status":
                    self.wifi_status, self.wifi_note = ev[2], ""
                    self._update_wifi_ui()
                if self.wifi_dialog is not None:
                    self.wifi_dialog.handle(ev)
            elif kind == "cmd":
                if ev[1] == "auto_status":
                    self._on_auto_status(ev[2])
                elif ev[1] == "auto_pair":
                    self._on_auto_pair(ev[2])
                elif ev[1] in ("todo", "page", "refresh"):
                    self._on_ui_cmd(ev[1], ev[2])
                elif self.wifi_dialog is not None:
                    self.wifi_dialog.handle(ev)
            elif kind == "fault":
                self.display_fault = ev[1]
                self._update_screen_ui()
            elif kind == "refreshed":
                self.last_refresh = ev[1]
                self._update_screen_ui()
                self._end_refresh(ev[1])
            elif kind == "silent":
                self.silent, self.silent_age = bool(ev[1]), ev[2]
                self._render_status()
            elif kind == "reboot":
                log.info("板子刚重启过")
                self.last_refresh = None
                self._update_screen_ui()
            elif kind == "port":
                self._on_port_followed(ev[1])
            elif kind == "todo_auto":
                if ev[1].get("ok"):
                    self.todo_status_var.set(f"{_hhmm_ts()} 日期变了，已自动重新推送待办")
            elif kind == "fw":
                self.fw_version = ev[1]
                self.fw_var.set("固件：" + (ev[1] or "—"))
                if ev[1]:
                    log.info("板子固件版本 %s", ev[1])
            elif kind == "ota":
                self._on_ota(ev)
            elif kind == "tray":
                if ev[1] == "show":
                    self.show_window()
                elif ev[1] == "quit":
                    if self.request_quit():
                        return
            elif kind == "preview":
                self._on_preview(ev[1], ev[2])
            elif kind == "wxpreview":
                self._on_wx_preview(ev[1], ev[2])
            elif kind == "geo":
                self._on_geo(ev[1], ev[2], ev[3])
            elif kind == "wx":
                self._on_wx(ev[1], ev[2])
            elif kind == "found":
                self._on_found(ev[1])
            elif kind == "found_error":
                self._on_found(None, ev[1])
            elif kind == "done":
                self.worker = None
                self.link_info, self.wifi_status, self.wifi_note = (None, None), None, ""
                self.display_fault = self.last_refresh = self.fw_version = None
                self.silent, self.silent_age, self._conn_error = False, None, False
                self.ota_running = False
                self._show_ota_bar(None)
                self._todo_busy = False
                self.refreshing = False
                if self._refresh_job is not None:
                    self.root.after_cancel(self._refresh_job)
                    self._refresh_job = None
                self.wx_status_var.set("")
                self.conn_btn.configure(state="normal", text="连接")
                self._lock_inputs(False)
                self._show_transport()
                self._update_wifi_ui()
                self._schedule_preview()
                if self._user_stop:   # 非用户主动断开 = 会话异常退出，保留错误状态
                    self._set_status("idle")
        if changed:
            self._render_summary()

    def _on_port_followed(self, device: str) -> None:
        """工作线程发现串口号变了、改用了另一个：显示跟着变（选「自动」时不动）。"""
        if self.transport() != "serial" or self.selected_port() is None or not device:
            return
        text = next((t for t, d in self._port_map.items() if d == device), device)
        self.port_var.set(text)

    def _on_auto_status(self, res: dict) -> None:
        """连上 USB 后自动问的一次 wifi_status 的结果。"""
        if res.get("ok"):
            if res.get("status"):
                self.wifi_status, self.wifi_note = res["status"], ""
        elif any(k in res.get("err", "") for k in ("unknown", "bad_type", "unsupported")):
            self.wifi_note = "固件不支持 Wi-Fi（需 1.3.0 以上）"
        elif res.get("err") not in ("stopped", "not_connected"):
            self.wifi_note = f"读取失败（{cmd_error_text(res.get('err', ''))}）"
        self._update_wifi_ui()

    def _drain_log(self) -> None:
        items = self.log_handler.drain()
        if not items:
            return
        t = self.log_text
        at_end = t.yview()[1] >= 0.999
        t.configure(state="normal")
        for level, text in items:
            tag = "err" if level >= logging.ERROR else "warn" if level >= logging.WARNING else ()
            t.insert("end", text + "\n", tag)
        extra = int(t.index("end-1c").split(".")[0]) - LOG_MAX_LINES
        if extra > 0:
            t.delete("1.0", f"{extra + 1}.0")
        t.configure(state="disabled")
        if at_end:
            t.see("end")

    def _render_summary(self) -> None:
        """数据明细（文字）立刻更新；屏幕预览稍等一下再渲染（见 _schedule_preview）。"""
        t = self.summary
        t.configure(state="normal")
        t.delete("1.0", "end")
        for i, line in enumerate(format_summary(self.msgs)):
            tag = "h" if line.startswith("【") else "muted" if "尚未推送" in line or "无数据" in line else ()
            t.insert("end", "\n" if i else "")
            t.insert("end", line, tag)
        t.configure(state="disabled")
        self._schedule_preview()

    # -- 屏幕预览 ------------------------------------------------------------
    def _schedule_preview(self) -> None:
        if self._closing:
            return
        if self._preview_job is not None:
            self.root.after_cancel(self._preview_job)
        self._preview_job = self.root.after(PREVIEW_DEBOUNCE_MS, self._start_preview)

    def preview_online(self) -> bool:
        """板子算在线 = 上位机正连着它推 v2。断开后板子过一会儿会画离线图标，预览直接画出那个样子。"""
        return self.link_info[1] == "v2"

    def _dash_view(self) -> bool:
        return self.view_page in ("auto", "dash")

    def _shown_ppm(self) -> bytes | None:
        """屏幕预览标签现在该显示的那张图（1×）：自动 / 仪表盘 = 实时仪表盘，待办 = 待办页，天气 = 天气页。"""
        if self.view_page == "todo":
            return self._todo_view_ppm
        if self.view_page == "weather":
            return self.wx_ppm
        return self.preview_ppm

    def _start_preview(self) -> None:
        self._preview_job = None
        if self._in_tray:
            return                         # 藏在托盘时不跑渲染器；msgs 照常更新，show_window 时补渲染
        dash = self._dash_view()
        if self.preview is None:
            if dash:
                self._show_placeholder(RENDER_MISSING_TEXT)
                self.preview_note_var.set("")
        elif not self.msgs:
            if dash:
                self._show_placeholder("还没有推送过数据。\n连上板子后，这里显示屏幕将要画出的内容。")
                self.preview_note_var.set(self.renderer_note)
        else:
            self.preview.submit(preview_input(self.msgs), self.preview_online())   # 看别的页时也照常渲染，切回来就是新的

    def _on_preview(self, ppm: bytes | None, note: str) -> None:
        dash = self._dash_view()
        if ppm is None:                    # 渲染失败：旧图（如果有）留着，提示换成原因
            log.warning("屏幕预览：%s", note)
            self._dash_note = note
            if dash:
                if self.preview_ppm is None:
                    self._show_placeholder("预览渲染失败，原因见下方")
                self.preview_note_var.set(note)
            return
        self.preview_ppm = ppm
        bits = [note or f"{_hhmm_ts()} 按固件绘图代码渲染" + ("" if self.preview_online() else "（板子离线时的样子）")]
        if self.renderer_note:
            bits.append(self.renderer_note)
        self._dash_note = "；".join(bits)
        if dash:
            self._show_preview_image()
            self.preview_note_var.set(self._dash_note)

    def _show_view(self) -> None:
        """按 view_page 刷新屏幕预览标签（点单选框 / 改了待办 / 换了城市时）。"""
        v = self.view_page
        if v == "todo":
            self._show_todo_view()
        elif v == "weather":
            self._show_weather_view()
        elif self.preview_ppm is not None:
            self._show_preview_image()
            self.preview_note_var.set(self._dash_note)
        else:
            self._start_preview()

    def _show_todo_view(self) -> None:
        if self._todo_view_ppm is None:
            try:
                black, red = todo_page.render_todo(self.todo_items)
                self._todo_view_ppm = todo_page.to_ppm(todo_page.to_rgb(black, red))
            except Exception as e:  # noqa: BLE001 - 字体 / Pillow 出问题只提示
                self._show_placeholder(f"待办页渲染失败：{type(e).__name__}: {e}")
                self.preview_note_var.set("")
                return
        self._show_preview_image()
        self.preview_note_var.set(f"{_hhmm_ts()} 待办页，按现在的待办渲染")

    def _show_weather_view(self) -> None:
        if self.wx_ppm is not None:
            self._show_preview_image()
            self.preview_note_var.set(self.wx_note)
        else:
            self._show_placeholder("正在拉取天气…")
            self.preview_note_var.set("")
        self.wx_previewer.request(self.wx)             # 10 分钟内同一座城市用缓存，不会重复联网

    def _on_wx_preview(self, ppm: bytes | None, note: str) -> None:
        self.wx_ppm = ppm
        if ppm is not None:
            self.wx_note = f"{_hhmm_ts()} 天气页，按固件绘图代码渲染（数据来自 Open-Meteo）"
            if self.renderer_note:
                self.wx_note += "；" + self.renderer_note
        else:
            self.wx_note, self.wx_text = "", note
            if "没有设置城市" not in note:
                log.warning("天气预览：%s", note)
        if self.view_page == "weather":
            if ppm is not None:
                self._show_preview_image()
                self.preview_note_var.set(self.wx_note)
            else:
                self._show_placeholder(note)
                self.preview_note_var.set("")

    def _show_preview_image(self) -> None:
        img = self.tk.PhotoImage(master=self.root, data=self._shown_ppm(), format="ppm")
        if self.preview_scale > 1:
            img = img.zoom(self.preview_scale)
        self._preview_img = img
        self.preview_label.configure(image=img, text="")

    def _show_placeholder(self, text: str) -> None:
        """还没有图时：和屏幕一样大的浅色底 + 一段说明文字。"""
        w, h = 400 * self.preview_scale, 300 * self.preview_scale
        img = self.tk.PhotoImage(master=self.root, width=w, height=h)
        img.put(PAPER, to=(0, 0, w, h))
        self._preview_img = img
        self.preview_label.configure(image=img, text=text, wraplength=w - 40)

    def on_preview_scale(self) -> None:
        scale = int(self.scale_var.get())
        if scale == self.preview_scale:
            return
        self.preview_scale = scale
        self.cfg["preview_scale"] = scale
        self._save_top(preview_scale=scale)
        if self._shown_ppm() is not None:
            self._show_preview_image()
        elif self._dash_view():
            self._start_preview()
        elif self.view_page == "weather":
            self._show_placeholder(self.wx_text or "正在拉取天气…")
        else:
            self._show_todo_view()
        self._fit_window()

    def _fit_window(self) -> None:
        """窗口放不下内容时撑大，但不超出屏幕（必要时往上 / 往左挪）；用户拉大的窗口不缩回去。"""
        root = self.root
        root.update_idletasks()
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight() - SCREEN_MARGIN
        mapped = bool(root.winfo_ismapped())
        cur_w, cur_h = (root.winfo_width(), root.winfo_height()) if mapped else (0, 0)
        need_w, need_h = root.winfo_reqwidth(), root.winfo_reqheight()
        if need_w <= cur_w and need_h <= cur_h:
            return
        w, h = min(max(need_w, cur_w), sw), min(max(need_h, cur_h), sh)
        x, y = (root.winfo_x(), root.winfo_y()) if mapped else ((sw - w) // 2, (sh - h) // 3)
        root.geometry(f"{w}x{h}+{max(0, min(x, sw - w))}+{max(0, min(y, sh - h))}")

    # -- 托盘 / 关闭 -------------------------------------------------------
    def show_window(self) -> None:
        if self._in_tray:
            self._in_tray = False
            self._schedule_preview()       # 藏起来期间错过的更新，现在补渲染一次
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def on_window_close(self) -> None:
        """窗口的 ×：有托盘就收进托盘（连接和推送照常，升级也照常），没有托盘就退出。"""
        if self.tray is None:
            self.request_quit()
            return
        if not self._tray_hint_shown:
            self._tray_hint_shown = True
            if not self.cfg.get("tray_note_off"):
                self._show_tray_note()
        self.root.withdraw()
        self._in_tray = True

    def _build_tray_note(self):
        """一次性提示窗（还没 grab / 显示）-> (Toplevel, 「不再提示」变量, 确定按钮)。"""
        tk, ttk, S = self.tk, self.ttk, self.S
        top = tk.Toplevel(self.root)
        top.title(TRAY_TITLE)
        top.transient(self.root)
        top.resizable(False, False)
        outer = ttk.Frame(top, padding=S(16))
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="已收进托盘", style="Status.TLabel").pack(anchor="w")
        ttk.Label(outer, wraplength=S(340), justify="left",
                  text="窗口关了，但上位机还在托盘里运行，继续给屏幕推送。\n"
                       "点托盘图标可以再打开窗口；右键托盘图标选「退出」才会真正退出。"
                  ).pack(anchor="w", pady=(S(6), S(10)))
        dont = tk.BooleanVar(value=False)
        ttk.Checkbutton(outer, text="不再提示", variable=dont).pack(anchor="w")
        ok = ttk.Button(outer, text="知道了", command=top.destroy)
        ok.pack(anchor="e", pady=(S(10), 0))
        top.bind("<Return>", lambda _e: top.destroy())
        top.bind("<Escape>", lambda _e: top.destroy())
        return top, dont, ok

    def _tray_note_dialog(self) -> bool:
        """弹出一次性提示，返回有没有勾「不再提示」。"""
        top, dont, ok = self._build_tray_note()
        center_over(top, self.root)
        try:
            top.grab_set()
        except self.tk.TclError:
            pass
        ok.focus_set()
        self.root.wait_window(top)
        return bool(dont.get())

    def _show_tray_note(self) -> None:
        if self._tray_note_dialog():
            self.cfg["tray_note_off"] = True
            self._save_top(tray_note_off=True)

    def request_quit(self) -> bool:
        """真正退出前的确认：升级固件期间先问一句。返回 True = 已退出。"""
        if self.ota_running:
            self.show_window()
            if not self.ask_yes_no("正在升级固件", "固件还在写入板子，现在退出会中断升级"
                                   "（板子仍是旧固件，不会变砖）。\n\n仍要退出吗？"):
                return False
        self.on_close()
        return True

    def on_close(self) -> None:
        self._closing = True
        for job in (self._spin_job, self._preview_job, self._refresh_job):
            if job is not None:
                self.root.after_cancel(job)
        self._spin_job = self._preview_job = self._refresh_job = None
        if self._poll_job is not None:
            self.root.after_cancel(self._poll_job)
            self._poll_job = None
        if self.wifi_dialog is not None:
            self.wifi_dialog.close()
        if self.worker is not None:
            self.worker.stop()
            self.worker.join(CLOSE_JOIN_S)
        detach_log_handler(self.log_handler)
        if self.tray is not None:
            self.tray.stop()
        self.root.destroy()


def main(argv: list[str] | None = None) -> int:
    import argparse
    import tkinter as tk
    ap = argparse.ArgumentParser(description="墨水屏副屏上位机（窗口 + 托盘）")
    ap.add_argument("--tray", action="store_true", help="启动时不显示窗口，只在托盘里运行")
    args = ap.parse_args(argv)
    instance = acquire_single_instance()
    if instance is None:   # 已经有一个在跑（多半在托盘里）：两个会抢同一块板子
        if not args.tray:
            from tkinter import messagebox
            r = tk.Tk()
            r.withdraw()
            messagebox.showinfo(TRAY_TITLE, "上位机已经在运行了，请点任务栏托盘里的图标打开窗口。", parent=r)
            r.destroy()
        return 0
    enable_dpi_awareness()     # 必须在创建 Tk 之前
    root = tk.Tk()
    apply_tk_scaling(root)
    autostart = None
    if sys.platform == "win32":
        autostart = RegistryAutostart()
    app = App(root, tray_factory=Tray, autostart=autostart, start_hidden=args.tray)
    root.after(300, app.auto_connect)  # 打开即按保存的方式（串口 / Wi-Fi）自动连接
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
