"""Sends low-confidence classifications to the webapp for human clarification.

Contract (webapp side, owned by the webapp team):

    POST {WEBAPP_URL}/api/clarifications
    Content-Type: multipart/form-data

    fields:
      device_id       str   - which trashbin sent this
      timestamp       str   - ISO 8601 UTC
      model_version   str   - .tflite filename the prediction came from
      predictions     str   - JSON list of {"class": str, "confidence": float},
                              top-k, descending confidence
      image           file  - JPEG frame that triggered clarification

    Response: 202 Accepted. The webapp is responsible for notifying a human,
    storing their corrected label, and forwarding it to Edge Impulse (see
    edge_impulse_upload.py) once confirmed.

If the request fails (device offline, webapp down), the frame + metadata are
queued locally and retried on the next call to flush_pending().
"""
import io
import json
import os
import time
from datetime import datetime, timezone

import requests

WEBAPP_URL = os.environ.get("WEBAPP_URL", "http://localhost:8000")
CLARIFICATION_ENDPOINT = f"{WEBAPP_URL.rstrip('/')}/api/clarifications"
REQUEST_TIMEOUT_S = float(os.environ.get("CLARIFICATION_TIMEOUT_S", "5"))

PENDING_DIR = os.environ.get(
    "CLARIFICATION_QUEUE_DIR",
    os.path.expanduser("~/.local/state/trashbin/pending_clarifications"),
)


def _post(image_bytes, predictions, device_id, model_version, timestamp):
    files = {"image": ("frame.jpg", image_bytes, "image/jpeg")}
    data = {
        "device_id": device_id,
        "timestamp": timestamp,
        "model_version": model_version,
        "predictions": json.dumps(
            [{"class": c, "confidence": conf} for c, conf in predictions]
        ),
    }
    resp = requests.post(
        CLARIFICATION_ENDPOINT, files=files, data=data, timeout=REQUEST_TIMEOUT_S
    )
    resp.raise_for_status()


def _queue_locally(image_bytes, predictions, device_id, model_version, timestamp):
    os.makedirs(PENDING_DIR, exist_ok=True)
    stamp = timestamp.replace(":", "-")
    base = os.path.join(PENDING_DIR, stamp)
    with open(base + ".jpg", "wb") as f:
        f.write(image_bytes)
    with open(base + ".json", "w") as f:
        json.dump(
            {
                "device_id": device_id,
                "timestamp": timestamp,
                "model_version": model_version,
                "predictions": [{"class": c, "confidence": conf} for c, conf in predictions],
            },
            f,
        )


def request_clarification(pil_image, predictions, device_id, model_version):
    """Best-effort notify; falls back to a local queue on failure."""
    buf = io.BytesIO()
    pil_image.convert("RGB").save(buf, format="JPEG", quality=90)
    image_bytes = buf.getvalue()
    timestamp = datetime.now(timezone.utc).isoformat()

    try:
        _post(image_bytes, predictions, device_id, model_version, timestamp)
    except requests.RequestException as e:
        print(f"[clarification] webapp unreachable ({e}); queuing locally")
        _queue_locally(image_bytes, predictions, device_id, model_version, timestamp)


def flush_pending():
    """Retry any locally-queued clarifications. Call periodically (e.g. cron)."""
    if not os.path.isdir(PENDING_DIR):
        return
    for fname in sorted(os.listdir(PENDING_DIR)):
        if not fname.endswith(".json"):
            continue
        base = os.path.join(PENDING_DIR, fname[: -len(".json")])
        meta_path, image_path = base + ".json", base + ".jpg"
        if not os.path.exists(image_path):
            continue
        with open(meta_path) as f:
            meta = json.load(f)
        with open(image_path, "rb") as f:
            image_bytes = f.read()
        predictions = [(p["class"], p["confidence"]) for p in meta["predictions"]]
        try:
            _post(image_bytes, predictions, meta["device_id"], meta["model_version"], meta["timestamp"])
            os.remove(meta_path)
            os.remove(image_path)
        except requests.RequestException:
            break  # webapp still unreachable, stop and retry later


if __name__ == "__main__":
    while True:
        flush_pending()
        time.sleep(60)
