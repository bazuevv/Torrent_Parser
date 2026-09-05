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
        stats = {"ok": True, "model": "glm-5.3", "context": 299300,
                 "history": [{"kind": "compact", "context_before": 0}]}
        usage = {
            "session_key_hash": hashlib.sha256(session.encode()).hexdigest(),
            "model": "gpt-5.6-sol",
            "effort": "high",
            "model_context_window": 258400,
            "turns_started": 2,
            "last": {"input_tokens": 63893, "cached_input_tokens": 50000,
                     "cache_write_input_tokens": 0},
        }
        result = cache_usage.apply_openai_usage(stats, usage, session)
        self.assertEqual(result["model"], "gpt-5.6-sol")
        self.assertEqual(result["context"], 63893)
        self.assertEqual(result["context_window"], 258400)
        self.assertEqual(result["effort"], "high")
        self.assertEqual(result["last"]["read"], 50000)
        self.assertEqual(result["last"]["verdict"], "попадание")
        self.assertEqual(result["last"]["cache_status"], "попадание")
        self.assertEqual(result["history"][0]["context_after"], 63893)

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

    def test_context_event_updates_are_collapsed_by_event_id(self):
        session = "session-42"
        session_hash = hashlib.sha256(session.encode()).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "codex-context-events.jsonl")
            with open(path, "w", encoding="utf-8") as handle:
                for after in (None, 63893):
                    handle.write(json.dumps({
                        "kind": "compact", "event_id": "turn:1",
                        "session_hash": session_hash,
                        "ts": "2026-09-05T10:00:00+00:00",
                        "context_before": 247329, "context_after": after,
                    }) + "\n")
            result = cache_usage.context_events(session, tmp)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["context_after"], 63893)

    def test_codex_context_window_uses_effective_percentage(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "models_cache.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"models": [{
                    "slug": "gpt-test", "context_window": 272000,
                    "effective_context_window_percent": 95,
                }]}, handle)
            result = cache_usage.codex_model_context_window("gpt-test", path)
        self.assertEqual(result, 258400)

    def test_openai_fallback_marks_missing_live_cache_usage_unknown(self):
        stats = {
            "ok": True, "context": 247329,
            "last": {"read": 0, "write": 0, "verdict": "промах"},
            "history": [{"kind": "compact", "context_window": None}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "models_cache.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"models": [{
                    "slug": "gpt-test", "context_window": 272000,
                    "effective_context_window_percent": 95,
                }]}, handle)
            cache_usage.apply_openai_fallback(stats, "gpt-test", path)
        self.assertEqual(stats["context"], 247329)
        self.assertEqual(stats["context_window"], 258400)
        self.assertEqual(stats["history"][0]["context_window"], 258400)
        self.assertIsNone(stats["last"]["read"])
        self.assertEqual(stats["last"]["verdict"], "н/д")

    def test_openai_first_live_turn_is_labeled_cold_start(self):
        session = "session-42"
        stats = {"ok": True, "last": {}, "history": []}
        usage = {
            "session_key_hash": hashlib.sha256(session.encode()).hexdigest(),
            "last": {"input_tokens": 249919, "cached_input_tokens": 0,
                     "cache_write_input_tokens": 0},
            "turns_started": 1,
        }
        cache_usage.apply_openai_usage(stats, usage, session)
        self.assertEqual(stats["last"]["cache_status"], "холодный старт")
        self.assertEqual(stats["last"]["read"], 0)
        self.assertEqual(stats["last"]["write"], 0)

    def test_openai_zero_input_record_restores_compaction_and_context(self):
        records = [
            {"timestamp": "2026-09-05T06:14:32Z", "message": {
                "model": "gpt-5.6-sol", "usage": {
                    "input_tokens": 247329, "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0, "output_tokens": 231,
                }}},
            {"timestamp": "2026-09-05T06:16:13Z", "message": {
                "model": "gpt-5.6-sol", "usage": {
                    "input_tokens": 0, "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0, "output_tokens": 231,
                }}},
            {"timestamp": "2026-09-05T09:46:50Z", "message": {
                "model": "gpt-5.6-sol", "usage": {
                    "input_tokens": 249919, "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0, "output_tokens": 173,
                }}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            transcript = os.path.join(tmp, "session-42.jsonl")
            with open(transcript, "w", encoding="utf-8") as handle:
                for record in records:
                    handle.write(json.dumps(record) + "\n")
            result = cache_usage.collect(transcript, state_dir=tmp)
        compact = next(item for item in result["history"]
                       if item["kind"] == "compact")
        self.assertEqual(result["context"], 249919)
        self.assertEqual(compact["context_before"], 247329)
        self.assertTrue(compact["inferred"])

    def test_compaction_is_included_in_history(self):
        state = {"started": "2026-09-05T09:00:00+00:00", "miss_log": []}
        history = cache_usage.build_history(state, [], [], [{
            "kind": "compact", "ts": "2026-09-05T10:00:00+00:00",
            "model": "gpt-5.6-sol", "context_before": 200000,
            "context_window": 258400, "context_after": None,
        }])
        self.assertEqual(history, [{
            "kind": "compact", "ts": "2026-09-05T10:00:00+00:00",
            "model": "gpt-5.6-sol", "context_before": 200000,
            "context_window": 258400, "context_after": None,
        }])

    def test_all_cache_hits_are_grouped_across_misses_and_models(self):
        marked = [
            {"ts": "2026-09-05T10:00:00+00:00", "model": "glm-5.3",
             "verdict": "попадание", "read": 100, "explain": None},
            {"ts": "2026-09-05T10:01:00+00:00", "model": "glm-5.3",
             "verdict": "попадание", "read": 120, "explain": None},
            {"ts": "2026-09-05T10:02:00+00:00", "model": "glm-5.3",
             "verdict": "частичное", "read": 20, "explain": None},
            {"ts": "2026-09-05T10:03:00+00:00", "model": "gpt-5.6-sol",
             "verdict": "попадание", "read": 200, "explain": "model"},
            {"ts": "2026-09-05T10:04:00+00:00", "model": "gpt-5.6-sol",
             "verdict": "попадание", "read": 250, "explain": None},
        ]
        runs = cache_usage.cache_hit_runs(marked, [], [])
        self.assertEqual([(run["count"], run["read"], run["models"])
                          for run in runs], [
            (4, 670, ["glm-5.3", "gpt-5.6-sol"]),
        ])
        self.assertEqual([
            (detail["count"], detail["read"], detail["model"])
            for detail in runs[0]["details"]
        ], [
            (2, 220, "glm-5.3"),
            (2, 450, "gpt-5.6-sol"),
        ])

    def test_account_event_does_not_split_session_cache_total(self):
        marked = [
            {"ts": "2026-09-05T10:00:00+00:00", "model": "same",
             "verdict": "попадание", "read": 100, "explain": None},
            {"ts": "2026-09-05T10:02:00+00:00", "model": "same",
             "verdict": "попадание", "read": 120, "explain": None},
        ]
        events = [{"ts": "2026-09-05T10:01:00+00:00"}]
        runs = cache_usage.cache_hit_runs(marked, events, [])
        self.assertEqual([run["count"] for run in runs], [2])
        self.assertEqual(runs[0]["read"], 220)
        self.assertEqual([detail["count"] for detail in runs[0]["details"]],
                         [1, 1])


if __name__ == "__main__":
    unittest.main()
