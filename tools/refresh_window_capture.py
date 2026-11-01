#!/usr/bin/env python3
"""One-owner serial capture for one controlled e-paper refresh window.

The tool opens the port once, keeps DTR/RTS inactive before open (no reset),
sends protocol-v1 ``stats`` lines and reads everything in the same process.
Every event is written as one JSON object per line with both UTC and
``time.monotonic_ns()`` timestamps:

  * TX: exact frame text, SHA-256, byte count, write start/end, write result.
  * RX: raw line, receive timestamp, parsed kind/fields, FIFO ACK mapping.
  * Refresh frames: ``DIAG epd planes`` / ``DIAG epd refresh done`` with the
    firmware's own ``busy_ms`` / ``total_ms`` values.

Protocol v1 has no message ids. The board answers one ``OK`` per accepted
line in order, so an ACK is mapped FIFO to the oldest unacknowledged TX. This
is an association by protocol order, not an id echoed by the firmware.

The target window is:
  previous frame done -> several distinct stats payloads in the >=180 s hold
  -> target payload acknowledged -> next frame start -> next frame done.

After the target frame completes the tool sends the exact same target frame
every 10 s (no new values) until a stop file appears or the bounded hold time
ends. That keeps the link online without changing what the panel shows.

Use ``--self-test`` to exercise the logger and the whole state machine against
an in-memory fake board. The self-test is not device evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import serial
except ImportError:  # only needed for a real run
    serial = None

BAUD = 115200
MAX_LINE = 1024
WARMUP_CPU = 11
WARMUP_MEM = 10
TARGET_CPU = 77
TARGET_MEM = 66
PAYLOAD_PLAN = (
    ("p1", 5.0, 22, 12),
    ("p2", 45.0, 33, 23),
    ("p3", 85.0, 44, 34),
    ("p4", 125.0, 55, 45),
    ("p5", 150.0, TARGET_CPU, TARGET_MEM),
)
TARGET_REPEAT_S = 15.0
HOLD_REPEAT_S = 10.0
A_DONE_TIMEOUT_S = 200.0
B_DONE_TIMEOUT_S = 300.0
WARMUP_ACK_TIMEOUT_S = 15.0


def utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def local_time_text(dt: datetime) -> str:
    """The firmware shows the host's ``time`` string unchanged."""
    return dt.astimezone().strftime("%m-%d %H:%M")


def build_stats(time_text: str, cpu: int, mem: int, disk: int = 55) -> dict:
    """A protocol-v1 stats object; values are controlled test values."""
    if not (0 <= cpu <= 100 and 0 <= mem <= 100 and 0 <= disk <= 100):
        raise ValueError("cpu/mem/disk must be 0..100")
    return {
        "v": 1,
        "type": "stats",
        "time": time_text,
        "cpu": int(cpu),
        "mem": int(mem),
        "disk": int(disk),
        "net_up": "1.1K",
        "net_down": "2.2K",
        "tok": {
            "block_tokens": "12.3K",
            "block_cost": 1.23,
            "burn_tpm": "4.5K",
            "remain_min": 42,
            "today_tokens": "56.7K",
            "today_cost": 2.34,
        },
    }


def encode_stats(msg: dict) -> tuple[str, bytes]:
    line = json.dumps(msg, ensure_ascii=False, separators=(",", ":"))
    data = (line + "\n").encode("utf-8")
    if len(data) > MAX_LINE:
        raise ValueError(f"frame too long: {len(data)} > {MAX_LINE}")
    return line, data


