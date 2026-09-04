#!/usr/bin/env python3
"""Tests for text conversion and Anthropic-compatible stream framing."""

import json
import unittest

from codex_anthropic_bridge import (
    BridgeError,
    TextTurn,
    build_prompt,
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

    def test_non_text_content_is_rejected(self):
        with self.assertRaisesRegex(BridgeError, "text content only"):
            build_prompt({
                "messages": [{"role": "user", "content": [
                    {"type": "image", "source": {"data": "..."}},
                ]}],
            })


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


if __name__ == "__main__":
    unittest.main()
