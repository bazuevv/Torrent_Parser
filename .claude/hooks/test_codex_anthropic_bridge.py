#!/usr/bin/env python3
"""Tests for text conversion and Anthropic-compatible stream framing."""

import json
import unittest

from codex_anthropic_bridge import (
    BridgeError,
    DynamicToolCall,
    TextTurn,
    build_prompt,
    collect_message,
    dynamic_tools,
    message_object,
    stream_events,
)


class PromptConversionTests(unittest.TestCase):
    def test_system_and_conversation_are_preserved(self):
        developer, prompt = build_prompt({
            "system": [{"type": "text", "text": "Answer briefly."}],
            "messages": [
                {"role": "user", "content": "Question"},
                {"role": "assistant", "content": [{"type": "text", "text": "Old"}]},
                {"role": "user", "content": "New question"},
            ],
        })
        self.assertIn("Answer briefly.", developer)
        self.assertIn("<assistant>\nOld\n</assistant>", prompt)
        self.assertTrue(prompt.endswith("<assistant>\n"))

    def test_tool_history_is_preserved(self):
        _, prompt = build_prompt({
            "messages": [
                {"role": "assistant", "content": [{
                    "type": "tool_use", "id": "call_1", "name": "Read",
                    "input": {"file_path": "/tmp/a"},
                }]},
                {"role": "user", "content": [{
                    "type": "tool_result", "tool_use_id": "call_1",
                    "content": "contents",
                }]},
            ],
        })
        self.assertIn('<tool_use id="call_1" name="Read">', prompt)
        self.assertIn('<tool_result id="call_1" is_error="false">', prompt)

    def test_dynamic_tool_schema_conversion(self):
        result = dynamic_tools({"tools": [{
            "name": "Read", "description": "Read a file",
            "input_schema": {"type": "object", "required": ["file_path"]},
        }]})
        self.assertEqual(result[0]["type"], "function")
        self.assertEqual(result[0]["inputSchema"]["required"], ["file_path"])


class AnthropicResponseTests(unittest.TestCase):
    def test_non_streaming_message_shape(self):
        message = message_object("msg_1", "gpt-test", "hello")
        self.assertEqual(message["role"], "assistant")
        self.assertEqual(message["content"][0]["text"], "hello")
        self.assertEqual(message["stop_reason"], "end_turn")

    def test_stream_has_required_order_and_text(self):
        turn = TextTurn("msg_1", "gpt-test", iter(["hel", "lo"]))
        events = list(stream_events(turn))
        self.assertEqual([event for event, _ in events], [
            "message_start",
            "content_block_start",
            "content_block_delta",
            "content_block_delta",
            "content_block_stop",
            "message_delta",
            "message_stop",
        ])
        encoded = json.dumps(events)
        self.assertIn("hel", encoded)
        self.assertIn("lo", encoded)

    def test_stream_emits_anthropic_tool_use(self):
        response = __import__("queue").Queue(maxsize=1)
        call = DynamicToolCall("call_1", "Read", {"file_path": "/tmp/a"}, response)
        turn = TextTurn("msg_1", "gpt-test", iter([call]))
        events = list(stream_events(turn))
        encoded = json.dumps(events)
        self.assertIn("tool_use", encoded)
        self.assertIn("input_json_delta", encoded)
        self.assertEqual(events[-2][1]["delta"]["stop_reason"], "tool_use")

    def test_non_streaming_tool_use(self):
        response = __import__("queue").Queue(maxsize=1)
        call = DynamicToolCall("call_1", "Bash", {"command": "pwd"}, response)
        message = collect_message(TextTurn("msg_1", "gpt-test", iter([call])))
        self.assertEqual(message["stop_reason"], "tool_use")
        self.assertEqual(message["content"][0]["name"], "Bash")


if __name__ == "__main__":
    unittest.main()
