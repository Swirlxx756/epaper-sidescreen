#!/usr/bin/env python3
"""Fixed protocol-v1 dashboard demo. No collectors, accounts or config file."""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

import epaper_host as host

INTERVAL = 10  # below firmware's 60-second offline timeout; not refresh timing


def sample_stats() -> dict:
    """Return a fresh complete snapshot, with a visible demo label in the header."""
    return {
        "v": 1, "type": "stats", "time": "DEMO 12:34",
        "cpu": 32, "mem": 61, "disk": 72,
        "net_up": "120K", "net_down": "3.4M", "gpu": 92, "gpu_temp": 52,
        "tok": {
            "block_tokens": "1.2M", "block_cost": 4.56,
            "burn_tpm": "3.3K", "remain_min": 135,
            "today_tokens": "2.4M", "today_cost": 8.90,
        },
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="print one JSON line and exit; no serial")
    mode.add_argument("--port", help="send continuously to this explicit port, e.g. COM5")
    mode.add_argument("--list-ports", action="store_true", help="enumerate ports without opening them")
    ap.add_argument("--log", type=Path, help="also write diagnostics to this file")
    args = ap.parse_args(argv)

    handlers = [logging.StreamHandler(sys.stderr)]
    if args.log:
        handlers.append(logging.FileHandler(args.log, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S", handlers=handlers)
    if args.dry_run:
        print(host.encode(sample_stats()).decode("utf-8"), end="", flush=True)
        return 0
    if args.list_ports:
        if host.list_ports is None:
            host.log.error("pyserial is not installed: python -m pip install pyserial>=3.5")
            return 1
        return host.cmd_list_ports()
    if host.serial is None:
        host.log.error("pyserial is not installed: python -m pip install pyserial>=3.5")
        return 1

    host.log.info("DEMO fixed examples only; Ctrl+C to stop; display unverified")
    link = host.Link(args.port, host.BAUD)
    next_retry = 0.0
    try:
        while True:
            started = time.monotonic()
            if not link.connected and started >= next_retry:
                link.connect()  # existing ping, RX reader, OK/ERR and no-reset policy
                next_retry = time.monotonic() + 10
            link.send(sample_stats())
            link.diagnose()
            time.sleep(max(0.0, INTERVAL - (time.monotonic() - started)))
    except KeyboardInterrupt:
        host.log.info("demo stopped")
        return 0
    finally:
        link.close()


if __name__ == "__main__":
    sys.exit(main())
