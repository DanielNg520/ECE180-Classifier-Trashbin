"""On-device inference for the UNO Q Linux side.

Loads the exported .tflite model, replicates the notebook's `eval_tf`
preprocessing (resize shorter side -> center crop -> ImageNet normalize)
without a torch dependency, and classifies a camera frame with a confidence
score. Predictions below CONFIDENCE_THRESHOLD are routed to the webapp for
human clarification via clarification_client.

Usage:
    python infer_uno_q.py path/to/model.tflite path/to/labels.txt frame.jpg
"""
import os
import sys

import numpy as np
from PIL import Image

try:
    from ai_edge_litert.interpreter import Interpreter
except ImportError:
    try:
        from tensorflow.lite.python.interpreter import Interpreter
    except ImportError:
        # Neither runtime present (e.g. a dev machine running the unit tests).
        # The module stays importable; WasteClassifier raises a clear error only
        # if you actually try to instantiate it without a runtime.
        Interpreter = None

from clarification_client import request_clarification

IMG_SIZE = 256
RESIZE_SIZE = round(IMG_SIZE * 256 / 224)  # 293, matches Cell 5's eval_tf ratio
IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
IMAGENET_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)

# Both values come from the training notebook's calibration artifacts:
#   TEMPERATURE          — export/confidence_calibration.json (Cell 12b)
#   CONFIDENCE_THRESHOLD — export/quantization_report.json, the
#                          `recommended_threshold` of the .tflite variant you
#                          actually deployed (int8 shifts logits, so the
#                          threshold is per-variant, not one global number).
# Defaults are a safe fallback if the env isn't configured.
CONFIDENCE_THRESHOLD = float(os.environ.get("CONFIDENCE_THRESHOLD", "0.60"))
TEMPERATURE = float(os.environ.get("TEMPERATURE", "1.0"))


def load_labels(labels_path):
    with open(labels_path) as f:
        return [line.strip() for line in f if line.strip()]


def _resize_crop(pil_image, img_size=IMG_SIZE, resize_size=RESIZE_SIZE):
    """Resize shorter side to resize_size + center-crop img_size.

    Returns an HWC uint8 RGB array. The PIL BILINEAR resize is the
    accuracy-sensitive step that must match the notebook's `eval_tf`, so it
    stays on PIL; everything after is plain numpy. The `.convert("RGB")` is
    skipped when the frame is already RGB (the camera-loop path always is),
    avoiding a redundant full-frame copy.
    """
    img = pil_image if pil_image.mode == "RGB" else pil_image.convert("RGB")
    w, h = img.size
    if w <= h:
        new_w, new_h = resize_size, round(h * resize_size / w)
    else:
        new_h, new_w = resize_size, round(w * resize_size / h)
    img = img.resize((new_w, new_h), Image.BILINEAR)

    left, top = (new_w - img_size) // 2, (new_h - img_size) // 2
    img = img.crop((left, top, left + img_size, top + img_size))
    return np.asarray(img)  # HWC uint8, RGB


def preprocess(pil_image, img_size=IMG_SIZE, resize_size=RESIZE_SIZE):
    """Float32 NCHW, ImageNet-normalized — the fp32 / dynamic-int8 input path.

    Matches torchvision's Resize(shorter_side) + CenterCrop + Normalize.
    (Static-int8 models take the fused uint8->int8 path in
    WasteClassifier._prepare_input instead, which is numerically equivalent.)
    """
    arr = _resize_crop(pil_image, img_size, resize_size).astype(np.float32) / 255.0
    arr = (arr - IMAGENET_MEAN) / IMAGENET_STD
    arr = arr.transpose(2, 0, 1)  # CHW
    return arr[np.newaxis, ...]  # NCHW float32


