#!/usr/bin/env python3
"""
PreToolUse hook. Классифицирует tool-вызовы, которые потенциально меняют
данные на диске, и возвращает permissionDecision: "ask" с громким
эмодзи-предупреждением в reason, если пути-аргументы выходят за пределы
проекта и /tmp/.

Покрывает:
  - Bash: команды rm/rmdir/unlink/shred/mv
  - Edit / Write: поле tool_input.file_path
  - NotebookEdit: поле tool_input.notebook_path

Логика: hook возвращает "ask" (не "deny"), чтобы НЕ блокировать жёстко,
а дать пользователю решить в окне подтверждения. В Claude Code правило
агрегации: deny > ask > allow — т.е. даже если bypass-check.sh возвращает
allow для этой же команды, наш "ask" перебьёт его и покажет промпт.
"""
import datetime
import json
import os
import pathlib
import re
import shlex
import sys

DESTRUCTIVE_BASH_CMDS = {"rm", "rmdir", "unlink", "shred", "mv"}
FILE_MODIFYING_TOOLS = {"Edit", "Write", "NotebookEdit"}
WRAPPERS = {"sudo", "env", "nohup"}
SPLIT_RE = re.compile(r"[\n;]|&&|\|\||\|")


def log_trigger(project_dir: str, event: dict) -> None:
    """Дописывает строку JSON в .claude/hooks-runtime/warn.log.
    Модель может прочитать этот файл, чтобы узнать, фаерился ли
    хук на предыдущую команду, и тем самым понять, показывалось
    ли пользователю окно подтверждения."""
    log_path = pathlib.Path(project_dir) / ".claude" / "hooks-runtime" / "warn.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        pass


def is_inside(path_str: str, safe_roots: list[str]) -> bool:
    for root in safe_roots:
        root_prefix = root.rstrip("/") + "/"
        if path_str == root or path_str.startswith(root_prefix):
            return True
    return False


def normalize_path(raw: str, cwd: str) -> str:
    expanded = os.path.expanduser(os.path.expandvars(raw))
    if not os.path.isabs(expanded):
        expanded = os.path.join(cwd, expanded)
    return os.path.normpath(expanded)


def strip_wrappers(tokens: list[str]) -> list[str]:
    while tokens and tokens[0] in WRAPPERS:
        tokens = tokens[1:]
    if tokens and tokens[0] == "timeout" and len(tokens) > 2:
        tokens = tokens[2:]
    return tokens


def analyze_bash(cmd: str, cwd: str, safe_roots: list[str]) -> list[tuple[str, list[str]]]:
    findings: list[tuple[str, list[str]]] = []
    for sub in SPLIT_RE.split(cmd):
        sub = sub.strip()
        if not sub:
            continue
        try:
            tokens = shlex.split(sub, posix=True)
        except ValueError:
            continue
        tokens = strip_wrappers(tokens)
        if not tokens:
            continue

        cmd_name = os.path.basename(tokens[0])
        if cmd_name not in DESTRUCTIVE_BASH_CMDS:
            continue

        bad: list[str] = []
        for tok in tokens[1:]:
            if tok.startswith("-"):
                continue
            if any(ch in tok for ch in "*?["):
                base = os.path.dirname(tok) or "."
                resolved = normalize_path(base, cwd)
            else:
                resolved = normalize_path(tok, cwd)
            if not is_inside(resolved, safe_roots):
                bad.append(tok)

        if bad:
            findings.append((cmd_name, bad))

    return findings


def build_reason_bash(findings: list[tuple[str, list[str]]], project_dir: str) -> str:
    lines = [
        "🚨🚨🚨 ОСТОРОЖНО: ДЕСТРУКТИВНАЯ КОМАНДА ВНЕ ПРОЕКТА 🚨🚨🚨",
        "",
    ]
    for cmd_name, bad in findings:
        lines.append(f"Команда: {cmd_name}")
        for p in bad:
            lines.append(f"  ⚠️  Путь вне разрешённых зон: {p}")
    lines.append("")
    lines.append(f"Разрешённые зоны: {project_dir} и /tmp/")
    lines.append("❗ Проверь команду внимательно перед одобрением!")
    return "\n".join(lines)


def build_reason_file(tool_name: str, path_raw: str, project_dir: str) -> str:
    lines = [
        "🚨🚨🚨 ОСТОРОЖНО: ЗАПИСЬ В ФАЙЛ ВНЕ ПРОЕКТА 🚨🚨🚨",
        "",
        f"Инструмент: {tool_name}",
        f"  ⚠️  Путь вне разрешённых зон: {path_raw}",
        "",
        f"Разрешённые зоны: {project_dir} и /tmp/",
        "❗ Проверь путь внимательно перед одобрением!",
        "❗ Запись по такому пути может перезаписать системные файлы",
        "   (~/.ssh/, ~/.bashrc, ~/.gitconfig и т.п.),",
        "   что эквивалентно проникновению/удалению данных.",
    ]
    return "\n".join(lines)


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0

    tool_name = data.get("tool_name", "")
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd", "")
    cwd = data.get("cwd") or project_dir
    if not project_dir:
        return 0

    safe_roots = [project_dir.rstrip("/"), "/tmp"]
    reason: str | None = None

    if tool_name == "Bash":
        cmd = data.get("tool_input", {}).get("command", "")
        if not isinstance(cmd, str) or not cmd:
            return 0
        findings = analyze_bash(cmd, cwd, safe_roots)
        if findings:
            reason = build_reason_bash(findings, project_dir)

    elif tool_name in FILE_MODIFYING_TOOLS:
        tool_input = data.get("tool_input", {})
        path_raw = tool_input.get("file_path") or tool_input.get("notebook_path", "")
        if not isinstance(path_raw, str) or not path_raw:
            return 0
        resolved = normalize_path(path_raw, cwd)
        if not is_inside(resolved, safe_roots):
            reason = build_reason_file(tool_name, path_raw, project_dir)

    else:
        return 0

    if reason is None:
        return 0

    session_id = data.get("session_id") or ""
    event = {
        "ts": datetime.datetime.now().isoformat(timespec="milliseconds"),
        "session": session_id,
        "tool": tool_name,
        "reason_head": reason.splitlines()[0] if reason else "",
    }
    if tool_name == "Bash":
        event["command"] = data.get("tool_input", {}).get("command", "")[:500]
    else:
        event["file_path"] = (
            data.get("tool_input", {}).get("file_path")
            or data.get("tool_input", {}).get("notebook_path")
            or ""
        )
    log_trigger(project_dir, event)

    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": reason,
        }
    }
    print(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
