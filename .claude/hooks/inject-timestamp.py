#!/usr/bin/env python3
"""
UserPromptSubmit hook. Добавляет в additionalContext метку времени
отправки сообщения пользователем, чтобы модель могла показать её
в своём ответе рядом с цитатой/обсуждением сообщения.

Формат вывода:
  [user message sent at YYYY-MM-DD HH:MM:SS]

Модель видит эту строку в контексте текущего запроса и включает
её в свой ответ, когда пользователь просил «показывать время
отправки сообщения». В самом UI чата метка не отображается —
это ограничение VSCode-расширения Claude Code, которое не
предоставляет хукам доступа к рендеру сообщений.
"""
import datetime
import json
import sys


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0

    if data.get("hook_event_name") and data.get("hook_event_name") != "UserPromptSubmit":
        return 0

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    context = f"[user message sent at {now}]"

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
