#!/usr/bin/env python3
"""
SessionEnd hook. Делает две вещи:

1. Удаляет marker текущей сессии (`.claude/hooks-runtime/<session_id>`) —
   штатная уборка после нормального закрытия.

2. Чистит «сиротские» marker'ы от сессий, которые упали/убиты и не
   запустили этот хук. Использует три источника для определения
   активных сессий:

   a) `/proc/<pid>/cmdline` всех claude-процессов → ищет `--resume <uuid>`.
      Покрывает возобновлённые сессии (у них ID в cmdline).
   b) `mtime` у `~/.claude/projects/*/*.jsonl` → сессия, в которую
      писали < 30 минут назад, считается живой.
   c) Grace period 2h: marker младше 2 часов не удаляется, даже если
      не распознали как активный. Safety net для новых idle-сессий.

Важно: marker НЕ может утечь в следующую сессию даже без этого хука.
Имя marker'а — `session_id`, и PreToolUse хук ищет файл строго по ID
СВОЕЙ сессии. У новой сессии ID другой — старые marker'ы для неё
невидимы. Этот хук нужен исключительно для гигиены диска.
"""
import json
import os
import pathlib
import re
import sys
import time

UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
)
JSONL_NAME_RE = re.compile(
    r'^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$'
)

RECENT_JSONL_SEC = 30 * 60
GRACE_PERIOD_SEC = 2 * 3600


def get_active_session_ids() -> set[str]:
    active: set[str] = set()

    proc = pathlib.Path("/proc")
    if proc.is_dir():
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            cmdline_path = entry / "cmdline"
            try:
                raw = cmdline_path.read_bytes()
            except (OSError, PermissionError):
                continue
            parts = raw.decode("utf-8", errors="replace").split("\x00")
            for i, part in enumerate(parts):
                if part == "--resume" and i + 1 < len(parts):
                    candidate = parts[i + 1]
                    if UUID_RE.match(candidate):
                        active.add(candidate)

    projects_dir = pathlib.Path.home() / ".claude" / "projects"
    if projects_dir.is_dir():
        now = time.time()
        for jsonl in projects_dir.glob("*/*.jsonl"):
            try:
                mtime = jsonl.stat().st_mtime
            except OSError:
                continue
            if now - mtime > RECENT_JSONL_SEC:
                continue
            m = JSONL_NAME_RE.match(jsonl.name)
            if m:
                active.add(m.group(1))

    return active


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0

    current_session = data.get("session_id") or ""
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", data.get("cwd", ""))
    if not project_dir:
        return 0

    runtime_dir = pathlib.Path(project_dir) / ".claude" / "hooks-runtime"

    if current_session:
        try:
            (runtime_dir / current_session).unlink(missing_ok=True)
        except Exception:
            pass

    if not runtime_dir.is_dir():
        return 0

    active = get_active_session_ids()
    now = time.time()

    for marker in runtime_dir.iterdir():
        if not marker.is_file():
            continue
        if marker.name == current_session:
            continue
        if marker.name in active:
            continue

        try:
            age = now - marker.stat().st_mtime
        except OSError:
            continue
        if age < GRACE_PERIOD_SEC:
            continue

        try:
            marker.unlink()
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
