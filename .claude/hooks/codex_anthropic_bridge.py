#!/usr/bin/env python3
"""Loopback-only Anthropic Messages facade backed by Codex app-server.

Phase 69.2 implements text requests and text streaming. Tool calls are added in
the next phase. OAuth remains wholly owned by the official Codex process.
"""

from __future__ import annotations

import argparse
import http.server
import ipaddress
import json
import os
import queue
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Iterator

from codex_app_server import CodexAppServerClient, CodexAppServerError


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18924
DEFAULT_REQUEST_TIMEOUT = 600.0
BRIDGE_INSTRUCTIONS = """You are providing the model response inside Claude Code.
Return only the answer to the conversation supplied by the user. Do not discuss
this bridge, do not call built-in Codex tools, and do not inspect the filesystem.
Follow the supplied system instructions and conversation faithfully."""


class BridgeError(RuntimeError):
    pass


def _text_blocks(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    chunks: list[str] = []
    for block in value:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                chunks.append(text)
    return "\n".join(chunks)


def build_prompt(payload: dict[str, Any]) -> tuple[str, str]:
    """Convert a text-only Anthropic request to Codex instructions and input."""
    system = _text_blocks(payload.get("system"))
    developer = BRIDGE_INSTRUCTIONS
    if system:
        developer += "\n\nSYSTEM INSTRUCTIONS FROM CLAUDE CODE:\n" + system

    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise BridgeError("messages must be a non-empty array")
    rendered: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            raise BridgeError("each message must be an object")
        role = message.get("role")
        if role not in ("user", "assistant"):
            raise BridgeError(f"unsupported message role: {role!r}")
        content = message.get("content")
        text = _text_blocks(content)
        if not text and content not in ("", []):
            raise BridgeError("phase 69.2 supports text content only")
        rendered.append(f"<{role}>\n{text}\n</{role}>")
    rendered.append("<assistant>\n")
    return developer, "\n\n".join(rendered)


def select_model(payload: dict[str, Any]) -> str | None:
    requested = payload.get("model")
    if isinstance(requested, str) and requested.startswith("gpt-"):
        return requested
    configured = os.environ.get("CODEX_BRIDGE_MODEL", "").strip()
    return configured or None


class EventRouter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queues: dict[str, queue.Queue[dict[str, Any]]] = {}

    def register(self, thread_id: str) -> queue.Queue[dict[str, Any]]:
        target: queue.Queue[dict[str, Any]] = queue.Queue()
        with self._lock:
            self._queues[thread_id] = target
        return target

    def unregister(self, thread_id: str) -> None:
        with self._lock:
            self._queues.pop(thread_id, None)

    def dispatch(self, message: dict[str, Any]) -> None:
        params = message.get("params")
        thread_id = params.get("threadId") if isinstance(params, dict) else None
        if not isinstance(thread_id, str):
            return
        with self._lock:
            target = self._queues.get(thread_id)
        if target is not None:
            target.put(message)


@dataclass
class TextTurn:
    message_id: str
    model: str
    chunks: Iterator[str]


class CodexTextBackend:
    """Run isolated text-only turns while sharing one authenticated process."""

    def __init__(self, *, timeout: float = DEFAULT_REQUEST_TIMEOUT) -> None:
        self.timeout = timeout
        self.router = EventRouter()
        self.client = CodexAppServerClient(notification_handler=self.router.dispatch)

    def start(self) -> "CodexTextBackend":
        self.client.start()
        return self

    def close(self) -> None:
        self.client.close()

    def begin(self, payload: dict[str, Any]) -> TextTurn:
        developer, prompt = build_prompt(payload)
        model = select_model(payload)
        params: dict[str, Any] = {
            "approvalPolicy": "never",
            "baseInstructions": BRIDGE_INSTRUCTIONS,
            "cwd": "/tmp",
            "developerInstructions": developer,
            "ephemeral": True,
            "sandbox": "read-only",
        }
        if model:
            params["model"] = model
        started = self.client.request("thread/start", params)
        thread = started.get("thread") if isinstance(started, dict) else None
        thread_id = thread.get("id") if isinstance(thread, dict) else None
        if not isinstance(thread_id, str):
            raise BridgeError("thread/start returned no thread id")
        actual_model = thread.get("model") or model or "codex"
        events = self.router.register(thread_id)
        try:
            turn = self.client.request("turn/start", {
                "threadId": thread_id,
                "input": [{"type": "text", "text": prompt}],
            })
        except Exception:
            self.router.unregister(thread_id)
            raise
        turn_obj = turn.get("turn") if isinstance(turn, dict) else None
        turn_id = turn_obj.get("id") if isinstance(turn_obj, dict) else None
        return TextTurn(
            message_id="msg_" + uuid.uuid4().hex,
            model=str(actual_model),
            chunks=self._chunks(thread_id, turn_id, events),
        )

    def _chunks(
        self,
        thread_id: str,
        turn_id: str | None,
        events: queue.Queue[dict[str, Any]],
    ) -> Iterator[str]:
        deadline = time.monotonic() + self.timeout
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if turn_id:
                        self.client.request("turn/interrupt", {"threadId": thread_id})
                    raise BridgeError("Codex turn timed out")
                try:
                    event = events.get(timeout=min(remaining, 1.0))
                except queue.Empty:
                    continue
                method = event.get("method")
                params = event.get("params") or {}
                if turn_id and params.get("turnId") not in (None, turn_id):
                    continue
                if method == "item/agentMessage/delta":
                    delta = params.get("delta")
                    if isinstance(delta, str) and delta:
                        yield delta
                elif method == "turn/completed":
                    turn = params.get("turn") or {}
                    status = turn.get("status")
                    if status != "completed":
                        error = turn.get("error") or {}
                        raise BridgeError(error.get("message") or f"turn {status}")
                    return
                elif method == "error":
                    error = params.get("error") or {}
                    raise BridgeError(error.get("message") or "Codex turn failed")
        finally:
            self.router.unregister(thread_id)


def message_object(message_id: str, model: str, text: str) -> dict[str, Any]:
    return {
        "id": message_id,
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "model": model,
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


def stream_events(turn: TextTurn) -> Iterator[tuple[str, dict[str, Any]]]:
    yield "message_start", {
        "type": "message_start",
        "message": message_object(turn.message_id, turn.model, "") | {
            "content": [], "stop_reason": None,
        },
    }
    yield "content_block_start", {
        "type": "content_block_start", "index": 0,
        "content_block": {"type": "text", "text": ""},
    }
    for chunk in turn.chunks:
        yield "content_block_delta", {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": chunk},
        }
    yield "content_block_stop", {"type": "content_block_stop", "index": 0}
    yield "message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": 0},
    }
    yield "message_stop", {"type": "message_stop"}


class BridgeHttpServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], backend: CodexTextBackend):
        self.backend = backend
        super().__init__(address, BridgeHandler)


class BridgeHandler(http.server.BaseHTTPRequestHandler):
    server: BridgeHttpServer
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: Any) -> None:
        # Request bodies and authorization headers must never reach a log.
        return

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._json(200, {"ok": True, "service": "claude-openai-bridge"})
        else:
            self._json(404, {"error": {"type": "not_found_error", "message": "not found"}})

    def do_POST(self) -> None:
        if self.path.rstrip("/") != "/v1/messages":
            self._json(404, {"error": {"type": "not_found_error", "message": "not found"}})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 32 * 1024 * 1024:
                raise BridgeError("invalid request size")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise BridgeError("request body must be an object")
            turn = self.server.backend.begin(payload)
            if payload.get("stream") is True:
                self._stream(turn)
            else:
                self._json(200, message_object(
                    turn.message_id, turn.model, "".join(turn.chunks)
                ))
        except (BridgeError, CodexAppServerError, json.JSONDecodeError) as exc:
            self._json(400, {"type": "error", "error": {
                "type": "api_error", "message": str(exc),
            }})

    def _stream(self, turn: TextTurn) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        for event, data in stream_events(turn):
            payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            self.wfile.write(f"event: {event}\ndata: {payload}\n\n".encode("utf-8"))
            self.wfile.flush()
        self.close_connection = True


def main() -> int:
    parser = argparse.ArgumentParser(description="Claude Code to Codex bridge")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    try:
        address = ipaddress.ip_address(args.host)
    except ValueError as exc:
        raise SystemExit(f"--host must be a numeric loopback address: {exc}")
    if not address.is_loopback:
        raise SystemExit("refusing to expose the bridge beyond loopback")

    backend = CodexTextBackend().start()
    try:
        server = BridgeHttpServer((args.host, args.port), backend)
        try:
            server.serve_forever(poll_interval=0.5)
        finally:
            server.server_close()
    except KeyboardInterrupt:
        pass
    finally:
        backend.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
