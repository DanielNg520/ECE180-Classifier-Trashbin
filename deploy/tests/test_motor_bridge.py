"""Unit + seam tests for motor_bridge.send_sort (HTTP -> motor app contract)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

import motor_bridge


class FakeResp:
    def __init__(self, ok=True, status_code=200, payload=None):
        self.ok = ok
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


class SendSortTest(unittest.TestCase):
    def setUp(self):
        self._enabled = motor_bridge.MOTOR_ENABLED
        self._post = motor_bridge._session.post
        motor_bridge.MOTOR_ENABLED = True

    def tearDown(self):
        motor_bridge.MOTOR_ENABLED = self._enabled
        motor_bridge._session.post = self._post

    def test_disabled_is_noop(self):
        motor_bridge.MOTOR_ENABLED = False
        self.assertFalse(motor_bridge.send_sort(2))

    def test_ack_success(self):
        calls = {}

        def fake_post(url, json=None, timeout=None):
            calls["url"], calls["json"] = url, json
            return FakeResp(payload={"ok": True, "landed": 2})

        motor_bridge._session.post = fake_post
        self.assertTrue(motor_bridge.send_sort(2))
        self.assertTrue(calls["url"].endswith("/sort"))
        self.assertEqual(calls["json"], {"bin": 2})

    def test_landed_minus_one_is_failure(self):
        motor_bridge._session.post = lambda *a, **k: FakeResp(payload={"ok": True, "landed": -1})
        self.assertFalse(motor_bridge.send_sort(9))

    def test_http_error_is_failure(self):
        motor_bridge._session.post = lambda *a, **k: FakeResp(ok=False, status_code=500, payload={})
        self.assertFalse(motor_bridge.send_sort(1))

    def test_unreachable_is_failure(self):
        def boom(*a, **k):
            raise requests.RequestException("connection refused")

        motor_bridge._session.post = boom
        self.assertFalse(motor_bridge.send_sort(1))

    def test_non_json_is_failure(self):
        class Bad(FakeResp):
            def json(self):
                raise ValueError("not json")

        motor_bridge._session.post = lambda *a, **k: Bad()
        self.assertFalse(motor_bridge.send_sort(1))


if __name__ == "__main__":
    unittest.main()
