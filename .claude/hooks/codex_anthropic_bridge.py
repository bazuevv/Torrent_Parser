#!/usr/bin/env python3
"""Loopback-only Anthropic Messages facade backed by Codex app-server.

Anthropic tools are exposed as Codex dynamic tools. OAuth remains wholly owned
by the official Codex process.
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
from codex_app_server import _safe_probe


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18924
DEFAULT_REQUEST_TIMEOUT = 600.0
SOURCE_MTIME = max(
    os.path.getmtime(__file__),
    os.path.getmtime(os.path.join(os.path.dirname(__file__), "codex_app_server.py")),
)
BRIDGE_INSTRUCTIONS = """You are providing the model response inside Claude Code.
Return only the answer to the conversation supplied by the user. Never call
built-in Codex tools and never inspect the filesystem directly. When dynamic
tools are supplied, they are Claude Code's tools: call them whenever the task
requires tool use. Follow the supplied system instructions and conversation
faithfully."""


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


def _image_input(block: dict[str, Any]) -> dict[str, Any]:
    source = block.get("source")
    if not isinstance(source, dict):
        raise BridgeError("image block has no source")
    source_type = source.get("type")
    if source_type == "base64":
        media_type = source.get("media_type")
        data = source.get("data")
        if not isinstance(media_type, str) or not media_type.startswith("image/"):
            raise BridgeError("base64 image has an invalid media_type")
        if not isinstance(data, str) or not data:
            raise BridgeError("base64 image has no data")
        url = f"data:{media_type};base64,{data}"
    elif source_type == "url":
        url = source.get("url")
        if not isinstance(url, str) or not url.startswith(("https://", "http://")):
            raise BridgeError("image URL must use http or https")
    else:
        raise BridgeError(f"unsupported image source: {source_type!r}")
    detail = block.get("detail")
    result: dict[str, Any] = {"type": "image", "url": url}
    if detail in ("auto", "low", "high", "original"):
        result["detail"] = detail
    return result


def _render_content(
    value: Any,
    image_inputs: list[dict[str, Any]] | None = None,
) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    rendered: list[str] = []
    for block in value:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text" and isinstance(block.get("text"), str):
            rendered.append(block["text"])
        elif block_type == "image":
            if image_inputs is None:
                raise BridgeError("image collection is not initialized")
            image_inputs.append(_image_input(block))
            rendered.append(f'<image attachment="{len(image_inputs)}" />')
        elif block_type in ("tool_use", "server_tool_use"):
            tag = str(block_type)
            rendered.append(
                f'<{tag} id="{block.get("id", "")}" '
                f'name="{block.get("name", "")}">\n'
                f'{json.dumps(block.get("input", {}), ensure_ascii=False)}\n'
                f"</{tag}>"
            )
        elif block_type == "tool_result":
            content = _render_content(block.get("content"), image_inputs)
            rendered.append(
                f'<tool_result id="{block.get("tool_use_id", "")}" '
                f'is_error="{str(bool(block.get("is_error"))).lower()}">\n'
                f"{content}\n</tool_result>"
            )
        elif block_type in ("thinking", "redacted_thinking"):
            # Previous providers may persist private reasoning in the
            # transcript. It is neither needed nor appropriate as input to
            # another model; retain only the fact that a block existed.
            rendered.append(f'<content_block type="{block_type}" omitted="true" />')
        else:
            # Claude Code adds new server-side result blocks over time. A
            # historical block must not make the whole conversation unusable:
            # preserve its public JSON as quoted context. Known binary image
            # blocks still take the native path above.
            public = {key: item for key, item in block.items()
                      if key not in ("signature", "data")}
            rendered.append(
                f"<content_block type={json.dumps(str(block_type))}>\n"
                f"{json.dumps(public, ensure_ascii=False)}\n"
                "</content_block>"
            )
    return "\n".join(rendered)


def build_request(
    payload: dict[str, Any],
) -> tuple[str, str, list[dict[str, Any]]]:
    """Convert an Anthropic conversation to Codex instructions and inputs."""
    image_inputs: list[dict[str, Any]] = []
    system = _text_blocks(payload.get("system"))
    developer = BRIDGE_INSTRUCTIONS
    if system:
        developer += "\n\nSYSTEM INSTRUCTIONS FROM CLAUDE CODE:\n" + system

    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise BridgeError("messages must be a non-empty array")
    rendered: list[str] = []
    privileged: list[tuple[str, str]] = []
    for message in messages:
        if not isinstance(message, dict):
            raise BridgeError("each message must be an object")
        role = message.get("role")
        if role not in ("system", "developer", "user", "assistant"):
            raise BridgeError(f"unsupported message role: {role!r}")
        content = message.get("content")
        text = _render_content(content, image_inputs)
        # Claude Code 2.1.220 may put additional privileged context in
        # `messages` instead of the top-level Anthropic `system` field.
        # Keep its precedence: it belongs in developerInstructions, not
        # among quoted user/assistant conversation turns.
        if role in ("system", "developer"):
            privileged.append((role, text))
            continue
        rendered.append(f"<{role}>\n{text}\n</{role}>")
    for role, text in privileged:
        developer += f"\n\n{role.upper()} MESSAGE FROM CLAUDE CODE:\n{text}"
    if image_inputs:
        developer += (
            "\n\nIMAGE ATTACHMENTS: Markers in the conversation refer to the "
            "attached image inputs in ascending order."
        )
    rendered.append("<assistant>\n")
    return developer, "\n\n".join(rendered), image_inputs


def build_prompt(payload: dict[str, Any]) -> tuple[str, str]:
    """Compatibility helper for tests and text-only callers."""
    developer, prompt, _images = build_request(payload)
    return developer, prompt


def select_model(payload: dict[str, Any]) -> str | None:
    requested = payload.get("model")
    if isinstance(requested, str) and requested.startswith("gpt-"):
        return requested
    configured = os.environ.get("CODEX_BRIDGE_MODEL", "").strip()
    return configured or None


def dynamic_tools(payload: dict[str, Any]) -> list[dict[str, Any]]:
    tools = payload.get("tools")
    if tools is None:
        return []
    if not isinstance(tools, list):
        raise BridgeError("tools must be an array")
    converted: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
            raise BridgeError("each tool must have a name")
        schema = tool.get("input_schema", {"type": "object"})
        if not isinstance(schema, dict):
            raise BridgeError(f"tool {tool['name']!r} has an invalid input_schema")
        converted.append({
            "type": "function",
            "name": tool["name"],
            "description": str(tool.get("description", "")),
            "inputSchema": schema,
        })
    return converted


def prepare_dynamic_tools(
    payload: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Rename names reserved by Codex and retain the Anthropic names."""
    prepared = dynamic_tools(payload)
    original_names: dict[str, str] = {}
    used = {tool["name"] for tool in prepared}
    for index, tool in enumerate(prepared):
        original = tool["name"]
        safe = original
        if original.startswith("mcp__"):
            safe = f"claude_mcp_tool_{index}"
            while safe in used:
                safe += "_"
            used.add(safe)
            tool["name"] = safe
            tool["description"] = (
                f"Claude Code tool {original}. " + tool["description"]
            ).strip()
        original_names[safe] = original
    return prepared, original_names


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
    chunks: Iterator[Any]


