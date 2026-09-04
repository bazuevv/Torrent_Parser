#!/usr/bin/env python3
"""Minimal, thread-safe client for the local Codex app-server.

The future Claude-to-Codex bridge must not read or copy ``~/.codex/auth.json``.
Instead it starts the official Codex app-server over stdio and asks that process
for account, model, and rate-limit information through its JSON-RPC protocol.

This module deliberately contains no HTTP or Anthropic protocol translation;
it is the authenticated transport foundation for the next implementation
phase.  It has no third-party Python dependencies.
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import platform
import queue
import shutil
import subprocess
import threading
from typing import Any, Callable, Sequence


DEFAULT_TIMEOUT = 15.0
STDERR_TAIL_LINES = 40


class CodexAppServerError(RuntimeError):
    """Base error for app-server discovery, transport, and RPC failures."""


class CodexRpcError(CodexAppServerError):
    """An app-server request completed with a JSON-RPC error response."""

    def __init__(self, method: str, error: Any):
        super().__init__(f"{method}: {error}")
        self.method = method
        self.error = error


def _extension_binary_candidates() -> list[str]:
    """Return Codex binaries bundled with VS Code extensions, newest first."""
    machine = platform.machine().lower()
    arch_names = {
        "x86_64": ("linux-x86_64", "linux-x64"),
        "amd64": ("linux-x86_64", "linux-x64"),
        "aarch64": ("linux-aarch64", "linux-arm64"),
        "arm64": ("linux-aarch64", "linux-arm64"),
    }.get(machine, ())
    patterns = []
    for arch in arch_names:
        patterns.append(os.path.expanduser(
            f"~/.vscode/extensions/openai.chatgpt-*/bin/{arch}/codex"
        ))
        patterns.append(os.path.expanduser(
            f"~/.vscode-server/extensions/openai.chatgpt-*/bin/{arch}/codex"
        ))
    found: list[str] = []
    for pattern in patterns:
        found.extend(glob.glob(pattern))
    return sorted(set(found), key=os.path.getmtime, reverse=True)


def find_codex_binary(explicit: str | None = None) -> str:
    """Resolve an executable Codex CLI without assuming the caller's PATH.

    ``CODEX_BIN`` is an intentional override for testing or nonstandard
    installations.  A normal executable on PATH wins next.  The extension
    lookup is needed because Claude Code's extension host does not necessarily
    inherit the PATH modification made by the OpenAI extension.
    """
    candidates: list[str] = []
    requested = explicit or os.environ.get("CODEX_BIN")
    if requested:
        candidates.append(os.path.expanduser(requested))
    on_path = shutil.which("codex")
    if on_path:
        candidates.append(on_path)
    candidates.extend(_extension_binary_candidates())

    checked: list[str] = []
    for candidate in candidates:
        absolute = os.path.abspath(candidate)
        if absolute in checked:
            continue
        checked.append(absolute)
        if os.path.isfile(absolute) and os.access(absolute, os.X_OK):
            return absolute
    detail = ", ".join(checked) if checked else "no candidates"
    raise CodexAppServerError(f"Codex CLI executable not found ({detail})")


class CodexAppServerClient:
    """Own one ``codex app-server`` process and multiplex JSON-RPC replies."""

    def __init__(
        self,
        codex_bin: str | None = None,
        *,
        command: Sequence[str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        notification_handler: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.timeout = timeout
        self.notification_handler = notification_handler
        self.command = list(command) if command else [
            find_codex_binary(codex_bin), "app-server", "--listen", "stdio://",
        ]
        self._process: subprocess.Popen[str] | None = None
        self._next_id = 1
        self._state_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._pending: dict[int, queue.Queue[Any]] = {}
        self._stderr_tail: collections.deque[str] = collections.deque(
            maxlen=STDERR_TAIL_LINES
        )
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._closed_error: CodexAppServerError | None = None

    def __enter__(self) -> "CodexAppServerClient":
        return self.start()

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()

    @property
    def stderr_tail(self) -> list[str]:
        return list(self._stderr_tail)

    def start(self) -> "CodexAppServerClient":
        if self._process is not None:
            return self
        try:
            self._process = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise CodexAppServerError(
                f"cannot start {' '.join(self.command)}: {exc}"
            ) from exc

        self._reader_thread = threading.Thread(
            target=self._read_stdout, name="codex-app-server-stdout", daemon=True
        )
        self._stderr_thread = threading.Thread(
            target=self._read_stderr, name="codex-app-server-stderr", daemon=True
        )
        self._reader_thread.start()
        self._stderr_thread.start()

        try:
            self.request("initialize", {
                "clientInfo": {
                    "name": "claude_code_bridge",
                    "title": "Claude Code OpenAI Bridge",
                    "version": "0.1.0",
                },
                "capabilities": {"experimentalApi": True},
            })
            self.notify("initialized", {})
        except Exception:
            self.close()
            raise
        return self

    def _read_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        for line in process.stderr:
            self._stderr_tail.append(line.rstrip())

    def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            for raw_line in process.stdout:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    self._stderr_tail.append(f"invalid stdout JSON: {line[:300]}")
                    continue
                self._dispatch(message)
        finally:
            code = process.poll()
            error = CodexAppServerError(
                f"Codex app-server closed unexpectedly (exit={code})"
            )
            self._fail_pending(error)

    def _dispatch(self, message: Any) -> None:
        if not isinstance(message, dict):
            self._stderr_tail.append("app-server emitted a non-object message")
            return
        request_id = message.get("id")
        method = message.get("method")

        if request_id is not None and method is None:
            with self._state_lock:
                waiter = self._pending.pop(request_id, None)
            if waiter is not None:
                waiter.put(message)
            return

        if request_id is not None and isinstance(method, str):
            # Phase 1 does not expose model tools yet.  Never leave an
            # unexpected server request hanging: answer it explicitly.
            self._send({
                "id": request_id,
                "error": {"code": -32601, "message": f"unsupported: {method}"},
            })
            return

        if isinstance(method, str) and self.notification_handler is not None:
            try:
                self.notification_handler(message)
            except Exception as exc:  # callback errors must not kill transport
                self._stderr_tail.append(f"notification handler failed: {exc}")

    def _fail_pending(self, error: CodexAppServerError) -> None:
        with self._state_lock:
            if self._closed_error is None:
                self._closed_error = error
            pending = list(self._pending.values())
            self._pending.clear()
        for waiter in pending:
            waiter.put(error)

    def _send(self, message: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise CodexAppServerError("Codex app-server is not running")
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        try:
            with self._write_lock:
                process.stdin.write(payload + "\n")
                process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise CodexAppServerError(f"cannot write to app-server: {exc}") from exc

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._send({"method": method, "params": params or {}})

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        process = self._process
        if process is None:
            raise CodexAppServerError("call start() before request()")
        with self._state_lock:
            if self._closed_error is not None:
                raise self._closed_error
            request_id = self._next_id
            self._next_id += 1
            waiter: queue.Queue[Any] = queue.Queue(maxsize=1)
            self._pending[request_id] = waiter
        try:
            self._send({"method": method, "id": request_id, "params": params or {}})
        except Exception:
            with self._state_lock:
                self._pending.pop(request_id, None)
            raise

        wait_for = self.timeout if timeout is None else timeout
        try:
            response = waiter.get(timeout=wait_for)
        except queue.Empty as exc:
            with self._state_lock:
                self._pending.pop(request_id, None)
            raise CodexAppServerError(
                f"timeout waiting for {method} after {wait_for:g}s"
            ) from exc
        if isinstance(response, Exception):
            raise response
        if "error" in response:
            raise CodexRpcError(method, response["error"])
        return response.get("result")

    def account(self, *, refresh_token: bool = False) -> dict[str, Any]:
        result = self.request("account/read", {"refreshToken": refresh_token})
        return result if isinstance(result, dict) else {}

    def models(self, *, include_hidden: bool = False) -> list[dict[str, Any]]:
        result = self.request("model/list", {
            "limit": 100,
            "includeHidden": include_hidden,
        })
        data = result.get("data") if isinstance(result, dict) else None
        return data if isinstance(data, list) else []

    def rate_limits(self) -> dict[str, Any]:
        result = self.request("account/rateLimits/read")
        return result if isinstance(result, dict) else {}

    def snapshot(self, *, include_rate_limits: bool = True) -> dict[str, Any]:
        data = {"account": self.account(), "models": self.models()}
        if include_rate_limits:
            data["rateLimits"] = self.rate_limits()
        return data

    def close(self) -> None:
        process = self._process
        if process is None:
            return
        self._process = None
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        try:
            process.terminate()
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=1)
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=1)
        for stream in (process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                stream.close()
        self._fail_pending(CodexAppServerError("Codex app-server client closed"))


def _safe_probe(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Keep useful diagnostics while avoiding accidental credential output."""
    account_result = snapshot.get("account")
    account = account_result.get("account") if isinstance(account_result, dict) else None
    safe_account: dict[str, Any] | None = None
    if isinstance(account, dict):
        safe_account = {
            key: account.get(key)
            for key in ("type", "email", "planType")
            if account.get(key) is not None
        }
    models = snapshot.get("models")
    safe_models = []
    if isinstance(models, list):
        for model in models:
            if isinstance(model, dict):
                safe_models.append({
                    key: model.get(key)
                    for key in ("id", "displayName", "isDefault")
                    if model.get(key) is not None
                })
    limits_result = snapshot.get("rateLimits")
    limits = limits_result.get("rateLimits") if isinstance(limits_result, dict) else None
    safe_limits = None
    if isinstance(limits, dict):
        safe_limits = {
            key: limits.get(key)
            for key in (
                "limitId", "limitName", "primary", "secondary", "credits",
                "planType", "rateLimitReachedType", "spendControlReached",
            )
            if key in limits
        }
    return {
        "account": safe_account,
        "requiresOpenaiAuth": (
            account_result.get("requiresOpenaiAuth")
            if isinstance(account_result, dict) else None
        ),
        "models": safe_models,
        "rateLimits": safe_limits,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe local Codex app-server")
    parser.add_argument("--codex-bin", help="explicit Codex CLI path")
    parser.add_argument(
        "--no-rate-limits", action="store_true",
        help="skip the network-backed ChatGPT rate-limit request",
    )
    args = parser.parse_args()
    try:
        with CodexAppServerClient(args.codex_bin) as client:
            snapshot = client.snapshot(include_rate_limits=not args.no_rate_limits)
        print(json.dumps(_safe_probe(snapshot), ensure_ascii=False, indent=2))
    except CodexAppServerError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
