#!/usr/bin/env python3
"""
Общий журнал для хуков и локального HTTP-сервера.

Зачем отдельный модуль: причину остановки сервера видно только если
писать обе стороны — и сам сервер (старт, сигналы, падения), и того,
кто его гасит (patch-claude-webview.py на SessionEnd или при устаревших
исходниках). Раздельные логи пришлось бы сопоставлять руками, поэтому
пишем в один файл с пометкой компонента.

Файл: .claude/hooks-runtime/http-server.log
Формат: `2026-08-18 07:40:12.345 [server:12345] сообщение`
        где 12345 — pid, чтобы отличать инстансы друг от друга.

Настройки читаются из .claude/patches/claude-custom-config.toml —
там же, где остальные (`serverLog`, `serverLogMaxBytes`). Конфиг
перечитывается на каждый вызов log(): запись редкая, а возможность
включить журнал без перезапуска сервера дороже экономии на чтении
маленького файла.

Ротация примитивная: при превышении serverLogMaxBytes файл
переименовывается в .1 (старый .1 затирается). Двух поколений хватает —
журнал нужен для разбора «почему упало только что», а не как архив.
"""
import os
import sys
import time

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    tomllib = None

HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
CLAUDE_DIR = os.path.dirname(HOOKS_DIR)
CONFIG_PATH = os.path.join(CLAUDE_DIR, "patches", "claude-custom-config.toml")
LOG_PATH = os.path.join(CLAUDE_DIR, "hooks-runtime", "http-server.log")

DEFAULT_ENABLED = True
DEFAULT_MAX_BYTES = 1_048_576  # 1 МиБ


def _config() -> tuple[bool, int]:
    if tomllib is None or not os.path.isfile(CONFIG_PATH):
        return DEFAULT_ENABLED, DEFAULT_MAX_BYTES
    try:
        with open(CONFIG_PATH, "rb") as fh:
            data = tomllib.load(fh)
    except Exception:
        return DEFAULT_ENABLED, DEFAULT_MAX_BYTES

    enabled = data.get("serverLog", DEFAULT_ENABLED)
    if not isinstance(enabled, bool):
        enabled = DEFAULT_ENABLED

    max_bytes = data.get("serverLogMaxBytes", DEFAULT_MAX_BYTES)
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
        max_bytes = DEFAULT_MAX_BYTES

    return enabled, max_bytes


def _rotate(max_bytes: int) -> None:
    try:
        if os.path.getsize(LOG_PATH) < max_bytes:
            return
    except OSError:
        return
    try:
        os.replace(LOG_PATH, LOG_PATH + ".1")
    except OSError:
        pass


def log(component: str, message: str) -> None:
    """Пишет строку в журнал. Никогда не бросает исключений: журнал —
    диагностика, он не имеет права ронять того, кого диагностирует."""
    try:
        enabled, max_bytes = _config()
        if not enabled:
            return
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        _rotate(max_bytes)
        now = time.time()
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
        stamp += f".{int((now % 1) * 1000):03d}"
        line = f"{stamp} [{component}:{os.getpid()}] {message}\n"
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:
        pass


def tail(lines: int = 40) -> str:
    """Последние строки журнала — для быстрого просмотра из CLI."""
    try:
        with open(LOG_PATH, encoding="utf-8", errors="replace") as fh:
            return "".join(fh.readlines()[-lines:])
    except OSError:
        return ""


if __name__ == "__main__":
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    sys.stdout.write(tail(count) or f"журнал пуст или отсутствует: {LOG_PATH}\n")
