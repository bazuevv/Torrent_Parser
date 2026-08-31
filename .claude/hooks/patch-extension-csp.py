#!/usr/bin/env python3
"""Хук: все правки `extension.js` расширения Claude Code.

Патчей два, и живут они в одном хуке намеренно — хуки одного события
выполняются параллельно, поэтому два процесса, переписывающих один и тот
же мегабайтный файл, затёрли бы правки друг друга (прецедент с
localize.py в CLAUDE.md). Имя файла историческое: сначала был только
CSP.

1. CSP — добавляет connect-src в CSP-meta тег (см. ниже).
2. Перезапуск extension host — дописывает в конец файла блок, который
   следит за заявкой от панели Accs, перезагружает webview-вкладки
   (`workbench.action.webview.reloadWebviewAction`) и вызывает
   `workbench.action.restartExtensionHost`. Нужен потому, что смена
   аккаунта провайдера подменяет ~/.claude/settings.json, а `env` оттуда
   читает CLI-процесс при старте — и стартует он один раз на активацию
   хоста. Перезагрузка webview нужна потому, что контент вкладок —
   iframe в окне VSCode — переживает смерть extension host: рестарт
   хоста сам по себе обновляет только tree view. Подробности — в
   комментарии к RESTART_BLOCK.

Патч 1: добавляет connect-src в CSP-meta тег extension.js Claude Code,
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

# --- блок перезапуска extension host -----------------------------------
#
# Дописывается в конец extension.js. Файл — CommonJS-бандл (внутри уже
# есть `require("vscode")`), поэтому дописанный top-level код исполняется
# при загрузке модуля, то есть на активации расширения, и `require` там
# доступен.
RESTART_BEGIN = "/* claude-exthost-restart */"
RESTART_END = "/* /claude-exthost-restart */"

RESTART_BLOCK_RE = re.compile(
    re.escape(RESTART_BEGIN) + r".*?" + re.escape(RESTART_END),
    re.DOTALL,
)

# Токен-протокол намеренно простой: заявку пишет http-server.py, здесь
# только чтение и сверка. Разбор см. в шапке RESTART_REQUEST_FILE
# в .claude/hooks/http-server.py — оба конца обязаны совпадать по именам
# файлов и по полю `token`.
RESTART_BLOCK = RESTART_BEGIN + """
// Перезапуск extension host по заявке от панели Accs.
//
// Смена аккаунта провайдера подменяет ~/.claude/settings.json, но `env`
// оттуда применяет к себе CLI-процесс `claude` при старте, а стартует он
// один раз на активацию extension host. Значит применить смену без
// полной перезагрузки окна можно только перезапуском хоста.
//
// Рестарт хоста обновляет только tree view (сессии слева, агенты
// справа): контент webview-вкладок — диалогов Claude Code — это iframe
// в окне VSCode, он переживает смерть extension host и остаётся со
// старым JS. Поэтому перед рестартом перезагружаем webview штатной
// командой «Developer: Reload Webviews». Порядок обязан быть именно
// таким: наш код живёт в умирающем процессе, «сначала рестарт, потом
// перезагрузка вкладок» исполнить некому.
//
// Webview командой VSCode не располагает, поэтому связь через файл:
// http-server.py кладёт <workspace>/.claude/hooks-runtime/
// restart-exthost-request.json, а этот блок его читает.
//
// Область действия задаётся сама: путь выводится из корней воркспейса
// ЭТОГО окна, поэтому чужие проекты заявку не видят, а окна с тем же
// проектом перезапустятся каждое по одному разу.
try {
  (function () {
    var vscode = require("vscode");
    var fs = require("fs");
    var path = require("path");
    var REQ = "restart-exthost-request.json";
    var ACK = "restart-exthost-ack.json";
    var POLL_MS = 1000;

    function tokenOf(file) {
      try {
        var d = JSON.parse(fs.readFileSync(file, "utf8"));
        return d && typeof d.token === "string" ? d.token : "";
      } catch (e) {
        // Файла нет, или его перезаписывают прямо сейчас — штатно.
        return "";
      }
    }

    function runtimeDirs() {
      var out = [];
      var folders = vscode.workspace.workspaceFolders || [];
      for (var i = 0; i < folders.length; i++) {
        try {
          out.push(path.join(folders[i].uri.fsPath, ".claude", "hooks-runtime"));
        } catch (e) {}
      }
      return out;
    }

    // dir -> токен, известный на момент последней проверки. Базовый
    // снимок берётся при первом же взгляде на папку, поэтому заявка,
    // которая только что вызвала перезапуск, после реактивации уже не
    // считается новой — иначе получился бы бесконечный цикл.
    var seen = Object.create(null);

    function scan() {
      var dirs = runtimeDirs();
      for (var i = 0; i < dirs.length; i++) {
        var dir = dirs[i];
        var tok = tokenOf(path.join(dir, REQ));
        if (!(dir in seen)) { seen[dir] = tok; continue; }
        if (!tok || tok === seen[dir]) continue;
        seen[dir] = tok;

        // Подтверждение пишем ДО команды: процесс вот-вот умрёт, после
        // неё записать уже не успеем. Если команда не найдётся —
        // подтверждение снимаем, чтобы панель не считала заявку принятой.
        var ackFile = path.join(dir, ACK);
        try {
          fs.writeFileSync(ackFile, JSON.stringify({
            token: tok, pid: process.pid, ts: Date.now(),
          }));
        } catch (e) {}

        var doRestart = function () {
          Promise.resolve(
            vscode.commands.executeCommand("workbench.action.restartExtensionHost")
          ).then(undefined, function () {
            // Команды нет (сборка VSCode другая) — перезагружаем окно
            // целиком. Дороже, но смена аккаунта всё-таки применится
            // (reloadWindow перезагружает и webview-вкладки).
            try { fs.unlinkSync(ackFile); } catch (e2) {}
            try { vscode.commands.executeCommand("workbench.action.reloadWindow"); } catch (e2) {}
          });
        };

        var reloadWebviews = function () {
          try {
            // «Developer: Reload Webviews»: контент всех webview окна
            // грузится заново. Точечно только вкладки Claude Code нельзя
            // — API расширения не перебирает чужие webview-панели.
            vscode.commands.executeCommand(
              "workbench.action.webview.reloadWebviewAction");
          } catch (e2) {}
          // Чуть ждём, чтобы iframe начали загрузку до смерти хоста:
          // рестарт сразу следом обрывал бы её на полпути.
          setTimeout(doRestart, 500);
        };

        // Пауза перед перезагрузкой webview: панель Accs опрашивает ack
        // каждые 400 мс (ACK_POLL_MS в claude-custom.js) — дадим ей
        // увидеть подтверждение и отрисовать «Расширение принимает
        // перезапуск…», сразу после reload её DOM исчезнет вместе
        // со вкладкой.
        try {
          setTimeout(reloadWebviews, 1600);
        } catch (e) {
          // Синхронно упасть тут нечему, но если вдруг — перезапуск
          // без перезагрузки webview лучше, чем ничего.
          try { fs.unlinkSync(ackFile); } catch (e2) {}
          doRestart();
        }
        return;
      }
    }

    scan();
    var timer = setInterval(scan, POLL_MS);
    // Таймер не должен сам по себе держать процесс живым.
    if (timer && typeof timer.unref === "function") timer.unref();
  })();
} catch (e) {
  console.error("claude-exthost-restart:", e);
}
""" + RESTART_END + "\n"


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


def _patch_restart_block(content: str) -> tuple[str, str | None]:
    """Вставляет/обновляет блок перезапуска extension host.

    status:
      - "already" — блок уже актуален, new_content = None;
      - "patched" — new_content содержит обновлённый файл.

    В отличие от CSP-патча провалиться здесь нечему: блок дописывается
    в конец файла и ни на какие внутренности бандла не опирается. Если
    блок от прошлой версии патча найден — заменяется целиком, чтобы не
    копить дубли.
    """
    m = RESTART_BLOCK_RE.search(content)
    if m:
        if m.group(0) == RESTART_BLOCK.rstrip("\n"):
            return ("already", None)
        patched = content[: m.start()] + RESTART_BLOCK.rstrip("\n") + content[m.end():]
        return ("patched", patched)

    tail = content if content.endswith("\n") else content + "\n"
    return ("patched", tail + "\n" + RESTART_BLOCK)


def _patch_ext_dir(ext_dir: str) -> tuple[str, str]:
    """Патчит extension.js в каталоге расширения.

    Возвращает (csp_status, restart_status). Оба патча живут в одном
    хуке и делают ОДНУ запись файла намеренно: хуки одного события
    выполняются параллельно, и два процесса, переписывающих один и тот
    же мегабайтный файл, затёрли бы правки друг друга (прецедент с
    localize.py в CLAUDE.md).

    Статусы:
      csp     — "no_file" | "unreadable" | "not_found" | "already" |
                "patched" | "write_failed";
      restart — то же, кроме "not_found".
    """
    ext_js = os.path.join(ext_dir, "extension.js")
    if not os.path.isfile(ext_js):
        return ("no_file", "no_file")
    try:
        content = _read(ext_js)
    except OSError:
        return ("unreadable", "unreadable")

    csp_status, csp_content = _patch_csp(content)
    if csp_content is not None:
        content = csp_content

    restart_status, restart_content = _patch_restart_block(content)
    if restart_content is not None:
        content = restart_content

    if csp_status != "patched" and restart_status != "patched":
        return (csp_status, restart_status)

    try:
        _write_if_changed(ext_js, content)
    except OSError:
        return (
            "write_failed" if csp_status == "patched" else csp_status,
            "write_failed" if restart_status == "patched" else restart_status,
        )
    return (csp_status, restart_status)


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
        status, restart_status = _patch_ext_dir(ext_dir)
        name = os.path.basename(ext_dir)

        if restart_status == "patched":
            messages.append(
                f"[exthost-restart WARNING] В `{name}/extension.js` только что "
                "обновлён блок перезапуска extension host. Чтобы он заработал, "
                "нужен `Developer: Reload Window` — Extension Host читает "
                "extension.js только при загрузке. До этого кнопка Accs "
                "переключит аккаунт, но применить смену предложением "
                "о перезапуске не сможет."
            )
        elif restart_status in ("unreadable", "write_failed"):
            messages.append(
                f"[exthost-restart WARNING] `{name}/extension.js` не удалось "
                f"{'прочитать' if restart_status == 'unreadable' else 'записать'} — "
                "блок перезапуска extension host не применён. Смена аккаунта "
                "в панели Accs будет требовать ручного `Developer: Reload Window`."
            )

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
