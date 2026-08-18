#!/usr/bin/env python3
"""SessionStart-хук: синхронизирует кастомные стили и JS-логику в webview
расширения Claude Code (anthropic.claude-code).

Что делает:
1. Копирует канонический CSS из `.claude/patches/claude-custom.css` в
   `webview/claude-custom.css` каждой установленной версии расширения,
   чтобы webview подгружал его как отдельный файл через <link>.
2. Читает канонический JS из `.claude/patches/claude-custom.js` и
   инлайнит его внутрь bootstrap-блока в `webview/index.js`. Отдельным
   файлом JS подгрузить не получается: CSP webview блокирует динамически
   созданные <script src=...>, поэтому код встраивается в уже
   доверенный index.js.
3. Дописывает в `webview/index.js` bootstrap-блок (с маркером
   `/* claude-green-timestamp */`), который:
     - инжектит <link rel="stylesheet" href="claude-custom.css">
       (CSS отдельным файлом — для CSS CSP менее строга);
     - выполняет инлайн-код кастомного JS.

Срабатывает молча. Изменения требуют Developer: Reload Window для
применения в текущем окне; на следующих запусках всё уже на месте.
"""

import glob
import json
import os
import re
import signal
import subprocess
import sys
import time
import tomllib
import urllib.request

HOME = os.path.expanduser("~")
EXT_GLOB = os.path.join(HOME, ".vscode/extensions/anthropic.claude-code-*-linux-x64")

PROJECT_DIR = os.environ.get("CLAUDE_PROJECT_DIR")
if not PROJECT_DIR:
    PROJECT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

PATCHES_DIR = os.path.join(PROJECT_DIR, ".claude", "patches")
CANONICAL_CSS = os.path.join(PATCHES_DIR, "claude-custom.css")
CANONICAL_JS = os.path.join(PATCHES_DIR, "claude-custom.js")
CANONICAL_CONFIG = os.path.join(PATCHES_DIR, "claude-custom-config.toml")

CUSTOM_CSS_NAME = "claude-custom.css"
# CSP-патч в extension.js вынесен в отдельный хук
# .claude/hooks/patch-extension-csp.py — зарегистрирован в settings.json
# только на SessionStart (extension.js не меняется в течение сессии).

HTTP_SERVER_SCRIPT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "http-server.py"
)
HTTP_SERVER_PORT = 18923

# Описание ожидаемых параметров: ключ → (валидатор, человекочитаемое требование).
# При отсутствии или невалидном значении хук добавит предупреждение в
# additionalContext, и модель сообщит об этом в чат.
REQUIRED_PARAMS = [
    (
        "logs",
        lambda v: isinstance(v, bool),
        "должен быть true или false",
    ),
    (
        "pollIntervalMs",
        lambda v: isinstance(v, int) and not isinstance(v, bool) and v > 0,
        "должен быть положительным целым числом (мс)",
    ),
    (
        "throttleMs",
        lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 0,
        "должен быть неотрицательным целым числом (мс)",
    ),
    (
        "visibilityRefreshDelayMs",
        lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 0,
        "должен быть неотрицательным целым числом (мс)",
    ),
    (
        "debugOverlay",
        lambda v: isinstance(v, bool),
        "должен быть true или false",
    ),
    (
        "locale",
        lambda v: isinstance(v, str) and len(v) >= 2,
        "должен быть строкой с кодом языка (например 'ru', 'en')",
    ),
    (
        "autoPingAfterSilenceSec",
        lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 0,
        "должен быть неотрицательным целым числом (сек)",
    ),
    (
        "pingIntervalSec",
        lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 0,
        "должен быть неотрицательным целым числом (сек)",
    ),
    (
        "maxPingsPerProcessing",
        lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 0,
        "должен быть неотрицательным целым числом",
    ),
    (
        "emojiPicker",
        lambda v: isinstance(v, bool),
        "должен быть true или false",
    ),
    (
        "emojiRecentLimit",
        lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 0,
        "должен быть неотрицательным целым числом",
    ),
    (
        "emojiAutoReplace",
        lambda v: isinstance(v, bool),
        "должен быть true или false",
    ),
    (
        "fixSettingsMenuItem",
        lambda v: isinstance(v, bool),
        "должен быть true или false",
    ),
    (
        "usageButton",
        lambda v: isinstance(v, bool),
        "должен быть true или false",
    ),
    (
        "bypassButton",
        lambda v: isinstance(v, bool),
        "должен быть true или false",
    ),
    (
        "cacheKeepalive",
        lambda v: isinstance(v, bool),
        "должен быть true или false",
    ),
    (
        "cacheKeepaliveMinutes",
        lambda v: isinstance(v, int) and not isinstance(v, bool) and v > 0,
        "должен быть положительным целым числом (мин)",
    ),
    (
        "cacheKeepaliveMessage",
        lambda v: isinstance(v, str) and len(v.strip()) > 0,
        "должен быть непустой строкой",
    ),
    (
        "cacheKeepaliveMinContext",
        lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 0,
        "должен быть неотрицательным целым числом (токенов)",
    ),
    (
        "serverLog",
        lambda v: isinstance(v, bool),
        "должен быть true или false",
    ),
    (
        "serverLogMaxBytes",
        lambda v: isinstance(v, int) and not isinstance(v, bool) and v > 0,
        "должен быть положительным целым числом (байт)",
    ),
    (
        "serverConfigWatchSec",
        lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 0,
        "должен быть неотрицательным целым числом (сек), 0 — не следить",
    ),
]

