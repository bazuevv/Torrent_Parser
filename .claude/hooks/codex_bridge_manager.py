#!/usr/bin/env python3
"""Lifecycle and safe status access for the loopback Codex bridge."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any


BRIDGE_HOST = "127.0.0.1"
BRIDGE_PORT = int(os.environ.get("CLAUDE_OPENAI_BRIDGE_PORT", "18925"))
HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE_SCRIPT = os.path.join(HERE, "codex_anthropic_bridge.py")
APP_SERVER_CLIENT = os.path.join(HERE, "codex_app_server.py")


def _url(path: str) -> str:
    return f"http://{BRIDGE_HOST}:{BRIDGE_PORT}{path}"


def _read(path: str, timeout: float = 1.0) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(_url(path), timeout=timeout) as response:
            value = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return None
    return value if isinstance(value, dict) else None


def health(timeout: float = 0.5) -> dict[str, Any] | None:
    value = _read("/health", timeout)
    if not value or value.get("service") != "claude-openai-bridge":
        return None
    return value


def account_snapshot(timeout: float = 8.0) -> dict[str, Any] | None:
    value = _read("/account", timeout)
    return value if value and value.get("ok") is True else None


def _source_mtime() -> float:
    return max(os.path.getmtime(BRIDGE_SCRIPT), os.path.getmtime(APP_SERVER_CLIENT))


def _owned_pid(pid: Any) -> int | None:
    if not isinstance(pid, int) or pid <= 1:
        return None
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            command = handle.read().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        return None
    return pid if BRIDGE_SCRIPT in command else None


def stop() -> bool:
    current = health()
    pid = _owned_pid(current.get("pid") if current else None)
    if pid is None:
        return False
    os.kill(pid, signal.SIGTERM)
    for _ in range(30):
        if health(0.1) is None:
            return True
        time.sleep(0.1)
    return True


def ensure() -> tuple[bool, str]:
    current = health()
    if current:
        reported = current.get("sourceMtime")
        if isinstance(reported, (int, float)) and reported >= _source_mtime():
            return True, "мост OpenAI уже работает"
        stop()

    subprocess.Popen(
        [sys.executable, BRIDGE_SCRIPT, "--host", BRIDGE_HOST,
         "--port", str(BRIDGE_PORT)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    for _ in range(60):
        current = health(0.2)
        if current:
            return True, "мост OpenAI запущен"
        time.sleep(0.1)
    return False, "мост OpenAI не запустился; проверьте авторизацию Codex"


if __name__ == "__main__":
    ok, message = ensure()
    print(json.dumps({"ok": ok, "message": message, "health": health()},
                     ensure_ascii=False))
    raise SystemExit(0 if ok else 1)
