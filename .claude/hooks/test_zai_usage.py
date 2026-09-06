#!/usr/bin/env python3
"""Tests for the Z.AI coding-plan quota windows (zai_usage)."""

import json
import os
import tempfile
import time
import unittest
from unittest import mock

import account_switcher

NOW_MS = time.time() * 1000
HOUR = 3600


def fake_response(body):
    class _Response:
        def read(self):
            return json.dumps(body).encode()
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
    return _Response()


def quota_body(limits):
    return {"code": 200, "msg": "Operation successful",
            "data": {"limits": limits, "level": "lite"}, "success": True}


class ZaiUsageTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache = os.path.join(tmp.name, "zai-usage-cache.json")
        patcher = mock.patch.object(
            account_switcher, "ZAI_USAGE_CACHE_FILE", self.cache)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.env = {"ANTHROPIC_BASE_URL": "https://api.z.ai",
                    "ANTHROPIC_AUTH_TOKEN": "test-key"}

    def fetch(self, body=None, error=None):
        calls = []
        def urlopen(request, timeout=None):
            calls.append(request)
            if error is not None:
                raise error
            return fake_response(body if body is not None else quota_body([
                {"type": "CREDIT_LIMIT", "unit": 3, "number": 5,
                 "usage": 2000, "currentValue": 166, "remaining": 1833,
                 "percentage": 8, "nextResetTime": NOW_MS + HOUR * 1000},
                {"type": "CREDIT_LIMIT", "unit": 6, "number": 1,
                 "usage": 10000, "currentValue": 6675, "remaining": 3324,
                 "percentage": 66, "nextResetTime": NOW_MS + 3 * HOUR * 1000},
            ]))
        with mock.patch("account_switcher.urllib.request.urlopen", urlopen):
            result = account_switcher.zai_usage(self.env)
        return result, calls

    def test_parses_windows_with_claude_like_keys(self):
        result, _ = self.fetch()

        self.assertIsNotNone(result)
        windows = result["windows"]
        self.assertEqual([w["key"] for w in windows],
                         ["five_hour", "seven_day"])
        self.assertEqual([w["label"] for w in windows], ["5 ч", "7 дн"])
        self.assertEqual([w["percent"] for w in windows], [8, 66])
        self.assertTrue(all(w["resetsInSec"] > 0 for w in windows))
        self.assertFalse(any(w["expired"] for w in windows))
        self.assertEqual(result["sourceLabel"], "Данные Z.AI")

    def test_result_is_cached_and_age_grows(self):
        first, _ = self.fetch()
        second, calls = self.fetch()  # сеть больше не зовём

        self.assertEqual(len(calls), 0)
        self.assertEqual(second["windows"], first["windows"])
        self.assertIsInstance(second.get("ageSec"), int)

    def test_not_zai_env_never_touches_network(self):
        self.env["ANTHROPIC_BASE_URL"] = "https://api.example.test"

        result, calls = self.fetch()

        self.assertIsNone(result)
        self.assertEqual(len(calls), 0)

    def test_network_failure_is_negative_cached(self):
        first, first_calls = self.fetch(error=OSError("down"))
        second, second_calls = self.fetch(error=OSError("down"))

        self.assertIsNone(first)
        self.assertEqual(len(first_calls), 1)
        self.assertIsNone(second)
        self.assertEqual(len(second_calls), 0)  # неудача свежа — ждём

    def test_past_reset_marks_window_expired(self):
        result, _ = self.fetch(body=quota_body([
            {"type": "CREDIT_LIMIT", "unit": 3, "number": 5,
             "percentage": 90, "nextResetTime": NOW_MS - 1000},
        ]))

        window = result["windows"][0]
        self.assertTrue(window["expired"])
        self.assertEqual(window["percent"], 0)
        self.assertIsNone(window["resetsInSec"])

    def test_unknown_window_kind_gets_generic_label(self):
        result, _ = self.fetch(body=quota_body([
            {"type": "CREDIT_LIMIT", "unit": 4, "number": 2,
             "percentage": 10, "nextResetTime": NOW_MS + HOUR * 1000},
        ]))

        window = result["windows"][0]
        self.assertIsNone(window["key"])
        self.assertEqual(window["label"], "2 ×4")

    def test_bigmodel_station_sends_raw_key(self):
        captured = []
        def urlopen(request, timeout=None):
            captured.append(request.get_header("Authorization"))
            return fake_response(quota_body([
                {"type": "CREDIT_LIMIT", "unit": 3, "number": 5,
                 "percentage": 1, "nextResetTime": NOW_MS + HOUR * 1000},
            ]))
        self.env["ANTHROPIC_BASE_URL"] = "https://open.bigmodel.cn"

        with mock.patch("account_switcher.urllib.request.urlopen", urlopen):
            result = account_switcher.zai_usage(self.env)

        self.assertIsNotNone(result)
        self.assertEqual(captured, ["test-key"])  # без «Bearer »

    def test_empty_limits_is_negative_cached(self):
        self.fetch(body=quota_body([]))
        _, calls = self.fetch(body=quota_body([]))

        self.assertEqual(len(calls), 0)


if __name__ == "__main__":
    unittest.main()