MARKER_BEGIN = "/* claude-green-timestamp */"
MARKER_END = "/* /claude-green-timestamp */"

# Настройки, которые пользователь меняет не в TOML, а в VSCode Settings UI
# (пункты в манифест расширения вписывает patch-extension-settings.py).
# Ключ VSCode → ключ в window.__CLAUDE_CUSTOM_CONFIG__, значение по
# умолчанию и допустимые варианты.
VSCODE_SETTINGS = [
    ("claudeCode.emojiButtonPlacement", "emojiButtonPlacement", "mic", ("mic", "footer")),
]

USER_SETTINGS_PATHS = [
    os.path.join(HOME, ".config/Code/User/settings.json"),
    os.path.join(HOME, ".config/Code - Insiders/User/settings.json"),
    os.path.join(HOME, ".vscode-server/data/Machine/settings.json"),
]

# Шаблон bootstrap, дописываемого в webview/index.js.
# Сначала прокидываем конфиг в `window.__CLAUDE_CUSTOM_CONFIG__`, затем
# подцепляем claude-custom.css через <link> (для CSS CSP лояльна), затем
# выполняем инлайн-код кастомного JS — он подставляется хуком из
# `.claude/patches/claude-custom.js`.
JS_BOOTSTRAP_TEMPLATE = """\
/* claude-green-timestamp */
;(function(){
  if (window.__claudeCustomBootInstalled) return;
  window.__claudeCustomBootInstalled = true;

  // Конфиг для inline-кода (из .claude/patches/claude-custom-config.json)
  window.__CLAUDE_CUSTOM_CONFIG__ = __CUSTOM_CONFIG_JSON__;

  function refUrl(){
    var ref = document.querySelector('link[rel="stylesheet"][href*="index.css"]')
           || document.querySelector('script[src*="index.js"]');
    if (!ref) return null;
    return ref.href || ref.src;
  }
  function loadCss(){
    if (document.getElementById('claude-custom-css')) return true;
    var ref = refUrl();
    if (!ref) return false;
    var link = document.createElement('link');
    link.id = 'claude-custom-css';
    link.rel = 'stylesheet';
    try { link.href = new URL('claude-custom.css', ref).href; }
    catch(e){ link.href = ref.replace(/(index\\.(css|js))([?#].*)?$/, 'claude-custom.css$3'); }
    document.head.appendChild(link);
    return true;
  }
  function tryLoadCss(){
    if (!loadCss()) {
      setTimeout(loadCss, 100);
      setTimeout(loadCss, 500);
      setTimeout(loadCss, 2000);
    }
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', tryLoadCss);
  } else {
    tryLoadCss();
  }

  // === inline custom JS из .claude/patches/claude-custom.js ===
__CUSTOM_JS__
  // === /inline custom JS ===
})();
/* /claude-green-timestamp */
"""

BLOCK_RE = re.compile(
    re.escape(MARKER_BEGIN) + r".*?" + re.escape(MARKER_END),
    re.DOTALL,
)


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _write_if_changed(path: str, new_content: str) -> bool:
    try:
        if os.path.isfile(path) and _read(path) == new_content:
            return False
    except OSError:
        pass
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_content)
        return True
    except OSError:
        return False


