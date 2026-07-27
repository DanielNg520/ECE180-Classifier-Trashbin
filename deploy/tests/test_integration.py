"""Overall test: frame -> classify_frame -> label -> physical bin.

Uses the real infer_uno_q + bin_map with a FakeInterpreter and the real
30-class labels.txt, so the inference->routing seam is exercised end to end.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from PIL import Image

import bin_map
import infer_uno_q as infer
from test_infer import FakeInterp  # reuse the interpreter stub

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_LABELS = os.path.join(_REPO, "exports", "labels.txt")

FLOAT_INPUT = {"index": 0, "dtype": np.float32, "quantization": (0.0, 0)}


class EndToEndTest(unittest.TestCase):
    def setUp(self):
        self.labels = infer.load_labels(_LABELS)
        self._orig_interp = infer.Interpreter
        self._orig_req = infer.request_clarification
        self._t = infer.CONFIDENCE_THRESHOLD
        infer.CONFIDENCE_THRESHOLD = 0.60
        self.clarifications = []
        infer.request_clarification = lambda *a, **k: self.clarifications.append(1)

    def tearDown(self):
        infer.Interpreter = self._orig_interp
        infer.request_clarification = self._orig_req
        infer.CONFIDENCE_THRESHOLD = self._t

    def _clf(self, logits):
        infer.Interpreter = lambda **kw: FakeInterp(FLOAT_INPUT, logits)
        return infer.WasteClassifier("m.tflite", _LABELS)

    def _logits_favoring(self, label):
        v = np.zeros(len(self.labels), dtype=np.float32)
        v[self.labels.index(label)] = 12.0
        return v

    def test_confident_item_routes_to_bin(self):
        clf = self._clf(self._logits_favoring("cardboard_boxes"))
        img = Image.new("RGB", (300, 300), (200, 180, 120))
        preds, flagged = infer.classify_frame(clf, img, device_id="dev")
        self.assertFalse(flagged)
        self.assertEqual(preds[0][0], "cardboard_boxes")
        self.assertEqual(bin_map.label_to_bin(preds[0][0]), 0)
        self.assertEqual(self.clarifications, [])

    def test_uncertain_item_triggers_clarification(self):
        clf = self._clf(np.full(len(self.labels), 0.1, dtype=np.float32))
        img = Image.new("RGB", (300, 300), (50, 50, 50))
        preds, flagged = infer.classify_frame(clf, img, device_id="dev")
        self.assertTrue(flagged)
        self.assertEqual(self.clarifications, [1])


if __name__ == "__main__":
    unittest.main()
