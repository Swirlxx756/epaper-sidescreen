#!/usr/bin/env python3
"""Read the Rapoo (VT1 MAX, 2.4G receiver) wireless mouse battery level.

Read-only: the module only calls ReadFile on an input-report handle opened
with GENERIC_READ and shared read/write. It never sends any report or feature
to the receiver or the mouse, and it does not open the serial port.

  py -3.14 -m host.mouse_rapoo --probe 30
  py -3.14 host/mouse_rapoo.py --probe 30
"""
from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from dataclasses import dataclass

log = logging.getLogger("mouse_rapoo")

VENDOR_ID = 0x24AE
PRODUCT_ID = 0x1444
USAGE_PAGE = 0xFF00
USAGE = 0x02

DEFAULT_REPORT_ID = 0x07
DEFAULT_OFFSET = 8   # 0x30 = 48 %, matched against TrafficMonitor on 2026-10-28
CHARGE_OFFSET = 7    # power state, compared plugged / unplugged on 2026-11-05
CHARGE_STATES = {0x01: False, 0x02: True}  # 01 = on battery, 02 = charging; anything else = unknown
RETRY_INTERVAL = 5.0
PROBE_BYTES = (8, 13, 14)  # candidate battery bytes printed by --probe


@dataclass(frozen=True)
class MouseReading:
    pct: int | None = None
    charging: bool | None = None
    observed_at: float | None = None
    status: str = "na"  # "ok" | "off" | "na"
    error: str | None = None


def parse_report(data: bytes, report_id: int = DEFAULT_REPORT_ID,
                 offset: int = DEFAULT_OFFSET) -> int | None:
    """Battery percent from one input report, or None if it does not apply."""
    if not data or offset < 1 or len(data) <= offset:
        return None
    if data[0] != report_id:
        return None
    pct = data[offset]
    if pct > 100:
        return None
    return pct


def parse_charging(data: bytes, report_id: int = DEFAULT_REPORT_ID,
                   offset: int = CHARGE_OFFSET) -> bool | None:
    """True = charging, False = on battery, None = not this report or an unknown state byte."""
    if not data or len(data) <= offset or data[0] != report_id:
        return None
    return CHARGE_STATES.get(data[offset])


# --------------------------------------------------------------------------
# HID backends. A backend only enumerates and reads; nothing here writes.
# --------------------------------------------------------------------------

class NullBackend:
    """Used on non-Windows platforms: never finds a receiver."""

    def list_collections(self) -> list[dict]:
        return []

    def find(self) -> tuple[str, int] | None:
        return None

    def open(self, path: str):  # pragma: no cover - never reached
        raise OSError("unsupported platform")

    def read(self, handle, size: int) -> bytes:  # pragma: no cover
        raise OSError("unsupported platform")

    def cancel(self, handle) -> None:
        pass

    def close(self, handle) -> None:
        pass


