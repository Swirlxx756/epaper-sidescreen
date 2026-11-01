"""Software-only tests for mouse_rapoo; never touches a real HID device."""
import queue
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mouse_rapoo as m

REPORT = bytes.fromhex("07 20 00 80 0c 80 0c 01 30 00 03 01 00 44 46 10 00 01 a9")


class FakeBackend:
    def __init__(self, present=True):
        self.present = present
        self.rx = queue.Queue()
        self.cancelled = threading.Event()
        self.unplugged = threading.Event()
        self.opened = 0
        self.closed = 0
        self.writes = 0  # must stay 0: the module is read-only

    def list_collections(self):
        return []

    def find(self):
        return ("fake", 19) if self.present else None

    def open(self, path):
        self.opened += 1
        self.cancelled.clear()
        self.unplugged.clear()
        return object()

    def read(self, handle, size):
        while True:
            if self.cancelled.is_set():
                raise OSError("cancelled")
            if self.unplugged.is_set():
                raise OSError("unplugged")
            try:
                return self.rx.get(timeout=0.01)
            except queue.Empty:
                continue

    def cancel(self, handle):
        self.cancelled.set()

    def close(self, handle):
        self.closed += 1

    def unplug(self):
        self.present = False
        self.unplugged.set()


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def wait_for(cond, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return cond()


class ParseReportTests(unittest.TestCase):
    def test_real_report(self):
        self.assertEqual(m.parse_report(REPORT, 0x07, 13), 0x44)
        self.assertEqual(m.parse_report(REPORT, 0x07, m.DEFAULT_OFFSET), 48)  # byte 8 = 0x30
        self.assertEqual(m.parse_report(REPORT, 0x07, 14), 0x46)

    def test_wrong_report_id(self):
        self.assertIsNone(m.parse_report(b"\x08" + REPORT[1:], 0x07, 13))

    def test_too_short(self):
        self.assertIsNone(m.parse_report(REPORT[:13], 0x07, 13))
        self.assertIsNone(m.parse_report(b"", 0x07, 13))

    def test_over_100_discarded(self):
        data = bytearray(REPORT)
        data[13] = 101
        self.assertIsNone(m.parse_report(bytes(data), 0x07, 13))
        data[13] = 100
        self.assertEqual(m.parse_report(bytes(data), 0x07, 13), 100)
        data[13] = 0
        self.assertEqual(m.parse_report(bytes(data), 0x07, 13), 0)


class RapooMouseTests(unittest.TestCase):
    def make(self, backend, **kw):
        clock = Clock()
        mouse = m.RapooMouse(backend=backend, clock=clock, retry_interval=0.02, **kw)
        self.addCleanup(mouse.stop)
        return mouse, clock

    def test_ok_after_report(self):
        be = FakeBackend()
        mouse, clock = self.make(be)
        mouse.start()
        be.rx.put(REPORT)
        self.assertTrue(wait_for(lambda: mouse.snapshot().status == "ok"))
        snap = mouse.snapshot()
        self.assertEqual(snap.pct, 48)
        self.assertIsNone(snap.charging)
        self.assertEqual(snap.observed_at, 1000.0)

    def test_offset_14(self):
        be = FakeBackend()
        mouse, _ = self.make(be, battery_offset=14)
        mouse.start()
        be.rx.put(REPORT)
        self.assertTrue(wait_for(lambda: mouse.snapshot().pct == 70))

    def test_found_but_no_report_is_off(self):
        be = FakeBackend()
        mouse, _ = self.make(be)
        mouse.start()
        self.assertTrue(wait_for(lambda: be.opened == 1))
        self.assertTrue(wait_for(lambda: mouse.snapshot().status == "off"))
        self.assertIsNone(mouse.snapshot().pct)

    def test_ignored_reports(self):
        be = FakeBackend()
        mouse, _ = self.make(be)
        mouse.start()
        be.rx.put(b"\x08" + REPORT[1:])
        be.rx.put(REPORT[:10])
        bad = bytearray(REPORT)
        bad[13] = 200
        be.rx.put(bytes(bad))
        be.rx.put(REPORT)  # sentinel: processed after the ignored ones
        self.assertTrue(wait_for(lambda: mouse.snapshot().pct == 48))

    def test_timeout_becomes_off_keeps_last(self):
        be = FakeBackend()
        mouse, clock = self.make(be, off_after=30.0)
        mouse.start()
        be.rx.put(REPORT)
        self.assertTrue(wait_for(lambda: mouse.snapshot().status == "ok"))
        clock.t += 30.0
        self.assertEqual(mouse.snapshot().status, "ok")
        clock.t += 0.5
        snap = mouse.snapshot()
        self.assertEqual(snap.status, "off")
        self.assertEqual(snap.pct, 48)
        self.assertEqual(snap.observed_at, 1000.0)

    def test_unplug_na_then_recover(self):
        be = FakeBackend()
        mouse, _ = self.make(be)
        mouse.start()
        be.rx.put(REPORT)
        self.assertTrue(wait_for(lambda: mouse.snapshot().status == "ok"))
        be.unplug()
        self.assertTrue(wait_for(lambda: mouse.snapshot().status == "na"))
        self.assertTrue(wait_for(lambda: be.closed == 1))
        be.present = True
        be.rx.put(REPORT)
        self.assertTrue(wait_for(lambda: mouse.snapshot().status == "ok"))
        self.assertEqual(be.opened, 2)

    def test_no_receiver_is_na(self):
        be = FakeBackend(present=False)
        mouse, _ = self.make(be)
        mouse.start()
        time.sleep(0.1)
        snap = mouse.snapshot()
        self.assertEqual(snap.status, "na")
        self.assertIsNone(snap.pct)
        self.assertIsNotNone(snap.error)

    def test_stop_does_not_hang(self):
        be = FakeBackend()
        mouse, _ = self.make(be)
        mouse.start()
        self.assertTrue(wait_for(lambda: be.opened == 1))
        t0 = time.monotonic()
        mouse.stop()
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertEqual(be.closed, 1)
        self.assertEqual(be.writes, 0)

    def test_stop_while_waiting_for_receiver(self):
        be = FakeBackend(present=False)
        mouse = m.RapooMouse(backend=be, retry_interval=60)
        mouse.start()
        time.sleep(0.05)
        t0 = time.monotonic()
        mouse.stop()
        self.assertLess(time.monotonic() - t0, 1.0)

    def test_null_backend_is_na(self):
        mouse = m.RapooMouse(backend=m.NullBackend())
        mouse.start()
        self.assertEqual(mouse.snapshot().status, "na")
        mouse.stop()

    def test_on_report_callback(self):
        be = FakeBackend()
        got = []
        mouse, _ = self.make(be, on_report=lambda d, t: got.append(d))
        mouse.start()
        be.rx.put(REPORT)
        self.assertTrue(wait_for(lambda: got == [REPORT]))


class ProbeTests(unittest.TestCase):
    def test_probe_summary(self):
        be = FakeBackend()
        be.list_collections = lambda: [
            {"path": r"\\?\hid#vid_24ae&pid_1444&mi_01&col09#x", "pid": 0x1444,
             "usage_page": 0xFF00, "usage": 2, "input_len": 19}]
        be.rx.put(REPORT)
        lines = []
        result = m.probe(0.3, backend=be, out=lines.append)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["bytes"][13], [0x44])
        self.assertTrue(any("byte 14: min=70 max=70" in line for line in lines))


if __name__ == "__main__":
    unittest.main()
