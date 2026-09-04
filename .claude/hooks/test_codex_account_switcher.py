#!/usr/bin/env python3
"""Tests for OpenAI account metadata shown by the provider switcher."""

import unittest
from unittest import mock

import account_switcher


class OpenAIUsageTests(unittest.TestCase):
    def test_runtime_model_effort_and_last_tokens_are_sanitized(self):
        runtime = account_switcher._openai_runtime({
            "model": "gpt-5.6-sol", "effort": "low",
            "last": {
                "input_tokens": 12_484, "cached_input_tokens": 5_888,
                "cache_write_input_tokens": 0, "output_tokens": 16,
                "reasoning_output_tokens": 8, "total_tokens": 12_500,
                "private": "drop",
            },
            "model_context_window": 258_400,
            "credential": "drop",
        })
        self.assertEqual(runtime["model"], "gpt-5.6-sol")
        self.assertEqual(runtime["effort"], "low")
        self.assertEqual(runtime["last"]["cached_input_tokens"], 5_888)
        self.assertEqual(runtime["modelContextWindow"], 258_400)
        self.assertNotIn("credential", runtime)

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
            "bridgeUsage": {
                "model": "gpt-active", "effort": "medium",
                "last": {"input_tokens": 100, "output_tokens": 5},
            },
        }):
            result = account_switcher.openai_account()
        self.assertEqual(result["plan"], "Plus")
        self.assertEqual(result["model"], "gpt-active")
        self.assertEqual(result["runtime"]["effort"], "medium")
        rendered = str(result).lower()
        self.assertNotIn("credential", rendered)
        self.assertNotIn("access_token", rendered)
        self.assertNotIn("auth_token", rendered)


if __name__ == "__main__":
    unittest.main()