class WindowsHidBackend:
    """ctypes over hid.dll / setupapi.dll / kernel32.dll."""

    GENERIC_READ = 0x80000000
    FILE_SHARE_RW = 0x3
    OPEN_EXISTING = 3

    def __init__(self, vid: int = VENDOR_ID, usage_page: int = USAGE_PAGE, usage: int = USAGE):
        import ctypes as C
        import ctypes.wintypes as W

        self.C, self.W = C, W
        self.vid, self.usage_page, self.usage = vid, usage_page, usage
        self.hid = C.WinDLL("hid")
        self.setupapi = C.WinDLL("setupapi")
        self.k32 = C.WinDLL("kernel32", use_last_error=True)
        self.invalid = W.HANDLE(-1).value

        class GUID(C.Structure):
            _fields_ = [("Data1", W.DWORD), ("Data2", W.WORD), ("Data3", W.WORD),
                        ("Data4", C.c_ubyte * 8)]

        class SP_DEVICE_INTERFACE_DATA(C.Structure):
            _fields_ = [("cbSize", W.DWORD), ("InterfaceClassGuid", GUID),
                        ("Flags", W.DWORD), ("Reserved", C.c_void_p)]

        class HIDD_ATTRIBUTES(C.Structure):
            _fields_ = [("Size", W.ULONG), ("VendorID", W.USHORT),
                        ("ProductID", W.USHORT), ("VersionNumber", W.USHORT)]

        class HIDP_CAPS(C.Structure):
            _fields_ = [("Usage", W.USHORT), ("UsagePage", W.USHORT),
                        ("InputReportByteLength", W.USHORT),
                        ("OutputReportByteLength", W.USHORT),
                        ("FeatureReportByteLength", W.USHORT),
                        ("Reserved", W.USHORT * 17), ("rest", W.USHORT * 10)]

        self.GUID, self.SDID = GUID, SP_DEVICE_INTERFACE_DATA
        self.ATTR, self.CAPS = HIDD_ATTRIBUTES, HIDP_CAPS

        sa, k32 = self.setupapi, self.k32
        sa.SetupDiGetClassDevsW.restype = C.c_void_p
        sa.SetupDiGetClassDevsW.argtypes = [C.POINTER(GUID), C.c_wchar_p, C.c_void_p, W.DWORD]
        sa.SetupDiEnumDeviceInterfaces.argtypes = [C.c_void_p, C.c_void_p, C.POINTER(GUID),
                                                   W.DWORD, C.POINTER(SP_DEVICE_INTERFACE_DATA)]
        sa.SetupDiGetDeviceInterfaceDetailW.argtypes = [C.c_void_p, C.POINTER(SP_DEVICE_INTERFACE_DATA),
                                                        C.c_void_p, W.DWORD, C.POINTER(W.DWORD),
                                                        C.c_void_p]
        sa.SetupDiDestroyDeviceInfoList.argtypes = [C.c_void_p]
        self.hid.HidD_GetAttributes.argtypes = [W.HANDLE, C.POINTER(HIDD_ATTRIBUTES)]
        self.hid.HidD_GetPreparsedData.argtypes = [W.HANDLE, C.POINTER(C.c_void_p)]
        self.hid.HidD_FreePreparsedData.argtypes = [C.c_void_p]
        self.hid.HidP_GetCaps.argtypes = [C.c_void_p, C.POINTER(HIDP_CAPS)]
        k32.CreateFileW.restype = W.HANDLE
        k32.CreateFileW.argtypes = [C.c_wchar_p, W.DWORD, W.DWORD, C.c_void_p, W.DWORD,
                                    W.DWORD, W.HANDLE]
        k32.ReadFile.argtypes = [W.HANDLE, C.c_void_p, W.DWORD, C.POINTER(W.DWORD), C.c_void_p]
        k32.CancelIoEx.argtypes = [W.HANDLE, C.c_void_p]
        k32.CloseHandle.argtypes = [W.HANDLE]

    def _paths(self):
        C, W = self.C, self.W
        guid = self.GUID()
        self.hid.HidD_GetHidGuid(C.byref(guid))
        info = self.setupapi.SetupDiGetClassDevsW(C.byref(guid), None, None, 0x12)  # PRESENT|DEVICEINTERFACE
        if info in (None, self.invalid):
            return
        try:
            index = 0
            while True:
                data = self.SDID()
                data.cbSize = C.sizeof(data)
                if not self.setupapi.SetupDiEnumDeviceInterfaces(info, None, C.byref(guid), index, C.byref(data)):
                    break
                need = W.DWORD()
                self.setupapi.SetupDiGetDeviceInterfaceDetailW(info, C.byref(data), None, 0, C.byref(need), None)
                buf = C.create_string_buffer(need.value)
                C.cast(buf, C.POINTER(W.DWORD))[0] = 8 if C.sizeof(C.c_void_p) == 8 else 6
                if self.setupapi.SetupDiGetDeviceInterfaceDetailW(info, C.byref(data), buf, need, None, None):
                    yield C.wstring_at(C.addressof(buf) + 4)
                index += 1
        finally:
            self.setupapi.SetupDiDestroyDeviceInfoList(info)

    def list_collections(self) -> list[dict]:
        """All HID collections of the vendor ID (metadata only, access 0)."""
        C = self.C
        out = []
        for path in self._paths():
            if f"vid_{self.vid:04x}" not in path.lower():
                continue
            h = self.k32.CreateFileW(path, 0, self.FILE_SHARE_RW, None, self.OPEN_EXISTING, 0, None)
            if h in (None, self.invalid):
                continue
            try:
                attr = self.ATTR()
                attr.Size = C.sizeof(attr)
                self.hid.HidD_GetAttributes(h, C.byref(attr))
                caps = self.CAPS()
                pp = C.c_void_p()
                if self.hid.HidD_GetPreparsedData(h, C.byref(pp)):
                    self.hid.HidP_GetCaps(pp, C.byref(caps))
                    self.hid.HidD_FreePreparsedData(pp)
                out.append({
                    "path": path, "pid": attr.ProductID,
                    "usage_page": caps.UsagePage, "usage": caps.Usage,
                    "input_len": caps.InputReportByteLength,
                })
            finally:
                self.k32.CloseHandle(h)
        return out

    def find(self) -> tuple[str, int] | None:
        for c in self.list_collections():
            if (c["usage_page"] == self.usage_page and c["usage"] == self.usage
                    and c["input_len"] > 0):
                return c["path"], c["input_len"]
        return None

    def open(self, path: str):
        h = self.k32.CreateFileW(path, self.GENERIC_READ, self.FILE_SHARE_RW, None,
                                 self.OPEN_EXISTING, 0, None)
        if h in (None, self.invalid):
            raise OSError(f"CreateFileW failed (err={self.C.get_last_error()})")
        return h

    def read(self, handle, size: int) -> bytes:
        buf = self.C.create_string_buffer(size)
        got = self.W.DWORD()
        if not self.k32.ReadFile(handle, buf, size, self.C.byref(got), None):
            raise OSError(f"ReadFile failed (err={self.C.get_last_error()})")
        return buf.raw[: got.value]

    def cancel(self, handle) -> None:
        self.k32.CancelIoEx(handle, None)

    def close(self, handle) -> None:
        self.k32.CloseHandle(handle)


