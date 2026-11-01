#!/usr/bin/env python3
"""ESP32 e-paper side screen - PC host.

Collects system stats + AI token usage (via tokscale or ccusage) and pushes one
protocol-v1 JSON line over USB serial every N seconds.

With v2 firmware (READY ... v2, or the board accepts a v2 ping) it instead pushes the
dashboard-v2 blocks (dash/mouse/quota/models/heat) built from Token Monitor's local
Hub file; see docs/dashboard-v2-protocol.md. v1 firmware keeps the v1 stats path.

  python epaper_host.py                 # auto-detect port, run forever
  python epaper_host.py --port COM5     # fixed port
  python epaper_host.py --port tcp://192.168.1.50   # over WiFi (firmware 1.3.0+)
  python epaper_host.py --discover      # find boards on the LAN (UDP 7331)
  python epaper_host.py --port COM5 --pair            # USB: fetch the board's OTA pairing code into config
  python epaper_host.py --ota firmware.bin            # WiFi: upgrade the firmware (firmware 1.3.1+)
  python epaper_host.py --dry-run --once
  python epaper_host.py --dry-run-v2    # print the five v2 messages, no serial
  python epaper_host.py --list-ports
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import logging
import math
import os
import select
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import dashboard_v2
import discovery
import tm_source
import weather
from weather import WxSync

try:
    import serial
    from serial.tools import list_ports
except ImportError:  # only needed when actually talking to the board
    serial = None
    list_ports = None

log = logging.getLogger("epaper_host")

MAX_LINE = 1024
BAUD = 115200

DEFAULT_CONFIG = {
    "port": None,              # null = auto-detect; "COM5", or "tcp://192.168.1.50[:7330]" over WiFi
    "transport": "serial",     # GUI only: "serial" | "wifi"
    "wifi_addr": None,         # GUI only: last used board address, e.g. "192.168.1.50"
    "ota_token": None,         # board pairing code for WiFi firmware upgrades (--pair / GUI over USB)
    "handshake_timeout": 120,   # diagnostic deadline; no reset on silence
    "response_timeout": 180,   # silence warning interval during operation
    "reconnect_interval": 10,  # I/O failures only
    "baud": BAUD,
    "interval": 30,            # seconds between stats messages
    "token_source": "tokscale",  # "tokscale" (all tools + Claude limits) or "ccusage"
    "token_interval": 60,      # seconds between token refreshes
    "token_timeout": 45,       # seconds before a CLI call is abandoned
    "tokscale_cmd": None,      # null = tokscale on PATH, else Token Monitor's bundled exe
    "tokscale_clients": None,  # null = all tools; e.g. "claude" or "claude,codex"
    "limits_interval": 180,    # seconds between `tokscale usage` (network) calls
    "ccusage_cmd": None,       # null = find ccusage(.cmd) on PATH
    "disk": None,              # null = system drive
    "gpu": True,               # query nvidia-smi if available
    "v2": None,                # dashboard-v2 options, merged over DEFAULT_V2 (see load_config)
}

DEFAULT_V2 = {
    "enabled": True,           # false = never probe/use v2, always v1 stats
    "tm_file": None,           # null = %APPDATA%\Token Monitor\hub-devices.json
    "mouse": True,             # read the mouse battery via mouse_rapoo.py
    "primary": "today",        # "today" | "total": which period the TOKENS block / models expand (live)
    "heat_rows": 7,            # heat map cells per column: 7 (weekday-aligned) or 5 (live)
    "heat_weeks": 10,          # heat map length in whole weeks, 4-26 (live); the board shows the last 10
    "calendar": False,         # true = the ACTIVITY block as this month's calendar (heat `view`:"cal", live)
    "refresh_min": 3,          # screen refresh at most every N minutes: 3/5/10/15/30/60 (dash `every`, live)
    "weather": None,           # offline weather page: {"on","place","lat","lon","after_min"}, see weather.py
}

# USB hints for discovery only; these do not identify a board or MCU.
VID_ESP32_NATIVE = 0x303A  # Espressif USB vendor
VID_WCH = 0x1A86           # CH340 / CH343 / CH9102
VID_SILABS = 0x10C4        # CP210x
VID_PRIORITY = {VID_ESP32_NATIVE: 0, VID_WCH: 1, VID_SILABS: 1}

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------
def compact(n: float, units=("", "K", "M", "B", "T"), always_decimal=True) -> str:
    """145_900_000 -> '145.9M', 3300 -> '3.3K', 512 -> '512'."""
    u = 0
    while abs(n) >= 1000 and u < len(units) - 1:
        n /= 1000
        u += 1
    decimals = 0 if u == 0 or (not always_decimal and round(abs(n), 1) >= 100) else 1
    if round(abs(n), decimals) >= 1000 and u < len(units) - 1:  # 999.96K -> 1.0M
        n /= 1000
        u += 1
        decimals = 0 if not always_decimal and abs(n) >= 100 else 1
    return f"{n:.{decimals}f}{units[u]}"


def fmt_rate(bytes_per_sec: float) -> str:
    """Network speed in B/s -> '120K', '3.4M', '0'."""
    return compact(max(0.0, bytes_per_sec), ("", "K", "M", "G", "T"), always_decimal=False)


def r2(x) -> float | None:
    return round(float(x), 2) if isinstance(x, (int, float)) else None


# ---------------------------------------------------------------------------
# System stats
# ---------------------------------------------------------------------------
class SystemCollector:
    def __init__(self, disk: str | None, gpu: bool):
        import psutil  # collection-only dependency; serial/demo use needs no psutil
        self._psutil = psutil
        self.disk = disk or (os.environ.get("SystemDrive", "C:") + "\\" if os.name == "nt" else "/")
        self.nvidia_smi = shutil.which("nvidia-smi") if gpu else None
        self._psutil.cpu_percent(None)  # prime: first call always returns 0
        self._net_prev = self._net_sample()

    def _net_sample(self):
        c = self._psutil.net_io_counters()
        return time.monotonic(), c.bytes_sent, c.bytes_recv

    def _net(self) -> tuple[str, str]:
        now = self._net_sample()
        t0, s0, r0 = self._net_prev
        self._net_prev = now
        dt = now[0] - t0
        if dt <= 0:
            return "0", "0"
        return fmt_rate((now[1] - s0) / dt), fmt_rate((now[2] - r0) / dt)

    def _gpu(self) -> dict:
        if not self.nvidia_smi:
            return {}
        try:
            out = subprocess.run(
                [self.nvidia_smi, "--query-gpu=utilization.gpu,temperature.gpu",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5, creationflags=NO_WINDOW,
            ).stdout
            util, temp = (p.strip() for p in out.splitlines()[0].split(","))
            return {"gpu": int(float(util)), "gpu_temp": int(float(temp))}
        except Exception as e:  # noqa: BLE001
            log.debug("nvidia-smi failed: %s", e)
            return {}

    def collect(self) -> dict:
        up, down = self._net()
        try:
            disk = round(self._psutil.disk_usage(self.disk).percent)
        except OSError:
            disk = None
        d = {
            "time": datetime.now().strftime("%m-%d %H:%M"),
            "cpu": round(self._psutil.cpu_percent(None)),
            "mem": round(self._psutil.virtual_memory().percent),
            "disk": disk,
            "net_up": up,
            "net_down": down,
        }
        d.update(self._gpu())
        return d


# ---------------------------------------------------------------------------
# Token usage (slow CLIs) - refreshed in a background thread, cached
# ---------------------------------------------------------------------------
def _decode_err(b: bytes) -> str:
    # cmd.exe error messages use the console code page (e.g. GBK), node uses UTF-8.
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        import locale
        return b.decode(locale.getpreferredencoding(False), "replace")


EMPTY_TOK = {
    "block_tokens": None, "block_cost": None, "burn_tpm": None, "remain_min": None,
    "today_tokens": None, "today_cost": None,
}

# tokscale.exe bundled with the Token Monitor desktop app (Javis603/token-monitor).
TOKSCALE_BUNDLED = (Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Token Monitor" / "resources"
                    / "app.asar.unpacked" / "node_modules" / "@tokscale" / "cli-win32-x64-msvc"
                    / "bin" / "tokscale.exe")
SESSION_HOURS = 5  # Claude session window


def _num(x) -> float:
    return float(x) if isinstance(x, (int, float)) else 0.0


def _minutes_until(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.astimezone()
    return max(0, round((t - datetime.now(t.tzinfo)).total_seconds() / 60))


class TokenCollector(threading.Thread):
    """Runs a slow usage CLI in the background and caches the `tok` object.

    Subclasses implement fetches() -> [(name, fn)]; each fn returns a partial
    `tok` dict. A failing fn leaves its fields null; nothing propagates."""

    source = "tokens"

    def __init__(self, cmd: str, interval: float, timeout: float):
        super().__init__(daemon=True, name=self.source)
        self.cmd = cmd
        self.interval = interval
        self.timeout = timeout
        self._lock = threading.Lock()
        self._data = dict(EMPTY_TOK)
        self._first = threading.Event()
        self._stop = threading.Event()

    def _run_json(self, *args: str):
        # .cmd/.bat shims must go through cmd.exe on Windows.
        use_shell = os.name == "nt" and self.cmd.lower().endswith((".cmd", ".bat"))
        argv = [self.cmd, *args]
        p = subprocess.run(
            subprocess.list2cmdline(argv) if use_shell else argv,
            shell=use_shell, capture_output=True, timeout=self.timeout,
            creationflags=NO_WINDOW,
        )
        out = p.stdout.decode("utf-8", errors="replace")
        if p.returncode != 0:
            raise RuntimeError(f"{self.source} {' '.join(args)} exit {p.returncode}: "
                               f"{_decode_err(p.stderr).strip()[:200]}")
        # Tolerate log noise before the JSON body.
        starts = [i for i in (out.find("{"), out.find("[")) if i >= 0]
        return json.loads(out[min(starts):] if starts else out)

    def fetches(self):
        raise NotImplementedError

    def refresh(self) -> None:
        new = dict(EMPTY_TOK)
        for name, fn in self.fetches():
            try:
                new.update(fn())
            except Exception as e:  # noqa: BLE001 - never crash the main loop
                log.warning("%s %s failed: %s", self.source, name, e)
        new = {k: v for k, v in new.items() if not k.startswith("_")}
        with self._lock:
            self._data = new
        self._first.set()

    def run(self) -> None:
        while not self._stop.is_set():
            t0 = time.monotonic()
            self.refresh()
            self._stop.wait(max(1.0, self.interval - (time.monotonic() - t0)))

    def stop(self) -> None:
        self._stop.set()

    def wait_first(self, timeout: float) -> bool:
        return self._first.wait(timeout)

    def get(self) -> dict:
        with self._lock:
            return dict(self._data)


class CcusageSource(TokenCollector):
    """Claude Code only, via `ccusage` (5h block estimated from local logs)."""

    source = "ccusage"

    def __init__(self, cmd: str | None, interval: float, timeout: float):
        super().__init__(cmd or shutil.which("ccusage.cmd") or shutil.which("ccusage") or "ccusage",
                         interval, timeout)

    def fetches(self):
        return [("blocks", self._block), ("daily", self._today)]

    def _block(self) -> dict:
        data = self._run_json("blocks", "--active", "--json")
        blocks = [b for b in data.get("blocks", []) if b.get("isActive") and not b.get("isGap")]
        if not blocks:  # no active 5h block right now
            return {"block_tokens": "0", "block_cost": 0.0, "burn_tpm": "0", "remain_min": None}
        b = blocks[-1]
        burn = (b.get("burnRate") or {}).get("tokensPerMinuteForIndicator")
        remain = (b.get("projection") or {}).get("remainingMinutes")
        tokens = b.get("totalTokens")
        return {
            "block_tokens": compact(tokens) if isinstance(tokens, (int, float)) else None,
            "block_cost": r2(b.get("costUSD")),
            "burn_tpm": compact(burn) if isinstance(burn, (int, float)) else None,
            "remain_min": int(remain) if isinstance(remain, (int, float)) else None,
        }

    def _today(self) -> dict:
        today = datetime.now()
        data = self._run_json("daily", "--json", "--since", today.strftime("%Y%m%d"))
        rows = [r for r in data.get("daily", [])
                if (r.get("period") or r.get("date")) == today.strftime("%Y-%m-%d")]
        # Newer ccusage may emit per-agent rows plus an "all" row; prefer "all".
        row = next((r for r in rows if r.get("agent") in (None, "all")), rows[0] if rows else None)
        if row is None:
            return {"today_tokens": "0", "today_cost": 0.0}
        tokens = row.get("totalTokens")
        return {
            "today_tokens": compact(tokens) if isinstance(tokens, (int, float)) else None,
            "today_cost": r2(row.get("totalCost", row.get("costUSD"))),
        }


class TokscaleSource(TokenCollector):
    """All AI tools via `tokscale` (the engine inside Token Monitor), plus the
    official Claude subscription limits from `tokscale usage`.

    Field mapping:
      today_tokens/today_cost  models --today (input+output+cache+reasoning)
      session_pct/weekly_pct   usage: Claude "Session" / "Weekly" used_percent
      remain_min               minutes until the Claude Session window resets
      block_tokens/block_cost  hourly buckets inside the current Session window
                               (reset - 5h .. now, hour granularity)
      burn_tpm                 (input+output) per minute over that window,
                               cache excluded like ccusage's indicator
    """

    source = "tokscale"

    def __init__(self, cmd: str | None, interval: float, timeout: float,
                 clients: str | None, limits_interval: float):
        if not cmd:
            cmd = shutil.which("tokscale") or shutil.which("tokscale.cmd")
            if not cmd and TOKSCALE_BUNDLED.exists():
                cmd = str(TOKSCALE_BUNDLED)
        super().__init__(cmd or "tokscale", interval, timeout)
        self.client_args = ("-c", clients) if clients else ()
        self.limits_interval = limits_interval
        self._limits: dict | None = None
        self._limits_at = float("-inf")

    def fetches(self):
        # Limits first: the block window depends on the session reset time.
        return [("usage", self._usage), ("models", self._today), ("hourly", self._block)]

    def _usage(self) -> dict:
        now = time.monotonic()
        if self._limits is None or now - self._limits_at >= self.limits_interval:
            self._limits_at = now  # also throttles retries after a failure
            data = self._run_json("usage", "--json")  # network call -> cached longer
            claude = next((p for p in data if str(p.get("provider", "")).lower() == "claude"), None)
            if claude is None:
                raise RuntimeError("no Claude provider in `tokscale usage` (not logged in?)")
            m = {str(x.get("label", "")).lower(): x for x in claude.get("metrics", [])}
            sess, week = m.get("session", {}), m.get("weekly", {})
            self._limits = {
                "session_pct": round(sess["used_percent"]) if "used_percent" in sess else None,
                "weekly_pct": round(week["used_percent"]) if "used_percent" in week else None,
                "_session_reset": sess.get("resets_at"),
            }
        if self._limits is None:
            return {}
        lim = dict(self._limits)
        lim["remain_min"] = _minutes_until(lim["_session_reset"])
        return lim

    def _today(self) -> dict:
        data = self._run_json("models", "--today", "--json", "--no-spinner", *self.client_args)
        tokens = sum(_num(e.get(k)) for e in data.get("entries", [])
                     for k in ("input", "output", "cacheRead", "cacheWrite", "reasoning"))
        return {"today_tokens": compact(tokens), "today_cost": r2(data.get("totalCost", 0.0))}

    def _block(self) -> dict:
        remain = _minutes_until((self._limits or {}).get("_session_reset"))
        if remain is None:
            return {}
        if remain == 0:  # window already reset, nothing used in the new one yet
            return {"block_tokens": "0", "block_cost": 0.0, "burn_tpm": "0"}
        now = datetime.now()
        start = now + timedelta(minutes=remain) - timedelta(hours=SESSION_HOURS)
        data = self._run_json("hourly", "--json", "--no-spinner",
                              "--since", start.strftime("%Y-%m-%d"), *self.client_args)
        first_hour = start.strftime("%Y-%m-%d %H:00")
        rows = [e for e in data.get("entries", []) if str(e.get("hour", "")) >= first_hour]
        tokens = sum(_num(e.get(k)) for e in rows for k in ("input", "output", "cacheRead", "cacheWrite"))
        io = sum(_num(e.get(k)) for e in rows for k in ("input", "output"))
        elapsed = max(1.0, (now - start.replace(minute=0, second=0, microsecond=0)).total_seconds() / 60)
        return {
            "block_tokens": compact(tokens),
            "block_cost": r2(sum(_num(e.get("cost")) for e in rows)),
            "burn_tpm": compact(io / elapsed),
        }


# ---------------------------------------------------------------------------
# Message building
# ---------------------------------------------------------------------------
def encode(msg: dict) -> bytes:
    line = json.dumps(msg, ensure_ascii=False, separators=(",", ":"))
    data = (line + "\n").encode("utf-8")
    if len(data) > MAX_LINE:
        raise ValueError(f"message too long ({len(data)} > {MAX_LINE} bytes)")
    return data


def build_stats(sysc: SystemCollector, tokc: TokenCollector) -> dict:
    msg = {"v": 1, "type": "stats"}
    msg.update(sysc.collect())
    msg["tok"] = tokc.get()
    return msg


# ---------------------------------------------------------------------------
# Dashboard v2 (Token Monitor Hub file + mouse battery)
# ---------------------------------------------------------------------------
V2_EVAL_INTERVAL = {"dash": 0.0, "mouse": 0.0, "quota": 60.0, "models": 60.0, "heat": 60.0}
V2_KEEPALIVE = 30.0   # ping when nothing else was sent for this long (< 120 s firmware offline timeout)
V2_ACK_WAIT = 2.0     # after each line, wait this long for OK/ERR before the next one
MAX_INTERVAL = 60.0   # push interval cap: keeps the 30 s keepalive well inside the board's 120 s offline timeout
SILENT_AFTER = 90.0   # connected but no READY/OK/ERR for this long = the board is not answering (UI: "无响应")
LATE_REPLY_WAIT = 2.0  # after a command timed out, wait this long for its late reply before resending


def clamp_interval(x) -> float:
    """Push interval in seconds: at least 1, at most MAX_INTERVAL (a longer one would let the board time out)."""
    return min(MAX_INTERVAL, max(1.0, float(x)))


def loop_wait(connected: bool, interval: float, retry: float, elapsed: float = 0.0) -> float:
    """How long a push loop sleeps after an iteration that took `elapsed`: the push interval while connected, but
    no longer than the reconnect interval while not (otherwise a lost link would be retried every `interval`)."""
    return max(0.0, (interval if connected else min(interval, retry)) - elapsed)


_coerced_logged: set = set()


def _warn_coerced(key: str, value, using) -> None:
    """A bad v2 option value was replaced by the default: say so once per distinct (key, value)."""
    mark = (key, repr(value))
    if mark not in _coerced_logged:
        _coerced_logged.add(mark)
        log.warning("config v2.%s=%r is not valid, using %r", key, value, using)


def v2_options(cfg: dict) -> dict:
    given = cfg.get("v2") or {}
    opts = {**DEFAULT_V2, **{k: v for k, v in given.items() if not k.startswith("_")}}
    for key, valid in (("primary", _valid_primary), ("heat_rows", _valid_rows),
                       ("heat_weeks", _valid_weeks), ("refresh_min", _valid_refresh)):
        good = valid(opts[key])
        if good is None:
            good = DEFAULT_V2[key]
            _warn_coerced(key, opts[key], good)
        opts[key] = good
    if opts["calendar"] is not True and opts["calendar"] not in (False, None):
        _warn_coerced("calendar", opts["calendar"], False)
    opts["calendar"] = opts["calendar"] is True
    return opts


def _valid_primary(x) -> str | None:
    x = str(x).strip().lower()
    return x if x in dashboard_v2.PRIMARIES else None


def _valid_weeks(x) -> int | None:
    lo, hi = dashboard_v2.HEAT_WEEKS
    return x if isinstance(x, int) and not isinstance(x, bool) and lo <= x <= hi else None


def _valid_refresh(x) -> int | None:
    return x if isinstance(x, int) and not isinstance(x, bool) and x in dashboard_v2.REFRESH_MINUTES else None


def _valid_rows(x) -> int | None:
    return x if isinstance(x, int) and not isinstance(x, bool) and x in dashboard_v2.HEAT_ROWS else None


def load_mouse_source():
    """A started mouse_rapoo.RapooMouse (snapshot() -> MouseReading) or None. Any import or start
    failure just means the mouse message reports st:"na"; the host keeps running."""
    try:
        import mouse_rapoo
        src = mouse_rapoo.RapooMouse()
        src.start()
        return src
    except Exception as e:  # noqa: BLE001 - optional component, never break the host
        log.info("mouse module unavailable (mouse will report na): %s", e)
        return None


class V2Pusher:
    """Decides which v2 messages are due. Cadence: dash+mouse every tick, quota/models/heat
    every 60 s; a message identical to the last one sent is skipped (keepalive ping instead)."""

    def __init__(self, tm: tm_source.TmSource, mouse=None, mouse_factory=None,
                 primary="today", heat_rows=7, heat_weeks=10, calendar=False, refresh_min=3,
                 options_loader=None, reload_every=10.0):
        self.tm = tm
        self.primary = primary
        self.heat_rows = heat_rows
        self.heat_weeks = heat_weeks
        self.calendar = calendar is True
        self.refresh_min = _valid_refresh(refresh_min) or DEFAULT_V2["refresh_min"]
        self._loader = options_loader     # () -> (primary|None, heat_rows, heat_weeks[, calendar[, refresh_min]]); raises on read failure
        self._reload_every = reload_every
        self._next_reload = float("-inf")
        self._reload_err = ""            # last reload error text, so a broken config is logged once per distinct error
        self.mouse = mouse           # object with snapshot(), or None
        self._mouse_factory = mouse_factory  # called once, on first use (so v1 runs never touch HID)
        self.reset()

    def close(self) -> None:
        stop = getattr(self.mouse, "stop", None)
        if callable(stop):
            stop()

    def reset(self) -> None:
        """New connection: forget what the firmware has been sent."""
        self._last: dict[str, dict] = {}
        self._next_eval: dict[str, float] = {}
        self._last_send = float("-inf")

    def set_options(self, primary=None, heat_rows=None, heat_weeks=None, calendar=None, refresh_min=None) -> None:
        """Change primary / heat_rows / heat_weeks / calendar / refresh_min live; invalid values are ignored.
        Affected blocks are re-sent."""
        primary = _valid_primary(primary) if primary is not None else None
        heat_rows, heat_weeks = _valid_rows(heat_rows), _valid_weeks(heat_weeks)
        if primary and primary != self.primary:
            self.primary = primary
            for kind in ("dash", "models"):
                self._next_eval.pop(kind, None)
        if heat_rows and heat_rows != self.heat_rows:
            self.heat_rows = heat_rows
            self._next_eval.pop("heat", None)
        if heat_weeks and heat_weeks != self.heat_weeks:
            self.heat_weeks = heat_weeks
            self._next_eval.pop("heat", None)
        if isinstance(calendar, bool) and calendar != self.calendar:
            self.calendar = calendar
            self._next_eval.pop("heat", None)
        refresh_min = _valid_refresh(refresh_min)
        if refresh_min and refresh_min != self.refresh_min:
            self.refresh_min = refresh_min
            self._next_eval.pop("dash", None)

    def _reload_options(self, mono: float) -> None:
        if self._loader is None or mono < self._next_reload:
            return
        self._next_reload = mono + self._reload_every
        try:
            primary, rows, weeks, *rest = self._loader()
        except Exception as e:  # noqa: BLE001 - config being edited / unreadable: keep the old values
            err = f"{type(e).__name__}: {e}"
            if err != self._reload_err:
                self._reload_err = err
                log.warning("config reload failed, keeping primary=%s heat_rows=%s heat_weeks=%s: %s",
                            self.primary, self.heat_rows, self.heat_weeks, err)
            return
        if self._reload_err:
            self._reload_err = ""
            log.info("config reload works again")
        self.set_options(primary, rows, weeks, *rest[:2])

    def mouse_reading(self):
        if self.mouse is None and self._mouse_factory is not None:
            factory, self._mouse_factory = self._mouse_factory, None
            self.mouse = factory()
        if self.mouse is None:
            return None
        try:
            return self.mouse.snapshot()
        except Exception as e:  # noqa: BLE001
            log.warning("mouse snapshot failed: %s", e)
            return None

    def due(self, mono: float, now: datetime) -> list[tuple[str, dict]]:
        self._reload_options(mono)
        snap = self.tm.snapshot(now)
        reading = None
        out = []
        for kind in dashboard_v2.KINDS:
            if mono < self._next_eval.get(kind, float("-inf")):
                continue
            if kind == "mouse":
                reading = self.mouse_reading()
            try:
                msg = dashboard_v2.build(kind, snap, reading, now, self.primary, self.heat_rows, self.heat_weeks,
                                         self.calendar, self.refresh_min)
            except ValueError as e:
                log.error("v2 %s message skipped: %s", kind, e)
                self._next_eval[kind] = mono + max(V2_EVAL_INTERVAL[kind], 10.0)
                continue
            if msg == self._last.get(kind):
                self._next_eval[kind] = mono + V2_EVAL_INTERVAL[kind]
            else:
                out.append((kind, msg))
        return out

    def mark_sent(self, kind: str, msg: dict, mono: float) -> None:
        self._last[kind] = msg
        self._next_eval[kind] = mono + V2_EVAL_INTERVAL[kind]
        self._last_send = mono

    def mark_rejected(self, kind: str, mono: float) -> None:
        """The board answered this block with ERR: forget it was sent so the next round sends it again."""
        self._last.pop(kind, None)
        self._next_eval.pop(kind, None)
        self._last_send = mono

    def mark_ping(self, mono: float) -> None:
        self._last_send = mono

    def keepalive_due(self, mono: float) -> bool:
        return mono - self._last_send >= V2_KEEPALIVE


def push_v2(link: "Link", pusher: V2Pusher, mono: float, now: datetime) -> int:
    """Send whatever is due, one line at a time (wait for the board's reply in between so a
    busy board is not flooded); an unanswered line ends this round, the rest retries next tick."""
    sent = 0
    for kind, msg in pusher.due(mono, now):
        before, errs = link.ack_count, link.err_count
        if not link.send(msg):
            break
        got = link.wait_ack(before, V2_ACK_WAIT)
        if got and link.err_count > errs:      # ERR: the block did not arrive, keep it due
            log.warning("board rejected v2 %s block (%s); it will be sent again", kind, link.last_err or "ERR")
            pusher.mark_rejected(kind, mono)
            continue
        pusher.mark_sent(kind, msg, mono)
        sent += 1
        if not got:
            break
    if not sent and pusher.keepalive_due(mono) and link.send({"v": 2, "type": "ping"}):
        pusher.mark_ping(mono)
    return sent


def cmd_dry_run_v2(cfg: dict, primary: str | None = None) -> int:
    """Print the five v2 messages built from the real Token Monitor file (no serial, and the
    mouse receiver is not opened: the mouse message is built from "no reading", st:"na")."""
    v2 = v2_options(cfg)
    source = tm_source.TmSource(v2["tm_file"])
    now = datetime.now().astimezone()
    snap = source.snapshot(now)
    if snap is None:
        log.error("cannot read Token Monitor file %s: %s", source.path, source.last_error)
    primary = _valid_primary(primary) if primary else v2["primary"]
    for kind, msg in dashboard_v2.build_all(snap, None, now, primary, v2["heat_rows"], v2["heat_weeks"],
                                            v2["calendar"], v2["refresh_min"]).items():
        data = encode(msg)
        out = getattr(sys.stdout, "buffer", None)  # raw UTF-8 regardless of the console code page
        if out is not None:
            out.write(data)
            out.flush()
        else:
            print(data.decode("utf-8"), end="", flush=True)
        log.info("v2 %-6s %4d bytes", kind, len(data))
    return 0 if snap is not None else 1


# ---------------------------------------------------------------------------
# Serial
# ---------------------------------------------------------------------------
def parse_ready_proto(line: str) -> str | None:
    """'READY epaper-sidescreen v2 GDEY042T81 400x300' -> 'v2' (v1 / unknown -> 'v1' / None)."""
    parts = line.split()
    if len(parts) < 3 or not parts[2].startswith("v") or not parts[2][1:].isdigit():
        return None
    return "v2" if int(parts[2][1:]) >= 2 else "v1"


def diag_kind(line: str) -> str:
    """Firmware DIAG line -> "fault" (panel stopped until reboot) | "refresh" (a refresh finished) | "warn" | "info"."""
    if line.startswith(("DIAG epd fault", "DIAG display stopped")):
        return "fault"
    if line.startswith("DIAG epd refresh done"):
        return "refresh"
    if line.startswith("DIAG epd warn"):
        return "warn"
    return "info"


def parse_fw_version(line: str) -> str | None:
    """'DIAG fw version=1.3.1 test_pattern=0' -> '1.3.1'; any other line -> None."""
    parts = line.split()
    if parts[:2] != ["DIAG", "fw"]:
        return None
    for part in parts[2:]:
        if part.startswith("version=") and len(part) > 8:
            return part[8:]
    return None


def _read_available(s) -> bytes:
    """TCP read: wait (up to the port timeout) for a first byte, then take everything already received in one go.
    (pyserial's socket in_waiting is 0/1, so read(in_waiting) would pull one byte per call.)"""
    first = s.read(1)
    sock = getattr(s, "_socket", None)
    if not first or sock is None:
        return first
    if select.select([sock], [], [], 0)[0]:     # an OSError here ends the reader like any read error
        return first + sock.recv(4096)
    return first


def describe_port(p) -> str:
    vid = f"{p.vid:04X}" if p.vid is not None else "----"
    pid = f"{p.pid:04X}" if p.pid is not None else "----"
    return f"{p.device:8} VID:PID={vid}:{pid}  {p.description}"


def find_port(ports=None) -> str | None:
    if ports is None and list_ports is None:
        return None
    candidates = []
    for p in list_ports.comports() if ports is None else ports:
        desc = f"{p.description} {p.manufacturer or ''}".upper()
        if p.vid in VID_PRIORITY or any(k in desc for k in ("CH340", "CH343", "CH910", "CP210", "ESP32")):
            candidates.append(p.device)
    # Never probe multiple devices or silently choose among ambiguous candidates.
    return candidates[0] if len(candidates) == 1 else None


TCP_SCHEMES = ("tcp://", "socket://")


def is_tcp_port(port) -> bool:
    """True for 'tcp://HOST[:PORT]' / 'socket://HOST[:PORT]' (board over WiFi); COM3 etc. -> False."""
    return isinstance(port, str) and port.strip().lower().startswith(TCP_SCHEMES)


def parse_tcp_port(port: str) -> tuple[str, int]:
    """'tcp://192.168.1.50' -> ('192.168.1.50', 7330). Raises ValueError when malformed."""
    from urllib.parse import urlsplit
    parts = urlsplit(port.strip())
    try:
        tcp_port = parts.port
    except ValueError as e:
        raise ValueError(f"bad port in {port!r}") from e
    if not parts.hostname:
        raise ValueError(f"no host in {port!r}")
    if tcp_port is None:
        tcp_port = discovery.DEFAULT_TCP_PORT
    if not 0 < tcp_port < 65536:
        raise ValueError(f"port out of range in {port!r}")
    return parts.hostname, tcp_port


OTA_PARTITION_SIZE = 0x140000   # bytes; the board answers ERR ota_too_big above this
ESP32_IMAGE_MAGIC = 0xE9         # first byte of every ESP32 app image (firmware.bin)
OTA_CHUNK = 4096


def check_firmware_image(image: bytes) -> str:
    """"" when `image` looks like an ESP32 app image that fits the OTA slot, else "bad_image" / "too_big"."""
    if not image or image[0] != ESP32_IMAGE_MAGIC:
        return "bad_image"
    if len(image) > OTA_PARTITION_SIZE:
        return "too_big"
    return ""


def read_firmware(path) -> tuple[bytes | None, str]:
    """Read and check a firmware file: (image, "") or (None, "unreadable" | "bad_image" | "too_big")."""
    try:
        with open(path, "rb") as f:
            image = f.read(OTA_PARTITION_SIZE + 1)   # never load a huge wrong file whole
    except OSError as e:
        log.warning("cannot read firmware file %s: %s", path, e)
        return None, "unreadable"
    err = check_firmware_image(image)
    return (None, err) if err else (image, "")


@dataclass
class CommandResult:
    """Outcome of Link.command(): the board's OK/ERR plus any WIFI lines seen while waiting."""
    ok: bool = False
    err: str = ""                       # "" when ok; else the ERR reason, "timeout" or "disconnected"
    wifi_scan: list | None = None       # nets from a `WIFI scan` line printed before the OK
    wifi_status: dict | None = None     # last `WIFI status` line seen during the wait
    pair: dict | None = None            # `WIFI pair` line ({"token","id"}) seen during the wait; never log it

    def as_dict(self) -> dict:
        return {"ok": self.ok, "err": self.err, "scan": self.wifi_scan, "status": self.wifi_status,
                "pair": self.pair}


class Link:
    """Main thread owns open/write/close; reader only reports state/events.

    The port is a serial device ("COM5") or a WiFi board ("tcp://192.168.1.50[:7330]"); above
    connect() the two behave the same (READY / OK / ERR lines, one JSON line per message)."""

    def __init__(self, port: str | None, baud: int, handshake_timeout=120,
                 response_timeout=180):
        self.fixed_port = port
        self.transport = "tcp" if is_tcp_port(port) else "serial"   # of the current / next connection
        self.wifi_status: dict | None = None   # last `WIFI {"type":"status",...}` line (USB serial only)
        self.wifi_scan: list | None = None     # nets of the last `WIFI {"type":"scan",...}` line
        self.on_wifi = None                    # optional callback(kind, data) from the reader thread
        self.on_diag = None                    # optional callback(kind, line) from the reader thread, see diag_kind()
        self.display_fault: str | None = None  # firmware's display-fault DIAG line on this connection
        self.pair_info: dict | None = None     # last `WIFI {"type":"pair",...}` line: token + board id
        self.wx_status: dict | None = None     # last `WX {"type":"status",...}` line (offline weather page, see weather.py)
        self.fw_version: str | None = None     # from `DIAG fw version=...` on this connection (TCP, 1.3.1+)
        self._wifi_status_seq = 0
        self._wifi_scan_seq = 0
        self._pair_seq = 0
        self.baud = baud
        self.handshake_timeout = handshake_timeout
        self.response_timeout = response_timeout
        self.ser = None
        self._reader = None
        self._stop_rx = threading.Event()
        self._failed = threading.Event()
        self._state = threading.Condition()  # also wakes wait_* when the reader sees a reply
        self.opened_at = 0.0
        self.last_response = None
        self.ok_count = 0
        self.err_count = 0
        self.sent_count = 0       # lines written on this connection; each gets exactly one OK/ERR
        self._next_warning = 0.0
        self.ready_proto = None   # "v1"/"v2" from the firmware's READY line, if seen
        self.ready_count = 0      # READY lines on this connection; a second one = the board rebooted
        self.proto = None         # negotiated: "v1"/"v2"; None = not decided yet
        self.last_err = ""
        self._next_probe = 0.0

    @property
    def connected(self) -> bool:
        # Transport only, NOT proof of firmware response or displayed pixels.
        return self.ser is not None and self.ser.is_open and not self._failed.is_set()

    def connect(self) -> bool:
        self.close()
        if self._reader is not None:
            log.error("previous %s reader has not stopped; refusing another reader", self.transport)
            return False
        tcp = is_tcp_port(self.fixed_port)
        self.transport = "tcp" if tcp else "serial"
        if tcp:
            try:
                addr_host, addr_port = parse_tcp_port(self.fixed_port)
            except ValueError as e:
                log.warning("bad tcp address: %s", e)
                return False
            port = f"{addr_host}:{addr_port}"
        else:
            port = self.fixed_port or find_port()
            if not port:
                log.warning("no unique serial candidate; use --list-ports and --port (USB hints are not board identification)")
                return False
        s = None
        try:
            if tcp:
                url_host = f"[{addr_host}]" if ":" in addr_host else addr_host
                s = serial.serial_for_url(f"socket://{url_host}:{addr_port}", timeout=0.5, write_timeout=2,
                                          do_not_open=True)
                # pyserial's socket open() ends with reset_input_buffer(), which throws away READY / DIAG fw
                # if the board was quick to send them; keep them.
                s.reset_input_buffer = lambda: None
                s.open()
                del s.reset_input_buffer
            else:
                s = serial.Serial()
                s.port, s.baudrate, s.timeout, s.write_timeout = port, self.baud, 0.5, 2
                s.dtr = False
                s.rts = False
                s.open()
        except (serial.SerialException, OSError) as e:
            if s is not None:
                try:
                    s.close()
                except (serial.SerialException, OSError):
                    pass
            if tcp:
                log.warning("connect tcp %s failed (board offline, wrong address, or another client connected): %s", port, e)
            else:
                log.warning("open %s failed (missing/busy port or permissions): %s", port, e)
            return False
        # Keep explicit port selection fixed; auto mode re-enumerates on reconnect.
        self.ser = s
        self._stop_rx.clear()
        self._failed.clear()
        with self._state:
            self.opened_at = time.monotonic()
            self.last_response = None
            self.ok_count = self.err_count = self.sent_count = 0
            self._next_warning = self.opened_at + self.handshake_timeout
            self.ready_proto = self.proto = None
            self.ready_count = 0
            self.last_err = ""
            self._next_probe = 0.0
            self.wifi_status = self.wifi_scan = None   # per connection: ask the board again
            self.wx_status = None
            self.display_fault = None                  # a faulted board repeats it on every TCP connect
            self.fw_version = None                     # likewise sent after READY on every TCP connect
        if tcp:
            log.info("tcp connection opened: %s; protocol response pending; display unverified", port)
        else:
            log.info("serial port opened: %s @ %d; protocol response pending; display unverified", port, self.baud)
        self._reader = threading.Thread(target=self._read_loop, args=(s,), daemon=True, name=f"{self.transport}-rx")
        self._reader.start()
        return self.send({"v": 1, "type": "ping"})

    def _line(self, line: str) -> None:
        kind = line.split(maxsplit=1)[0] if line else ""
        if kind in ("READY", "OK", "ERR"):
            with self._state:
                first = self.last_response is None
                self.last_response = time.monotonic()
                self._next_warning = self.last_response + self.response_timeout
                self.ok_count += kind == "OK"
                self.err_count += kind == "ERR"
                if kind == "ERR":
                    self.last_err = line
                elif kind == "READY":
                    self.ready_proto = parse_ready_proto(line)
                    if self.ready_count:        # READY again on the same connection: the board rebooted
                        self.display_fault = None
                        self.wx_status = None
                        self.proto = self.ready_proto
                    self.ready_count += 1
                self._state.notify_all()
            if first:
                log.info("protocol response received; display remains unverified")
            if kind == "ERR":
                log.warning("board protocol error: %s", line)
            elif kind == "OK":
                log.info("board: %s (message received only; not display completion)", line)
            else:
                log.info("board: %s", line)
        elif kind == "DISPLAY":
            log.info("firmware display diagnostic (reported, not visually verified): %s", line)
        elif kind == "WIFI":
            self._wifi_line(line[4:].strip())
        elif kind == "WX":
            self._wx_line(line[2:].strip())
        elif kind == "DIAG":
            self._diag_line(line)
        elif line:
            log.warning("unrecognized serial line (check port/baud/firmware): %.160s", line)

    def _diag_line(self, line: str) -> None:
        kind = diag_kind(line)
        version = parse_fw_version(line)
        if version:
            with self._state:
                self.fw_version = version
        if kind == "fault":
            with self._state:
                self.display_fault = line
            log.error("board display fault, panel no longer refreshes (press EN/reset): %s", line)
        elif kind == "warn":
            log.warning("board: %s", line)
        else:
            log.info("board: %s", line)
        callback = self.on_diag
        if callback is not None:
            try:
                callback(kind, line)
            except Exception as e:  # noqa: BLE001 - a listener must not kill the reader
                log.warning("diag listener failed: %s", e)

    def _wifi_line(self, payload: str) -> None:
        """`WIFI {json}`: type "status" -> wifi_status, "scan" -> wifi_scan, "pair" -> pair_info. Never raises."""
        try:
            data = json.loads(payload)
            if not isinstance(data, dict):
                raise ValueError("not an object")
            kind = data.get("type")
        except ValueError as e:
            # a garbled pair line still carries the pairing code: don't echo it
            log.warning("bad WIFI line from board (%s): %.80s", e, "(pair line hidden)" if "token" in payload else payload)
            return
        if kind == "pair":
            token, board_id = data.get("token"), data.get("id")
            if not isinstance(token, str) or not token:
                log.warning("bad WIFI pair line from board (no token)")
                return
            data = {"type": "pair", "token": token, "id": board_id if isinstance(board_id, str) else ""}
            with self._state:
                self.pair_info = data
                self._pair_seq += 1
                self._state.notify_all()
            log.info("board pairing code received (id %s)", data["id"] or "?")
            self._notify_wifi(kind, data)
            return
        if kind == "status":
            with self._state:
                self.wifi_status = data
                self._wifi_status_seq += 1
                self._state.notify_all()
            parts = [str(data.get("st", "?")), *(str(data[k]) for k in ("ssid", "ip") if data.get(k))]
            log.info("board wifi: %s", " ".join(parts))
        elif kind == "scan":
            nets = data.get("nets")
            nets = [n for n in nets if isinstance(n, dict)] if isinstance(nets, list) else []
            with self._state:
                self.wifi_scan = nets
                self._wifi_scan_seq += 1
                self._state.notify_all()
            log.info("board wifi: scan found %d networks", len(nets))
            data = {**data, "nets": nets}
        else:
            log.warning("unknown WIFI line type from board: %.80s", payload)
            return
        self._notify_wifi(kind, data)

    def _wx_line(self, payload: str) -> None:
        """`WX {json}` (reply to wx_status). Never raises."""
        data = weather.parse_wx_status(payload)
        if data is None:
            log.warning("bad WX line from board: %.80s", payload)
            return
        with self._state:
            self.wx_status = data

    def _notify_wifi(self, kind: str, data: dict) -> None:
        callback = self.on_wifi
        if callback is not None:
            try:
                callback(kind, data)
            except Exception as e:  # noqa: BLE001 - a listener must not kill the reader
                log.warning("wifi listener failed: %s", e)

    def _read_loop(self, s) -> None:
        buf = bytearray()
        dropping = False
        # A socket read(256) keeps waiting until 256 bytes or the timeout; read what is there instead.
        tcp = self.transport == "tcp"
        try:
            while not self._stop_rx.is_set() and s.is_open:
                for byte in (_read_available(s) if tcp else s.read(256)):
                    if byte == 10:
                        if not dropping:
                            self._line(buf.decode("utf-8", "replace").strip())
                        buf.clear()
                        dropping = False
                    elif not dropping:
                        buf.append(byte)
                        if len(buf) > MAX_LINE:
                            buf.clear()
                            dropping = True
                            log.warning("overlong %s line discarded; check baud/port/firmware", self.transport)
        except (serial.SerialException, OSError, TypeError, AttributeError) as e:
            if not self._stop_rx.is_set():
                log.warning("%s read failed, reconnect scheduled: %s", self.transport, e)
        finally:
            if not self._stop_rx.is_set():
                self._failed.set()
                with self._state:
                    self._state.notify_all()   # wake command()/wait_* so they notice the disconnect

    def diagnose(self, now=None) -> None:
        if not self.connected:
            return
        now = time.monotonic() if now is None else now
        with self._state:
            if now < self._next_warning:
                return
            initial = self.last_response is None
            self._next_warning = now + self.response_timeout
        log.warning("%s; check selected port, baud, firmware or long refresh. "
                    "Continuing stats without reset/reopen; display unverified",
                    "handshake deadline: no READY/OK/ERR" if initial else "protocol response timeout")

    @property
    def has_response(self) -> bool:
        return self.last_response is not None

    @property
    def ack_count(self) -> int:
        with self._state:
            return self.ok_count + self.err_count

    def reply_age(self, now: float | None = None) -> float | None:
        """Seconds since the board last said READY / OK / ERR on this connection (since the connection opened when it
        never has); None when not connected. The UI shows "无响应" past SILENT_AFTER."""
        if not self.connected:
            return None
        with self._state:
            ref = self.last_response if self.last_response is not None else self.opened_at
        return max(0.0, (time.monotonic() if now is None else now) - ref)

    @property
    def silent(self) -> bool:
        age = self.reply_age()
        return age is not None and age > SILENT_AFTER

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Wait (default V2_ACK_WAIT) until every line sent so far was answered, or the link failed."""
        if timeout is None:
            timeout = V2_ACK_WAIT
        with self._state:
            return self._state.wait_for(
                lambda: self.ok_count + self.err_count >= self.sent_count or self._failed.is_set(), timeout)

    def wait_handshake(self, timeout: float) -> bool:
        """Wait (without sleeping in a loop) for the first READY/OK/ERR after open."""
        with self._state:
            return self._state.wait_for(lambda: self.last_response is not None, timeout)

    def wait_ack(self, before: int, timeout: float) -> bool:
        """Wait until more replies than `before` (ack_count) have arrived."""
        with self._state:
            return self._state.wait_for(lambda: self.ok_count + self.err_count > before, timeout)

    def _mark(self) -> tuple:
        with self._state:
            return (self.ok_count + self.err_count, self.ok_count,
                    self._wifi_scan_seq, self._wifi_status_seq, self._pair_seq)

    def _collect(self, mark: tuple, timeout: float) -> CommandResult:
        """Wait for the reply that follows `mark` (a _mark() taken before the send) and turn it into a result."""
        ack0, ok0, scan0, status0, pair0 = mark
        with self._state:
            self._state.wait_for(
                lambda: self.ok_count + self.err_count > ack0 or self._failed.is_set(), timeout)
            answered = self.ok_count + self.err_count > ack0
            if answered:
                ok = self.ok_count > ok0
                err = "" if ok else (self.last_err[3:].strip() or "error")
            else:
                ok, err = False, "disconnected" if self._failed.is_set() else "timeout"
            scan = list(self.wifi_scan) if self._wifi_scan_seq != scan0 and self.wifi_scan is not None else None
            status = dict(self.wifi_status) if self._wifi_status_seq != status0 and self.wifi_status is not None else None
            pair = dict(self.pair_info) if self._pair_seq != pair0 and self.pair_info is not None else None
        return CommandResult(ok, err, scan, status, pair)

    def command(self, msg: dict, timeout: float = 5.0, retry_on_timeout: bool = False) -> CommandResult:
        """Send one command and wait for its OK / ERR (count-based, like wait_ack). Call it only from
        the thread that owns the link (the worker / main loop) so no other send interleaves. The
        message is never logged: wifi_set carries the WiFi password.

        Earlier lines are given up to V2_ACK_WAIT to be answered first, so a late reply to one of them is not
        mistaken for this command's. retry_on_timeout: when no reply comes, wait LATE_REPLY_WAIT for a late one,
        then send the line once more (only for idempotent messages, e.g. image chunks)."""
        self.wait_idle()
        mark = self._mark()
        if not self.send(msg):
            return CommandResult(False, "disconnected")
        res = self._collect(mark, timeout)
        if retry_on_timeout and res.err == "timeout" and self.connected:
            res = self._collect(mark, LATE_REPLY_WAIT)
            if res.err == "timeout" and self.connected:
                log.info("no reply to %s in %.1f s, sending it once more", msg.get("type", "?"), timeout)
                self.wait_idle(0.0)
                mark = self._mark()
                if not self.send(msg):
                    return CommandResult(False, "disconnected")
                res = self._collect(mark, timeout)
        return res

    def ota(self, image: bytes, token: str, progress=None, begin_timeout=20.0, end_timeout=60.0) -> CommandResult:
        """Upgrade the firmware over WiFi (docs/dashboard-v2-protocol.md, "WiFi 升级固件"): ota_begin, then the
        raw image bytes on the same connection, then the board's final OK / ERR. Same threading rule as
        command(). `progress(sent, total)` is called after each chunk. The token is never logged.

        Local errors (nothing sent): tcp_only, bad_image, too_big, no_token. A begin or final reply that
        never comes, or an ERR before the whole image went out, marks the connection failed (reconnect)."""
        if self.transport != "tcp":
            return CommandResult(False, "tcp_only")
        bad = check_firmware_image(image)
        if bad:
            return CommandResult(False, bad)
        if not token:
            return CommandResult(False, "no_token")
        total, md5 = len(image), hashlib.md5(image).hexdigest()
        # begin_timeout covers the board erasing the OTA slot before it says OK
        res = self.command({"v": 2, "type": "ota_begin", "size": total, "md5": md5, "token": token}, begin_timeout)
        if not res.ok:
            if res.err == "timeout":
                self._failed.set()
            return res
        log.info("firmware upload started: %d bytes, md5 %s", total, md5)
        with self._state:
            ack0, ok0 = self.ok_count + self.err_count, self.ok_count
        s, sent = self.ser, 0
        final_ack, final_ok = ack0, ok0      # reply counts just before the last chunk: only a later reply is the final one
        old_timeout = getattr(s, "write_timeout", None)
        try:
            if old_timeout is not None:
                s.write_timeout = 10      # the board may pause reading while it writes flash
            while sent < total:
                if not self.connected:
                    break
                if self.ack_count > ack0:  # the board gave up early (ERR ota_timeout etc.)
                    break
                chunk = image[sent:sent + OTA_CHUNK]
                if sent + len(chunk) >= total:
                    with self._state:
                        final_ack, final_ok = self.ok_count + self.err_count, self.ok_count
                if s.write(chunk) != len(chunk):
                    raise OSError("short write")
                sent += len(chunk)
                if progress is not None:
                    progress(sent, total)
        except (serial.SerialException, OSError) as e:
            log.warning("firmware upload interrupted after %d of %d bytes: %s", sent, total, e)
            self._failed.set()
        finally:
            if old_timeout is not None:
                with contextlib.suppress(Exception):
                    s.write_timeout = old_timeout
        base_ack, base_ok = (final_ack, final_ok) if sent >= total else (ack0, ok0)
        with self._state:
            self._state.wait_for(
                lambda: self.ok_count + self.err_count > base_ack or self._failed.is_set(), end_timeout)
            if self.ok_count + self.err_count > base_ack:
                ok = self.ok_count > base_ok
                err = "" if ok else (self.last_err[3:].strip() or "error")
            else:
                ok, err = False, "disconnected" if self._failed.is_set() else "timeout"
        if ok and sent < total:     # an OK before the whole image went out is not the final reply
            ok, err = False, "ota_incomplete"
        if ok:
            log.info("firmware upload done; board is rebooting")
        else:
            log.warning("firmware upload failed: %s (board keeps its old firmware)", err)
            # Only ota_md5 / ota_end come after the board took the whole image. Anything else (timeout,
            # ota_timeout, ota_write, ...) may leave image bytes in flight - even when we already handed them
            # all to the socket - that the board would read as lines: reconnect.
            if sent < total or err not in ("ota_md5", "ota_end"):
                self._failed.set()
        return CommandResult(ok, err)

    def negotiate(self, probe_timeout=3.0, retry=15.0) -> str | None:
        """Decide v1/v2 for this connection. None = undecided (no reply yet, or the v2 probe went
        unanswered; it is retried every `retry` seconds)."""
        if self.proto or not self.connected:
            return self.proto
        with self._state:
            ready, responded = self.ready_proto, self.last_response is not None
        if ready:
            self.proto = ready
        elif responded and time.monotonic() >= self._next_probe:
            self.proto = self._probe_v2(probe_timeout)
            if self.proto is None:
                self._next_probe = time.monotonic() + retry
        if self.proto:
            log.info("firmware protocol: %s", self.proto)
        return self.proto

    def _probe_v2(self, timeout) -> str | None:
        """Send a v2 ping: OK -> v2, ERR bad_version -> v1, anything else -> undecided."""
        with self._state:
            ok0, err0 = self.ok_count, self.err_count
        if not self.send({"v": 2, "type": "ping"}):
            return None
        with self._state:
            self._state.wait_for(lambda: self.ok_count > ok0 or self.err_count > err0, timeout)
            if self.ok_count > ok0:
                return "v2"
            if self.err_count > err0 and "bad_version" in self.last_err:
                return "v1"
        return None

    def wait_response(self, timeout, *, after_ok=None) -> bool:
        deadline = time.monotonic() + timeout
        while self.connected:
            with self._state:
                if self.err_count:
                    return False
                if (self.last_response is not None if after_ok is None else self.ok_count > after_ok):
                    return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.05, remaining))
        self.diagnose()
        return False

    def send(self, msg: dict) -> bool:
        data = encode(msg)
        if not self.connected:
            return False
        try:
            # write_timeout bounds this operation; avoid an unbounded flush().
            if self.ser.write(data) != len(data):
                raise OSError("short serial write")
            with self._state:
                self.sent_count += 1
            return True
        except (serial.SerialException, OSError) as e:
            log.warning("%s write failed, reconnect scheduled: %s", self.transport, e)
            self._failed.set()
            return False

    def close(self) -> None:
        self._stop_rx.set()
        if self.ser is not None:
            try:
                if hasattr(self.ser, "cancel_read"):
                    self.ser.cancel_read()
            except (OSError, serial.SerialException):
                pass
            try:
                self.ser.close()
            except (OSError, serial.SerialException):
                pass
        self.ser = None
        if self._reader is not None:
            self._reader.join(timeout=3)
            if not self._reader.is_alive():
                self._reader = None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def default_config_path() -> Path:
    # PyInstaller --onefile unpacks to a temp dir; look next to the exe instead.
    base = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve()
    return base.with_name("config.json")


def _read_text(path: Path) -> str:
    """Config file text. utf-8-sig tolerates a BOM (Notepad's "UTF-8" adds one). On Windows a read that overlaps
    another writer's os.replace fails with PermissionError for a moment: retry a few times."""
    for attempt in range(REPLACE_RETRIES):
        try:
            with open(path, encoding="utf-8-sig") as f:
                return f.read()
        except PermissionError:
            if attempt == REPLACE_RETRIES - 1:
                raise
            time.sleep(0.05 * (attempt + 1))
    raise AssertionError("unreachable")


def load_config(path: Path | None) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if path is None:
        path = default_config_path()
        if not path.exists():
            return cfg
    data = json.loads(_read_text(path))
    cfg.update({k: v for k, v in data.items() if not k.startswith("_")})
    log.debug("loaded config %s", path)
    return cfg


REPLACE_RETRIES = 3     # attempts for os.replace / reading a config file that another process has locked for a moment
SECRET_KEYS = ("pass", "password", "wifi_pass", "wifi_password")   # WiFi passwords never go into the config file


def save_config(path: Path, updates: dict | None = None, *, v2: dict | None = None) -> None:
    """Update config.json: `updates` sets top-level keys, `v2` keys under the `v2` object; everything else
    is kept as is. A missing file is created; an unreadable one (broken JSON) raises ValueError instead of
    being overwritten. Written to a temp file and swapped in, so a concurrent reader never sees half a file."""
    path = Path(path)
    updates = dict(updates or {})
    if any(k in SECRET_KEYS for k in (*updates, *(v2 or {}))):
        raise ValueError("passwords must not be saved in the config file")
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(_read_text(path))
        except (OSError, ValueError) as e:
            raise ValueError(f"{path.name} cannot be parsed, not saved: {e}") from e
        if not isinstance(data, dict):
            raise ValueError(f"{path.name} is not a JSON object, not saved")
    data.update(updates)
    if v2 is not None:
        section = data.get("v2")
        if not isinstance(section, dict):
            section = {}
        section.update(v2)
        data["v2"] = section
    _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _atomic_write(path: Path, text: str) -> None:
    """Write `text` next to `path` under a unique temp name (two writers never share one), fsync it, then swap it in.
    os.replace fails with PermissionError while another process (editor, antivirus, a reader) has the target open:
    retry a few times before giving up. The temp file never outlives a failure."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(REPLACE_RETRIES):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == REPLACE_RETRIES - 1:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def save_config_checked(path: Path, updates: dict | None = None, *, v2: dict | None = None) -> str:
    """save_config that returns "" on success or a one-line error text instead of raising (for the UI to show)."""
    try:
        save_config(path, updates, v2=v2)
    except (OSError, ValueError) as e:
        log.error("saving %s failed: %s", Path(path).name, e)
        return f"{type(e).__name__}: {e}"
    return ""


def cmd_list_ports() -> int:
    if list_ports is None:
        print("pyserial is not installed: pip install -r requirements.txt")
        return 1
    ports = list(list_ports.comports())
    if not ports:
        print("No serial ports found.")
    for p in ports:
        print(describe_port(p))
    print(f"Unique USB-hint candidate (not board identification): {find_port(ports) or '(none)'}")
    print("WiFi boards (firmware 1.3.0+): --discover to find them, --port tcp://<IP>[:7330] to use one.")
    return 0


def cmd_discover(timeout: float = 1.5) -> int:
    """Print the boards that answer a UDP broadcast: name id ip:port fw busy/free. 0 = found some."""
    boards = discovery.discover(timeout)
    for b in boards:
        print(f"{b['name']} {b['id'] or '-'} {b['ip']}:{b['port']} {b['fw'] or '-'} {'busy' if b['busy'] else 'free'}")
    if not boards:
        print("No boards answered (board must be on WiFi; broadcasts may be blocked: use --port tcp://<IP>).")
    return 0 if boards else 1


def cmd_pair(port: str | None, baud: int, config_path: Path) -> int:
    """USB serial: ask the board for its OTA pairing code and save it as ota_token. Prints the board id only."""
    if is_tcp_port(port):
        log.error("--pair works over USB serial only (use --port COMx)")
        return 1
    link = Link(port, baud)
    try:
        if not link.connect() or not link.wait_response(5.0, after_ok=0):   # ping OK first: no mix-up
            log.error("no response from the board on %s", port or "(auto-detected port)")
            return 2
        res = link.command({"v": 2, "type": "pair"}, 5.0)
    finally:
        link.close()
    if not res.ok or not res.pair:
        log.error("pairing failed: %s", res.err or "no pairing code in the reply (firmware older than 1.3.1?)")
        return 2
    try:
        save_config(config_path, {"ota_token": res.pair["token"]})
    except (OSError, ValueError) as e:
        log.error("cannot save the pairing code: %s", e)
        return 1
    print(f"Paired with board {res.pair['id'] or '?'}; pairing code saved to {config_path}.")
    return 0


def cmd_ota(path: Path, port: str | None, baud: int, cfg: dict) -> int:
    """WiFi: upload a firmware image (firmware.bin) to the board. 0 = written, the board reboots into it."""
    if not is_tcp_port(port):
        addr = cfg.get("wifi_addr")
        if not addr:
            log.error("--ota needs a WiFi board: --port tcp://<IP> (or wifi_addr / a tcp:// port in the config)")
            return 1
        port = addr if is_tcp_port(addr) else f"tcp://{addr}"
    token = cfg.get("ota_token")
    if not token:
        log.error("no ota_token in the config: run --pair once with the board on USB")
        return 1
    image, err = read_firmware(path)
    if err:
        log.error("%s: %s", path, {"bad_image": "not an ESP32 firmware image",
                                   "too_big": f"larger than the OTA slot ({OTA_PARTITION_SIZE} bytes)"}.get(err, err))
        return 1
    link = Link(port, baud)
    shown = [-1]

    def progress(sent, total):
        pct = sent * 100 // total
        if pct // 10 != shown[0] // 10 or sent == total:
            shown[0] = pct
            print(f"upload {pct}% ({sent}/{total} bytes)", flush=True)

    try:
        if not link.connect() or not link.wait_handshake(10.0):
            log.error("no response from the board at %s", port)
            return 2
        res = link.ota(image, token, progress)
    finally:
        link.close()
    if not res.ok:
        log.error("firmware upgrade failed: %s", res.err)
        return 2
    print("Firmware written; the board is rebooting (it rolls back by itself if the new firmware fails to start).")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="ESP32 e-paper side screen host")
    ap.add_argument("--port", help="serial port, e.g. COM5 (default: auto-detect), or tcp://HOST[:7330] for a WiFi board")
    ap.add_argument("--baud", type=int, help="serial baud rate (default 115200)")
    ap.add_argument("--interval", type=float, help="seconds between updates (default 30)")
    ap.add_argument("--config", type=Path, help="config JSON (default: config.json next to this script)")
    ap.add_argument("--dry-run", action="store_true", help="don't open serial, print JSON to stdout")
    ap.add_argument("--once", action="store_true", help="send a single update and exit")
    ap.add_argument("--primary", choices=dashboard_v2.PRIMARIES,
                    help="v2: which period the TOKENS block and models expand (overrides config v2.primary)")
    ap.add_argument("--dry-run-v2", action="store_true",
                    help="don't open serial; print the five dashboard-v2 messages built from the Token Monitor file")
    ap.add_argument("--v2", action="store_true",
                    help="skip v1/v2 detection and use v2 (with --once: send one v2 round and exit)")
    ap.add_argument("--list-ports", action="store_true", help="list serial ports and exit")
    ap.add_argument("--discover", action="store_true",
                    help="find WiFi boards on the LAN (UDP broadcast), print them and exit")
    ap.add_argument("--pair", action="store_true",
                    help="USB serial only: fetch the board's pairing code into the config (ota_token) and exit")
    ap.add_argument("--ota", type=Path, metavar="FIRMWARE_BIN",
                    help="upgrade the board's firmware over WiFi (--port tcp://IP or the config's port / wifi_addr; "
                         "needs ota_token from --pair) and exit")
    ap.add_argument("--log", type=Path, help="also write the log to this file")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = ap.parse_args(argv)

    handlers: list[logging.Handler] = []
    if sys.stderr is not None:  # None under pythonw / --noconsole builds
        handlers.append(logging.StreamHandler(sys.stderr))
    if args.log:
        handlers.append(logging.FileHandler(args.log, encoding="utf-8"))
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S",
        handlers=handlers or [logging.NullHandler()],
    )
    if args.list_ports:
        return cmd_list_ports()
    if args.discover:
        return cmd_discover()

    try:
        cfg = load_config(args.config)
        for key in ("handshake_timeout", "response_timeout", "reconnect_interval"):
            cfg[key] = float(cfg[key])
            if not math.isfinite(cfg[key]) or cfg[key] <= 0:
                raise ValueError(f"{key} must be a positive finite number")
        baud = args.baud if args.baud is not None else int(cfg["baud"])
        if baud <= 0:
            raise ValueError("baud must be positive")
    except (OSError, ValueError, TypeError) as e:
        log.error("bad config file: %s", e)
        return 1
    port = args.port or cfg["port"]
    interval = clamp_interval(args.interval or cfg["interval"])

    if args.pair or args.ota:
        if serial is None:
            log.error("pyserial is not installed: pip install -r requirements.txt")
            return 1
        if args.pair:
            return cmd_pair(port, baud, args.config or default_config_path())
        return cmd_ota(args.ota, port, baud, cfg)

    if args.dry_run_v2:
        return cmd_dry_run_v2(cfg, args.primary)
    if not args.dry_run and serial is None:
        log.error("pyserial is not installed: pip install -r requirements.txt")
        return 1

    source = str(cfg["token_source"]).lower()
    if source not in ("tokscale", "ccusage"):
        log.error('token_source must be "tokscale" or "ccusage", got %r', cfg["token_source"])
        return 1
    tok_interval, tok_timeout = float(cfg["token_interval"]), float(cfg["token_timeout"])
    v2 = v2_options(cfg)
    use_v2 = (bool(v2["enabled"]) or args.v2) and not args.dry_run

    class V1Stack:
        """System + token collectors for the v1 stats path (psutil, tokscale/ccusage)."""

        def __init__(self):
            self.sysc = SystemCollector(cfg["disk"], cfg["gpu"])
            if source == "ccusage":
                self.tokc: TokenCollector = CcusageSource(cfg["ccusage_cmd"], tok_interval, tok_timeout)
            else:
                self.tokc = TokscaleSource(cfg["tokscale_cmd"], tok_interval, tok_timeout,
                                           cfg["tokscale_clients"], float(cfg["limits_interval"]))
            self.tokc.start()
            log.info("token source: %s (%s)", self.tokc.source, self.tokc.cmd)

        def stats(self) -> dict:
            return build_stats(self.sysc, self.tokc)

    # v2 never needs them, so with serial + continuous they are created on the first v1 tick;
    # --once and the dry runs keep the original eager behaviour.
    lazy_v1 = use_v2 and not args.once and not args.v2
    v1: V1Stack | None = None
    def reload_v2_options():
        """Re-read the config file; primary / heat_rows / heat_weeks / calendar / refresh_min follow it live (--primary wins)."""
        fresh = v2_options(load_config(args.config))
        return ((None if args.primary else fresh["primary"]), fresh["heat_rows"], fresh["heat_weeks"],
                fresh["calendar"], fresh["refresh_min"])

    pusher = V2Pusher(tm_source.TmSource(v2["tm_file"]),
                      mouse_factory=load_mouse_source if v2["mouse"] else None,
                      primary=args.primary or v2["primary"], heat_rows=v2["heat_rows"], heat_weeks=v2["heat_weeks"],
                      calendar=v2["calendar"], refresh_min=v2["refresh_min"], options_loader=reload_v2_options) if use_v2 else None
    wx = WxSync(lambda: weather.weather_options(load_config(args.config))) if use_v2 else None
    link = None
    session = backoff = None
    next_retry = 0.0
    retry = max(5.0, float(cfg["reconnect_interval"]))
    try:
        if not (args.v2 and not args.dry_run):
            if not lazy_v1:
                v1 = V1Stack()
            if args.once:
                log.info("waiting for first token snapshot (up to %.0fs); Ctrl+C to exit", tok_timeout * 3 + 5)
                v1.tokc.wait_first(tok_timeout * 3 + 5)
                time.sleep(1.0)
        if not args.dry_run:
            import worker   # shared with the GUI (when run as a script, __main__ is registered as "epaper_host")
            link = Link(port, baud, float(cfg["handshake_timeout"]),
                        float(cfg["response_timeout"]))
            backoff = worker.Backoff()
            if use_v2:
                session = worker.V2Session(link, pusher, wx, force_v2=args.v2)
        while True:
            t0 = time.monotonic()
            try:
                msg = v1.stats() if v1 is not None and (link is None or args.once) else None
                if link is None:
                    print(encode(msg).decode("utf-8"), end="", flush=True)
                else:
                    if not link.connected and t0 >= next_retry:
                        link.connect()
                        next_retry = time.monotonic() + retry
                        if use_v2 and not args.once and not args.v2 and link.connected:
                            link.wait_handshake(2.0)  # READY/OK tell us v1 or v2 before the first data line
                    if args.once and args.v2:
                        if not link.wait_response(link.handshake_timeout, after_ok=0):
                            log.error("no handshake response; port open alone is not protocol success")
                            return 2
                        with link._state:
                            baseline = link.ok_count
                        sent = push_v2(link, pusher, t0, datetime.now().astimezone())
                        with link._state:
                            acked = link.ok_count - baseline
                        if sent < len(dashboard_v2.KINDS) or acked < sent or link.err_count:
                            log.error("v2 receipt not confirmed (timeout, ERR or disconnect); display unverified")
                            return 2
                        return 0
                    if args.once:
                        if not link.wait_response(link.handshake_timeout, after_ok=0):
                            log.error("no handshake response; port open alone is not protocol success")
                            return 2
                        # Ping OK precedes stats; v1 has no message IDs for stronger correlation.
                        with link._state:
                            baseline = link.ok_count
                        if not link.send(msg) or not link.wait_response(link.response_timeout, after_ok=baseline):
                            log.error("stats receipt not confirmed (timeout, ERR or disconnect); display unverified")
                            return 2
                    elif link.connected:
                        proto = session.step(t0, datetime.now().astimezone()) if use_v2 else "v1"
                        if proto == "v2":
                            pass     # session.step already pushed the due blocks
                        elif proto == "v1" or not link.has_response:
                            # v1 firmware, or a board that has not answered anything (original behaviour)
                            if v1 is None:
                                v1 = V1Stack()
                            link.send(v1.stats())
                        # else: replied to the first ping but the v2 probe is still pending - hold stats
                        link.diagnose()
                if backoff is not None:
                    backoff.reset()
            except KeyboardInterrupt:
                raise
            except Exception:  # noqa: BLE001 - one bad iteration must not end the host
                if args.once or backoff is None:
                    raise
                delay = backoff.next()
                log.exception("push loop error, retrying in %.0f s", delay)
                time.sleep(delay)
                continue
            if args.once:
                return 0
            time.sleep(loop_wait(link is None or link.connected, interval, retry, time.monotonic() - t0))
    except KeyboardInterrupt:
        log.info("bye")
        return 130 if args.once else 0
    finally:
        if v1 is not None:
            v1.tokc.stop()
        if pusher is not None:
            pusher.close()
        if link is not None:
            link.close()


if __name__ == "__main__":
    sys.modules.setdefault("epaper_host", sys.modules[__name__])   # worker.py imports us by name: share this instance
    sys.exit(main())
