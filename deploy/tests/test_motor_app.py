"""Seam test for the motor App's Linux side: HTTP handler -> Bridge RPC.

Stubs arduino.app_utils so main.py imports without the App Lab runtime. The
module-level server start is guarded under __main__, so import is side-effect
free (no port bind, no App.run()).
"""
import os
import sys
import types
import unittest

_PY_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "motor_app", "python"
)
sys.path.insert(0, _PY_DIR)


class _Bridge:
    calls = []

    @staticmethod
    def call(name, arg):
        _Bridge.calls.append((name, arg))
        return arg  # MCU echoes the bin it landed on


class _App:
    @staticmethod
    def run():
        raise AssertionError("App.run() must not run at import time")


def _install_fake_runtime():
    pkg = types.ModuleType("arduino")
    utils = types.ModuleType("arduino.app_utils")
    utils.App = _App
    utils.Bridge = _Bridge
    pkg.app_utils = utils
    sys.modules["arduino"] = pkg
    sys.modules["arduino.app_utils"] = utils


_install_fake_runtime()
import main  # noqa: E402


class DoSortTest(unittest.TestCase):
    def setUp(self):
        _Bridge.calls = []

    def test_forwards_bin_to_bridge(self):
        landed = main.do_sort(3)
        self.assertEqual(landed, 3)
        self.assertEqual(_Bridge.calls, [("sort", 3)])

    def test_coerces_to_int(self):
        main.do_sort("2")
        self.assertEqual(_Bridge.calls, [("sort", 2)])

    def test_serialized_by_lock(self):
        # The module holds a single lock so concurrent RPCs can't overlap.
        import threading

        self.assertIsInstance(main._lock, type(threading.Lock()))


if __name__ == "__main__":
    unittest.main()