def default_backend():
    if sys.platform == "win32":
        return WindowsHidBackend()
    return NullBackend()


# --------------------------------------------------------------------------
# Reader
# --------------------------------------------------------------------------

class RapooMouse:
    def __init__(self, battery_offset: int = DEFAULT_OFFSET, report_id: int = DEFAULT_REPORT_ID,
                 off_after: float = 30.0, *, backend=None, clock=time.time,
                 retry_interval: float = RETRY_INTERVAL, on_report=None):
        self.battery_offset = battery_offset
        self.report_id = report_id
        self.off_after = off_after
        self._backend = backend if backend is not None else default_backend()
        self._clock = clock
        self._retry = retry_interval
        self._on_report = on_report
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._handle = None
        self._found = False
        self._pct: int | None = None
        self._charging: bool | None = None
        self._observed_at: float | None = None
        self._error: str | None = None
        self._logged_error: str | None = None

    # -- public ----------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        if isinstance(self._backend, NullBackend):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="rapoo-mouse", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is None:
            return
        deadline = time.monotonic() + timeout
        while thread.is_alive() and time.monotonic() < deadline:
            with self._lock:
                handle = self._handle
            if handle is not None:
                try:
                    self._backend.cancel(handle)
                except Exception:  # noqa: BLE001
                    pass
            thread.join(0.1)
        self._thread = None

    def snapshot(self) -> MouseReading:
        with self._lock:
            now = self._clock()
            if not self._found:
                return MouseReading(None, None, None, "na", self._error)
            if self._observed_at is None or now - self._observed_at > self.off_after:
                return MouseReading(self._pct, None, self._observed_at, "off", self._error)
            return MouseReading(self._pct, self._charging, self._observed_at, "ok", self._error)

    # -- internals -------------------------------------------------------
    def _note_error(self, message: str) -> None:
        with self._lock:
            self._error = message
        if message != self._logged_error:
            self._logged_error = message
            log.warning("rapoo mouse: %s", message)

    def _set_found(self, found: bool) -> None:
        with self._lock:
            self._found = found

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                target = self._backend.find()
            except Exception as exc:  # noqa: BLE001
                target = None
                self._note_error(f"enumerate failed: {exc}")
            if target is None:
                self._set_found(False)
                with self._lock:
                    if self._error is None:
                        self._error = "receiver not found"
                self._stop.wait(self._retry)
                continue
            path, size = target
            try:
                handle = self._backend.open(path)
            except Exception as exc:  # noqa: BLE001
                self._set_found(False)
                self._note_error(f"open failed: {exc}")
                self._stop.wait(self._retry)
                continue
            with self._lock:
                self._handle = handle
                self._found = True
                self._error = None
            self._logged_error = None
            try:
                self._read_loop(handle, size)
            finally:
                with self._lock:
                    self._handle = None
                    self._found = False
                try:
                    self._backend.close(handle)
                except Exception:  # noqa: BLE001
                    pass
            if not self._stop.is_set():
                self._stop.wait(self._retry)

    def _read_loop(self, handle, size: int) -> None:
        while not self._stop.is_set():
            try:
                data = self._backend.read(handle, size)
            except Exception as exc:  # noqa: BLE001
                if not self._stop.is_set():
                    self._note_error(f"read failed: {exc}")
                return
            if not data:
                continue
            now = self._clock()
            if self._on_report is not None:
                try:
                    self._on_report(bytes(data), now)
                except Exception:  # noqa: BLE001
                    log.exception("on_report callback failed")
            pct = parse_report(data, self.report_id, self.battery_offset)
            if pct is None:
                continue
            with self._lock:
                self._pct = pct
                self._charging = parse_charging(data, self.report_id)
                self._observed_at = now


