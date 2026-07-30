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

    def test_step_forwards_absolute_position(self):
        self.assertEqual(main.do_step(800), 800)
        self.assertEqual(_Bridge.calls, [("step", 800)])

    def test_step_range_is_validated_before_the_bridge(self):
        for bad in (-1, 1601):
            with self.assertRaises(ValueError):
                main.do_step(bad)
        self.assertEqual(_Bridge.calls, [])

    def test_step_accepts_both_ends_of_the_revolution(self):
        main.do_step(0)
        main.do_step(main.STEPS_PER_REV)
        self.assertEqual(_Bridge.calls, [("step", 0), ("step", 1600)])

    def test_nudge_passes_signed_delta(self):
        main.do_nudge(-50)
        self.assertEqual(_Bridge.calls, [("nudge", -50)])

    def test_nudge_beyond_one_revolution_is_rejected(self):
        with self.assertRaises(ValueError):
            main.do_nudge(2000)
        self.assertEqual(_Bridge.calls, [])

    def test_servo_angle_validated(self):
        main.do_servo(160)
        self.assertEqual(_Bridge.calls, [("servo", 160)])
        with self.assertRaises(ValueError):
            main.do_servo(181)

    def test_home_and_pos_take_no_argument(self):
        main.do_home()
        main.do_pos()
        self.assertEqual(_Bridge.calls, [("home", 0), ("pos", 0)])

    def test_routes_cover_both_motors(self):
        self.assertEqual(
            set(main._POST_ROUTES),
            {"/sort", "/step", "/nudge", "/servo", "/home", "/zero", "/release"},
        )

    def test_serialized_by_lock(self):
        # The module holds a single lock so concurrent RPCs can't overlap.
        import threading

        self.assertIsInstance(main._lock, type(threading.Lock()))


if __name__ == "__main__":
    unittest.main()
