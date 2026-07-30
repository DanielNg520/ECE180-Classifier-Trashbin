"""Tests for the manual motor CLI (deploy/motor_cli.py).

No board and no ssh: subprocess.run is stubbed, so what's under test is the
argument validation, the ssh/curl command we build, and the REPL parsing.
"""
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import motor_cli  # noqa: E402


def _proc(stdout, returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def _ok(**extra):
    payload = {"ok": True}
    payload.update(extra)
    return _proc(json.dumps(payload))


class CommandBuildingTest(unittest.TestCase):
    def test_step_posts_absolute_position(self):
        with mock.patch("subprocess.run", return_value=_ok(landed=800)) as run:
            self.assertEqual(motor_cli.step(800), 800)
        argv = run.call_args[0][0]
        self.assertEqual(argv[0], "ssh")
        remote = argv[-1]
        self.assertIn("/step", remote)
        self.assertIn('{"step":800}', remote)

    def test_get_has_no_post_flags(self):
        with mock.patch("subprocess.run", return_value=_ok(step=400)) as run:
            self.assertEqual(motor_cli.pos(), 400)
        remote = run.call_args[0][0][-1]
        self.assertIn("/pos", remote)
        self.assertNotIn("POST", remote)

    def test_ssh_target_port_and_jump(self):
        with mock.patch("subprocess.run", return_value=_ok(landed=0)) as run:
            motor_cli.step(0, ssh="arduino@localhost", port=2222, jump="root@droplet")
        argv = run.call_args[0][0]
        self.assertIn("arduino@localhost", argv)
        self.assertEqual(argv[argv.index("-p") + 1], "2222")
        self.assertEqual(argv[argv.index("-J") + 1], "root@droplet")

    def test_bin_maps_to_step_multiple(self):
        with mock.patch("subprocess.run", return_value=_ok(landed=800)) as run:
            motor_cli.goto_bin(2)
        self.assertIn('{"step":800}', run.call_args[0][0][-1])

    def test_servo_posts_angle(self):
        with mock.patch("subprocess.run", return_value=_ok(landed=160)) as run:
            self.assertEqual(motor_cli.servo(160), 160)
        self.assertIn('{"angle":160}', run.call_args[0][0][-1])


class ValidationTest(unittest.TestCase):
    def test_step_range_is_rejected_locally(self):
        with mock.patch("subprocess.run") as run:
            for bad in (-1, 1601, 5000):
                with self.assertRaises(motor_cli.MotorError):
                    motor_cli.step(bad)
            run.assert_not_called()

    def test_step_accepts_both_ends(self):
        with mock.patch("subprocess.run", return_value=_ok(landed=0)):
            motor_cli.step(0)
            motor_cli.step(1600)   # a full turn wraps to 0 on the MCU

    def test_servo_and_bin_ranges(self):
        with mock.patch("subprocess.run") as run:
            with self.assertRaises(motor_cli.MotorError):
                motor_cli.servo(181)
            with self.assertRaises(motor_cli.MotorError):
                motor_cli.goto_bin(4)
            run.assert_not_called()

    def test_minus_one_from_mcu_is_an_error(self):
        # The MCU reports -1 for a refused move (e.g. servo compiled out).
        with mock.patch("subprocess.run", return_value=_ok(landed=-1)):
            with self.assertRaises(motor_cli.MotorError):
                motor_cli.servo(90)

    def test_unreachable_app_is_reported(self):
        with mock.patch("subprocess.run", return_value=_proc("")):
            with self.assertRaisesRegex(motor_cli.MotorError, "Trashbin Motor"):
                motor_cli.step(100)

    def test_ssh_failure_is_reported(self):
        with mock.patch("subprocess.run", return_value=_proc("", 255, "no route")):
            with self.assertRaisesRegex(motor_cli.MotorError, "ssh failed"):
                motor_cli.step(100)

    def test_non_json_reply(self):
        with mock.patch("subprocess.run", return_value=_proc("<html>nope")):
            with self.assertRaisesRegex(motor_cli.MotorError, "non-JSON"):
                motor_cli.step(100)


class ReplParsingTest(unittest.TestCase):
    def setUp(self):
        self.conn = {}

    def test_bare_number_is_an_absolute_step(self):
        with mock.patch("subprocess.run", return_value=_ok(landed=800)) as run:
            out = motor_cli._run_line("800", self.conn)
        self.assertIn('{"step":800}', run.call_args[0][0][-1])
        self.assertIn("step 800", out)
        self.assertIn("bin 2", out)      # 800 pulses == bin 2 exactly

    def test_signed_number_is_a_relative_nudge(self):
        with mock.patch("subprocess.run", return_value=_ok(landed=850)) as run:
            motor_cli._run_line("+50", self.conn)
        self.assertIn('{"delta":50}', run.call_args[0][0][-1])
        with mock.patch("subprocess.run", return_value=_ok(landed=750)) as run:
            motor_cli._run_line("-50", self.conn)
        self.assertIn('{"delta":-50}', run.call_args[0][0][-1])

    def test_named_commands(self):
        with mock.patch("subprocess.run", return_value=_ok(landed=1)) as run:
            motor_cli._run_line("sort 1", self.conn)
        self.assertIn("/sort", run.call_args[0][0][-1])
        # `home` moves, then reads back the position, so both replies need a step.
        with mock.patch("subprocess.run", return_value=_ok(landed=0, step=0)) as run:
            motor_cli._run_line("home", self.conn)
        self.assertIn("/pos", run.call_args[0][0][-1])          # the read-back
        self.assertIn("/home", run.call_args_list[0][0][0][-1])  # the move

    def test_gibberish_raises_value_error(self):
        with self.assertRaises(ValueError):
            motor_cli._run_line("wiggle it", {})


class DescribeTest(unittest.TestCase):
    def test_bin_boundaries_are_exact(self):
        self.assertIn("bin 0", motor_cli.describe(0))
        self.assertIn("bin 1", motor_cli.describe(400))
        self.assertIn("bin 3", motor_cli.describe(1200))

    def test_off_bin_shows_the_offset(self):
        self.assertIn("bin 2 +25", motor_cli.describe(825))

    def test_degrees(self):
        self.assertIn("180.0 deg", motor_cli.describe(800))


if __name__ == "__main__":
    unittest.main()
