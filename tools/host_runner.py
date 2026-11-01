#!/usr/bin/env python3
"""Run host/epaper_host.py until a stop file appears or a maximum duration passes.

The stop is delivered as KeyboardInterrupt in the main thread so the official
host closes the serial port through its normal finally block (no extra reset).
The host source is not modified.
"""
from __future__ import annotations

import argparse
import _thread
import os
import sys
import threading
import time
from pathlib import Path


def stamp() -> str:
    t = time.time()
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) + ".%03d" % int(t * 1000 % 1000)


def say(text: str) -> None:
    if sys.stderr is not None:
        print(text, file=sys.stderr, flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run the official host with a controllable stop")
    ap.add_argument("--port", required=True)
    ap.add_argument("--log", required=True, help="passed to epaper_host.py --log")
    ap.add_argument("--stop-file", required=True)
    ap.add_argument("--max-seconds", type=float, default=7200.0)
    ap.add_argument("--host-dir", default="host")
    args = ap.parse_args(argv)

    host_dir = Path(args.host_dir).resolve()
    sys.path.insert(0, str(host_dir))
    import epaper_host  # noqa: E402

    stop = Path(args.stop_file)
    pidfile = Path(str(stop) + ".pid")
    pidfile.write_text(str(os.getpid()), encoding="ascii")
    deadline = time.time() + args.max_seconds
    say(f"{stamp()} runner pid={os.getpid()} host_dir={host_dir} max={args.max_seconds:.0f}s")

    def watch() -> None:
        while True:
            if stop.exists():
                reason = "stop file"
                break
            if time.time() >= deadline:
                reason = "max duration"
                break
            time.sleep(0.5)
        say(f"{stamp()} stop requested: {reason}; interrupting host main thread")
        _thread.interrupt_main()

    threading.Thread(target=watch, name="stop-watch", daemon=True).start()
    try:
        rc = epaper_host.main(["--port", args.port, "--log", args.log])
    except KeyboardInterrupt:
        rc = 130
    say(f"{stamp()} host returned {rc}")
    return int(rc)


if __name__ == "__main__":
    raise SystemExit(main())
