#!/usr/bin/env python3
"""The v2 session shared by the command line (epaper_host.main) and the GUI: no tkinter, testable headless.

  V2Session   one step of "connected -> negotiate -> (weather sync) -> push the due v2 blocks"; notices a new
              connection and a rebooted board (a second READY line) and then forgets what the board was sent.
  Backoff     growing delay after an iteration raised, so a loop never dies silently and never spins.
  HostWorker  the GUI's background thread: V2Session in a loop plus commands / OTA / TODO / WiFi probing,
              talking to the window through an event queue (see the HostWorker docstring).
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from datetime import datetime

import epaper_host as host
import tm_source
import weather

log = logging.getLogger("epaper_host.worker")

COMMAND_MAX_S = 15.0       # 一条命令最多让工作线程停多久
WIFI_PROBE_S = 2.0
WIFI_PROBE_RETRY_S = 5.0   # 第一次探测超时（板子正忙着刷屏）时再试一次用的超时
PORT_FALLBACK_AFTER = 3    # 串口连续打不开、且已不在设备列表里 N 次后，改用自动检测到的板子
BACKOFF_BASE_S, BACKOFF_MAX_S = 2.0, 30.0
RETRY_MIN_S = 5.0          # 重连间隔下限


class Backoff:
    """2 s, 4 s, 8 s ... up to 30 s after consecutive failures; reset() after a good iteration."""

    def __init__(self, base: float = BACKOFF_BASE_S, cap: float = BACKOFF_MAX_S):
        self.base, self.cap, self.failures = base, cap, 0

    def next(self) -> float:
        delay = min(self.cap, self.base * 2 ** min(self.failures, 10))
        self.failures += 1
        return delay

    def reset(self) -> None:
        self.failures = 0


class V2Session:
    """The part of the v2 loop that the CLI and the GUI worker share. Call step() once per tick from the thread
    that owns the link.

      on_proto(proto)   after negotiation, before anything is pushed (the GUI reports link / status here)
      on_v2_ready(link) once per connection (and again after a board reboot) when the board turned out to speak v2
      on_reboot()       the board rebooted while connected (a second READY on the same connection)
    """

    def __init__(self, link, pusher, wx=None, *, force_v2: bool = False, on_v2_ready=None, on_reboot=None):
        self.link, self.pusher, self.wx = link, pusher, wx
        self.force_v2 = force_v2
        self.on_v2_ready = on_v2_ready
        self.on_reboot = on_reboot
        self._session = None       # link.opened_at of the connection the pusher state belongs to
        self._ready_seen = 0
        self._v2_ready_done = False

    def _forget(self) -> None:
        """New connection or rebooted board: resend everything, sync the weather settings again."""
        self.pusher.reset()
        if self.wx is not None:
            self.wx.resync()
        self._v2_ready_done = False

    def step(self, mono: float, now: datetime, on_proto=None) -> str | None:
        link = self.link
        if not link.connected:
            return None
        if self._session != link.opened_at:
            self._session = link.opened_at
            self._ready_seen = link.ready_count
            self._forget()
        elif link.ready_count > self._ready_seen:
            if self._ready_seen >= 1:
                log.info("board restarted (READY again on the same connection): resending all blocks")
                self._forget()
                if self.on_reboot is not None:
                    self.on_reboot()
            self._ready_seen = link.ready_count
        proto = "v2" if self.force_v2 else link.negotiate()
        if on_proto is not None:
            on_proto(proto)
        if proto == "v2":
            if not self._v2_ready_done:
                self._v2_ready_done = True
                if self.on_v2_ready is not None:
                    self.on_v2_ready(link)
            if self.wx is not None and link.connected:
                try:
                    self.wx.sync(link)    # offline weather settings: once per connection / change
                except Exception as e:  # noqa: BLE001 - optional feature, never break the host
                    log.warning("weather sync failed: %s", type(e).__name__)
            host.push_v2(link, self.pusher, mono, now)
        return proto


class ObservedPusher(host.V2Pusher):
    """V2Pusher + 每条消息真正发出去后回调一次（供界面展示）。不改协议行为。"""

    def __init__(self, *args, on_sent=None, **kwargs):
        self._on_sent = on_sent
        super().__init__(*args, **kwargs)

    def mark_sent(self, kind: str, msg: dict, mono: float) -> None:
        super().mark_sent(kind, msg, mono)
        if self._on_sent is not None:
            self._on_sent(kind, msg)


class HostWorker(threading.Thread):
    """一次连接会话：循环里连串口 -> 协商 -> v2 则 push_v2（和 epaper_host.main() 共用 V2Session）。

    和界面只通过 `events` 队列和 request_* 交流：
      ("status", state, text)   state = connecting | v2 | v1 | error
      ("msg", kind, msg, ts)    一条消息已发出（板子回 ERR 的块不算，下一轮重发）
      ("link", transport, proto) 连接形态变了：transport = serial | tcp（没连上时 None），proto = v1 | v2 | None
      ("wifi", kind, data)      板子在 USB 串口上发来 `WIFI {json}`：kind = status | scan | pair
      ("cmd", tag, result)      request_command 的结果：{"ok","err","scan","status","pair"}；
                                列表命令（待办图片等）的结果是 {"ok","err","sent","total"[,"failed"]}，只在最后一条
                                （img_end / refresh_now）收到回复、或中途失败后才发，失败时界面应取消「待办预览」固定；
                                USB v2 会话开始时自动发的两条：tag = auto_status（wifi_status）、auto_pair（pair，
                                result["pair"] 里是配对码 {"token","id"}）
      ("fault", line | None)    本次连接上板子报的屏幕驱动故障（DIAG 行），变化时才发
      ("refreshed", ts)         板子报告一次整屏刷新完成（DIAG epd refresh done）；固件没有「刷新开始」的行，
                                界面想显示「刷新中」可以在 refresh_now 命令成功后自己计时
      ("fw", version | None)    本次连接上板子报的固件版本（WiFi 连接 READY 后的 DIAG fw 行），变化时才发
      ("ota", "progress", sent, total)  request_ota 传输进度（约每 5% 一次）
      ("ota", "done", result)   request_ota 的结果：{"ok","err",...}；ok 时板子随后断开并重启
      ("wx", kind, data)      离线天气设置同步的结果：kind = status（data = 板子的 wx_status）| unsupported | error
      ("silent", bool, age)   已连接但板子超过 SILENT_AFTER（90 秒）没有任何回复 = True（界面显示「无响应」），恢复 = False
      ("reboot",)             检测到板子重启（同一连接上又收到 READY）；已重发所有数据块和天气设置
      ("port", device)        串口自动改用了新的设备（原来的串口不在了，自动检测到唯一的板子）
      ("todo_auto", result)   日期变了，自动重新渲染并推送了待办图片（不刷新屏幕）：{"ok","err",...}
      ("done",)               线程结束（串口和鼠标读取都已关闭）
    另有两个只读的辅助：reply_age()（距板子上次回复的秒数，None = 没连上）和 `port`（当前实际用的串口）。
    request_options / request_reset / request_command / request_ota 只记下请求并唤醒循环，真正的修改在
    工作线程里、两次推送之间做，所以不会和正在进行的 push_v2 抢 pusher / link 状态；一条命令最多占用
    它自己的 timeout（扫描约 10 秒），升级固件约 1 分钟，期间不推送数据。命令内容（含 WiFi 密码、配对码）不写日志。
    每一轮循环单独 try/except：出错记日志、状态变成 error、退避 2–30 秒后继续，线程不会悄悄死掉。
    """

    def __init__(self, cfg: dict, port: str | None, events: queue.Queue, *, link_factory=host.Link,
                 tm_factory=tm_source.TmSource, mouse_factory=host.load_mouse_source, now_fn=None,
                 options: dict | None = None, ports_fn=None):
        super().__init__(daemon=True, name="host-worker")
        self.cfg = cfg
        self.port = port or None
        self.events = events
        self.opts = options if options is not None else host.v2_options(cfg)
        self._link_factory = link_factory
        self._tm_factory = tm_factory
        self._mouse_factory = mouse_factory
        self._ports_fn = ports_fn
        self._now = now_fn or (lambda: datetime.now().astimezone())
        self._stop_evt = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._pending: dict = {}
        self._reset = False
        self._commands: list[tuple[str, dict, float]] = []
        self._status: tuple[str, str] | None = None
        self._link_info: tuple | None = None
        self._fault: str | None = None
        self._fw: str | None = None
        self._silent = False
        self._ota: tuple | None = None      # (固件 bytes 或文件路径, 配对码)
        self._wx_opts = weather.weather_options(cfg)
        self._wx = weather.WxSync(lambda: self._wx_opts, lambda kind, data: self.events.put(("wx", kind, data)))
        self._todo_date = None              # 上次成功推送待办图片时的日期；None = 还没推送过
        self._link = self._pusher = self._session = None
        self._backoff = Backoff()
        self._next_retry = 0.0
        self._rebooting = False
        self._open_failures = 0
        self._interval = 30.0
        self._retry = 10.0

    # -- 界面线程调用 ------------------------------------------------------
    def request_options(self, **opts) -> None:
        with self._lock:
            self._pending.update({k: v for k, v in opts.items() if v is not None})
        self._wake.set()

    def request_reset(self) -> None:
        with self._lock:
            self._reset = True
        self._wake.set()

    def request_command(self, tag: str, msg: dict | list, timeout: float = 5.0) -> None:
        """让工作线程在两次推送之间发一条命令并等它的 OK / ERR；结果以 ("cmd", tag, result) 回来。
        msg 是 list 时按顺序发一串（每条各等 timeout），第一条失败就停；结果多一个 "sent"（已确认条数）。
        tag == "todo" 且成功：记下今天的日期，之后日期变了会自动重新推送待办图片。"""
        with self._lock:
            self._commands.append((tag, msg, min(max(float(timeout), 0.5), COMMAND_MAX_S)))
        self._wake.set()

    def request_weather(self, opts: dict | None) -> None:
        """离线天气设置改了（weather.weather_options 的格式）：下一轮发给板子。"""
        with self._lock:
            self._wx_opts = opts
        self._wake.set()

    def request_ota(self, image, token: str) -> None:
        """让工作线程在两次推送之间升级固件（image 是固件 bytes 或 .bin 路径）；进度和结果以 ("ota", ...) 回来。"""
        with self._lock:
            self._ota = (image, token)
        self._wake.set()

    def reply_age(self) -> float | None:
        """距板子上次回复（READY / OK / ERR）的秒数；没连上 = None。"""
        link = self._link
        return link.reply_age() if link is not None else None

    def stop(self) -> None:
        self._stop_evt.set()
        self._wake.set()

    # -- 工作线程 ----------------------------------------------------------
    def _emit_status(self, state: str, text: str) -> None:
        if self._status != (state, text):
            self._status = (state, text)
            self.events.put(("status", state, text))

    def _emit_link(self, transport: str | None, proto: str | None) -> None:
        if self._link_info != (transport, proto):
            self._link_info = (transport, proto)
            self.events.put(("link", transport, proto))

    def _on_sent(self, kind: str, msg: dict) -> None:
        self.events.put(("msg", kind, msg, datetime.now()))

    def _on_wifi(self, kind: str, data: dict) -> None:   # 读线程调用；队列线程安全
        self.events.put(("wifi", kind, data))

    def _on_diag(self, kind: str, _line: str) -> None:   # 读线程调用；故障状态由循环从 link 读，见 _emit_fault
        if kind == "refresh":
            self.events.put(("refreshed", datetime.now()))

    def _emit_fault(self, fault: str | None) -> None:
        if self._fault != fault:
            self._fault = fault
            self.events.put(("fault", fault))

    def _emit_silent(self, link) -> None:
        age = link.reply_age() if link is not None else None
        silent = age is not None and age > host.SILENT_AFTER
        if silent != self._silent:
            self._silent = silent
            self.events.put(("silent", silent, age))

    def _run_commands(self, link) -> None:
        with self._lock:
            cmds, self._commands = self._commands, []
        for tag, msg, timeout in cmds:
            started = self._now()
            if self._stop_evt.is_set():
                result = host.CommandResult(False, "stopped")
            elif not link.connected:
                result = host.CommandResult(False, "not_connected")
            elif isinstance(msg, list):
                result = self._run_sequence(link, msg, timeout)
            else:
                log.info("发送命令 %s", msg.get("type"))    # 只记类型；wifi_set 的内容含密码
                try:
                    result = link.command(msg, timeout)
                except Exception as e:  # noqa: BLE001 - 异常文字不带命令内容，只报类型
                    result = host.CommandResult(False, type(e).__name__)
            result = result.as_dict() if hasattr(result, "as_dict") else result
            if tag == "todo" and result.get("ok"):
                self._todo_date = started.date()
            self.events.put(("cmd", tag, result))

    def _run_sequence(self, link, msgs: list, timeout: float) -> dict:
        """依次发一串消息（待办图片的 img_begin / img_chunk / img_end / refresh_now）；每条等它的 OK。
        图片的 begin / chunk 超时没回时重发一次（按偏移写，重发无害），其余失败就停。
        -> {"ok","err","sent","total"}，sent = 已经被板子确认的条数；结果只在最后一条的回复到了（或失败）之后返回。"""
        log.info("发送 %d 条消息：%s …", len(msgs), msgs[0].get("type") if msgs else "-")
        sent, total = 0, len(msgs)
        for m in msgs:
            if self._stop_evt.is_set():
                return {"ok": False, "err": "stopped", "sent": sent, "total": total}
            if not link.connected:
                return {"ok": False, "err": "not_connected", "sent": sent, "total": total}
            try:
                if m.get("type") in ("img_begin", "img_chunk"):
                    res = link.command(m, timeout, retry_on_timeout=True)
                else:
                    res = link.command(m, timeout)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "err": type(e).__name__, "sent": sent, "total": total}
            if not res.ok:
                return {"ok": False, "err": res.err, "sent": sent, "total": total, "failed": m.get("type")}
            sent += 1
        return {"ok": True, "err": "", "sent": sent, "total": total}

    def _repush_todo_if_new_day(self, link) -> None:
        """待办图片左上角之外的日期（MM-DD）是画进图里的：日期变了就重画并重推（不发 refresh_now，下次刷屏时显示）。"""
        if self._todo_date is None or not link.connected or link.proto != "v2":
            return
        now = self._now()
        if now.date() == self._todo_date:
            return
        import todo_page    # Pillow: only the GUI path needs it
        items = todo_page.todo_items(self.cfg)
        try:
            frames, _img = todo_page.push_frames(items, now, refresh=False)
        except Exception as e:  # noqa: BLE001
            log.warning("待办图片重新渲染失败：%s", type(e).__name__)
            self._todo_date = now.date()      # 别每轮都重试；下次手动推送或明天再说
            return
        result = self._run_sequence(link, frames, 8.0)
        if result["ok"]:
            self._todo_date = now.date()
            log.info("日期变了，已重新推送待办图片")
        elif result["err"] in ("not_connected", "stopped"):
            return
        else:
            log.warning("日期变了，重新推送待办图片失败：%s", result["err"])
            self._todo_date = now.date()      # 失败也记下，免得每轮重发；用户手动推送即可
        self.events.put(("todo_auto", result))

    def _probe_wifi(self, link) -> None:
        """USB 串口连上并确认是 v2 后问一次板子的 WiFi 状态（开机那一行状态我们多半没赶上），
        再要一次配对码（WiFi 升级固件要用；旧固件回 ERR unknown_type，由界面忽略）。
        板子正在刷屏时可能来不及回：超时就用更长的超时再问一次。"""
        for tag, msg in (("auto_status", {"v": 2, "type": "wifi_status"}), ("auto_pair", {"v": 2, "type": "pair"})):
            result = link.command(msg, WIFI_PROBE_S)
            if result.err == "timeout" and link.connected:
                result = link.command(msg, WIFI_PROBE_RETRY_S)
            self.events.put(("cmd", tag, result.as_dict()))

    def _on_v2_ready(self, link) -> None:
        if not host.is_tcp_port(self.port):
            self._probe_wifi(link)

    def _emit_fw(self, version: str | None) -> None:
        if self._fw != version:
            self._fw = version
            self.events.put(("fw", version))

    def _run_ota(self, link) -> bool:
        """有升级请求就做（阻塞到结束）。返回 True = 已写入、板子在重启，调用方别马上重连。"""
        with self._lock:
            req, self._ota = self._ota, None
        if req is None:
            return False
        image, token = req
        if self._stop_evt.is_set():
            result = host.CommandResult(False, "stopped")
        elif not link.connected:
            result = host.CommandResult(False, "not_connected")
        else:
            if not isinstance(image, (bytes, bytearray)):
                image, err = host.read_firmware(image)
                if err:
                    self.events.put(("ota", "done", host.CommandResult(False, err).as_dict()))
                    return False
            log.info("开始升级固件（%d 字节），期间暂停推送", len(image))
            last = [-1]

            def progress(sent, total):
                pct = sent * 100 // total
                if pct // 5 != last[0] // 5 or sent == total:     # 约每 5% 报一次
                    last[0] = pct
                    self.events.put(("ota", "progress", sent, total))
            try:
                result = link.ota(bytes(image), token, progress)
            except Exception as e:  # noqa: BLE001 - 异常文字不带配对码，只报类型
                result = host.CommandResult(False, type(e).__name__)
        self.events.put(("ota", "done", result.as_dict()))
        if not result.ok:
            return False
        deadline = time.monotonic() + 3.0        # 板子约 0.5 秒后自己断开；等一下，别抢在重启前重连
        while link.connected and time.monotonic() < deadline and not self._stop_evt.is_set():
            time.sleep(0.05)
        link.close()
        return True

    def _apply_pending(self, pusher: host.V2Pusher) -> None:
        with self._lock:
            opts, reset = self._pending, self._reset
            self._pending, self._reset = {}, False
        if opts:
            pusher.set_options(**opts)
        if reset:
            pusher.reset()
            log.info("已请求重发全部数据块")

    def _follow_port(self, link) -> None:
        """串口连续打不开、而且已经不在设备列表里（拔了换口 / 重新枚举成别的 COM 号）：自动检测到唯一的板子就改用它。"""
        if host.is_tcp_port(self.port):
            return
        self._open_failures += 1
        if self._open_failures < PORT_FALLBACK_AFTER or not self.port:
            return
        try:
            ports = list(self._ports_fn()) if self._ports_fn is not None else (
                list(host.list_ports.comports()) if host.list_ports is not None else [])
        except Exception as e:  # noqa: BLE001
            log.warning("枚举串口失败: %s", type(e).__name__)
            return
        if any(p.device == self.port for p in ports):
            return
        found = host.find_port(ports)
        if found and found != self.port:
            log.info("串口 %s 已经不在了，改用自动检测到的 %s", self.port, found)
            self.port = link.fixed_port = found
            self._open_failures = 0
            self.events.put(("port", found))

    def _on_proto(self, link, proto, tcp) -> None:
        self._emit_link(getattr(link, "transport", "serial"), proto)
        # link.connect() 清掉旧连接的故障，所以这里读到的只属于本次连接
        self._emit_fault(getattr(link, "display_fault", None))
        self._emit_fw(getattr(link, "fw_version", None))
        if proto == "v2":
            self._emit_status("v2", "已连接 v2")
        elif proto == "v1":
            self._emit_status("v1", "已连接 v1（不支持）")
        else:
            self._emit_status("connecting", "已连接，等待固件响应…" if tcp else "已打开串口，等待固件响应…")

    def _iteration(self) -> float:
        """一轮：处理请求 -> 需要时连接 -> 一步 V2Session。返回下一轮前最多等多久（秒）。"""
        link, pusher, session = self._link, self._pusher, self._session
        tcp = host.is_tcp_port(self.port)
        self._wake.clear()          # 先清再处理：处理期间新来的请求会让下面的 wait 立刻返回
        self._apply_pending(pusher)
        self._run_commands(link)
        if self._run_ota(link):
            self._next_retry = time.monotonic() + self._retry   # 板子重启要几秒，过一会儿再连
            self._rebooting = True
        t0 = time.monotonic()       # 命令 / 升级可能占用很久：计时从这里开始，别让 pusher 的节奏提前
        attempted = False
        if not link.connected and t0 >= self._next_retry:
            attempted = True
            self._emit_status("connecting", "连接中…")
            self._rebooting = False
            link.connect()
            self._next_retry = time.monotonic() + self._retry
            if link.connected:
                link.wait_handshake(2.0)   # READY / OK 先到，免得第一轮还不知道 v1 还是 v2
        if not link.connected:
            self._emit_link(None, None)
            self._emit_fault(None)
            self._emit_fw(None)
            self._emit_silent(None)
            if self._rebooting:
                self._emit_status("connecting", "板子重启中，稍后自动重连…")
            else:
                self._emit_status("error", f"无法连接 {self.port}，稍后重试（详见日志）" if tcp
                                  else f"无法打开串口 {self.port or '(自动检测)'}，稍后重试（详见日志）")
                if attempted:
                    self._follow_port(link)
        else:
            self._open_failures = 0
            session.step(t0, self._now(), on_proto=lambda proto: self._on_proto(link, proto, tcp))
            self._repush_todo_if_new_day(link)
            link.diagnose()
            self._emit_silent(link)
        return host.loop_wait(link.connected, self._interval, self._retry, time.monotonic() - t0)

    def run(self) -> None:
        pusher = link = None
        try:
            cfg, opts = self.cfg, self.opts
            self._interval = host.clamp_interval(cfg["interval"])
            self._retry = max(RETRY_MIN_S, float(cfg["reconnect_interval"]))
            self._pusher = pusher = ObservedPusher(
                self._tm_factory(opts["tm_file"]),
                mouse_factory=self._mouse_factory if opts["mouse"] else None,
                primary=opts["primary"], heat_rows=opts["heat_rows"], heat_weeks=opts["heat_weeks"],
                calendar=opts["calendar"], refresh_min=opts["refresh_min"],
                on_sent=self._on_sent)
            self._link = link = self._link_factory(self.port, int(cfg["baud"]), float(cfg["handshake_timeout"]),
                                                   float(cfg["response_timeout"]))
            link.on_wifi = self._on_wifi
            link.on_diag = self._on_diag
            self._session = V2Session(link, pusher, self._wx, on_v2_ready=self._on_v2_ready,
                                      on_reboot=lambda: self.events.put(("reboot",)))
            self._emit_status("connecting", "连接中…")
            while not self._stop_evt.is_set():
                try:
                    wait = self._iteration()
                    self._backoff.reset()
                except Exception as e:  # noqa: BLE001 - 一轮出错不能让线程死掉：记日志、退避、下一轮重来
                    wait = self._backoff.next()
                    log.exception("上位机循环出错（%.0f 秒后重试）", wait)
                    self._emit_status("error", f"{type(e).__name__}: {e}")
                    if self._backoff.failures >= 3:       # 连着出错：断开连接，下一轮重新连
                        try:
                            link.close()
                        except Exception:  # noqa: BLE001
                            pass
                self._wake.wait(wait)
        except Exception as e:  # noqa: BLE001 - 起步阶段出错（建 pusher / link）：线程不能悄悄死掉
            log.exception("上位机会话异常退出")
            self._emit_status("error", f"{type(e).__name__}: {e}")
        finally:
            with self._lock:
                leftover, self._commands = self._commands, []
            for tag, _msg, _timeout in leftover:
                self.events.put(("cmd", tag, host.CommandResult(False, "stopped").as_dict()))
            with self._lock:
                ota, self._ota = self._ota, None
            if ota is not None:
                self.events.put(("ota", "done", host.CommandResult(False, "stopped").as_dict()))
            for closer in (pusher and pusher.close, link and link.close):
                if closer is not None:
                    try:
                        closer()
                    except Exception as e:  # noqa: BLE001
                        log.warning("关闭失败: %s", e)
            self.events.put(("done",))