class WasteClassifier:
    def __init__(self, model_path, labels_path, num_threads=4):
        if Interpreter is None:
            raise RuntimeError(
                "No TFLite runtime found — install `ai-edge-litert` (or tensorflow) "
                "on the device. See README 'Python environment note'."
            )
        self.labels = load_labels(labels_path)
        self.model_name = os.path.basename(model_path)
        self.interpreter = Interpreter(model_path=model_path, num_threads=num_threads)
        self.interpreter.allocate_tensors()
        self.input_detail = self.interpreter.get_input_details()[0]
        self.output_detail = self.interpreter.get_output_details()[0]

        # For a quantized (int8/uint8) input, fold the ImageNet normalization and
        # the input quantization into a single per-channel affine, so a frame
        # goes uint8 -> int8 in one multiply-add with no float32 intermediate.
        #   normalized = (px/255 - mean) / std
        #   q          = round(normalized / scale + zero_point)
        #             = round(px * A + B)        with, per channel:
        #   A = 1 / (255 * std * scale)
        #   B = zero_point - mean / (std * scale)
        # Numerically equivalent to the old normalize-then-quantize two-step.
        self._quantized = self.input_detail["dtype"] in (np.int8, np.uint8)
        if self._quantized:
            scale, zero_point = self.input_detail["quantization"]
            self._q_A = (1.0 / (255.0 * IMAGENET_STD * scale)).astype(np.float32)
            self._q_B = (zero_point - IMAGENET_MEAN / (IMAGENET_STD * scale)).astype(np.float32)
            self._q_info = np.iinfo(self.input_detail["dtype"])

        print(
            f"[infer] {self.model_name}: input dtype={np.dtype(self.input_detail['dtype']).name}, "
            f"{'fused int8' if self._quantized else 'float'} path, {num_threads} threads",
            flush=True,
        )

    def _prepare_input(self, pil_image):
        """PIL frame -> model-ready NCHW tensor (fused int8 or normalized float)."""
        hwc = _resize_crop(pil_image)  # HWC uint8 RGB
        if self._quantized:
            q = np.round(hwc.astype(np.float32) * self._q_A + self._q_B)
            q = np.clip(q, self._q_info.min, self._q_info.max).astype(self.input_detail["dtype"])
            arr = q.transpose(2, 0, 1)[np.newaxis, ...]
        else:
            f = hwc.astype(np.float32) / 255.0
            f = (f - IMAGENET_MEAN) / IMAGENET_STD
            arr = f.transpose(2, 0, 1)[np.newaxis, ...].astype(self.input_detail["dtype"])
        return arr

    def _run(self, arr):
        inp, out = self.input_detail, self.output_detail
        self.interpreter.set_tensor(inp["index"], arr)
        self.interpreter.invoke()
        logits = self.interpreter.get_tensor(out["index"])[0].astype(np.float32)

        out_scale, out_zero_point = out["quantization"]
        if out_scale:
            logits = (logits - out_zero_point) * out_scale
        return logits

    @staticmethod
    def _softmax(logits):
        e = np.exp(logits - logits.max())
        return e / e.sum()

    def classify(self, pil_image, topk=3):
        """Returns (predictions, needs_clarification).

        predictions: list of (class_name, confidence) sorted descending, len topk.
        """
        arr = self._prepare_input(pil_image)
        # Temperature scaling (Cell 12b): argmax is unchanged, but confidence
        # becomes an approximately calibrated P(correct), which is what the
        # clarification threshold was tuned against.
        probs = self._softmax(self._run(arr) / TEMPERATURE)
        top_idx = np.argsort(probs)[::-1][:topk]
        predictions = [(self.labels[i], float(probs[i])) for i in top_idx]
        needs_clarification = predictions[0][1] < CONFIDENCE_THRESHOLD
        return predictions, needs_clarification


def classify_frame(classifier, pil_image, device_id=None):
    """Reference entry point for the RTOS -> Linux handoff.

    Classifies a frame and, if top-1 confidence is below CONFIDENCE_THRESHOLD,
    fires a clarification request to the webapp so a human can pick the
    correct label. Returns the same (predictions, needs_clarification) tuple
    as WasteClassifier.classify.
    """
    predictions, needs_clarification = classifier.classify(pil_image)
    if needs_clarification:
        request_clarification(
            pil_image,
            predictions,
            device_id=device_id or os.environ.get("DEVICE_ID", "unknown"),
            model_version=classifier.model_name,
        )
    return predictions, needs_clarification


if __name__ == "__main__":
    model_path, labels_path, frame_path = sys.argv[1:4]
    clf = WasteClassifier(model_path, labels_path)
    img = Image.open(frame_path)
    preds, flagged = classify_frame(clf, img)
    for cls, conf in preds:
        print(f"  {cls:35s} {conf * 100:5.1f}%")
    if flagged:
        print(f"[low confidence < {CONFIDENCE_THRESHOLD:.0%} — clarification requested]")