# --------------------------------------------------------------------------
# Probe CLI
# --------------------------------------------------------------------------

def probe(seconds: float, backend=None, out=print) -> dict:
    backend = backend if backend is not None else default_backend()
    cols = backend.list_collections()
    out(f"HID collections for VID {VENDOR_ID:04X}: {len(cols)}")
    for c in cols:
        out(f"  pid={c['pid']:04X} page=0x{c['usage_page']:04X} usage=0x{c['usage']:02X} "
            f"in={c['input_len']} {c['path'].lower().split('#')[1] if '#' in c['path'] else c['path']}")
    seen: dict[int, list[int]] = {}
    count = [0]

    def on_report(data: bytes, t: float) -> None:
        count[0] += 1
        vals = {idx: (data[idx] if len(data) > idx else None) for idx in PROBE_BYTES}
        for idx, v in vals.items():
            if v is not None:
                seen.setdefault(idx, []).append(v)
        out(f"{time.strftime('%H:%M:%S', time.localtime(t))} len={len(data)} "
            f"{data.hex(' ')}  " + " ".join(f"b{i}={v}" for i, v in vals.items()))

    mouse = RapooMouse(backend=backend, on_report=on_report)
    out(f"listening for {seconds:.0f}s (read-only)...")
    mouse.start()
    end = time.monotonic() + seconds
    try:
        while time.monotonic() < end:
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        mouse.stop()
    out(f"reports: {count[0]}")
    for idx in PROBE_BYTES:
        vals = seen.get(idx)
        if vals:
            out(f"byte {idx}: min={min(vals)} max={max(vals)} distinct={sorted(set(vals))}")
        else:
            out(f"byte {idx}: no data")
    return {"count": count[0], "bytes": seen}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rapoo mouse battery reader (read-only)")
    parser.add_argument("--probe", type=float, metavar="SECONDS", help="listen and print reports")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.probe is None:
        mouse = RapooMouse()
        mouse.start()
        try:
            time.sleep(8)
            print(mouse.snapshot())
        finally:
            mouse.stop()
        return 0
    probe(args.probe)
    return 0


if __name__ == "__main__":
    sys.exit(main())