def parse_kv(line: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for token in line.split():
        if "=" in token:
            key, value = token.split("=", 1)
            out[key] = value
    return out


class EventLog:
    """JSONL event log plus a human-readable transcript."""

    def __init__(self, path: Path, transcript: Path | None, clock_ns, utc_fn):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.fh = path.open("w", encoding="utf-8", newline="\n")
        self.tfh = transcript.open("w", encoding="utf-8", newline="\n") if transcript else None
        self.clock_ns = clock_ns
        self.utc_fn = utc_fn

    def emit(self, event: str, **fields) -> dict:
        rec = {
            "utc": utc_iso(self.utc_fn()),
            "mono_ns": int(self.clock_ns()),
            "event": event,
        }
        rec.update(fields)
        self.fh.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.fh.flush()
        if self.tfh is not None:
            self.tfh.write(self._human(rec) + "\n")
            self.tfh.flush()
        return rec

    @staticmethod
    def _human(rec: dict) -> str:
        parts = [f"{rec['utc']} mono_ns={rec['mono_ns']} {rec['event']}"]
        for key, value in rec.items():
            if key in ("utc", "mono_ns", "event"):
                continue
            if isinstance(value, str) and (" " in value or '"' in value or "\n" in value):
                value = json.dumps(value, ensure_ascii=False)
            parts.append(f"{key}={value}")
        return " ".join(parts)

    def close(self) -> None:
        self.fh.close()
        if self.tfh is not None:
            self.tfh.close()


class SerialTransport:
    """The only owner of the serial port for the whole window."""

    def __init__(self, port: str) -> None:
        self.port = port
        self.ser = None

    def open(self) -> None:
        if serial is None:
            raise RuntimeError("pyserial is not installed")
        s = serial.Serial()
        s.port = self.port
        s.baudrate = BAUD
        s.timeout = 0
        s.write_timeout = 2
        # Set inactive before open, exactly like the handoff passive scripts.
        s.dtr = False
        s.rts = False
        s.open()
        self.ser = s

    def in_waiting(self) -> int:
        return int(self.ser.in_waiting) if self.ser is not None else 0

    def read(self, size: int) -> bytes:
        if self.ser is None:
            return b""
        return self.ser.read(max(1, size))

    def write(self, data: bytes) -> int:
        if self.ser is None:
            raise RuntimeError("serial port is not open")
        n = self.ser.write(data)
        self.ser.flush()
        return int(n or 0)

    def close(self) -> None:
        if self.ser is not None:
            self.ser.close()
            self.ser = None


class FakeBoard:
    """In-memory board for --self-test. Not device evidence."""

    def __init__(self, clock_ns):
        self.clock_ns = clock_ns
        self.queued: list[tuple[int, bytes]] = []
        self.latest_msg: dict | None = None
        self.shown_sig: str | None = None
        self.frame_busy = False
        self.last_done_ns = -10**18
        self.frame_started_ns: int | None = None
        self.closed = False
        self.writes: list[bytes] = []

    def open(self) -> None:
        self.closed = False

    @staticmethod
    def _sig(msg: dict | None) -> str:
        if msg is None:
            return "none"
        return json.dumps(msg, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _queue(self, due_ns: int, text: str) -> None:
        self.queued.append((int(due_ns), (text + "\n").encode("utf-8")))
        self.queued.sort(key=lambda item: item[0])

    def _maybe_start(self, now_ns: int) -> None:
        if self.frame_busy or self.latest_msg is None:
            return
        if self._sig(self.latest_msg) == self.shown_sig:
            return
        if now_ns - self.last_done_ns < 180_000_000_000:
            return
        start_ns = now_ns + 50_000_000  # 50 ms after the write is serviced
        self.frame_busy = True
        self.frame_started_ns = start_ns
        self._queue(
            start_ns,
            "DIAG epd planes path=v2_old black_cmd=0x10 black_ink_px=16910 "
            "ry_cmd=0x13 ry_invert=0 ry_color_px=720 ry_sent_zero_bits=720 "
            "ry_sent_one_bits=119280",
        )
        self._queue(start_ns + 23_078_000_000, "DIAG epd refresh done path=v2_old busy_ms=22140 total_ms=23078")
        self.shown_sig = self._sig(self.latest_msg)

    def write(self, data: bytes) -> int:
        self.writes.append(bytes(data))
        now = int(self.clock_ns())
        self._queue(now + 5_000_000, "OK")
        try:
            msg = json.loads(data.decode("utf-8").strip())
        except Exception:
            msg = None
        if isinstance(msg, dict) and msg.get("type") == "stats":
            self.latest_msg = msg
            self._maybe_start(now)
        return len(data)

    def poll(self) -> bytes:
        now = int(self.clock_ns())
        self._maybe_start(now)
        out = bytearray()
        while self.queued and self.queued[0][0] <= now:
            _, chunk = self.queued.pop(0)
            out.extend(chunk)
        # A frame becomes idle at its done time.
        if self.frame_busy and self.frame_started_ns is not None:
            done_ns = self.frame_started_ns + 23_078_000_000
            if now >= done_ns:
                self.frame_busy = False
                self.last_done_ns = done_ns
        return bytes(out)

    def read(self, size: int) -> bytes:
        data = self.poll()
        return data[:size]

    def in_waiting(self) -> int:
        return len(self.poll())

    def close(self) -> None:
        self.closed = True


class Capture:
    def __init__(
        self,
        transport,
        log: EventLog,
        args,
        clock_ns=time.monotonic_ns,
        utc_fn=datetime.now,
        sleep_fn=time.sleep,
        mode: str = "real",
    ):
        self.transport = transport
        self.log = log
        self.args = args
        self.clock_ns = clock_ns
        self.utc_fn = utc_fn
        self.sleep_fn = sleep_fn
        self.mode = mode

        self.start_ns = int(clock_ns())
        self.phase = "init"
        self.stop_requested: str | None = None
        self.stop_file_content: str | None = None
        self.exit_code = 0
        self.invalid: str | None = None
        self.window_valid: bool | None = None

        self.tx_seq = 0
        self.rx_seq = 0
        self.tx_records: list[dict] = []
        self.pending_acks: list[dict] = []
        self.rx_lines = 0

        self.frames: list[dict] = []
        self.current_frame: dict | None = None
        self.frame_no = 0
        self.a_done: dict | None = None
        self.b_start: dict | None = None
        self.b_done: dict | None = None
        self.window_complete = False

        self.target: dict | None = None
        self.target_line: str | None = None
        self.target_data: bytes | None = None
        self.target_msg: dict | None = None
        self.target_time_text: str | None = None
        self.target_ack_mono_ns: int | None = None
        self.target_ack_utc: str | None = None
        self.target_repeat_ns: int | None = None

        self.warmup_tx: dict | None = None
        self.warmup_ack_ns: int | None = None
        self.warmup_deadline_ns = self.start_ns + int(WARMUP_ACK_TIMEOUT_S * 1e9)
        self.next_warmup_ns = self.start_ns
        self.payload_queue: list[dict] = []
        self.hold_start_ns: int | None = None
        self.hold_start_utc: str | None = None
        self.hold_next_ns: int | None = None
        self.rx_buf = bytearray()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def run(self) -> int:
        self.log.emit(
            "start",
            mode=self.mode,
            pid=os.getpid(),
            argv=sys.argv,
            cwd=str(Path.cwd()),
            port=self.args.port,
            baud=BAUD,
            max_total_seconds=self.args.max_total_seconds,
            max_hold_seconds=self.args.max_hold_seconds,
            stop_file=str(self.args.stop_file) if self.args.stop_file else None,
            note="single serial owner; DTR/RTS inactive before open; no reset",
        )
        try:
            self.transport.open()
        except Exception as exc:
            self.invalid = f"open failed: {exc!r}"
            self.exit_code = 4
            self.log.emit("error", stage="open", error=repr(exc))
            self._finish()
            return self.exit_code

        self.log.emit(
            "open",
            port=self.args.port,
            baud=BAUD,
            dtr_before_open=False,
            rts_before_open=False,
            in_waiting=self.transport.in_waiting(),
            note="DTR=False RTS=False were set before open; no reset pulse sent",
        )
        self.phase = "warmup"
        self.next_warmup_ns = self.clock_ns()
        try:
            while True:
                now = self.clock_ns()
                if self._check_stop(now):
                    break
                data = self.transport.read(4096)
                if data:
                    self._feed(data)
                if self.invalid is not None:
                    break
                self._maybe_send(now)
                if self.invalid is not None:
                    break
                self.sleep_fn(0.002)
        except KeyboardInterrupt:
            self.stop_requested = "KeyboardInterrupt"
            self.exit_code = 0
        except Exception as exc:
            self.log.emit("error", stage="loop", error=repr(exc), traceback=traceback.format_exc())
            self.stop_requested = f"exception: {exc!r}"
            self.exit_code = 3
        finally:
            if self.stop_requested is None and self.invalid is not None:
                self.stop_requested = f"invalid: {self.invalid}"
                self.exit_code = 2
            self._finish()
        return self.exit_code

    def _check_stop(self, now: int) -> bool:
        if self.stop_requested is not None:
            return True
        if now - self.start_ns > self.args.max_total_seconds * 1e9:
            self.stop_requested = "max_total_seconds"
            self.exit_code = 0
            return True
        if self.phase == "hold" and self.hold_start_ns is not None:
            if now - self.hold_start_ns > self.args.max_hold_seconds * 1e9:
                self.stop_requested = "max_hold_seconds"
                self.exit_code = 0
                return True
        if self.args.stop_file and Path(self.args.stop_file).exists():
            try:
                self.stop_file_content = Path(self.args.stop_file).read_text(encoding="utf-8").strip()
            except OSError as exc:
                self.stop_file_content = f"<read error: {exc!r}>"
            self.stop_requested = "stop_file"
            self.exit_code = 0
            return True
        if self.warmup_tx is not None and self.warmup_ack_ns is None and now > self.warmup_deadline_ns:
            self.invalid = "no OK for warmup stats within 15 s"
            self.exit_code = 2
            self.stop_requested = f"invalid: {self.invalid}"
            self.log.emit("error", stage="timeout", error=self.invalid)
            return True
        if self.a_done is None and now - self.start_ns > A_DONE_TIMEOUT_S * 1e9:
            self.invalid = f"no previous frame done within {A_DONE_TIMEOUT_S:.0f} s"
            self.exit_code = 2
            self.stop_requested = f"invalid: {self.invalid}"
            self.log.emit("error", stage="timeout", error=self.invalid)
            return True
        if self.a_done is not None and self.b_done is None:
            if now - self.a_done["done"]["mono_ns"] > B_DONE_TIMEOUT_S * 1e9:
                self.invalid = f"no target frame done within {B_DONE_TIMEOUT_S:.0f} s of previous done"
                self.exit_code = 2
                self.stop_requested = f"invalid: {self.invalid}"
                self.log.emit("error", stage="timeout", error=self.invalid)
                return True
        return False

    def _finish(self) -> None:
        if self.stop_requested is None:
            self.stop_requested = "loop_exit"
        self.log.emit(
            "stop_requested",
            reason=self.stop_requested,
            stop_file_content=self.stop_file_content,
            exit_code=self.exit_code,
        )
        close_ok = False
        close_error = None
        try:
            self.transport.close()
            close_ok = True
        except Exception as exc:
            close_error = repr(exc)
            self.exit_code = max(self.exit_code, 3)
        self.log.emit("close", port=self.args.port, close_ok=close_ok, close_error=close_error)

        max_ack = max((r["ack_latency_ns"] for r in self.tx_records if "ack_latency_ns" in r), default=None)
        ack_latencies = [r["ack_latency_ns"] for r in self.tx_records if "ack_latency_ns" in r]
        unacked = [r["tx_id"] for r in self.tx_records if "ack_latency_ns" not in r]
        frames_after_b = []
        if self.b_done is not None:
            frames_after_b = [
                f["frame_no"] for f in self.frames
                if f.get("start") and f["start"]["mono_ns"] > self.b_done["done"]["mono_ns"]
            ]
        self.log.emit(
            "stop",
            reason=self.stop_requested,
            exit_code=self.exit_code,
            tx_count=self.tx_seq,
            rx_count=self.rx_lines,
            window_complete=self.window_complete,
            window_valid=self.window_valid,
            final_target_tx_id=self.target["tx_id"] if self.target else None,
            final_target_frame_sha256=self.target["frame_sha256"] if self.target else None,
            final_target_time=self.target_time_text,
            final_target_cpu=self.target_msg.get("cpu") if self.target_msg else None,
            hold_start_utc=self.hold_start_utc,
            serial_close_ok=close_ok,
            serial_close_error=close_error,
            frames_after_target_done=frames_after_b,
            bye="not applicable: this is the controlled sender, not epaper_host.py",
        )
        self.log.emit(
            "summary",
            mode=self.mode,
            window_complete=self.window_complete,
            window_valid=self.window_valid,
            interval_prev_done_to_target_planes_ns=(
                self.b_start["start"]["mono_ns"] - self.a_done["done"]["mono_ns"]
                if self.b_start and self.a_done else None
            ),
            interval_prev_done_to_target_done_ns=(
                self.b_done["done"]["mono_ns"] - self.a_done["done"]["mono_ns"]
                if self.b_done and self.a_done else None
            ),
            interval_target_planes_to_done_ns=(
                self.b_done["done"]["mono_ns"] - self.b_start["start"]["mono_ns"]
                if self.b_done and self.b_start else None
            ),
            a_total_ms=self.a_done["done"]["fields"].get("total_ms") if self.a_done else None,
            a_busy_ms=self.a_done["done"]["fields"].get("busy_ms") if self.a_done else None,
            b_total_ms=self.b_done["done"]["fields"].get("total_ms") if self.b_done else None,
            b_busy_ms=self.b_done["done"]["fields"].get("busy_ms") if self.b_done else None,
            max_ack_latency_ns=max_ack,
            median_ack_latency_ns=int(statistics.median(ack_latencies)) if ack_latencies else None,
            unacked_tx_ids=unacked,
            tx_roles={role: sum(1 for r in self.tx_records if r["role"] == role)
                      for role in sorted({r["role"] for r in self.tx_records})},
            frame_count=len(self.frames),
            frames_after_target_done=frames_after_b,
        )
        self.log.close()

    # ------------------------------------------------------------------
    # sending
    # ------------------------------------------------------------------
    def _maybe_send(self, now: int) -> None:
        if self.phase == "warmup":
            if now >= self.next_warmup_ns:
                self._send_stats("warmup", WARMUP_CPU, WARMUP_MEM)
                self.next_warmup_ns = now + int(10e9)
            return

        while self.payload_queue and now >= self.payload_queue[0]["due_ns"]:
            item = self.payload_queue.pop(0)
            self._send_stats(item["role"], item["cpu"], item["mem"])
            if item["role"] == "p5":
                self.phase = "wait_b"
                self.target_repeat_ns = now + int(TARGET_REPEAT_S * 1e9)

        if self.phase == "wait_b" and self.target is not None and self.target_repeat_ns is not None:
            if now >= self.target_repeat_ns:
                self._send_stats("target_hold", TARGET_CPU, TARGET_MEM, reuse_target=True)
                self.target_repeat_ns = now + int(TARGET_REPEAT_S * 1e9)

        if self.phase == "hold" and self.hold_next_ns is not None and now >= self.hold_next_ns:
            self._send_stats("hold", TARGET_CPU, TARGET_MEM, reuse_target=True)
            self.hold_next_ns = now + int(HOLD_REPEAT_S * 1e9)

    def _send_stats(self, role: str, cpu: int, mem: int, reuse_target: bool = False) -> None:
        if reuse_target:
            if self.target_line is None or self.target_data is None or self.target_msg is None:
                self.invalid = "target repeat requested before target existed"
                return
            line, data, msg = self.target_line, self.target_data, self.target_msg
        else:
            text = local_time_text(self.utc_fn())
            msg = build_stats(text, cpu, mem)
            line, data = encode_stats(msg)

        self.tx_seq += 1
        tx_id = self.tx_seq
        start_mono = self.clock_ns()
        start_utc = self.utc_fn()
        result = "not_written"
        end_mono = start_mono
        end_utc = start_utc
        try:
            written = self.transport.write(data)
            end_mono = self.clock_ns()
            end_utc = self.utc_fn()
            result = f"ok:{written}"
        except Exception as exc:
            end_mono = self.clock_ns()
            end_utc = self.utc_fn()
            result = f"error:{exc!r}"
            self.invalid = f"write failed for {role}: {exc!r}"

        record = {
            "tx_id": tx_id,
            "role": role,
            "frame": line,
            "frame_sha256": sha256_text(line),
            "bytes": len(data),
            "write_start_utc": utc_iso(start_utc),
            "write_start_mono_ns": int(start_mono),
            "write_end_utc": utc_iso(end_utc),
            "write_end_mono_ns": int(end_mono),
            "write_result": result,
            "payload_time": msg["time"],
            "payload_cpu": msg["cpu"],
            "payload_mem": msg["mem"],
            "payload_disk": msg["disk"],
            "reused_target": reuse_target,
        }
        self.log.emit("tx", **record)
        self.tx_records.append(record)
        self.pending_acks.append(record)

        if role == "warmup" and self.warmup_tx is None:
            self.warmup_tx = record
        if role == "p5":
            self.target = record
            self.target_line = line
            self.target_data = data
            self.target_msg = msg
            self.target_time_text = msg["time"]

    # ------------------------------------------------------------------
    # receiving
    # ------------------------------------------------------------------
    def _feed(self, data: bytes) -> None:
        self.rx_buf.extend(data)
        while b"\n" in self.rx_buf:
            raw, _, self.rx_buf = self.rx_buf.partition(b"\n")
            line = raw.rstrip(b"\r").decode("utf-8", "replace")
            self._handle_line(line)

    def _pop_ack(self) -> dict | None:
        return self.pending_acks.pop(0) if self.pending_acks else None

    def _handle_line(self, line: str) -> None:
        self.rx_lines += 1
        rx_mono = int(self.clock_ns())
        rx_utc = self.utc_fn()
        kind = "other"
        extra: dict = {}
        ack = None

        if line == "OK":
            kind = "ok"
            ack = self._pop_ack()
            if ack is None:
                extra["unexpected_ok"] = True
            else:
                ack["ack_mono_ns"] = rx_mono
                ack["ack_utc"] = utc_iso(rx_utc)
                ack["ack_latency_ns"] = rx_mono - ack["write_end_mono_ns"]
                extra.update({
                    "ack_tx_id": ack["tx_id"],
                    "ack_role": ack["role"],
                    "ack_latency_ns": ack["ack_latency_ns"],
                })
                if ack["role"] == "warmup" and self.warmup_ack_ns is None:
                    self.warmup_ack_ns = rx_mono
                if ack["role"] == "p5":
                    self.target_ack_mono_ns = rx_mono
                    self.target_ack_utc = utc_iso(rx_utc)
        elif line.startswith("ERR "):
            kind = "err"
            ack = self._pop_ack()
            if ack is not None:
                extra.update({"ack_tx_id": ack["tx_id"], "ack_role": ack["role"]})
            self.invalid = f"board returned {line!r}" + (f" after tx {ack['tx_id']}" if ack else "")
        elif line.startswith("DIAG epd planes"):
            kind = "planes"
            extra["fields"] = parse_kv(line)
        elif line.startswith("DIAG epd refresh done"):
            kind = "done"
            extra["fields"] = parse_kv(line)
        elif line.startswith("DIAG epd fault") or line.startswith("DIAG display stopped"):
            kind = "fault"
            self.invalid = f"fault line seen: {line}"
        elif line.startswith("READY"):
            kind = "ready"
            self.invalid = f"unexpected READY (board reset/reboot): {line}"
        elif line.startswith("DIAG "):
            kind = "diag"
            extra["fields"] = parse_kv(line)
        elif not line:
            kind = "blank"

        self.log.emit(
            "rx",
            rx_seq=self.rx_lines,
            line=line,
            rx_utc=utc_iso(rx_utc),
            rx_mono_ns=rx_mono,
            kind=kind,
            **extra,
        )

        if kind == "planes":
            self._on_planes(extra["fields"], rx_mono, rx_utc, line)
        elif kind == "done":
            self._on_done(extra["fields"], rx_mono, rx_utc, line)

    def _on_planes(self, fields: dict, rx_mono: int, rx_utc: datetime, line: str) -> None:
        self.frame_no += 1
        frame = {
            "frame_no": self.frame_no,
            "start": {
                "utc": utc_iso(rx_utc),
                "mono_ns": rx_mono,
                "fields": fields,
                "line": line,
            },
            "done": None,
        }
        self.frames.append(frame)
        self.current_frame = frame
        self.log.emit(
            "frame_planes",
            frame_no=self.frame_no,
            planes_utc=utc_iso(rx_utc),
            planes_mono_ns=rx_mono,
            fields=fields,
        )
        if self.a_done is not None and self.b_start is None:
            self.b_start = frame
            self.log.emit("b_start", frame_no=self.frame_no, planes_utc=utc_iso(rx_utc), planes_mono_ns=rx_mono)
            if self.target is None:
                self.invalid = "target frame started before p5 was sent"
        elif self.phase == "hold":
            self.log.emit("hold_frame_planes", frame_no=self.frame_no)
            self.invalid = "a new refresh started during the hold after the target frame"

    def _on_done(self, fields: dict, rx_mono: int, rx_utc: datetime, line: str) -> None:
        frame = self.current_frame
        if frame is None or frame.get("done") is not None:
            self.frame_no += 1
            frame = {
                "frame_no": self.frame_no,
                "start": None,
                "done": None,
            }
            self.frames.append(frame)
        frame["done"] = {
            "utc": utc_iso(rx_utc),
            "mono_ns": rx_mono,
            "fields": fields,
            "line": line,
        }
        self.current_frame = None
        from_planes = None
        if frame["start"] is not None:
            from_planes = rx_mono - frame["start"]["mono_ns"]
        prev_done = None
        for other in reversed(self.frames):
            if other is not frame and other.get("done") is not None:
                prev_done = other["done"]["mono_ns"]
                break
        self.log.emit(
            "frame_done",
            frame_no=frame["frame_no"],
            done_utc=utc_iso(rx_utc),
            done_mono_ns=rx_mono,
            fields=fields,
            busy_ms=fields.get("busy_ms"),
            total_ms=fields.get("total_ms"),
            from_planes_ns=from_planes,
            from_prev_done_ns=(rx_mono - prev_done) if prev_done is not None else None,
        )

        if self.a_done is None:
            self.a_done = frame
            self.log.emit(
                "a_done",
                frame_no=frame["frame_no"],
                done_utc=utc_iso(rx_utc),
                done_mono_ns=rx_mono,
                busy_ms=fields.get("busy_ms"),
                total_ms=fields.get("total_ms"),
            )
            base = rx_mono
            self.payload_queue = [
                {"role": role, "cpu": cpu, "mem": mem, "due_ns": base + int(offset * 1e9)}
                for role, offset, cpu, mem in PAYLOAD_PLAN
            ]
            self.phase = "wait_payloads"
            return

        if self.b_start is None:
            self.invalid = "target frame done arrived before its planes line"
            return

        if self.b_done is None:
            self.b_done = frame
            self._finish_window()
            return

        self.invalid = "an extra refresh completed during the hold"
        self.log.emit("extra_frame_done", frame_no=frame["frame_no"])

    # ------------------------------------------------------------------
    # window validation
    # ------------------------------------------------------------------
    def _finish_window(self) -> None:
        assert self.a_done is not None
        assert self.b_start is not None
        assert self.b_done is not None
        assert self.target is not None

        prev_done_ns = self.a_done["done"]["mono_ns"]
        b_start_ns = self.b_start["start"]["mono_ns"]
        b_done_ns = self.b_done["done"]["mono_ns"]
        target = self.target
        reasons: list[str] = []

        if target["write_end_mono_ns"] > b_start_ns:
            reasons.append("p5 write completed after target frame start")
        if self.target_ack_mono_ns is None:
            reasons.append("p5 was not acknowledged")
        elif self.target_ack_mono_ns > b_start_ns:
            reasons.append("p5 ACK arrived after target frame start")

        different_after_start = [
            t for t in self.tx_records
            if t["write_end_mono_ns"] > b_start_ns and t["frame_sha256"] != target["frame_sha256"]
        ]
        if different_after_start:
            reasons.append(
                "different payload(s) written after target frame start: "
                + ",".join(str(t["tx_id"]) for t in different_after_start)
            )

        before_start = [t for t in self.tx_records if t["write_end_mono_ns"] <= b_start_ns]
        latest_before = max(before_start, key=lambda t: t["tx_id"]) if before_start else None
        if latest_before is None or latest_before["frame_sha256"] != target["frame_sha256"]:
            reasons.append("p5/target was not the latest payload before target frame start")

        target_ack_before = self.target_ack_mono_ns is not None and self.target_ack_mono_ns <= b_start_ns
        target_write_before = target["write_end_mono_ns"] <= b_start_ns
        tx_during_wait = [
            t for t in self.tx_records
            if prev_done_ns <= t["write_start_mono_ns"] < b_start_ns
        ]
        distinct_payloads = []
        seen = set()
        for t in tx_during_wait:
            if t["frame_sha256"] not in seen:
                seen.add(t["frame_sha256"])
                distinct_payloads.append(t)

        valid = not reasons
        self.window_valid = valid
        self.window_complete = True
        if not valid:
            self.invalid = "window invalid: " + "; ".join(reasons)

        b_total_ms = self.b_done["done"]["fields"].get("total_ms")
        b_total_ns = int(b_total_ms) * 1_000_000 if b_total_ms and b_total_ms.isdigit() else None
        self.log.emit(
            "window_complete",
            valid=valid,
            reasons=reasons,
            prev_done_frame=self.a_done["frame_no"],
            prev_done_utc=self.a_done["done"]["utc"],
            prev_done_mono_ns=prev_done_ns,
            prev_done_busy_ms=self.a_done["done"]["fields"].get("busy_ms"),
            prev_done_total_ms=self.a_done["done"]["fields"].get("total_ms"),
            target_frame=self.b_start["frame_no"],
            target_planes_utc=self.b_start["start"]["utc"],
            target_planes_mono_ns=b_start_ns,
            target_done_utc=self.b_done["done"]["utc"],
            target_done_mono_ns=b_done_ns,
            target_busy_ms=self.b_done["done"]["fields"].get("busy_ms"),
            target_total_ms=b_total_ms,
            interval_prev_done_to_target_planes_ns=b_start_ns - prev_done_ns,
            interval_prev_done_to_target_done_ns=b_done_ns - prev_done_ns,
            interval_target_planes_to_done_ns=b_done_ns - b_start_ns,
            host_planes_to_done_minus_total_ms_ns=(
                (b_done_ns - b_start_ns) - b_total_ns if b_total_ns is not None else None
            ),
            target_tx_id=target["tx_id"],
            target_time=target["payload_time"],
            target_cpu=target["payload_cpu"],
            target_mem=target["payload_mem"],
            target_frame_sha256=target["frame_sha256"],
            target_write_start_utc=target["write_start_utc"],
            target_write_end_utc=target["write_end_utc"],
            target_ack_utc=self.target_ack_utc,
            target_write_before_start=target_write_before,
            target_ack_before_start=target_ack_before,
            target_to_target_planes_ns=b_start_ns - target["write_end_mono_ns"],
            target_ack_to_target_planes_ns=(
                b_start_ns - self.target_ack_mono_ns if self.target_ack_mono_ns is not None else None
            ),
            latest_payload_before_start_tx_id=latest_before["tx_id"] if latest_before else None,
            latest_payload_before_start_sha256=latest_before["frame_sha256"] if latest_before else None,
            tx_ids_during_wait=[t["tx_id"] for t in tx_during_wait],
            distinct_payload_tx_ids=[t["tx_id"] for t in distinct_payloads],
            distinct_payload_count=len(distinct_payloads),
        )

        if valid:
            self.phase = "hold"
            self.hold_start_ns = self.clock_ns()
            self.hold_start_utc = utc_iso(self.utc_fn())
            self.hold_next_ns = self.hold_start_ns + int(5e9)
            self.log.emit("hold_start", target_tx_id=target["tx_id"], target_time=target["payload_time"],
                          target_cpu=target["payload_cpu"],
                          note="same target frame repeated; no new values")
        else:
            self.stop_requested = "window invalid"
            self.exit_code = 2


def make_fake_clock(start_utc: datetime):
    state = {"ns": 1_000_000_000}  # positive, like time.monotonic_ns()

    def clock_ns() -> int:
        return state["ns"]

    def utc_fn() -> datetime:
        return start_utc + timedelta(microseconds=state["ns"] / 1000)

    def sleep_fn(seconds: float) -> None:
        state["ns"] += int(seconds * 1e9)

    return clock_ns, utc_fn, sleep_fn, state


def run_self_test(args) -> int:
    """Exercise logging and the full state machine against a fake board."""
    start_utc = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    clock_ns, utc_fn, sleep_fn, state = make_fake_clock(start_utc)
    board = FakeBoard(clock_ns)
    log = EventLog(args.log, args.transcript, clock_ns, utc_fn)
    args.port = "SIM"
    args.max_total_seconds = 600.0
    args.max_hold_seconds = 5.0
    cap = Capture(board, log, args, clock_ns=clock_ns, utc_fn=utc_fn, sleep_fn=sleep_fn, mode="self_test")

    # Run with a hard bound on simulated wall-clock iterations.
    class StopAfter:
        def __init__(self, limit_ns):
            self.limit_ns = limit_ns

        def __call__(self) -> int:
            return clock_ns()

    code = cap.run()
    if code not in (0,):
        print(f"SELF-TEST FAIL: capture exit code {code}")
        return 1

    # Read the log back and assert the exact things the field report relies on.
    records = []
    with args.log.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))

    def first(event: str):
        return next((r for r in records if r["event"] == event), None)

    def all_events(event: str):
        return [r for r in records if r["event"] == event]

    errors = []
    wc = first("window_complete")
    if wc is None:
        errors.append("no window_complete event")
    else:
        if not wc.get("valid"):
            errors.append(f"window not valid: {wc.get('reasons')}")
        if wc.get("distinct_payload_count", 0) < 5:
            errors.append(f"only {wc.get('distinct_payload_count')} distinct payloads")
        if wc.get("target_cpu") != TARGET_CPU:
            errors.append("target cpu mismatch")
        interval = wc.get("interval_prev_done_to_target_planes_ns")
        if interval is None or interval < 180_000_000_000:
            errors.append(f"interval below 180 s: {interval}")
        if not wc.get("target_write_before_start"):
            errors.append("target write not before target frame start")
        if not wc.get("target_ack_before_start"):
            errors.append("target ACK not before target frame start")

    tx = all_events("tx")
    if len([t for t in tx if t.get("role") in ("p1", "p2", "p3", "p4", "p5")]) < 5:
        errors.append("fewer than five planned payload TX events")
    for rec in tx[:3]:
        if not rec.get("frame"):
            errors.append("TX record missing exact frame")
            break
        if not str(rec.get("write_result", "")).startswith("ok:"):
            errors.append(f"TX write result not ok: {rec.get('write_result')}")
            break
        for key in ("write_start_utc", "write_start_mono_ns", "write_end_utc", "write_end_mono_ns"):
            if rec.get(key) is None:
                errors.append(f"TX record missing {key}")
                break

    rx = all_events("rx")
    if not any(r.get("line") == "OK" and r.get("ack_tx_id") for r in rx):
        errors.append("no raw OK line with FIFO ack mapping")
    if not any("DIAG epd refresh done" in str(r.get("line", "")) for r in rx):
        errors.append("no raw DIAG epd refresh done line")
    for rec in records:
        try:
            datetime.fromisoformat(rec["utc"])
        except Exception:
            errors.append(f"bad UTC timestamp: {rec.get('utc')!r}")
            break
        if not isinstance(rec.get("mono_ns"), int) or rec["mono_ns"] <= 0:
            errors.append("bad monotonic timestamp")
            break

    if first("close") is None:
        errors.append("no close event")
    if first("stop") is None:
        errors.append("no stop event")

    if errors:
        print("SELF-TEST FAIL")
        for err in errors:
            print(" -", err)
        return 1
    print("SELF-TEST PASS")
    print(f"  log: {args.log}")
    print(f"  simulated interval prev done -> target planes: {wc['interval_prev_done_to_target_planes_ns'] / 1e9:.6f} s")
    print(f"  target: tx {wc['target_tx_id']} time={wc['target_time']!r} cpu={wc['target_cpu']} mem={wc['target_mem']}")
    print(f"  distinct payloads in wait: {wc['distinct_payload_count']}  frames: {len(all_events('frame_done'))}")
    return 0


