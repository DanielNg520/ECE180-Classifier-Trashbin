"""Unit tests for clarification_client: post, offline queue, prune, flush."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from PIL import Image

import clarification_client as cc


def _img():
    return Image.new("RGB", (32, 32), (10, 20, 30))


PREDS = [("plastic_water_bottles", 0.4), ("glass_food_jars", 0.3)]


class ClarificationTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self._orig_dir = cc.PENDING_DIR
        self._orig_post = cc._session.post
        cc.PENDING_DIR = self._dir

    def tearDown(self):
        cc.PENDING_DIR = self._orig_dir
        cc._session.post = self._orig_post

    def _pending(self):
        return sorted(f for f in os.listdir(self._dir) if f.endswith(".json"))

    def test_success_does_not_queue(self):
        cc._session.post = lambda *a, **k: _Ok()
        cc.request_clarification(_img(), PREDS, "dev1", "model.tflite")
        self.assertEqual(self._pending(), [])

    def test_failure_queues_locally(self):
        def boom(*a, **k):
            raise requests.RequestException("down")

        cc._session.post = boom
        cc.request_clarification(_img(), PREDS, "dev1", "model.tflite")
        self.assertEqual(len(self._pending()), 1)
        # image saved alongside metadata
        self.assertEqual(len([f for f in os.listdir(self._dir) if f.endswith(".jpg")]), 1)

    def test_prune_caps_queue(self):
        self._orig_max = cc.MAX_PENDING
        cc.MAX_PENDING = 3
        try:
            cc._session.post = self._raise
            for i in range(6):
                # distinct timestamps so filenames differ
                cc._queue_locally(b"x", PREDS, "dev1", "m", f"2026-01-01T00-00-0{i}")
            self.assertLessEqual(len(self._pending()), 3)
        finally:
            cc.MAX_PENDING = self._orig_max

    def test_flush_posts_and_clears(self):
        cc._session.post = self._raise
        cc.request_clarification(_img(), PREDS, "dev1", "m.tflite")
        self.assertEqual(len(self._pending()), 1)

        posted = {"n": 0}

        def ok(*a, **k):
            posted["n"] += 1
            return _Ok()

        cc._session.post = ok
        cc.flush_pending()
        self.assertEqual(posted["n"], 1)
        self.assertEqual(self._pending(), [])

    @staticmethod
    def _raise(*a, **k):
        raise requests.RequestException("down")


class _Ok:
    status_code = 200

    def raise_for_status(self):
        pass


if __name__ == "__main__":
    unittest.main()
