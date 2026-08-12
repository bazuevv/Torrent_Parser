#!/usr/bin/env python3
"""Хук: добавляет connect-src в CSP-meta тег extension.js Claude Code,
чтобы webview JS (наш claude-custom.js) мог делать fetch на:

  - https://api.anthropic.com — on-demand-ping проверки интернета (📡 в overlay).
  - http://localhost:*        — http-server.py: /save-log, /locale-drift
                                и любые будущие endpoint'ы локального сервиса.

Без этого патча CSP webview по умолчанию `default-src 'none'` блокирует
любой fetch (TypeError: Failed to fetch при попытке).

Идемпотентен:
  - Уже пропатчено нашим текущим CSP_CONNECT_SRC → ничего не делаем.
  - Найден старый/иной connect-src (от прошлых версий патча или из самого
    Anthropic) → удаляем его и вставляем актуальный.
  - CSP не найден (Anthropic поменял структуру до неузнаваемости) → молча
    выходим, пишет ничего.

CSP-meta тег в extension.js собирается template literal'ом вида:
    <meta http-equiv="Content-Security-Policy" content="default-src 'none';
     ${L}; ${O}; ${A}; script-src 'nonce-${D}'; ${I};">

Раньше патч искал якорь `${O};">` (предполагая что ${O} последняя
переменная), но после обновления Claude Code 2.1.126 шаблон удлинился
(${A}, ${I}), и старый якорь перестал находиться. Теперь захватывается
весь content="..." регуляркой по `${X}` внутри (отличает основной CSP
от вспомогательного error-page CSP с `{{NONCE}}` плейсхолдером).

Регистрация в .claude/settings.json — только на SessionStart. Файл
extension.js обновляется лишь при переустановке/обновлении расширения
Claude Code, поэтому повторять патч на каждом UserPromptSubmit смысла
нет (а сама операция, хоть и идемпотентна, читает мегабайтный файл
с диска).
"""

import glob
import json
import os
import re
import sys

HOME = os.path.expanduser("~")
EXT_GLOB = os.path.join(HOME, ".vscode/extensions/anthropic.claude-code-*-linux-x64")

CSP_CONNECT_SRC = "connect-src https://api.anthropic.com http://localhost:*"

# Захватываем основной CSP-meta тег.
# Признак отличающий его от error-page CSP — наличие `${...}` (template
# literal-переменная) внутри content. Error-page CSP содержит только
# буквальный `{{NONCE}}` плейсхолдер, без `${...}`.
#
# Имя переменной НЕ фиксируем: минификатор выбирает его сам и меняет
# от сборки к сборке. В 2.1.126 это были одиночные заглавные
# (`${L}; ${O}; ${A}`), в 2.1.220 — строчные (`${p}; ${f}; ${m}`).
# Прежний паттерн `\$\{[A-Z]\}` после обновления перестал совпадать,
# патч тихо переставал применяться, и webview терял fetch к
# api.anthropic.com и localhost (пинг 📡 показывал csp_blocked,
# locale-drift и models-list не доезжали до http-server.py).
CSP_META_RE = re.compile(
    r'(<meta http-equiv="Content-Security-Policy" content=")'
    r"(default-src 'none';[^\"]*?\$\{[A-Za-z_$][A-Za-z0-9_$]*\}[^\"]*?)"
    r'(")',
    re.DOTALL,
)

# Удаляем уже существующий connect-src (любой формы), чтобы заменить
# его актуальным. Захватывает с ведущим пробелом и опционально с `;` на
# конце, чтобы при удалении не оставалось двойного пробела/висящих `;`.
OLD_CONNECT_SRC_RE = re.compile(r"\s*connect-src[^;\"]*;?")


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _write_if_changed(path: str, new_content: str) -> bool:
    try:
        old = _read(path)
    except OSError:
        old = None
    if old == new_content:
        return False
    with open(path, "w", encoding="utf-8") as f:
        f.write(new_content)
    return True


