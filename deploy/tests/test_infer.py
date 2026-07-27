"""Unit tests for infer_uno_q: preprocessing, fused int8 quant, classify.

Run:  python -m unittest discover -s deploy/tests
No TFLite runtime or camera needed — a FakeInterpreter is injected.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from PIL import Image

import infer_uno_q as infer


def _labels_file(names):
    f = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
    f.write("\n".join(names))
    f.close()
    return f.name


class FakeInterp:
    """Minimal TFLite Interpreter stand-in: returns fixed logits."""

    def __init__(self, input_detail, logits):
        self._in = input_detail
        self._out = {"index": 99, "dtype": np.float32, "quantization": (0.0, 0)}
        self._logits = np.asarray(logits, dtype=np.float32)
        self.last_input = None

    def allocate_tensors(self):
        pass

    def get_input_details(self):
        return [self._in]

    def get_output_details(self):
        return [self._out]

    def set_tensor(self, index, arr):
        self.last_input = arr

    def invoke(self):
        pass

    def get_tensor(self, index):
        return self._logits[np.newaxis, :]  # shape (1, N); classifier takes [0]


def _build_clf(input_detail, logits, labels):
    orig = infer.Interpreter
    infer.Interpreter = lambda **kw: FakeInterp(input_detail, logits)
    try:
        return infer.WasteClassifier("model_x.tflite", _labels_file(labels))
    finally:
        infer.Interpreter = orig


def _rand_img(w=600, h=400, seed=0):
    rng = np.random.default_rng(seed)
    return Image.fromarray((rng.random((h, w, 3)) * 255).astype("uint8"), "RGB")


FLOAT_INPUT = {"index": 0, "dtype": np.float32, "quantization": (0.0, 0)}


class ResizeCropTest(unittest.TestCase):
    def test_shape_and_dtype(self):
        arr = infer._resize_crop(_rand_img())
        self.assertEqual(arr.shape, (infer.IMG_SIZE, infer.IMG_SIZE, 3))
        self.assertEqual(arr.dtype, np.uint8)

    def test_non_rgb_is_converted(self):
        gray = Image.new("L", (400, 400), 128)
        arr = infer._resize_crop(gray)  # must not raise; RGB-ified
        self.assertEqual(arr.shape[-1], 3)


class PreprocessTest(unittest.TestCase):
    def test_normalization(self):
        img = Image.new("RGB", (400, 400), (128, 128, 128))
        arr = infer.preprocess(img)
        self.assertEqual(arr.shape, (1, 3, infer.IMG_SIZE, infer.IMG_SIZE))
        self.assertEqual(arr.dtype, np.float32)
        expect_r = (128 / 255.0 - float(infer.IMAGENET_MEAN[0])) / float(infer.IMAGENET_STD[0])
        np.testing.assert_allclose(arr[0, 0].mean(), expect_r, rtol=1e-4)


class FusedQuantTest(unittest.TestCase):
    def test_matches_two_step(self):
        """Fused uint8->int8 must equal the old normalize-then-quantize path."""
        scale, zero_point = 0.018, -5
        clf = _build_clf(
            {"index": 0, "dtype": np.int8, "quantization": (scale, zero_point)},
            [0.0, 0.0, 0.0],
            ["a", "b", "c"],
        )
        img = _rand_img(seed=7)
        new = clf._prepare_input(img)

        f = infer.preprocess(img)
        info = np.iinfo(np.int8)
        old = np.clip(np.round(f / scale + zero_point), info.min, info.max).astype(np.int8)

        self.assertEqual(new.dtype, np.int8)
        self.assertEqual(new.shape, (1, 3, infer.IMG_SIZE, infer.IMG_SIZE))
        diff = np.abs(new.astype(np.int16) - old.astype(np.int16))
        self.assertLessEqual(int(diff.max()), 1, "fused quant differs by >1 LSB")
        self.assertGreater((diff == 0).mean(), 0.99, "fused quant not equivalent")

    def test_float_path_dtype(self):
        clf = _build_clf(FLOAT_INPUT, [0.0, 0.0, 0.0], ["a", "b", "c"])
        arr = clf._prepare_input(_rand_img())
        self.assertEqual(arr.dtype, np.float32)


class SoftmaxTest(unittest.TestCase):
    def test_sums_to_one(self):
        p = infer.WasteClassifier._softmax(np.array([1.0, 5.0, 2.0]))
        np.testing.assert_allclose(p.sum(), 1.0, rtol=1e-6)
        self.assertEqual(int(np.argmax(p)), 1)


class ClassifyTest(unittest.TestCase):
    def setUp(self):
        self._t = infer.CONFIDENCE_THRESHOLD
        infer.CONFIDENCE_THRESHOLD = 0.60

    def tearDown(self):
        infer.CONFIDENCE_THRESHOLD = self._t

    def test_confident_prediction(self):
        clf = _build_clf(FLOAT_INPUT, [1.0, 5.0, 2.0], ["a", "b", "c"])
        preds, flagged = clf.classify(_rand_img(), topk=3)
        self.assertEqual(preds[0][0], "b")
        self.assertEqual(len(preds), 3)
        confs = [c for _, c in preds]
        self.assertEqual(confs, sorted(confs, reverse=True))
        self.assertFalse(flagged)

    def test_low_confidence_flagged(self):
        clf = _build_clf(FLOAT_INPUT, [1.0, 1.1, 1.0], ["a", "b", "c"])
        preds, flagged = clf.classify(_rand_img())
        self.assertTrue(flagged)

    def test_quant_path_argmax(self):
        clf = _build_clf(
            {"index": 0, "dtype": np.int8, "quantization": (0.02, 0)},
            [0.1, 0.2, 9.0],
            ["a", "b", "c"],
        )
        preds, _ = clf.classify(_rand_img())
        self.assertEqual(preds[0][0], "c")

    def test_no_runtime_raises(self):
        orig = infer.Interpreter
        infer.Interpreter = None
        try:
            with self.assertRaises(RuntimeError):
                infer.WasteClassifier("m.tflite", _labels_file(["a"]))
        finally:
            infer.Interpreter = orig


if __name__ == "__main__":
    unittest.main()
