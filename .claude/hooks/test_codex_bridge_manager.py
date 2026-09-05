#!/usr/bin/env python3
"""Tests for stable read-only state exposed by codex_bridge_manager."""

import json
import os
import tempfile
import unittest
from unittest import mock

import codex_bridge_manager


class BridgeUsageSnapshotTests(unittest.TestCase):
    def test_persisted_snapshot_avoids_slow_account_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "usage.json")
            expected = {
                "session_key_hash": "abc",
                "model": "gpt-5.6-sol",
                "effort": "medium",
                "last": {"cached_input_tokens": 63744},
            }
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(expected, handle)
            with mock.patch.object(
                codex_bridge_manager, "BRIDGE_USAGE_FILE", path,
            ), mock.patch.object(
                codex_bridge_manager, "account_snapshot",
            ) as account_snapshot:
                actual = codex_bridge_manager.bridge_usage_snapshot()

        self.assertEqual(actual, expected)
        account_snapshot.assert_not_called()

    def test_missing_file_falls_back_to_live_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "missing.json")
            expected = {"session_key_hash": "def", "effort": "high"}
            with mock.patch.object(
                codex_bridge_manager, "BRIDGE_USAGE_FILE", path,
            ), mock.patch.object(
                codex_bridge_manager, "account_snapshot",
                return_value={"ok": True, "bridgeUsage": expected},
            ) as account_snapshot:
                actual = codex_bridge_manager.bridge_usage_snapshot(timeout=2.5)

        self.assertEqual(actual, expected)
        account_snapshot.assert_called_once_with(timeout=2.5)


if __name__ == "__main__":
    unittest.main()
