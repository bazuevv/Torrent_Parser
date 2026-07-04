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
import os
import re
import sys

HOME = os.path.expanduser("~")
EXT_GLOB = os.path.join(HOME, ".vscode/extensions/anthropic.claude-code-*-linux-x64")

CSP_CONNECT_SRC = "connect-src https://api.anthropic.com http://localhost:*"

# Захватываем основной CSP-meta тег.
# Признак отличающий его от error-page CSP — наличие `${X}` (template
# literal-переменная) внутри content. Error-page CSP содержит только
# буквальный `{{NONCE}}` плейсхолдер, без `${...}`.
CSP_META_RE = re.compile(
    r'(<meta http-equiv="Content-Security-Policy" content=")'
    r"(default-src 'none';[^\"]*?\$\{[A-Z]\}[^\"]*?)"
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


def _patch_csp(content: str) -> str | None:
    """Возвращает обновлённый content или None, если ничего менять не надо.

    Алгоритм:
      1. Найти основной CSP-meta тег по `${X}` маркеру.
      2. Из его content удалить старый connect-src (если был).
      3. Дописать наш CSP_CONNECT_SRC перед закрывающей кавычкой.
      4. Если результат байт-в-байт совпадает с исходным — None (no-op).
    """
    m = CSP_META_RE.search(content)
    if not m:
        return None

    inner = m.group(2)
    # Уберём любые existing connect-src (предыдущие версии, чужие)
    cleaned = OLD_CONNECT_SRC_RE.sub("", inner).rstrip()
    if not cleaned.endswith(";"):
        cleaned += ";"

    new_inner = cleaned + " " + CSP_CONNECT_SRC + ";"
    if new_inner == inner:
        return None  # уже актуально

    return content[: m.start()] + m.group(1) + new_inner + m.group(3) + content[m.end():]


def _patch_ext_dir(ext_dir: str) -> bool:
    ext_js = os.path.join(ext_dir, "extension.js")
    if not os.path.isfile(ext_js):
        return False
    try:
        content = _read(ext_js)
    except OSError:
        return False

    new_content = _patch_csp(content)
    if new_content is None:
        return False

    return _write_if_changed(ext_js, new_content)


def main() -> int:
    for ext_dir in glob.glob(EXT_GLOB):
        _patch_ext_dir(ext_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
