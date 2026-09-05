#!/usr/bin/env python3
"""Tests for text conversion and Anthropic-compatible stream framing."""

import json
import os
import tempfile
import threading
import unittest
from unittest import mock

from codex_anthropic_bridge import (
    BridgeError,
    CodexTextBackend,
    DynamicToolCall,
    TextTurn,
    USAGE_READY,
    build_prompt,
    build_request,
    collect_message,
    claude_session_key,
    dynamic_tools,
    _dynamic_result,
    _followup_payload,
    _normalized_usage,
    message_object,
    prepare_dynamic_tools,
    select_effort,
    stream_events,
)


class PromptConversionTests(unittest.TestCase):
    def test_codex_token_usage_fields_are_preserved(self):
        self.assertEqual(_normalized_usage({
            "inputTokens": 120,
            "cachedInputTokens": 80,
            "cacheWriteInputTokens": 20,
            "outputTokens": 10,
            "reasoningOutputTokens": 7,
            "totalTokens": 130,
        }), {
            "input_tokens": 120,
            "cached_input_tokens": 80,
            "cache_write_input_tokens": 20,
            "output_tokens": 10,
            "reasoning_output_tokens": 7,
            "total_tokens": 130,
        })

    def test_claude_session_id_is_extracted_from_metadata_user_id(self):
        self.assertEqual(claude_session_key({"metadata": {"user_id": json.dumps({
            "device_id": "device-1", "session_id": "session-42",
        })}}), "session-42")

    def test_followup_contains_only_new_user_message(self):
        old = [{"role": "user", "content": "first"}]
        previous = [json.dumps(old[0], ensure_ascii=False, sort_keys=True)]
        payload = {"messages": old + [
            {"role": "assistant", "content": "answer"},
            {"role": "user", "content": "second"},
        ]}
        self.assertEqual(_followup_payload(payload, previous)["messages"], [
            {"role": "user", "content": "second"},
        ])

    def test_rewritten_history_sends_only_latest_user_message(self):
        payload = {"messages": [
            {"role": "assistant", "content": "compacted summary"},
            {"role": "user", "content": "continue safely"},
        ]}
        followup = _followup_payload(payload, ["different"])
        self.assertEqual(followup["messages"], [
            {"role": "user", "content": "continue safely"},
        ])

    def test_ephemeral_tail_after_new_user_does_not_hide_followup(self):
        payload = {"messages": [
            {"role": "user", "content": "old"},
            {"role": "user", "content": "new request"},
            {"role": "assistant", "content": "ephemeral scaffold"},
        ]}
        followup = _followup_payload(payload, ["fingerprint-not-present"])
        self.assertEqual(followup["messages"], [
            {"role": "user", "content": "new request"},
        ])

    def test_claude_output_config_effort_is_selected(self):
        self.assertEqual(select_effort({
            "output_config": {"effort": "high"}, "effort": "low",
        }), "high")

    def test_tool_result_becomes_successful_dynamic_response(self):
        result = _dynamic_result({
            "type": "tool_result", "tool_use_id": "call_1",
            "content": [{"type": "text", "text": "contents"}],
        })
        self.assertTrue(result["success"])
        self.assertEqual(result["contentItems"][0]["text"], "contents")

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

    def test_system_role_inside_messages_keeps_privileged_precedence(self):
        developer, prompt = build_prompt({
            "messages": [
                {"role": "system", "content": "Internal context"},
                {"role": "developer", "content": [{
                    "type": "text", "text": "Extension instruction",
                }]},
                {"role": "user", "content": "Tell me about yourself"},
            ],
        })
        self.assertIn("SYSTEM MESSAGE FROM CLAUDE CODE:\nInternal context", developer)
        self.assertIn("DEVELOPER MESSAGE FROM CLAUDE CODE", developer)
        self.assertNotIn("<system>", prompt)
        self.assertIn("<user>\nTell me about yourself\n</user>", prompt)

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

    def test_base64_image_becomes_codex_image_input(self):
        developer, prompt, images = build_request({
            "messages": [{"role": "user", "content": [
                {"type": "image", "source": {
                    "type": "base64", "media_type": "image/png",
                    "data": "aGVsbG8=",
                }},
                {"type": "text", "text": "Describe it"},
            ]}],
        })
        self.assertIn('<image attachment="1" />', prompt)
        self.assertIn("IMAGE ATTACHMENTS", developer)
        self.assertEqual(images, [{
            "type": "image", "url": "data:image/png;base64,aGVsbG8=",
        }])

    def test_image_inside_tool_result_is_preserved(self):
        _, prompt, images = build_request({
            "messages": [{"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": "call_1",
                "content": [{"type": "image", "source": {
                    "type": "url", "url": "https://example.test/image.png",
                }}],
            }]}],
        })
        self.assertIn('<image attachment="1" />', prompt)
        self.assertEqual(images[0]["url"], "https://example.test/image.png")

    def test_server_tool_use_is_preserved_as_history(self):
        _, prompt, _ = build_request({
            "messages": [{"role": "assistant", "content": [{
                "type": "server_tool_use", "id": "call_1",
                "name": "analyze_image", "input": {},
            }]}],
        })
        self.assertIn('<server_tool_use id="call_1" name="analyze_image">', prompt)
        self.assertIn("</server_tool_use>", prompt)

    def test_private_thinking_is_omitted_without_rejecting_history(self):
        _, prompt, _ = build_request({
            "messages": [{"role": "assistant", "content": [{
                "type": "thinking", "thinking": "private chain",
                "signature": "opaque-signature",
            }]}],
        })
        self.assertIn('type="thinking" omitted="true"', prompt)
        self.assertNotIn("private chain", prompt)
        self.assertNotIn("opaque-signature", prompt)

    def test_unknown_future_block_is_quoted_instead_of_rejected(self):
        _, prompt, _ = build_request({
            "messages": [{"role": "assistant", "content": [{
                "type": "future_server_result", "value": "kept",
                "signature": "secret-opaque-value",
            }]}],
        })
        self.assertIn("future_server_result", prompt)
        self.assertIn("kept", prompt)
        self.assertNotIn("secret-opaque-value", prompt)

    def test_dynamic_tool_schema_conversion(self):
        result = dynamic_tools({"tools": [{
            "name": "Read", "description": "Read a file",
            "input_schema": {"type": "object", "required": ["file_path"]},
        }]})
        self.assertEqual(result[0]["type"], "function")
        self.assertEqual(result[0]["inputSchema"]["required"], ["file_path"])

    def test_reserved_mcp_tool_name_round_trips(self):
        tools, names = prepare_dynamic_tools({"tools": [{
            "name": "mcp__drive__search", "description": "Search",
            "input_schema": {"type": "object"},
        }]})
        self.assertEqual(tools[0]["name"], "claude_mcp_tool_0")
        self.assertEqual(names["claude_mcp_tool_0"], "mcp__drive__search")


