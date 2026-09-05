#!/usr/bin/env python3
"""Loopback-only Anthropic Messages facade backed by Codex app-server.

Anthropic tools are exposed as Codex dynamic tools. OAuth remains wholly owned
by the official Codex process.
"""

from __future__ import annotations

import argparse
import hashlib
import http.server
import ipaddress
import json
import os
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import chain
from typing import Any, Iterator

from codex_app_server import CodexAppServerClient, CodexAppServerError
from codex_app_server import _safe_probe


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18924
DEFAULT_REQUEST_TIMEOUT = 600.0
CONTEXT_EVENTS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "hooks-runtime", "codex-context-events.jsonl",
)
BRIDGE_USAGE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "hooks-runtime", "codex-bridge-usage.json",
)
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


def select_effort(payload: dict[str, Any]) -> str | None:
    """Translate Claude's Messages API effort into a Codex turn override."""
    output_config = payload.get("output_config")
    candidates = [
        output_config.get("effort") if isinstance(output_config, dict) else None,
        payload.get("effort"),
    ]
    for value in candidates:
        if isinstance(value, str) and value in (
            "low", "medium", "high", "xhigh", "max",
        ):
            return value
    return None


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


def claude_session_key(payload: dict[str, Any]) -> str | None:
    """Extract the stable Claude Code session id from Anthropic metadata."""
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        return None
    direct = metadata.get("session_id")
    if isinstance(direct, str) and direct:
        return direct
    user_id = metadata.get("user_id")
    if not isinstance(user_id, str) or not user_id:
        return None
    try:
        decoded = json.loads(user_id)
    except json.JSONDecodeError:
        return None
    session_id = decoded.get("session_id") if isinstance(decoded, dict) else None
    return session_id if isinstance(session_id, str) and session_id else None


def _message_fingerprints(payload: dict[str, Any]) -> list[str]:
    messages = payload.get("messages")
    if not isinstance(messages, list):
        raise BridgeError("messages must be a non-empty array")
    return [json.dumps(item, ensure_ascii=False, sort_keys=True) for item in messages]


def _followup_payload(
    payload: dict[str, Any], previous: list[str],
) -> dict[str, Any]:
    """Return the newest real user input without replaying old history.

    Claude Code may append ephemeral hook, attachment, or assistant scaffolding
    after the user's message.  Those blocks are not stable between API calls,
    so an anchor against the former last fingerprint can land after the new
    user message and make the suffix appear empty.  A persistent Codex thread
    already owns every earlier turn: the only conversation block it needs is
    the newest non-tool-result user message.
    """
    del previous  # retained in the signature for callers and focused tests
    messages = payload.get("messages") or []
    for item in reversed(messages):
        if (isinstance(item, dict) and item.get("role") == "user"
                and not _is_tool_result_only(item.get("content"))):
            result = dict(payload)
            result["messages"] = [item]
            return result
    raise BridgeError("Claude session has no new user input")


def _is_tool_result_only(content: Any) -> bool:
    return (
        isinstance(content, list) and bool(content)
        and all(isinstance(block, dict) and block.get("type") == "tool_result"
                for block in content)
    )


def _tool_results(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for message in payload.get("messages") or []:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            call_id = block.get("tool_use_id")
            if isinstance(call_id, str) and call_id:
                results[call_id] = block
    return results


def _dynamic_result(block: dict[str, Any]) -> dict[str, Any]:
    content = block.get("content")
    items: list[dict[str, Any]] = []
    if isinstance(content, str):
        items.append({"type": "inputText", "text": content})
    elif isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text" and isinstance(item.get("text"), str):
                items.append({"type": "inputText", "text": item["text"]})
            elif item.get("type") == "image":
                image = _image_input(item)
                items.append({"type": "inputImage", "imageUrl": image["url"]})
    if not items:
        items.append({"type": "inputText", "text": ""})
    return {"contentItems": items, "success": not bool(block.get("is_error"))}


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
    usage: dict[str, int] = field(default_factory=dict)


@dataclass
class DynamicToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]
    _response: queue.Queue[dict[str, Any]]

    def resolve(self, result: dict[str, Any]) -> None:
        self._response.put(result)