@dataclass
class DynamicToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]
    _response: queue.Queue[dict[str, Any]]

    def defer(self) -> None:
        self._response.put({
            "contentItems": [{
                "type": "inputText",
                "text": "Execution is delegated to Claude Code in the next API request.",
            }],
            "success": False,
        })


class CodexTextBackend:
    """Run isolated text-only turns while sharing one authenticated process."""

    def __init__(self, *, timeout: float = DEFAULT_REQUEST_TIMEOUT) -> None:
        self.timeout = timeout
        self.router = EventRouter()
        self._tool_lock = threading.Lock()
        self._tool_queues: dict[
            str, tuple[queue.Queue[DynamicToolCall], dict[str, str]]
        ] = {}
        self.client = CodexAppServerClient(
            notification_handler=self.router.dispatch,
            server_request_handler=self._handle_server_request,
        )

    def start(self) -> "CodexTextBackend":
        self.client.start()
        return self

    def close(self) -> None:
        self.client.close()

    def snapshot(self) -> dict[str, Any]:
        """Safe account/model/limit data; never exposes OAuth credentials."""
        return _safe_probe(self.client.snapshot())

    def begin(self, payload: dict[str, Any]) -> TextTurn:
        developer, prompt, image_inputs = build_request(payload)
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
        tools, tool_names = prepare_dynamic_tools(payload)
        if tools:
            params["dynamicTools"] = tools
        started = self.client.request("thread/start", params)
        thread = started.get("thread") if isinstance(started, dict) else None
        thread_id = thread.get("id") if isinstance(thread, dict) else None
        if not isinstance(thread_id, str):
            raise BridgeError("thread/start returned no thread id")
        actual_model = thread.get("model") or model or "codex"
        events = self.router.register(thread_id)
        tool_calls: queue.Queue[DynamicToolCall] = queue.Queue()
        with self._tool_lock:
            self._tool_queues[thread_id] = (tool_calls, tool_names)
        try:
            turn = self.client.request("turn/start", {
                "threadId": thread_id,
                "input": [{"type": "text", "text": prompt}] + image_inputs,
            })
        except Exception:
            self.router.unregister(thread_id)
            with self._tool_lock:
                self._tool_queues.pop(thread_id, None)
            raise
        turn_obj = turn.get("turn") if isinstance(turn, dict) else None
        turn_id = turn_obj.get("id") if isinstance(turn_obj, dict) else None
        return TextTurn(
            message_id="msg_" + uuid.uuid4().hex,
            model=str(actual_model),
            chunks=self._chunks(thread_id, turn_id, events, tool_calls),
        )

    def _handle_server_request(self, message: dict[str, Any]) -> dict[str, Any]:
        method = message.get("method")
        if method != "item/tool/call":
            raise BridgeError(f"unsupported Codex server request: {method}")
        params = message.get("params")
        if not isinstance(params, dict):
            raise BridgeError("dynamic tool request has no params")
        thread_id = params.get("threadId")
        with self._tool_lock:
            session = self._tool_queues.get(thread_id)
        if session is None:
            raise BridgeError("dynamic tool request belongs to an inactive thread")
        target, tool_names = session
        arguments = params.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError as exc:
                raise BridgeError(f"invalid dynamic tool arguments: {exc}") from exc
        if not isinstance(arguments, dict):
            raise BridgeError("dynamic tool arguments must be an object")
        response: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        target.put(DynamicToolCall(
            call_id=str(params.get("callId") or "toolu_" + uuid.uuid4().hex),
            name=tool_names.get(
                str(params.get("tool") or ""), str(params.get("tool") or "")
            ),
            arguments=arguments,
            _response=response,
        ))
        try:
            return response.get(timeout=self.timeout)
        except queue.Empty as exc:
            raise BridgeError("Claude Code did not accept the dynamic tool call") from exc

    def _chunks(
        self,
        thread_id: str,
        turn_id: str | None,
        events: queue.Queue[dict[str, Any]],
        tool_calls: queue.Queue[DynamicToolCall],
    ) -> Iterator[Any]:
        deadline = time.monotonic() + self.timeout
        delegated: DynamicToolCall | None = None
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if turn_id:
                        self.client.request("turn/interrupt", {"threadId": thread_id})
                    raise BridgeError("Codex turn timed out")
                try:
                    delegated = tool_calls.get_nowait()
                except queue.Empty:
                    delegated = None
                if delegated is not None:
                    yield delegated
                    return
                try:
                    event = events.get(timeout=min(remaining, 0.1))
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
            if delegated is not None:
                delegated.defer()
                if turn_id:
                    try:
                        self.client.request("turn/interrupt", {"threadId": thread_id})
                    except CodexAppServerError:
                        pass
            self.router.unregister(thread_id)
            with self._tool_lock:
                self._tool_queues.pop(thread_id, None)