def _make_symlink(target: str, link_path: str) -> bool:
    """Создаёт симлинк link_path → target (абсолютный путь). Возвращает
    True, если симлинк был создан или пересоздан.
    Если по link_path уже есть правильный симлинк — ничего не делает.
    Если там обычный файл или другой симлинк — удаляет и пересоздаёт.
    """
    target_abs = os.path.abspath(target)
    try:
        if os.path.islink(link_path):
            try:
                current = os.readlink(link_path)
            except OSError:
                current = None
            if current and os.path.abspath(current) == target_abs:
                return False
            os.remove(link_path)
        elif os.path.lexists(link_path):
            os.remove(link_path)
        os.symlink(target_abs, link_path)
        return True
    except OSError:
        return False


def _upsert_marker_block(path: str, body: str) -> bool:
    """Заменить или дописать блок с маркером в файле."""
    try:
        content = _read(path)
    except OSError:
        return False
    desired = body.strip()
    if MARKER_BEGIN in content:
        existing = BLOCK_RE.search(content)
        if existing and existing.group(0).strip() == desired:
            return False
        # lambda — чтобы re не интерпретировал \s/\u в `desired` как escape
        new_content = BLOCK_RE.sub(lambda _m: desired, content, count=1)
    else:
        new_content = content.rstrip() + "\n\n" + body
    return _write_if_changed(path, new_content)


def _read_config() -> tuple[dict, list[str]]:
    """Читает конфиг из claude-custom-config.toml.

    Возвращает (config, issues), где:
      - config — словарь, прочитанный из TOML (пустой при отсутствии
        файла или ошибке парсинга);
      - issues — список человекочитаемых строк с проблемами:
        отсутствие файла, ошибка парсинга, отсутствующий или невалидный
        параметр.
    """
    issues: list[str] = []

    if not os.path.isfile(CANONICAL_CONFIG):
        issues.append(
            f"файл `{os.path.relpath(CANONICAL_CONFIG, PROJECT_DIR)}` отсутствует"
        )
        return ({}, issues)

    try:
        with open(CANONICAL_CONFIG, "rb") as f:
            cfg = tomllib.load(f)
    except OSError as exc:
        issues.append(f"файл конфига нечитаем: {exc}")
        return ({}, issues)
    except tomllib.TOMLDecodeError as exc:
        issues.append(f"файл конфига невалиден TOML: {exc}")
        return ({}, issues)

    if not isinstance(cfg, dict):
        issues.append("корень файла должен быть TOML-таблицей")
        return ({}, issues)

    for key, validator, requirement in REQUIRED_PARAMS:
        if key not in cfg:
            issues.append(
                f"параметр `{key}` отсутствует — {requirement}"
            )
        elif not validator(cfg[key]):
            issues.append(
                f"параметр `{key}` имеет неверное значение `{cfg[key]!r}` — {requirement}"
            )

    return (cfg, issues)


def _strip_jsonc(text: str) -> str:
    """Убирает из JSONC комментарии и висячие запятые.

    settings.json VSCode — это JSONC: в нём легально `// комментарий`,
    `/* блок */` и запятая перед закрывающей скобкой. json.loads на
    таком падает, а тащить сюда внешний парсер ради одного ключа
    незачем. Идём по символам, чтобы не срезать `//` внутри строкового
    значения (например, в пути `http://localhost`).
    """
    out = []
    i = 0
    n = len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:  # экранированная кавычка
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1

    cleaned = "".join(out)
    # Висячие запятые: `,` перед `}` или `]` (возможно через пробелы).
    return re.sub(r",(\s*[}\]])", r"\1", cleaned)