def run_real(args) -> int:
    if serial is None:
        print("pyserial is not installed", file=sys.stderr)
        return 4
    transport = SerialTransport(args.port)
    log = EventLog(args.log, args.transcript, time.monotonic_ns, datetime.now)
    cap = Capture(transport, log, args, mode="real")
    return cap.run()


def parse_args(argv: list[str] | None = None):
    ap = argparse.ArgumentParser(description="single-owner serial refresh-window capture")
    ap.add_argument("port", nargs="?", help="COM port, e.g. COM3 (omit with --self-test)")
    ap.add_argument("--port", dest="port_opt", help="alternate way to pass the COM port")
    ap.add_argument("--log", type=Path, required=True, help="JSONL event log (raw, exact)")
    ap.add_argument("--transcript", type=Path, help="human-readable transcript (default: next to --log)")
    ap.add_argument("--stop-file", type=Path, help="stop gracefully when this file appears")
    ap.add_argument("--crash-file", type=Path, help="write a traceback here on an uncaught error")
    ap.add_argument("--max-total-seconds", type=float, default=2400.0, help="hard process lifetime bound")
    ap.add_argument("--max-hold-seconds", type=float, default=1800.0, help="bounded hold after target frame")
    ap.add_argument("--self-test", action="store_true", help="run the fake-board self-test; no device is opened")
    args = ap.parse_args(argv)
    if not args.port:
        args.port = args.port_opt
    if args.self_test and not args.port:
        args.port = "SIM"
    if not args.self_test and not args.port:
        ap.error("port is required for a real run")
    if args.transcript is None:
        args.transcript = args.log.with_suffix(".log")
    if args.crash_file is None:
        args.crash_file = args.log.with_suffix(".crash.txt")
    if args.max_total_seconds <= 0 or args.max_hold_seconds <= 0:
        ap.error("time bounds must be positive")
    if args.max_hold_seconds > args.max_total_seconds:
        ap.error("max-hold-seconds must not exceed max-total-seconds")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.self_test:
        return run_self_test(args)
    return run_real(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException:
        tb = traceback.format_exc()
        if sys.stderr is not None:
            sys.stderr.write(tb)
            sys.stderr.flush()
        # Best effort: derive the crash path from --log if present.
        crash = None
        try:
            idx = sys.argv.index("--log")
            crash = Path(sys.argv[idx + 1]).with_suffix(".crash.txt")
        except Exception:
            crash = Path(".venv/p40-window.crash.txt")
        try:
            crash.parent.mkdir(parents=True, exist_ok=True)
            crash.write_text(tb, encoding="utf-8")
        except Exception:
            pass
        raise