def message_object(
    message_id: str,
    model: str,
    text: str = "",
    *,
    content: list[dict[str, Any]] | None = None,
    stop_reason: str = "end_turn",
) -> dict[str, Any]:
    return {
        "id": message_id,
        "type": "message",
        "role": "assistant",
        "content": content if content is not None else [{"type": "text", "text": text}],
        "model": model,
        "stop_reason": stop_reason,
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
    index = 0
    text_open = False
    emitted = False
    stop_reason = "end_turn"
    chunks = turn.chunks
    for chunk in chunks:
        if isinstance(chunk, str):
            if not text_open:
                yield "content_block_start", {
                    "type": "content_block_start", "index": index,
                    "content_block": {"type": "text", "text": ""},
                }
                text_open = True
                emitted = True
            yield "content_block_delta", {
                "type": "content_block_delta", "index": index,
                "delta": {"type": "text_delta", "text": chunk},
            }
        elif isinstance(chunk, DynamicToolCall):
            if text_open:
                yield "content_block_stop", {"type": "content_block_stop", "index": index}
                index += 1
                text_open = False
            emitted = True
            yield "content_block_start", {
                "type": "content_block_start", "index": index,
                "content_block": {
                    "type": "tool_use", "id": chunk.call_id,
                    "name": chunk.name, "input": {},
                },
            }
            yield "content_block_delta", {
                "type": "content_block_delta", "index": index,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": json.dumps(chunk.arguments, ensure_ascii=False),
                },
            }
            yield "content_block_stop", {"type": "content_block_stop", "index": index}
            stop_reason = "tool_use"
            break
    if hasattr(chunks, "close"):
        chunks.close()
    if text_open:
        yield "content_block_stop", {"type": "content_block_stop", "index": index}
    elif not emitted:
        yield "content_block_start", {
            "type": "content_block_start", "index": index,
            "content_block": {"type": "text", "text": ""},
        }
        yield "content_block_stop", {"type": "content_block_stop", "index": index}
    yield "message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
        "usage": {"output_tokens": 0},
    }
    yield "message_stop", {"type": "message_stop"}


