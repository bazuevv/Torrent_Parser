#!/usr/bin/env python3
"""Tests for live OpenAI Usage metadata and compaction history."""

import hashlib
import json
import os
import tempfile
import unittest

import cache_usage


class OpenAIUsageTests(unittest.TestCase):
    def test_live_usage_overlays_only_matching_claude_session(self):
        session = "session-42"
        stats = {"ok": True, "model": "glm-5.3", "context": 299300}
        usage = {
            "session_key_hash": hashlib.sha256(session.encode()).hexdigest(),
            "model": "gpt-5.6-sol",
            "model_context_window": 258400,
            "turns_started": 2,
            "last": {"input_tokens": 63893, "cached_input_tokens": 50000,
                     "cache_write_input_tokens": 0},
        }
        result = cache_usage.apply_openai_usage(stats, usage, session)
        self.assertEqual(result["model"], "gpt-5.6-sol")
        self.assertEqual(result["context"], 63893)
        self.assertEqual(result["context_window"], 258400)
        self.assertEqual(result["last"]["read"], 50000)
        self.assertEqual(result["last"]["verdict"], "попадание")

        stale = {"ok": True, "model": "glm-5.3", "context": 299300}
        cache_usage.apply_openai_usage(stale, usage, "another-session")
        self.assertEqual(stale["model"], "glm-5.3")
        self.assertNotIn("context_window", stale)

    def test_context_events_are_filtered_by_session(self):
        session = "session-42"
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "codex-context-events.jsonl")
            events = [
                {"kind": "compact", "session_hash": hashlib.sha256(
                    session.encode()).hexdigest(), "ts": "2026-09-05T10:00:00+00:00",
                 "model": "gpt-5.6-sol", "context_before": 200000},
                {"kind": "compact", "session_hash": "other",
                 "ts": "2026-09-05T10:01:00+00:00"},
            ]
            with open(path, "w", encoding="utf-8") as handle:
                for event in events:
                    handle.write(json.dumps(event) + "\n")
            result = cache_usage.context_events(session, tmp)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["context_before"], 200000)

    def test_compaction_is_included_in_history(self):
        state = {"started": "2026-09-05T09:00:00+00:00", "miss_log": []}
        history = cache_usage.build_history(state, [], [], [{
            "kind": "compact", "ts": "2026-09-05T10:00:00+00:00",
            "model": "gpt-5.6-sol", "context_before": 200000,
            "context_window": 258400,
        }])
        self.assertEqual(history, [{
            "kind": "compact", "ts": "2026-09-05T10:00:00+00:00",
            "model": "gpt-5.6-sol", "context_before": 200000,
            "context_window": 258400,
        }])


if __name__ == "__main__":
    unittest.main()