class AnthropicResponseTests(unittest.TestCase):
    def test_non_streaming_message_shape(self):
        message = message_object("msg_1", "gpt-test", "hello")
        self.assertEqual(message["role"], "assistant")
        self.assertEqual(message["content"][0]["text"], "hello")
        self.assertEqual(message["stop_reason"], "end_turn")

    def test_non_streaming_message_reports_real_usage(self):
        turn = TextTurn("msg_1", "gpt-test", iter(["ok"]), {
            "input_tokens": 120, "cached_input_tokens": 80,
            "cache_write_input_tokens": 20, "output_tokens": 10,
        })
        usage = collect_message(turn)["usage"]
        self.assertEqual(usage["input_tokens"], 120)
        self.assertEqual(usage["cache_read_input_tokens"], 80)
        self.assertEqual(usage["cache_creation_input_tokens"], 20)
        self.assertEqual(usage["output_tokens"], 10)

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

    def test_stream_primes_usage_before_message_start(self):
        usage = {}

        def chunks():
            usage.update({
                "input_tokens": 246872,
                "cached_input_tokens": 100000,
                "output_tokens": 3,
            })
            yield "ok"

        events = list(stream_events(TextTurn(
            "msg_1", "gpt-test", chunks(), usage,
        )))
        start_usage = events[0][1]["message"]["usage"]
        self.assertEqual(start_usage["input_tokens"], 246872)
        self.assertEqual(start_usage["cache_read_input_tokens"], 100000)

    def test_stream_buffers_only_until_usage_notification(self):
        usage = {}
        progress = []

        def chunks():
            progress.append("early text")
            yield "early"
            usage["input_tokens"] = 100
            progress.append("usage")
            yield USAGE_READY
            progress.append("late text")
            yield "late"

        events = stream_events(TextTurn("msg_1", "gpt-test", chunks(), usage))
        first_event = next(events)
        self.assertEqual(first_event[0], "message_start")
        self.assertEqual(first_event[1]["message"]["usage"]["input_tokens"], 100)
        self.assertEqual(progress, ["early text", "usage"])
        self.assertIn("late", json.dumps(list(events)))

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

    def test_messages_route_accepts_claude_beta_query(self):
        handler = object.__new__(__import__(
            "codex_anthropic_bridge").BridgeHandler)
        handler.path = "/v1/messages?beta=true"
        handler.headers = {"Content-Length": "2"}
        handler.rfile = __import__("io").BytesIO(b"{}")
        handler.server = mock.Mock()
        handler.server.backend.begin.side_effect = BridgeError("messages required")
        handler._json = mock.Mock()
        handler.do_POST()
        self.assertNotEqual(handler._json.call_args.args[0], 404)


