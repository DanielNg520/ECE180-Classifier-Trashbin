"""Seam tests for camera_loop: motion-diff helper + classify->sort actuation.

cv2 isn't installed on the dev machine, so a minimal fake is injected before
import. Only the numpy/decision logic is exercised, not real capture.
"""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np


def _install_fake_cv2():
    m = types.ModuleType("cv2")
    m.COLOR_BGR2RGB = 4
    m.COLOR_BGR2GRAY = 6
    m.CAP_PROP_FRAME_WIDTH = 3
    m.CAP_PROP_FRAME_HEIGHT = 4

    def cvtColor(a, code):
        if code == m.COLOR_BGR2GRAY:
            return a[..., 0].copy()  # 2D gray
        return a[..., ::-1].copy()  # channel swap

    def resize(a, size):
        w, h = size
        if a.ndim == 3:
            return np.zeros((h, w, a.shape[2]), dtype=a.dtype)
        return np.zeros((h, w), dtype=a.dtype)

    m.cvtColor = cvtColor
    m.resize = resize
    m.VideoCapture = object
    sys.modules["cv2"] = m


_install_fake_cv2()
import camera_loop as cl


class GraySmallTest(unittest.TestCase):
    def test_shape_and_dtype(self):
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        g = cl._gray_small(frame)
        self.assertEqual(g.shape, (120, 160))
        self.assertEqual(g.dtype, np.dtype("int16"))


class ActuationSeamTest(unittest.TestCase):
    def setUp(self):
        self._cf = cl.classify_frame
        self._ss = cl.send_sort
        self._re = cl.report_event
        cl.report_event = lambda *a, **k: None
        self.sorts = []
        cl.send_sort = lambda b: self.sorts.append(b)

    def tearDown(self):
        cl.classify_frame = self._cf
        cl.send_sort = self._ss
        cl.report_event = self._re

    def _frame(self):
        return np.zeros((480, 640, 3), dtype=np.uint8)

    def _clf(self):
        return types.SimpleNamespace(model_name="m.tflite")

    def test_confident_actuates_correct_bin(self):
        cl.classify_frame = lambda clf, img, device_id=None: (
            [("newspaper", 0.92), ("magazines", 0.05)],
            False,
        )
        cl.classify_and_report(clf=self._clf(), frame_bgr=self._frame())
        self.assertEqual(self.sorts, [0])  # newspaper -> bin 0

    def test_low_confidence_does_not_actuate(self):
        cl.classify_frame = lambda clf, img, device_id=None: (
            [("newspaper", 0.4), ("magazines", 0.3)],
            True,
        )
        cl.classify_and_report(clf=self._clf(), frame_bgr=self._frame())
        self.assertEqual(self.sorts, [])


if __name__ == "__main__":
    unittest.main()