def _read_jsonc(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        return {}
    try:
        data = json.loads(_strip_jsonc(raw))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _vscode_settings() -> dict:
    """Настройки VSCode: user-уровень, поверх — workspace-уровень.

    Именно такой приоритет использует сам VSCode, поэтому настройка,
    выставленная для проекта, перебивает глобальную.
    """
    merged: dict = {}
    for path in USER_SETTINGS_PATHS:
        merged.update(_read_jsonc(path))
    merged.update(_read_jsonc(os.path.join(PROJECT_DIR, ".vscode", "settings.json")))
    return merged


def _apply_vscode_settings(config: dict) -> dict:
    """Подмешивает в конфиг значения из VSCode Settings UI.

    Значение из settings.json приоритетнее TOML: пункт в UI — то, что
    пользователь трогает руками чаще всего. Невалидное значение
    игнорируется, чтобы опечатка в settings.json не ломала webview.
    """
    settings = _vscode_settings()
    for vs_key, cfg_key, default, allowed in VSCODE_SETTINGS:
        value = settings.get(vs_key)
        if value not in allowed:
            value = config.get(cfg_key) if config.get(cfg_key) in allowed else default
        config[cfg_key] = value
    return config


def _build_bootstrap(custom_js: str, config: dict) -> str:
    """Подставляет конфиг и инлайн-код кастомного JS в шаблон bootstrap."""
    config_json = json.dumps(config, ensure_ascii=False)
    body = JS_BOOTSTRAP_TEMPLATE.replace("__CUSTOM_CONFIG_JSON__", config_json)
    body = body.replace("__CUSTOM_JS__", custom_js.strip())
    return body


def _pid_file_path() -> str:
    return os.path.join(PROJECT_DIR, ".claude", "hooks-runtime", "http-server.pid")


def _http_server_status(timeout: float = 0.4) -> dict | None:
    """Спрашивает сервер напрямую: `GET /ping`. None — не отвечает.

    Раньше живость определялась по PID-файлу, и это ломалось в обе
    стороны. Файл устаревал (SIGKILL не даёт отработать finally) —
    и тогда `os.kill(pid, 0)` мог попасть в чужой процесс с тем же pid.
    Файл пропадал при живом процессе — и тогда хук пытался поднять
    второй инстанс, тот не мог занять порт и молча умирал, а правки
    в http-server.py переставали подхватываться.

    Проверка ответом снимает оба случая сразу: отвечает наш сервер —
    значит он и работает; на порту чужой процесс — `status` не «ok»,
    и мы это увидим, а не примем за своего.
    """
    url = f"http://127.0.0.1:{HTTP_SERVER_PORT}/ping"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict) or data.get("status") != "ok":
        return None
    return data


def _server_sources_mtime() -> float:
    """Свежесть исходников сервера — тот же набор файлов, что считает
    сам сервер в SCRIPT_MTIME. Списки обязаны совпадать, иначе сервер
    будет перезапускаться на каждом сообщении (или не будет никогда)."""
    here = os.path.dirname(HTTP_SERVER_SCRIPT)
    newest = 0.0
    for name in ("http-server.py", "cache_usage.py", "hook_log.py"):
        try:
            newest = max(newest, os.path.getmtime(os.path.join(here, name)))
        except OSError:
            pass
    # Конфига здесь намеренно нет — см. комментарий у _sources_mtime()
    # в http-server.py: его перечитывают на лету, и перезапуск ради
    # правки настройки только плодит окна недоступности.
    return newest


def _spawn_http_server() -> None:
    """Просто запускает процесс, без проверок — их делает вызывающий."""
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = PROJECT_DIR
    env["CLAUDE_HTTP_PORT"] = str(HTTP_SERVER_PORT)
    subprocess.Popen(
        [sys.executable, HTTP_SERVER_SCRIPT],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,  # отвязать от родительского процесса
    )


def _hlog(message: str) -> None:
    """Пишет в тот же журнал, что и сервер. Импорт ленивый: хук должен
    работать, даже если hook_log почему-то отсутствует."""
    try:
        sys.path.insert(0, os.path.dirname(HTTP_SERVER_SCRIPT))
        import hook_log
        hook_log.log("hook", message)
    except Exception:
        pass


