#!/usr/bin/env python3
"""
PreToolUse hook for Bash: блокирует составные команды, в которых
одна и та же shell-переменная и присваивается (VAR=...), и используется ($VAR).
Цель — заставить переписать команду с литеральными путями, чтобы
Claude Code permission matcher мог статически проверить пути и не
запрашивать подтверждение.

Exit codes:
  0 — пропустить команду
  2 — заблокировать, stderr отдать обратно модели
"""
import json
import re
import sys

SAFE_VARS = {
    "HOME", "USER", "PWD", "OLDPWD", "PATH", "SHELL",
    "LANG", "LC_ALL", "LC_CTYPE", "TERM", "EDITOR", "IFS",
    "TMPDIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME",
    "CLAUDE_PROJECT_DIR",
}

def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0

    if data.get("tool_name") != "Bash":
        return 0

    cmd = data.get("tool_input", {}).get("command", "")
    if not cmd or not isinstance(cmd, str):
        return 0

    assignments = set(re.findall(r'^\s*([A-Za-z_][A-Za-z0-9_]*)=', cmd, re.MULTILINE))
    assignments -= SAFE_VARS

    usages = set(m.group(1) for m in re.finditer(r'\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?', cmd))

    conflicts = sorted(assignments & usages)

    if conflicts:
        msg = (
            "HOOK BLOCK: в Bash-команде переменные {vars} одновременно "
            "присваиваются (VAR=...) и используются ($VAR). "
            "Правило проекта: переписать без shell-переменных, с литеральными "
            "абсолютными путями в каждой подкоманде. "
            "См. memory/feedback_bash_no_shell_vars_in_paths.md. "
            "Если цикл — развернуть его в серию явных команд."
        ).format(vars=conflicts)
        print(msg, file=sys.stderr)
        return 2

    return 0

if __name__ == "__main__":
    sys.exit(main())