@dataclass
class BridgeSession:
    key: str
    thread_id: str
    model: str
    effort: str | None
    events: queue.Queue[dict[str, Any]]
    tool_calls: queue.Queue[DynamicToolCall]
    tool_names: dict[str, str]
    tool_signature: str
    seen_messages: list[str]
    active_turn_id: str | None = None
    pending_tools: dict[str, DynamicToolCall] = field(default_factory=dict)
    response_lock: threading.Lock = field(default_factory=threading.Lock)
    last_usage: dict[str, int] = field(default_factory=dict)
    total_usage: dict[str, int] = field(default_factory=dict)
    context_window: int | None = None
    turns_started: int = 0
    tool_continuations: int = 0
    initial_input_chars: int | None = None
    last_input_chars: int = 0
    seen_compactions: set[str] = field(default_factory=set)
    pending_compaction: dict[str, Any] | None = None
    prior_context: int = 0


TOKEN_USAGE_FIELDS = {
    "inputTokens": "input_tokens",
    "cachedInputTokens": "cached_input_tokens",
    "cacheWriteInputTokens": "cache_write_input_tokens",
    "outputTokens": "output_tokens",
    "reasoningOutputTokens": "reasoning_output_tokens",
    "totalTokens": "total_tokens",
}
USAGE_READY = object()


def _normalized_usage(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, int] = {}
    for source, target in TOKEN_USAGE_FIELDS.items():
        amount = value.get(source, 0)
        if isinstance(amount, int) and amount >= 0:
            result[target] = amount
    return result


