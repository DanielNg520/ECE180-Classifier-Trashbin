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
    from tensorflow.lite.python.interpreter import Interpreter

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


def preprocess(pil_image, img_size=IMG_SIZE, resize_size=RESIZE_SIZE):
    """Matches torchvision's Resize(shorter_side) + CenterCrop(img_size) + Normalize."""
    img = pil_image.convert("RGB")
    w, h = img.size
    if w <= h:
        new_w, new_h = resize_size, round(h * resize_size / w)
    else:
        new_h, new_w = resize_size, round(w * resize_size / h)
    img = img.resize((new_w, new_h), Image.BILINEAR)

    left, top = (new_w - img_size) // 2, (new_h - img_size) // 2
    img = img.crop((left, top, left + img_size, top + img_size))

    arr = np.asarray(img).astype(np.float32) / 255.0  # HWC, 0-1
    arr = (arr - IMAGENET_MEAN) / IMAGENET_STD
    arr = arr.transpose(2, 0, 1)  # CHW
    return arr[np.newaxis, ...]  # NCHW float32


class WasteClassifier:
    def __init__(self, model_path, labels_path, num_threads=4):
        self.labels = load_labels(labels_path)
        self.model_name = os.path.basename(model_path)
        self.interpreter = Interpreter(model_path=model_path, num_threads=num_threads)
        self.interpreter.allocate_tensors()
        self.input_detail = self.interpreter.get_input_details()[0]
        self.output_detail = self.interpreter.get_output_details()[0]

    def _run(self, arr):
        inp, out = self.input_detail, self.output_detail
        if inp["dtype"] in (np.int8, np.uint8):
            scale, zero_point = inp["quantization"]
            info = np.iinfo(inp["dtype"])
            arr = np.clip(np.round(arr / scale + zero_point), info.min, info.max)
            arr = arr.astype(inp["dtype"])
        else:
            arr = arr.astype(inp["dtype"])

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
        arr = preprocess(pil_image)
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
