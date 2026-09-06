#!/usr/bin/env python3
"""Offline checks for tmp/codex_tool_token_experiment.py."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("codex_tool_token_experiment.py")
SPEC = importlib.util.spec_from_file_location("codex_tool_token_experiment", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
experiment = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = experiment
SPEC.loader.exec_module(experiment)


class ExperimentTests(unittest.TestCase):
    def test_raw_subsets_start_with_omitted_tools(self) -> None:
        tools = [
            {"type": "function", "name": "a", "description": "", "inputSchema": {}},
            {"type": "function", "name": "b", "description": "", "inputSchema": {}},
        ]
        metrics = experiment.raw_subset_metrics(tools, len)
        self.assertEqual(metrics[0]["raw_json_chars"], 0)
        self.assertEqual(metrics[0]["raw_json_tokens"], 0)
        self.assertEqual(metrics[1]["raw_json_tokens"], len(experiment.compact_json(tools[:1])))
        self.assertEqual(metrics[2]["raw_json_tokens"], len(experiment.compact_json(tools)))

    def test_enrichment_telescopes_to_full_increment(self) -> None:
        rows = [
            {"input_tokens": 100, "raw_json_tokens": 0},
            {"input_tokens": 116, "raw_json_tokens": 10},
            {"input_tokens": 143, "raw_json_tokens": 25},
        ]
        experiment.enrich_rows(rows)
        self.assertEqual(rows[1]["server_delta"], 16)
        self.assertEqual(rows[1]["internal_delta"], 6)
        self.assertEqual(rows[2]["server_delta"], 27)
        self.assertEqual(rows[2]["internal_delta"], 12)
        self.assertEqual(sum(row["server_delta"] for row in rows[1:]), 43)
        summary = experiment.summarize(rows)
        self.assertEqual(summary["server_tool_increment_tokens"], 43)
        self.assertEqual(summary["raw_tool_json_tokens"], 25)
        self.assertEqual(summary["internal_tool_increment_tokens"], 18)

    def test_null_raw_tokens_leave_internal_delta_unknown(self) -> None:
        rows = [
            {"input_tokens": 100, "raw_json_tokens": 0},
            {"input_tokens": 120, "raw_json_tokens": None},
        ]
        experiment.enrich_rows(rows)
        self.assertEqual(rows[1]["server_delta"], 20)
        self.assertIsNone(rows[1]["raw_delta"])
        self.assertIsNone(rows[1]["internal_delta"])

    def test_collector_accepts_usage_before_completion(self) -> None:
        collector = experiment.EventCollector()
        collector.on_notification({
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "thread-1",
                "tokenUsage": {"last": {
                    "inputTokens": 123, "cachedInputTokens": 45,
                    "outputTokens": 2, "totalTokens": 125,
                }},
            },
        })
        collector.on_notification({
            "method": "turn/completed",
            "params": {
                "threadId": "thread-1",
                "turn": {"id": "turn-1", "status": "completed"},
            },
        })
        usage = collector.wait_for_turn("thread-1", "turn-1", 1)
        self.assertEqual(usage["input_tokens"], 123)
        self.assertEqual(usage["cached_input_tokens"], 45)

    def test_tool_calls_are_recorded_and_rejected(self) -> None:
        collector = experiment.EventCollector()
        response = collector.on_server_request({
            "method": "item/tool/call",
            "params": {"threadId": "thread-1", "tool": "Bash"},
        })
        self.assertEqual(collector.tool_calls("thread-1"), ["Bash"])
        self.assertFalse(response["success"])
        self.assertEqual(response["contentItems"][0]["type"], "inputText")

    def test_unrelated_thread_events_are_ignored(self) -> None:
        collector = experiment.EventCollector()
        collector.events.put({
            "method": "turn/completed",
            "params": {
                "threadId": "other",
                "turn": {"id": "other-turn", "status": "completed"},
            },
        })
        collector.events.put({
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "thread-1",
                "tokenUsage": {"last": {"inputTokens": 7}},
            },
        })
        collector.events.put({
            "method": "turn/completed",
            "params": {
                "threadId": "thread-1",
                "turn": {"id": "turn-1", "status": "completed"},
            },
        })
        self.assertEqual(
            collector.wait_for_turn("thread-1", "turn-1", 1)["input_tokens"], 7,
        )

    def test_resume_requires_matching_experiment(self) -> None:
        tools = [{"name": "Agent"}]
        document = {
            "format_version": 1,
            "capture_sha256": "abc",
            "model": "model-a",
            "effort": "medium",
            "prompt_mode": "probe",
            "tokenizer": "tiktoken:o200k_base",
            "tool_order": ["Agent"],
            "rows": [{"step": 0, "input_tokens": 100}],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            rows = experiment.load_resume_rows(
                path, capture_hash="abc", model="model-a", effort="medium",
                prompt_mode="probe", tokenizer="tiktoken:o200k_base",
                tools=tools,
            )
            self.assertEqual(len(rows), 1)
            with self.assertRaises(experiment.ExperimentError):
                experiment.load_resume_rows(
                    path, capture_hash="different", model="model-a",
                    effort="medium", prompt_mode="probe",
                    tokenizer="tiktoken:o200k_base", tools=tools,
                )

    def test_csv_uses_lf_without_carriage_returns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.csv"
            experiment.write_csv(path, [{
                "step": 0,
                "added_tool": None,
                "tool_count": 0,
                "input_tokens": 100,
            }])
            content = path.read_bytes()
            self.assertNotIn(b"\r", content)
            self.assertTrue(content.endswith(b"\n"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
