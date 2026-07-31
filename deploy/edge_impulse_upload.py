"""Reference implementation for pushing a human-corrected sample to Edge Impulse.

Intended to be called by the webapp backend right after a human confirms/
corrects a label for a clarification request (see clarification_client.py's
contract). Uploading here means the sample lands in the shared Edge Impulse
project's data acquisition, so the next retrain-and-redeploy cycle benefits
every UNO Q running this model — not just the one that captured the frame.

Requires an Edge Impulse API key for the project (Dashboard > Keys), passed
via EI_API_KEY or the api_key argument. Never commit the key.

Docs: https://docs.edgeimpulse.com/reference/edge-impulse-api/ingestion-api
"""
import os

import requests

INGESTION_URL = "https://ingestion.edgeimpulse.com/api/{category}/files"


def upload_to_edge_impulse(image_path, label, api_key=None, category="training"):
    """category: 'training' or 'testing'. Returns the parsed JSON response.

    Edge Impulse auto-splits into train/test if you always use 'training';
    the webapp can alternate categories to keep a held-out set, matching this
    repo's stratified split philosophy.
    """
    api_key = api_key or os.environ["EI_API_KEY"]
    url = INGESTION_URL.format(category=category)

    with open(image_path, "rb") as f:
        files = {"data": (os.path.basename(image_path), f, "image/jpeg")}
        headers = {"x-api-key": api_key, "x-label": label}
        resp = requests.post(url, headers=headers, files=files, timeout=15)
    resp.raise_for_status()
    return resp.json()


if __name__ == "__main__":
    import sys

    image_path, label = sys.argv[1], sys.argv[2]
    print(upload_to_edge_impulse(image_path, label))
