#!/usr/bin/env python3
"""
UserPromptSubmit hook. Ловит магическое слово `да!` в сообщении
пользователя и ставит marker-файл для текущей сессии. Пока marker
существует, PreToolUse хук `bypass-check.sh` будет авто-approve-ить
все tool-вызовы этой сессии (см. настройку в .claude/settings.json).

Marker автоматически удаляется при SessionEnd хуком `bypass-cleanup.py`.
"""
import json
import os
import pathlib
import re
import sys
import time

MAGIC_WORD_RE = re.compile(r'(?<![А-Яа-яЁё])да!', re.IGNORECASE)

def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0

    session_id = data.get("session_id") or ""
    if not session_id:
        return 0

    prompt = data.get("prompt", "")
    if not isinstance(prompt, str):
        return 0

    if not MAGIC_WORD_RE.search(prompt):
        return 0

    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", data.get("cwd", ""))
    if not project_dir:
        return 0

    marker_dir = pathlib.Path(project_dir) / ".claude" / "hooks-runtime"
    marker_dir.mkdir(parents=True, exist_ok=True)
    marker = marker_dir / session_id
    marker.write_text(f"{time.time()}\n")

    context = (
        "⚡ BYPASS-РЕЖИМ ВКЛЮЧЁН для этой сессии (триггер: слово 'да!'). "
        "Следующие tool-вызовы проходят без окна подтверждения. "
        "Deny-правила в settings.json и хук bash-no-var-paths.py продолжают работать. "
        "Отключится автоматически при закрытии сессии."
    )
    output = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        }
    }
    print(json.dumps(output, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    sys.exit(main())