def _ensure_http_server() -> list[str]:
    """Приводит сервер в рабочее состояние. Возвращает список проблем.

    Обёрнуто межпроцессным локом: хуки разных окон срабатывают
    одновременно, и без него оба увидели бы «сервера нет» и запустили
    по своему. Второй умрёт на занятом порту, но по дороге успел бы
    наследить в pid-файле и выдать ложное предупреждение.

    Три случая:
      1. Отвечает и поднят со свежих исходников — ничего не делаем.
      2. Отвечает, но исходники новее — гасим и поднимаем заново.
         Без этого правки в http-server.py живут только до ручного
         убийства процесса: «жив» не значит «актуален».
      3. Не отвечает — поднимаем. Если после запуска так и не ответил,
         порт почти наверняка занят посторонним процессом; молчать тут
         нельзя, webview потеряет все запросы к серверу.
    """
    if not os.path.isfile(HTTP_SERVER_SCRIPT):
        return []

    lock = _acquire_spawn_lock()
    if lock is None:
        # Лок держит соседнее окно — оно прямо сейчас поднимает сервер.
        # Ждём его результат вместо параллельного запуска.
        for _ in range(30):
            time.sleep(0.1)
            if _http_server_status(timeout=0.2) is not None:
                return []
        _hlog("сосед держит лок запуска, но сервер так и не поднялся")
        return []

    try:
        return _ensure_http_server_locked()
    finally:
        _release_spawn_lock(lock)


def _spawn_lock_path() -> str:
    return os.path.join(PROJECT_DIR, ".claude", "hooks-runtime", "http-server.lock")


def _acquire_spawn_lock():
    """Неблокирующий межпроцессный лок. None — занят кем-то другим.

    На системах без fcntl (Windows) лок не берём: там от дубликата
    защищает только отказ bind'а, и этого достаточно — проигравший
    инстанс просто выходит.
    """
    try:
        import fcntl
    except ImportError:
        return False  # не None, чтобы вызывающий продолжил работу
    try:
        os.makedirs(os.path.dirname(_spawn_lock_path()), exist_ok=True)
        fh = open(_spawn_lock_path(), "w")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except OSError:
        try:
            fh.close()
        except Exception:
            pass
        return None


def _release_spawn_lock(lock) -> None:
    if not lock:
        return
    try:
        lock.close()  # закрытие снимает flock
    except Exception:
        pass


def _ensure_http_server_locked() -> list[str]:
    status = _http_server_status()
    if status is not None:
        running = 0.0
        try:
            running = float(status.get("script_mtime") or 0)
        except (TypeError, ValueError):
            running = 0.0
        # Полсекунды запаса: mtime и время старта берутся не атомарно.
        sources = _server_sources_mtime()
        if running >= sources - 0.5:
            return []
        _hlog(
            f"перезапуск: исходники новее кода сервера "
            f"(running={running:.3f}, sources={sources:.3f}), pid={status.get('pid')}"
        )
        _stop_http_server(status)
    else:
        _hlog("сервер не отвечает на /ping — поднимаю")

    _spawn_http_server()
    for _ in range(30):
        time.sleep(0.1)
        st = _http_server_status(timeout=0.2)
        if st is not None:
            _hlog(f"поднялся, pid={st.get('pid')}")
            return []
    _hlog(f"НЕ поднялся за 3 с — порт {HTTP_SERVER_PORT} занят кем-то ещё?")
    return [
        f"HTTP-сервер хуков не поднялся на порту {HTTP_SERVER_PORT} за 3 с. "
        "Скорее всего порт занят посторонним процессом. Пока это так, "
        "webview остаётся без кнопок Cache и ByPass, пинга 📡 и "
        "детектора locale-drift."
    ]


def _stop_http_server(status: dict | None = None) -> None:
    """Останавливает сервер.

    Pid берём из ответа `/ping` — он авторитетнее PID-файла, который
    остаётся устаревшим после SIGKILL и вовсе пропадает, если прошлая
    остановка успела его удалить, не добив процесс.
    """
    if status is None:
        status = _http_server_status()

    pid = None
    if status:
        try:
            pid = int(status.get("pid") or 0) or None
        except (TypeError, ValueError):
            pid = None

    pid_file = _pid_file_path()
    if pid is None and os.path.isfile(pid_file):
        try:
            with open(pid_file) as f:
                pid = int(f.read().strip())
        except (OSError, ValueError):
            pid = None

    if pid is not None:
        _hlog(f"останавливаю сервер pid={pid}")
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as exc:
            _hlog(f"SIGTERM не прошёл: {exc}")
        # Ждём, пока порт освободится: следом почти всегда идёт запуск,
        # и без ожидания новый инстанс упрётся в занятый порт.
        for _ in range(20):
            if _http_server_status(timeout=0.15) is None:
                break
            time.sleep(0.1)

    try:
        os.remove(pid_file)
    except OSError:
        pass