class PersistentSessionTests(unittest.TestCase):
    class FakeClient:
        def __init__(self):
            self.requests = []
            self.turn_number = 0

        def request(self, method, params):
            self.requests.append((method, params))
            if method == "thread/start":
                return {"thread": {
                    "id": "thread-1", "model": "gpt-test",
                    "reasoningEffort": "low",
                }}
            if method == "turn/start":
                self.turn_number += 1
                return {"turn": {"id": f"turn-{self.turn_number}"}}
            return {}

        def snapshot(self):
            return {}

    @staticmethod
    def payload(messages):
        return {
            "metadata": {"user_id": json.dumps({"session_id": "claude-1"})},
            "messages": messages,
            "tools": [{
                "name": "Read", "description": "Read",
                "input_schema": {"type": "object"},
            }],
        }

    def test_tool_result_resumes_same_codex_turn_and_thread(self):
        backend = CodexTextBackend(timeout=1)
        backend.client = self.FakeClient()
        first_messages = [{"role": "user", "content": "Read a file"}]
        first = backend.begin(self.payload(first_messages))
        holder = {}

        def request_tool():
            holder["response"] = backend._handle_server_request({
                "method": "item/tool/call",
                "params": {
                    "threadId": "thread-1", "callId": "call-1",
                    "tool": "Read", "arguments": {"file_path": "/tmp/a"},
                },
            })

        worker = threading.Thread(target=request_tool)
        worker.start()
        tool_message = collect_message(first)
        self.assertEqual(tool_message["stop_reason"], "tool_use")

        second_messages = first_messages + [
            {"role": "assistant", "content": [{
                "type": "tool_use", "id": "call-1", "name": "Read",
                "input": {"file_path": "/tmp/a"},
            }]},
            {"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": "call-1",
                "content": "file contents",
            }]},
        ]
        second = backend.begin(self.payload(second_messages))
        worker.join(timeout=1)
        self.assertEqual(holder["response"]["contentItems"][0]["text"], "file contents")
        session = backend._sessions["claude-1"]
        session.events.put({
            "method": "item/agentMessage/delta",
            "params": {"threadId": "thread-1", "turnId": "turn-1", "delta": "done"},
        })
        session.events.put({
            "method": "turn/completed",
            "params": {"threadId": "thread-1", "turnId": "turn-1",
                       "turn": {"status": "completed"}},
        })
        self.assertEqual(collect_message(second)["content"][0]["text"], "done")

        third_messages = second_messages + [
            {"role": "assistant", "content": "done"},
            {"role": "user", "content": "Now summarize"},
        ]
        third = backend.begin(self.payload(third_messages))
        turn_starts = [params for method, params in backend.client.requests
                       if method == "turn/start"]
        thread_starts = [1 for method, _params in backend.client.requests
                         if method == "thread/start"]
        self.assertEqual(len(thread_starts), 1)
        self.assertEqual(len(turn_starts), 2)
        self.assertIn("Now summarize", turn_starts[-1]["input"][0]["text"])
        self.assertNotIn("file contents", turn_starts[-1]["input"][0]["text"])
        self.assertEqual(session.turns_started, 2)
        self.assertEqual(session.tool_continuations, 1)
        self.assertEqual(
            session.last_input_chars, len(turn_starts[-1]["input"][0]["text"]),
        )
        session.events.put({
            "method": "turn/completed",
            "params": {"threadId": "thread-1", "turnId": "turn-2",
                       "turn": {"status": "completed"}},
        })
        collect_message(third)

    def test_usage_notification_updates_turn_and_safe_snapshot(self):
        backend = CodexTextBackend(timeout=1)
        backend.client = self.FakeClient()
        turn = backend.begin(self.payload([{"role": "user", "content": "hello"}]))
        backend._handle_notification({
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "thread-1", "turnId": "turn-1",
                "tokenUsage": {
                    "last": {
                        "inputTokens": 100, "cachedInputTokens": 75,
                        "cacheWriteInputTokens": 5, "outputTokens": 12,
                        "reasoningOutputTokens": 8, "totalTokens": 112,
                    },
                    "total": {
                        "inputTokens": 100, "cachedInputTokens": 75,
                        "cacheWriteInputTokens": 5, "outputTokens": 12,
                        "reasoningOutputTokens": 8, "totalTokens": 112,
                    },
                    "modelContextWindow": 258400,
                },
            },
        })
        session = backend._sessions["claude-1"]
        session.events.put({
            "method": "turn/completed",
            "params": {"threadId": "thread-1", "turnId": "turn-1",
                       "turn": {"status": "completed"}},
        })
        message = collect_message(turn)
        self.assertEqual(message["usage"]["input_tokens"], 100)
        self.assertEqual(message["usage"]["cache_read_input_tokens"], 75)
        with mock.patch("codex_anthropic_bridge._safe_probe", return_value={"account": {}}):
            snapshot = backend.snapshot()
        self.assertEqual(snapshot["bridgeUsage"]["last"]["total_tokens"], 112)
        self.assertEqual(snapshot["bridgeUsage"]["model_context_window"], 258400)
        self.assertEqual(snapshot["bridgeUsage"]["effort"], "low")
        self.assertEqual(snapshot["bridgeUsage"]["turns_started"], 1)

    def test_safe_usage_snapshot_survives_bridge_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "usage.json")
            backend = CodexTextBackend(timeout=1, usage_state_file=path)
            backend.client = self.FakeClient()
            backend.begin(self.payload([{"role": "user", "content": "hello"}]))
            backend._handle_notification({
                "method": "thread/tokenUsage/updated",
                "params": {"threadId": "thread-1", "tokenUsage": {
                    "last": {"inputTokens": 247329, "cachedInputTokens": 200000},
                    "total": {"inputTokens": 247329, "cachedInputTokens": 200000},
                    "modelContextWindow": 258400,
                }},
            })
            restored = CodexTextBackend(timeout=1, usage_state_file=path)
            restored.client = self.FakeClient()
            with mock.patch("codex_anthropic_bridge._safe_probe", return_value={}):
                snapshot = restored.snapshot()["bridgeUsage"]
        self.assertEqual(snapshot["last"]["input_tokens"], 247329)
        self.assertEqual(snapshot["last"]["cached_input_tokens"], 200000)
        self.assertEqual(snapshot["model_context_window"], 258400)

    def test_usage_arriving_after_completion_is_included_in_stream_start(self):
        backend = CodexTextBackend(timeout=1)
        backend.client = self.FakeClient()
        turn = backend.begin(self.payload([{"role": "user", "content": "hello"}]))
        session = backend._sessions["claude-1"]
        session.events.put({
            "method": "item/agentMessage/delta",
            "params": {"threadId": "thread-1", "turnId": "turn-1", "delta": "ok"},
        })
        session.events.put({
            "method": "turn/completed",
            "params": {"threadId": "thread-1", "turnId": "turn-1",
                       "turn": {"status": "completed"}},
        })

        def delayed_usage():
            __import__("time").sleep(0.01)
            backend._handle_notification({
                "method": "thread/tokenUsage/updated",
                "params": {"threadId": "thread-1", "turnId": "turn-1",
                           "tokenUsage": {
                               "last": {"inputTokens": 89800,
                                        "cachedInputTokens": 25300,
                                        "outputTokens": 12},
                               "total": {"inputTokens": 89800,
                                         "cachedInputTokens": 25300,
                                         "outputTokens": 12},
                               "modelContextWindow": 258400,
                           }},
            })

        worker = threading.Thread(target=delayed_usage)
        worker.start()
        events = list(stream_events(turn))
        worker.join(timeout=1)
        usage = events[0][1]["message"]["usage"]
        self.assertEqual(usage["input_tokens"], 89800)
        self.assertEqual(usage["cache_read_input_tokens"], 25300)

    def test_high_effort_is_forwarded_to_turn_and_reported(self):
        backend = CodexTextBackend(timeout=1)
        backend.client = self.FakeClient()
        payload = self.payload([{"role": "user", "content": "think"}])
        payload["output_config"] = {"effort": "high"}
        turn = backend.begin(payload)
        session = backend._sessions["claude-1"]
        turn_start = next(params for method, params in backend.client.requests
                          if method == "turn/start")
        self.assertEqual(turn_start["effort"], "high")
        self.assertEqual(session.effort, "high")
        session.events.put({
            "method": "turn/completed",
            "params": {"threadId": "thread-1", "turnId": "turn-1",
                       "turn": {"status": "completed"}},
        })
        collect_message(turn)

    def test_compaction_notification_is_persisted_once(self):
        backend = CodexTextBackend(timeout=1)
        backend.client = self.FakeClient()
        backend.begin(self.payload([{"role": "user", "content": "hello"}]))
        backend._handle_notification({
            "method": "thread/tokenUsage/updated",
            "params": {"threadId": "thread-1", "tokenUsage": {
                "last": {"inputTokens": 120}, "total": {"inputTokens": 120},
                "modelContextWindow": 258400,
            }},
        })
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "codex_anthropic_bridge.CONTEXT_EVENTS_FILE",
            os.path.join(tmp, "events.jsonl"),
        ):
            modern = {
                "method": "item/completed",
                "params": {"threadId": "thread-1", "turnId": "turn-1",
                           "item": {"id": "compact-1", "type": "contextCompaction"}},
            }
            backend._handle_notification(modern)
            backend._handle_notification({
                "method": "thread/compacted",
                "params": {"threadId": "thread-1", "turnId": "turn-1"},
            })
            backend._handle_notification({
                "method": "thread/tokenUsage/updated",
                "params": {"threadId": "thread-1", "tokenUsage": {
                    "last": {"inputTokens": 80}, "total": {"inputTokens": 200},
                    "modelContextWindow": 258400,
                }},
            })
            with open(os.path.join(tmp, "events.jsonl"), encoding="utf-8") as handle:
                events = [json.loads(line) for line in handle]
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["event_id"], events[1]["event_id"])
        self.assertEqual(events[0]["kind"], "compact")
        self.assertEqual(events[0]["context_before"], 120)
        self.assertNotIn("context_after", events[0])
        self.assertEqual(events[1]["context_after"], 80)
        self.assertEqual(events[1]["context_window"], 258400)

    def test_history_and_tool_metadata_changes_do_not_recreate_thread(self):
        backend = CodexTextBackend(timeout=1)
        backend.client = self.FakeClient()
        first = backend.begin(self.payload([
            {"role": "user", "content": "old long conversation"},
        ]))
        session = backend._sessions["claude-1"]
        session.events.put({
            "method": "turn/completed",
            "params": {"threadId": "thread-1", "turnId": "turn-1",
                       "turn": {"status": "completed"}},
        })
        collect_message(first)

        changed = self.payload([
            {"role": "assistant", "content": "new compacted summary"},
            {"role": "user", "content": "continue after compaction"},
        ])
        changed["tools"][0]["description"] = "Updated description"
        second = backend.begin(changed)
        thread_starts = [params for method, params in backend.client.requests
                         if method == "thread/start"]
        turn_starts = [params for method, params in backend.client.requests
                       if method == "turn/start"]
        self.assertEqual(len(thread_starts), 1)
        self.assertEqual(len(turn_starts), 2)
        latest_input = turn_starts[-1]["input"][0]["text"]
        self.assertIn("continue after compaction", latest_input)
        self.assertNotIn("old long conversation", latest_input)
        session.events.put({
            "method": "turn/completed",
            "params": {"threadId": "thread-1", "turnId": "turn-2",
                       "turn": {"status": "completed"}},
        })
        collect_message(second)


if __name__ == "__main__":
    unittest.main()
