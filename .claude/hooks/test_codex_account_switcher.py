#!/usr/bin/env python3
"""Tests for OpenAI account metadata shown by the provider switcher."""

import unittest
from unittest import mock

import account_switcher


class OpenAIUsageTests(unittest.TestCase):
    def test_rate_limit_windows_are_converted_for_existing_usage_ui(self):
        with mock.patch("account_switcher.time.time", return_value=1_000):
            usage = account_switcher._openai_usage({
                "primary": {
                    "usedPercent": 31,
                    "windowDurationMins": 300,
                    "resetsAt": 1_600,
                },
                "secondary": {
                    "usedPercent": 42,
                    "windowDurationMins": 10_080,
                    "resetsAt": 2_000,
                },
            })
        self.assertEqual([w["label"] for w in usage["windows"]], ["5 ч", "7 дн"])
        self.assertEqual(usage["windows"][0]["resetsInSec"], 600)
        self.assertEqual(usage["sourceLabel"], "Данные Codex App Server")

    def test_snapshot_identity_never_needs_credentials(self):
        with mock.patch.object(account_switcher.codex_bridge_manager,
                               "account_snapshot", return_value={
            "account": {"email": "person@example.test", "planType": "plus"},
            "models": [{"id": "gpt-test", "isDefault": True}],
            "rateLimits": {},
        }):
            result = account_switcher.openai_account()
        self.assertEqual(result["plan"], "Plus")
        self.assertEqual(result["model"], "gpt-test")
        self.assertNotIn("token", str(result).lower())


if __name__ == "__main__":
    unittest.main()