def _anthropic_usage(usage: dict[str, int]) -> dict[str, int]:
    return {
        "input_tokens": usage.get("input_tokens", 0),
        "cache_read_input_tokens": usage.get("cached_input_tokens", 0),
        "cache_creation_input_tokens": usage.get("cache_write_input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
    }


class CodexTextBackend:
    """Keep one Codex thread per Claude Code session."""

    def __init__(
        self, *, timeout: float = DEFAULT_REQUEST_TIMEOUT,
        usage_state_file: str | None = None,
    ) -> None:
        self.timeout = timeout
        self.router = EventRouter()
        self._tool_lock = threading.Lock()
        self._sessions_lock = threading.Lock()
        self._usage_lock = threading.Lock()
        self._sessions: dict[str, BridgeSession] = {}
        self._tool_queues: dict[str, BridgeSession] = {}
        self._usage_state_file = usage_state_file
        self._latest_usage: dict[str, Any] = self._load_usage_state()
        self.client = CodexAppServerClient(
            notification_handler=self._handle_notification,
            server_request_handler=self._handle_server_request,
        )

    def start(self) -> "CodexTextBackend":
        self.client.start()
        return self

    def close(self) -> None:
        self.client.close()

    def snapshot(self) -> dict[str, Any]:
        """Safe account/model/limit data; never exposes OAuth credentials."""
        result = _safe_probe(self.client.snapshot())
        with self._usage_lock:
            result["bridgeUsage"] = dict(self._latest_usage)
        return result

    def _load_usage_state(self) -> dict[str, Any]:
        if not self._usage_state_file:
            return {}
        try:
            with open(self._usage_state_file, encoding="utf-8") as handle:
                value = json.load(handle)
        except (OSError, ValueError, TypeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _save_usage_state(self, value: dict[str, Any]) -> None:
        if not self._usage_state_file or not value.get("last"):
            return
        try:
            os.makedirs(os.path.dirname(self._usage_state_file), exist_ok=True)
            temporary = self._usage_state_file + ".tmp"
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False)
            os.replace(temporary, self._usage_state_file)
        except OSError:
            return

    def _handle_notification(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        if method == "thread/tokenUsage/updated":
            params = message.get("params")
            thread_id = params.get("threadId") if isinstance(params, dict) else None
            token_usage = params.get("tokenUsage") if isinstance(params, dict) else None
            with self._tool_lock:
                session = self._tool_queues.get(thread_id)
            if session is not None and isinstance(token_usage, dict):
                last = _normalized_usage(token_usage.get("last"))
                total = _normalized_usage(token_usage.get("total"))
                context_window = token_usage.get("modelContextWindow")
                session.last_usage.clear()
                session.last_usage.update(last)
                session.total_usage = total
                session.context_window = (
                    context_window if isinstance(context_window, int) else None
                )
                if session.pending_compaction is not None:
                    session.pending_compaction["context_after"] = last.get(
                        "input_tokens", 0,
                    )
                    self._append_context_event(session.pending_compaction)
                    session.pending_compaction = None
                self._publish_session(session)
        elif method in ("item/completed", "thread/compacted"):
            params = message.get("params")
            thread_id = params.get("threadId") if isinstance(params, dict) else None
            item = params.get("item") if isinstance(params, dict) else None
            is_compaction = (
                method == "thread/compacted"
                or (isinstance(item, dict) and item.get("type") == "contextCompaction")
            )
            with self._tool_lock:
                session = self._tool_queues.get(thread_id)
            if session is not None and is_compaction:
                self._record_compaction(session, params or {})
        self.router.dispatch(message)

    def _record_compaction(
        self, session: BridgeSession, params: dict[str, Any],
    ) -> None:
        """Persist one safe Usage-history row for each Codex compaction."""
        item = params.get("item")
        keys = []
        turn_id = params.get("turnId")
        item_id = item.get("id") if isinstance(item, dict) else None
        if isinstance(turn_id, str) and turn_id:
            keys.append("turn:" + turn_id)
        if isinstance(item_id, str) and item_id:
            keys.append("item:" + item_id)
        if keys and any(key in session.seen_compactions for key in keys):
            return
        session.seen_compactions.update(keys)
        event = {
            "kind": "compact",
            "event_id": keys[0] if keys else "compact:" + uuid.uuid4().hex,
            "ts": datetime.now(timezone.utc).isoformat(),
            "session_hash": hashlib.sha256(session.key.encode()).hexdigest(),
            "model": session.model,
            "context_before": (
                session.last_usage.get("input_tokens", 0) or session.prior_context
            ),
            "context_window": session.context_window,
        }
        session.pending_compaction = event
        # Сохраняем событие сразу. Если Extension Host или мост умрёт до
        # следующей tokenUsage notification, строка сжатия всё равно останется;
        # поздняя usage допишет вторую версию с context_after, а читатель
        # объединит обе по event_id.
        self._append_context_event(event)

    @staticmethod
    def _append_context_event(event: dict[str, Any]) -> None:
        try:
            os.makedirs(os.path.dirname(CONTEXT_EVENTS_FILE), exist_ok=True)
            with open(CONTEXT_EVENTS_FILE, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError:
            return

    def _publish_session(self, session: BridgeSession) -> None:
        """Expose only counters and model metadata, never prompts or ids."""
        session_hash = hashlib.sha256(session.key.encode()).hexdigest()
        value = {
            "model": session.model,
            "effort": session.effort,
            "last": dict(session.last_usage),
            "total": dict(session.total_usage),
            "model_context_window": session.context_window,
            "turns_started": session.turns_started,
            "tool_continuations": session.tool_continuations,
            "initial_input_chars": session.initial_input_chars,
            "last_input_chars": session.last_input_chars,
            "updated_at": int(time.time()),
            "session_key_hash": session_hash,
        }
        with self._usage_lock:
            previous = self._latest_usage
            # turn/start очищает session.last_usage, чтобы в Anthropic stream
            # не ушли счётчики прошлого хода. Для панели при этом сохраняем
            # последний подтверждённый снимок до прихода новой телеметрии.
            if (not value["last"] and previous.get("session_key_hash") == session_hash
                    and previous.get("last")):
                value["last"] = dict(previous["last"])
                value["total"] = dict(previous.get("total") or {})
                value["model_context_window"] = (
                    value["model_context_window"]
                    or previous.get("model_context_window")
                )
            self._latest_usage = value
            self._save_usage_state(value)

    def begin(self, payload: dict[str, Any]) -> TextTurn:
        stable_key = claude_session_key(payload)
        key = stable_key or "request:" + uuid.uuid4().hex
        tools, tool_names = prepare_dynamic_tools(payload)
        tool_signature = json.dumps(tools, ensure_ascii=False, sort_keys=True)
        with self._sessions_lock:
            session = self._sessions.get(key)
        if session is not None:
            session.response_lock.acquire()
            try:
                return self._continue_or_start(
                    session, payload, tools, tool_names, tool_signature,
                )
            except Exception:
                if session.response_lock.locked():
                    session.response_lock.release()
                raise
        return self._start_session(
            key, payload, tools, tool_names, tool_signature, stable_key is not None,
        )

    def _start_session(
        self,
        key: str,
        payload: dict[str, Any],
        tools: list[dict[str, Any]],
        tool_names: dict[str, str],
        tool_signature: str,
        persistent: bool,
    ) -> TextTurn:
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
        if tools:
            params["dynamicTools"] = tools
        started = self.client.request("thread/start", params)
        thread = started.get("thread") if isinstance(started, dict) else None
        thread_id = thread.get("id") if isinstance(thread, dict) else None
        if not isinstance(thread_id, str):
            raise BridgeError("thread/start returned no thread id")
        actual_model = thread.get("model") or model or "codex"
        actual_effort = thread.get("reasoningEffort")
        session = BridgeSession(
            key=key,
            thread_id=thread_id,
            model=str(actual_model),
            effort=actual_effort if isinstance(actual_effort, str) else None,
            events=self.router.register(thread_id),
            tool_calls=queue.Queue(),
            tool_names=tool_names,
            tool_signature=tool_signature,
            seen_messages=_message_fingerprints(payload),
        )
        self._publish_session(session)
        session.response_lock.acquire()
        with self._tool_lock:
            self._tool_queues[thread_id] = session
        if persistent:
            with self._sessions_lock:
                self._sessions[key] = session
        try:
            self._start_turn(
                session, prompt, image_inputs,
                model=model, effort=select_effort(payload),
            )
        except Exception:
            self._discard_session(session)
            session.response_lock.release()
            raise
        return self._text_turn(session)

    def _continue_or_start(
        self,
        session: BridgeSession,
        payload: dict[str, Any],
        tools: list[dict[str, Any]],
        tool_names: dict[str, str],
        tool_signature: str,
    ) -> TextTurn:
        if session.active_turn_id:
            supplied = _tool_results(payload)
            matched = 0
            for call_id, block in supplied.items():
                call = session.pending_tools.pop(call_id, None)
                if call is not None:
                    call.resolve(_dynamic_result(block))
                    matched += 1
            if not matched:
                raise BridgeError("Codex turn is waiting for a Claude tool_result")
            session.tool_continuations += matched
            session.seen_messages = _message_fingerprints(payload)
            self._publish_session(session)
            return self._text_turn(session)

        followup = _followup_payload(payload, session.seen_messages)
        if tool_signature != session.tool_signature:
            # Claude can reorder or refresh tool descriptions between turns.
            # Never replay a long transcript merely because that metadata
            # changed; the live Codex thread already owns the conversation.
            session.tool_names = tool_names
            session.tool_signature = tool_signature
        _developer, prompt, image_inputs = build_request(followup)
        session.seen_messages = _message_fingerprints(payload)
        self._start_turn(
            session, prompt, image_inputs,
            model=select_model(payload), effort=select_effort(payload),
        )
        return self._text_turn(session)

    def _start_turn(
        self,
        session: BridgeSession,
        prompt: str,
        image_inputs: list[dict[str, Any]],
        *,
        model: str | None = None,
        effort: str | None = None,
    ) -> None:
        session.prior_context = session.last_usage.get(
            "input_tokens", session.prior_context,
        )
        session.last_usage.clear()
        params: dict[str, Any] = {
            "threadId": session.thread_id,
            "input": [{"type": "text", "text": prompt}] + image_inputs,
        }
        if model:
            params["model"] = model
        if effort:
            params["effort"] = effort
        turn = self.client.request("turn/start", params)
        turn_obj = turn.get("turn") if isinstance(turn, dict) else None
        turn_id = turn_obj.get("id") if isinstance(turn_obj, dict) else None
        if not isinstance(turn_id, str):
            raise BridgeError("turn/start returned no turn id")
        session.active_turn_id = turn_id
        if model:
            session.model = model
        if effort:
            session.effort = effort
        session.turns_started += 1
        session.last_input_chars = len(prompt)
        if session.initial_input_chars is None:
            session.initial_input_chars = len(prompt)
        self._publish_session(session)

    def _text_turn(self, session: BridgeSession) -> TextTurn:
        return TextTurn(
            message_id="msg_" + uuid.uuid4().hex,
            model=session.model,
            chunks=self._locked_chunks(session),
            usage=session.last_usage,
        )

    def _locked_chunks(self, session: BridgeSession) -> Iterator[Any]:
        try:
            yield from self._chunks(session)
        finally:
            session.response_lock.release()

    def _discard_session(self, session: BridgeSession) -> None:
        self.router.unregister(session.thread_id)
        with self._tool_lock:
            self._tool_queues.pop(session.thread_id, None)
        with self._sessions_lock:
            if self._sessions.get(session.key) is session:
                self._sessions.pop(session.key, None)

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
        arguments = params.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError as exc:
                raise BridgeError(f"invalid dynamic tool arguments: {exc}") from exc
        if not isinstance(arguments, dict):
            raise BridgeError("dynamic tool arguments must be an object")
        response: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        call = DynamicToolCall(
            call_id=str(params.get("callId") or "toolu_" + uuid.uuid4().hex),
            name=session.tool_names.get(
                str(params.get("tool") or ""), str(params.get("tool") or "")
            ),
            arguments=arguments,
            _response=response,
        )
        session.pending_tools[call.call_id] = call
        session.tool_calls.put(call)
        try:
            return response.get(timeout=self.timeout)
        except queue.Empty as exc:
            raise BridgeError("Claude Code did not accept the dynamic tool call") from exc

    def _chunks(
        self,
        session: BridgeSession,
    ) -> Iterator[Any]:
        deadline = time.monotonic() + self.timeout
        delegated = False
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if session.active_turn_id:
                        self.client.request(
                            "turn/interrupt", {"threadId": session.thread_id},
                        )
                    raise BridgeError("Codex turn timed out")
                try:
                    tool_call = session.tool_calls.get_nowait()
                except queue.Empty:
                    tool_call = None
                if tool_call is not None:
                    delegated = True
                    yield tool_call
                    return
                try:
                    event = session.events.get(timeout=min(remaining, 0.1))
                except queue.Empty:
                    continue
                method = event.get("method")
                params = event.get("params") or {}
                if (session.active_turn_id
                        and params.get("turnId") not in (None, session.active_turn_id)):
                    continue
                if method == "item/agentMessage/delta":
                    delta = params.get("delta")
                    if isinstance(delta, str) and delta:
                        yield delta
                elif method == "thread/tokenUsage/updated" and session.last_usage:
                    yield USAGE_READY
                elif method == "turn/completed":
                    turn = params.get("turn") or {}
                    status = turn.get("status")
                    if status != "completed":
                        error = turn.get("error") or {}
                        raise BridgeError(error.get("message") or f"turn {status}")
                    session.active_turn_id = None
                    # After automatic compaction App Server can emit the
                    # completion before its final tokenUsage notification.
                    # Give that telemetry a short grace period so Claude's
                    # message_start and transcript do not permanently record
                    # zero input/cache tokens for an otherwise valid turn.
                    usage_deadline = min(deadline, time.monotonic() + 0.5)
                    while not session.last_usage and time.monotonic() < usage_deadline:
                        try:
                            trailing = session.events.get(
                                timeout=max(
                                    0.001,
                                    min(0.1, usage_deadline - time.monotonic()),
                                ),
                            )
                        except queue.Empty:
                            continue
                        if (trailing.get("method") == "thread/tokenUsage/updated"
                                and session.last_usage):
                            yield USAGE_READY
                            break
                    return
                elif method == "error":
                    error = params.get("error") or {}
                    raise BridgeError(error.get("message") or "Codex turn failed")
        finally:
            # A yielded tool call intentionally leaves the Codex turn alive.
            # Claude Code returns tool_result in its next Anthropic request.
            if session.active_turn_id and not delegated:
                try:
                    self.client.request(
                        "turn/interrupt", {"threadId": session.thread_id},
                    )
                except CodexAppServerError:
                    pass
                session.active_turn_id = None


def message_object(
    message_id: str,
    model: str,
    text: str = "",
    *,
    content: list[dict[str, Any]] | None = None,
    stop_reason: str = "end_turn",
    usage: dict[str, int] | None = None,
) -> dict[str, Any]:
    return {
        "id": message_id,
        "type": "message",
        "role": "assistant",
        "content": content if content is not None else [{"type": "text", "text": text}],
        "model": model,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": _anthropic_usage(usage or {}),
    }


def stream_events(turn: TextTurn) -> Iterator[tuple[str, dict[str, Any]]]:
    chunks = turn.chunks
    pending = []
    for first in chunks:
        if first is USAGE_READY:
            break
        pending.append(first)

    # Priming the Codex iterator lets tokenUsage/updated run before the
    # Anthropic message_start frame is serialized.  Without it Claude Code
    # permanently records input/cache usage as zero even though the final
    # bridge snapshot contains the correct values.
    yield "message_start", {
        "type": "message_start",
        "message": message_object(
            turn.message_id, turn.model, "", usage=turn.usage,
        ) | {
            "content": [], "stop_reason": None,
        },
    }
    index = 0
    text_open = False
    emitted = False
    stop_reason = "end_turn"
    for chunk in chain(pending, chunks):
        if chunk is USAGE_READY:
            continue
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
        "usage": {"output_tokens": turn.usage.get("output_tokens", 0)},
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
        usage=turn.usage,
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

    backend = CodexTextBackend(usage_state_file=BRIDGE_USAGE_FILE).start()
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