def collect_message(turn: TextTurn) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    text: list[str] = []
    stop_reason = "end_turn"
    chunks = turn.chunks
    for chunk in chunks:
        if isinstance(chunk, str):
            text.append(chunk)
        elif isinstance(chunk, DynamicToolCall):
            if text:
                content.append({"type": "text", "text": "".join(text)})
                text.clear()
            content.append({
                "type": "tool_use", "id": chunk.call_id,
                "name": chunk.name, "input": chunk.arguments,
            })
            stop_reason = "tool_use"
            break
    if hasattr(chunks, "close"):
        chunks.close()
    if text or not content:
        content.append({"type": "text", "text": "".join(text)})
    return message_object(
        turn.message_id, turn.model, content=content, stop_reason=stop_reason,
    )


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
            self._json(200, {
                "ok": True,
                "service": "claude-openai-bridge",
                "pid": os.getpid(),
                "sourceMtime": SOURCE_MTIME,
            })
        elif self.path == "/account":
            try:
                self._json(200, {"ok": True} | self.server.backend.snapshot())
            except CodexAppServerError as exc:
                self._json(503, {"ok": False, "error": str(exc)})
        else:
            self._json(404, {"error": {"type": "not_found_error", "message": "not found"}})

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/")
        if path != "/v1/messages":
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
                self._json(200, collect_message(turn))
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