def _patch_csp(content: str) -> tuple[str, str | None]:
    """Возвращает (status, new_content).

    status:
      - "not_found" — CSP-meta тег не распознан (Anthropic поменял
        структуру шаблона); патчить нечего, это повод для warning'а;
      - "already"   — connect-src уже актуален, new_content = None;
      - "patched"   — new_content содержит обновлённый файл.

    Алгоритм:
      1. Найти основной CSP-meta тег по `${...}` маркеру.
      2. Из его content удалить старый connect-src (если был).
      3. Дописать наш CSP_CONNECT_SRC перед закрывающей кавычкой.
    """
    m = CSP_META_RE.search(content)
    if not m:
        return ("not_found", None)

    inner = m.group(2)
    # Уберём любые existing connect-src (предыдущие версии, чужие)
    cleaned = OLD_CONNECT_SRC_RE.sub("", inner).rstrip()
    if not cleaned.endswith(";"):
        cleaned += ";"

    new_inner = cleaned + " " + CSP_CONNECT_SRC + ";"
    if new_inner == inner:
        return ("already", None)

    patched = (
        content[: m.start()] + m.group(1) + new_inner + m.group(3) + content[m.end():]
    )
    return ("patched", patched)


def _patch_ext_dir(ext_dir: str) -> str:
    """Патчит extension.js в каталоге расширения. Возвращает status:
    "no_file" | "unreadable" | "not_found" | "already" | "patched" | "write_failed".
    """
    ext_js = os.path.join(ext_dir, "extension.js")
    if not os.path.isfile(ext_js):
        return "no_file"
    try:
        content = _read(ext_js)
    except OSError:
        return "unreadable"

    status, new_content = _patch_csp(content)
    if status != "patched":
        return status

    try:
        _write_if_changed(ext_js, new_content or "")
    except OSError:
        return "write_failed"
    return "patched"


def _emit_context(lines: list[str]) -> None:
    """Кладёт диагностику в additionalContext SessionStart.

    Молчаливый провал этого патча уже стоил нам рабочего fetch'а
    в webview (см. шапку файла), поэтому оба интересных исхода —
    «не нашли CSP» и «только что пропатчили» — сообщаются модели
    маркером `[csp-patch WARNING]`, а она обязана показать их
    пользователю (правило в CLAUDE.md).
    """
    if not lines:
        return
    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "\n".join(lines),
        }
    }
    try:
        print(json.dumps(output, ensure_ascii=False))
    except OSError:
        pass


def main() -> int:
    messages: list[str] = []
    for ext_dir in glob.glob(EXT_GLOB):
        status = _patch_ext_dir(ext_dir)
        name = os.path.basename(ext_dir)
        if status == "not_found":
            messages.append(
                f"[csp-patch WARNING] В `{name}/extension.js` не найден CSP-meta тег "
                "нужной структуры — connect-src НЕ добавлен. Значит webview не может "
                "делать fetch: пинг 📡 покажет csp_blocked, а locale-drift/models-list "
                "не дойдут до http-server.py. Проверь CSP_META_RE в "
                "`.claude/hooks/patch-extension-csp.py` — Anthropic поменял шаблон."
            )
        elif status == "patched":
            messages.append(
                f"[csp-patch WARNING] В `{name}/extension.js` только что добавлен "
                "connect-src (раньше его не было). Чтобы патч подхватился, нужен "
                "`Developer: Reload Window` — Extension Host читает extension.js "
                "только при загрузке."
            )
        elif status in ("unreadable", "write_failed"):
            messages.append(
                f"[csp-patch WARNING] `{name}/extension.js` не удалось "
                f"{'прочитать' if status == 'unreadable' else 'записать'} — "
                "патч CSP не применён. Проверь права на файл."
            )
    _emit_context(messages)
    return 0


if __name__ == "__main__":
    sys.exit(main())
