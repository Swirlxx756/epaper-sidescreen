#!/usr/bin/env python3
"""Passive serial capture for the production bring-up.

Modes (never writes data to the board):
  quiet  open with DTR/RTS inactive; PASS after SECONDS of continuous silence.
         Any received line restarts the silence timer. Progress goes to stderr.
  reset  open with DTR/RTS inactive, one 100 ms RTS pulse, then log SECONDS.
  listen open with DTR/RTS inactive, then log SECONDS without any reset.

Every received line is printed with a millisecond timestamp. The same lines are
also written to --log when given. Raw logs stay in the ignored .venv directory.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import serial


def stamp(t: float | None = None) -> str:
    t = time.time() if t is None else t
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) + ".%03d" % int(t * 1000 % 1000)


class Writer:
    def __init__(self, path: Path | None) -> None:
        self.fh = path.open("w", encoding="utf-8") if path else None

    def line(self, text: str, *, err: bool = False) -> None:
        stream = sys.stderr if err else sys.stdout
        if stream is not None:
            print(text, file=stream, flush=True)
        if self.fh is not None:
            self.fh.write(text + "\n")
            self.fh.flush()

    def close(self) -> None:
        if self.fh is not None:
            self.fh.close()
            self.fh = None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="passive serial capture")
    ap.add_argument("port", help="COM port, e.g. COM3")
    ap.add_argument("seconds", type=float, help="silence window for quiet; capture length otherwise")
    ap.add_argument("mode", choices=("quiet", "reset", "listen"))
    ap.add_argument("--log", type=Path, help="also write lines to this UTF-8 file")
    args = ap.parse_args(argv)

    out = Writer(args.log)
    s = serial.Serial()
    s.port = args.port
    s.baudrate = 115200
    s.timeout = 0.5
    s.dtr = False
    s.rts = False
    opened = time.time()
    s.open()
    out.line(f"{stamp(opened)} opened {args.port} 115200 DTR=False RTS=False, no data sent (mode={args.mode})")

    start = last = time.time()
    got = 0
    planes = done = faults = 0
    next_beat = start + 55.0
    try:
        if args.mode == "reset":
            s.rts = True
            time.sleep(0.1)
            s.rts = False
            out.line(f"{stamp()} single RTS reset (100 ms pulse)")

        while True:
            now = time.time()
            if args.mode == "quiet":
                if now - last >= args.seconds:
                    out.line(
                        f"{stamp(now)} quiet {args.seconds:.0f}s: PASS "
                        f"(continuous silence since {stamp(last)}; opened {stamp(opened)})"
                    )
                    return 0
                if now - start > max(3600.0, args.seconds * 4):
                    out.line(f"{stamp(now)} quiet: FAIL, no {args.seconds:.0f}s silence within limit", err=True)
                    return 3
                if now >= next_beat:
                    out.line(
                        f"# {stamp(now)} progress: {now - last:.0f}s since last output, "
                        f"need {args.seconds:.0f}s",
                        err=True,
                    )
                    next_beat = now + 55.0
            elif now - start >= args.seconds:
                break

            line = s.readline().decode("utf-8", "replace").rstrip("\r\n")
            if not line:
                continue
            got += 1
            last = time.time()
            if args.mode == "quiet":
                next_beat = last + 55.0
            out.line(f"{stamp(last)} {line}")
            if "DIAG epd planes" in line:
                planes += 1
            elif "DIAG epd refresh done" in line:
                done += 1
            if line.startswith("DIAG epd fault") or line.startswith("DIAG display stopped"):
                faults += 1
                out.line(f"{stamp(last)} STOP: fault/display-stopped line seen", err=True)
                return 2

        out.line(
            f"{stamp()} capture end: lines={got} planes={planes} refresh_done={done} "
            f"faults={faults} elapsed={time.time() - start:.1f}s"
        )
        return 2 if faults else 0
    finally:
        s.close()
        out.line(f"{stamp()} closed {args.port}")
        out.close()


if __name__ == "__main__":
    raise SystemExit(main())
