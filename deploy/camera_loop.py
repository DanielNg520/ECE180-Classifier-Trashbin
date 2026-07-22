"""Live camera test loop for the UNO Q Linux side.

Grabs frames from a USB (UVC) webcam with OpenCV, classifies each one with
the exported .tflite model, prints the top-3, and optionally reports every
result to the dashboard webapp (POST {WEBAPP_URL}/api/events, JSON).

Low-confidence frames still go through classify_frame -> clarification queue,
same as production.

Usage:
    python camera_loop.py path/to/model.tflite path/to/labels.txt
    # env: CONFIDENCE_THRESHOLD, TEMPERATURE, WEBAPP_URL, DEVICE_ID,
    #      CAMERA_INDEX (default 0), INTERVAL_S (default 2)
"""
import json
import os
import sys
import time
from datetime import datetime, timezone

import cv2
import requests
from PIL import Image

from infer_uno_q import WasteClassifier, classify_frame, CONFIDENCE_THRESHOLD
from bin_map import label_to_bin, BIN_NAMES
from motor_bridge import send_sort

WEBAPP_URL = os.environ.get("WEBAPP_URL", "").rstrip("/")
DEVICE_ID = os.environ.get("DEVICE_ID", "uno-q-dev")
CAMERA_INDEX = os.environ.get("CAMERA_INDEX")  # unset = auto-detect
INTERVAL_S = float(os.environ.get("INTERVAL_S", "2"))
TRIGGER = os.environ.get("TRIGGER", "motion")  # "motion" or "interval"
# A pixel counts as "changed" when its grayscale value moves by more than
# PIXEL_DELTA (0-255) between consecutive downscaled frames; motion triggers
# when more than MOTION_THRESHOLD percent of pixels changed. Sensor noise
# rarely pushes a pixel past ~15 levels, so even a small/distant object
# (a fraction of a percent of the frame) stands out. Tune with the printed
# scores: ambient noise is ~0.0-0.2%, a distant hand 1-5%, a close item 20%+.
PIXEL_DELTA = float(os.environ.get("PIXEL_DELTA", "18"))
MOTION_THRESHOLD = float(os.environ.get("MOTION_THRESHOLD", "0.8"))  # % of pixels
SETTLE_QUIET_SAMPLES = 3  # consecutive below-threshold samples = object at rest
COOLDOWN_S = float(os.environ.get("COOLDOWN_S", "3"))  # re-arm delay after classifying


def open_camera():
    """Open CAMERA_INDEX if set, else probe for the first real capture device.

    On the UNO Q, /dev/video0 and /dev/video1 are the Qualcomm codec engines,
    which open but never produce frames — so probe with an actual read().
    """
    if CAMERA_INDEX is not None:
        return cv2.VideoCapture(int(CAMERA_INDEX))
    for idx in range(10):
        cap = cv2.VideoCapture(idx)
        if cap.isOpened():
            ok, _ = cap.read()
            if ok:
                print(f"auto-detected camera at index {idx}")
                return cap
        cap.release()
    sys.exit("No working camera found — check `ls /dev/video*` and the hub's power")


def report_event(predictions, needs_clarification, model_version, ms_frame):
    """Best-effort: a dead dashboard must never stop the bin."""
    if not WEBAPP_URL:
        return
    try:
        requests.post(
            f"{WEBAPP_URL}/api/events",
            json={
                "device_id": DEVICE_ID,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "model_version": model_version,
                "predictions": [{"class": c, "confidence": p} for c, p in predictions],
                "needs_clarification": needs_clarification,
                "ms_frame": ms_frame,
            },
            timeout=3,
        )
    except requests.RequestException as e:
        print(f"[dashboard] unreachable: {e}")


def main():
    model_path, labels_path = sys.argv[1:3]
    clf = WasteClassifier(model_path, labels_path)

    cap = open_camera()
    if not cap.isOpened():
        sys.exit("Cannot open camera — check `ls /dev/video*`")
    # Ask for a modest resolution; the model only needs 256x256 anyway and
    # big frames just waste USB bandwidth and resize time.
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

    try:
        if TRIGGER == "motion":
            motion_loop(cap, clf)
        else:
            interval_loop(cap, clf)
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        cap.release()


def read_fresh(cap):
    """Drain buffered frames so we classify *now*, not 5 frames ago."""
    for _ in range(3):
        cap.grab()
    ok, frame_bgr = cap.read()
    return frame_bgr if ok else None


def classify_and_report(clf, frame_bgr):
    img = Image.fromarray(cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB))
    t0 = time.perf_counter()
    preds, flagged = classify_frame(clf, img, device_id=DEVICE_ID)
    ms = (time.perf_counter() - t0) * 1000
    top_cls, top_conf = preds[0]
    flag = "  << needs clarification" if flagged else ""
    print(f"{ms:6.1f} ms  {top_cls:35s} {top_conf * 100:5.1f}%{flag}")
    report_event(preds, flagged, clf.model_name, ms)

    # Actuate only on a confident prediction. Low-confidence frames went to the
    # clarification queue instead — driving the arm on a guess would mis-sort.
    if not flagged:
        target_bin = label_to_bin(top_cls)
        print(f"        -> bin {target_bin} ({BIN_NAMES[target_bin]})")
        send_sort(target_bin)


def interval_loop(cap, clf):
    print(f"Classifying every {INTERVAL_S}s — Ctrl-C to stop.")
    while True:
        frame = read_fresh(cap)
        if frame is None:
            print("frame grab failed, retrying...")
            time.sleep(1)
            continue
        classify_and_report(clf, frame)
        time.sleep(INTERVAL_S)


def _gray_small(frame_bgr):
    gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    return cv2.resize(gray, (160, 120)).astype("int16")


def motion_loop(cap, clf, sample_s=0.2):
    """Classify once per deposited item instead of on a timer.

    State machine: ARMED (watch for a frame-diff spike) -> SETTLING (motion
    started; wait until SETTLE_QUIET_SAMPLES consecutive quiet samples, i.e.
    the item has come to rest) -> classify one frame -> COOLDOWN_S -> ARMED.
    Diffing a 160x120 grayscale frame costs ~1 ms, so idle CPU stays near zero.
    """
    print(f"Motion-triggered (threshold {MOTION_THRESHOLD}) — Ctrl-C to stop.")
    prev = None
    settling = False
    quiet = 0
    while True:
        frame = read_fresh(cap)
        if frame is None:
            time.sleep(1)
            continue
        gray = _gray_small(frame)
        if prev is not None:
            score = float((abs(gray - prev) > PIXEL_DELTA).mean() * 100)
            if not settling:
                if score > MOTION_THRESHOLD:
                    print(f"[motion {score:.1f}%] settling...")
                    settling, quiet = True, 0
            else:
                quiet = quiet + 1 if score <= MOTION_THRESHOLD else 0
                if quiet >= SETTLE_QUIET_SAMPLES:
                    classify_and_report(clf, frame)
                    settling = False
                    time.sleep(COOLDOWN_S)
                    prev = None  # scene changed during cooldown; re-baseline
                    continue
        prev = gray
        time.sleep(sample_s)


if __name__ == "__main__":
    main()