def _read_hook_event_name() -> str:
    """Читает `hook_event_name` из stdin-JSON, который Claude Code
    передаёт хуку. Если stdin пустой/невалидный (например, скрипт
    запущен из терминала) — возвращает 'UserPromptSubmit' как наиболее
    видимое для пользователя событие.
    """
    fallback = "UserPromptSubmit"
    if sys.stdin.isatty():
        return fallback
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return fallback
        data = json.loads(raw)
        if isinstance(data, dict):
            return data.get("hook_event_name") or fallback
    except (json.JSONDecodeError, OSError):
        pass
    return fallback


def _emit_issues(config_issues: list[str], server_issues: list[str],
                 event_name: str) -> None:
    """Кладёт предупреждения в `additionalContext` под маркерами, по
    которым модель обязана сообщить пользователю.

    Оба вида уходят одним JSON: harness читает со stdout ровно один
    объект, и вторым print'ом мы бы сломали разбор.
    """
    if not config_issues and not server_issues:
        return
    body_lines = []
    if config_issues:
        body_lines.append(
            "[claude-custom-config WARNING] Проблемы в "
            "`.claude/patches/claude-custom-config.toml` — сообщи пользователю:"
        )
        for issue in config_issues:
            body_lines.append(f"- {issue}")
    if server_issues:
        if body_lines:
            body_lines.append("")
        body_lines.append(
            "[http-server WARNING] Локальный сервер хуков — сообщи пользователю:"
        )
        for issue in server_issues:
            body_lines.append(f"- {issue}")
    output = {
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "additionalContext": "\n".join(body_lines),
        }
    }
    try:
        print(json.dumps(output, ensure_ascii=False))
    except OSError:
        pass


def main() -> int:
    event_name = _read_hook_event_name()

    js_canonical = _read(CANONICAL_JS) if os.path.isfile(CANONICAL_JS) else ""
    config, config_issues = _read_config()
    config = _apply_vscode_settings(config)
    bootstrap = _build_bootstrap(js_canonical, config)


    for ext_dir in glob.glob(EXT_GLOB):
        webview_dir = os.path.join(ext_dir, "webview")
        if not os.path.isdir(webview_dir):
            continue

        # 1. webview/claude-custom.css — симлинк на канонический CSS.
        #    Тогда любой fetch JS-наблюдателем читает актуальный файл
        #    напрямую (без задержки до следующего UserPromptSubmit).
        if os.path.isfile(CANONICAL_CSS):
            _make_symlink(
                CANONICAL_CSS, os.path.join(webview_dir, CUSTOM_CSS_NAME)
            )

        # 2. Вставляем/обновляем bootstrap-блок в index.js
        index_js = os.path.join(webview_dir, "index.js")
        if os.path.isfile(index_js):
            _upsert_marker_block(index_js, bootstrap)

        # 3. Удаляем устаревший claude-connectivity.css (пинг теперь через JS)
        stale_conn = os.path.join(webview_dir, "claude-connectivity.css")
        if os.path.isfile(stale_conn):
            try:
                os.remove(stale_conn)
            except OSError:
                pass

    # 4. HTTP-сервер: поднимаем на SessionStart и UserPromptSubmit,
    #    гасим на SessionEnd. _ensure_http_server сам решает, нужен ли
    #    перезапуск (сервер жив, но поднят со старых исходников) и
    #    возвращает проблемы, если поднять не удалось.
    server_issues: list[str] = []
    if event_name in ("SessionStart", "UserPromptSubmit"):
        server_issues = _ensure_http_server()
    elif event_name == "SessionEnd":
        # Сервер НЕ гасим. Он один на проект и общий для всех окон,
        # а SessionEnd прилетает от каждого закрытия и от каждого
        # Reload Window — раньше это роняло сервер у всех остальных,
        # и до ближайшего сообщения webview получал «недоступен»
        # (видно в журнале, фаза 21.18). Простаивающий локальный
        # сервер на 127.0.0.1 ничего не стоит, а при смене исходников
        # он перезапускается сам.
        _hlog("SessionEnd — сервер оставляю работать (он общий для окон)")

    # 5. Предупреждения уходят последними и одним объектом: модель
    #    увидит маркеры `[claude-custom-config WARNING]` и
    #    `[http-server WARNING]` и сообщит пользователю в чате.
    _emit_issues(config_issues, server_issues, event_name)

    return 0


if __name__ == "__main__":
    sys.exit(main())
