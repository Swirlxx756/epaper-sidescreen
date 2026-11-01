"""Demo contract/isolation/lifecycle tests. All serial devices are simulated."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dashboard_demo as demo
from test_host import FakeSerial, eventually


class DemoTests(unittest.TestCase):
    def test_v1_complete_snapshot_contract(self):
        # firmware/README.md §6 and main.cpp parseStats: all visible fields,
        # legal types, non-null GPU (otherwise its rows would be hidden).
        wire = demo.host.encode(demo.sample_stats())
        self.assertTrue(wire.endswith(b"\n"))
        self.assertEqual(wire.count(b"\n"), 1)
        self.assertLessEqual(len(wire), 1024)
        msg = json.loads(wire)
        self.assertEqual((msg.pop("v"), msg.pop("type")), (1, "stats"))
        for key in ("cpu", "mem", "disk", "gpu"):
            value = msg.pop(key)
            self.assertIs(type(value), int)
            self.assertTrue(0 <= value <= 100)
        self.assertEqual(msg.pop("gpu_temp"), 52)  # numeric -> 52°C
        self.assertEqual(msg.pop("time"), "DEMO 12:34")
        self.assertEqual(msg.pop("net_up"), "120K")
        self.assertEqual(msg.pop("net_down"), "3.4M")
        tok = msg.pop("tok")
        self.assertFalse(msg)  # no extension fields needed for this layout
        for key in ("block_tokens", "burn_tpm", "today_tokens"):
            self.assertRegex(tok.pop(key), r"^\d\.\d[KM]$")
        for key in ("block_cost", "today_cost"):
            self.assertIs(type(tok.pop(key)), float)
        self.assertEqual(tok.pop("remain_min"), 135)  # numeric -> 2h15m
        self.assertFalse(tok)

    def test_dry_run_uses_no_collectors_config_or_link(self):
        output = io.StringIO()
        with contextlib.ExitStack() as stack:
            for name in ("SystemCollector", "TokscaleSource", "CcusageSource", "Link", "load_config"):
                stack.enter_context(patch.object(demo.host, name, side_effect=AssertionError(name)))
            stack.enter_context(patch.object(demo.host, "serial", None))
            stack.enter_context(contextlib.redirect_stdout(output))
            self.assertEqual(demo.main(["--dry-run"]), 0)
        self.assertEqual(json.loads(output.getvalue()), demo.sample_stats())

    def test_dry_run_in_python_without_site_packages(self):
        # -S excludes psutil/pyserial. Forbid tool execution and psutil import
        # explicitly too; this checks a fresh process, including module startup.
        script = Path(demo.__file__).resolve()
        code = """
import builtins, runpy, subprocess, sys
original_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name == 'psutil':
        raise AssertionError('demo imported psutil')
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded_import
def forbidden(*args, **kwargs):
    raise AssertionError('demo executed an external tool')
subprocess.Popen = forbidden
sys.path.insert(0, sys.argv[1])
sys.argv = [sys.argv[2], '--dry-run']
runpy.run_path(sys.argv[0], run_name='__main__')
"""
        result = subprocess.run([sys.executable, "-S", "-c", code, str(script.parent), str(script)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), demo.sample_stats())

    def test_continuous_send_and_ctrl_c_release_serial(self):
        for respond in (True, False):
            with self.subTest(respond=respond):
                device = FakeSerial(respond=respond)
                sleeps = []

                def sleep(seconds):
                    sleeps.append(seconds)
                    if len(sleeps) == 3:
                        if respond:
                            eventually(lambda: device.rx.empty())
                        raise KeyboardInterrupt

                with patch.object(demo.host, "serial", SimpleNamespace(
                    Serial=lambda: device, SerialException=OSError
                )), patch.object(demo.time, "sleep", side_effect=sleep), patch.object(
                    demo.host, "SystemCollector", side_effect=AssertionError("collector")
                ), patch.object(demo.host, "TokscaleSource", side_effect=AssertionError("tokens")):
                    self.assertEqual(demo.main(["--port", "COM_TEST"]), 0)
                self.assertEqual(device.writes, [{"v": 1, "type": "ping"}] + [demo.sample_stats()] * 3)
                self.assertEqual((device.port, device.baudrate), ("COM_TEST", 115200))
                self.assertFalse(device.dtr)
                self.assertFalse(device.rts)
                self.assertTrue(all(0 < delay <= 10 for delay in sleeps))
                self.assertFalse(device.is_open)
                self.assertGreaterEqual(device.closed, 1)
                self.assertFalse(any(t.name == "serial-rx" for t in threading.enumerate()))

    def test_missing_serial_dependency_fails_before_open(self):
        with patch.object(demo.host, "serial", None), patch.object(demo.host, "Link") as link:
            self.assertEqual(demo.main(["--port", "COM_TEST"]), 1)
        link.assert_not_called()


if __name__ == "__main__":
    unittest.main()
