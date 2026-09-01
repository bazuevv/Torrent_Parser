#!/usr/bin/env python3
"""Локальный HTTP-сервер для связи webview JS → файловая система.

Запускается хуком patch-claude-webview.py на SessionStart.
Убивается хуком на SessionEnd (через PID-файл).

Endpoints:
  POST /webview-error — тело = JSON с исключением webview, дописывает строку в
                        .claude/hooks-runtime/webview-errors.log (свои ошибки страницы
                        иначе нигде не видны: в логи VSCode они не попадают)
  POST /save-log      — тело = текст лога, сохраняет в .claude/hooks-runtime/debug-log-<ts>.txt
  POST /locale-drift  — тело = JSON {items: [{section,label,title}, ...]}, перезаписывает
                        .claude/hooks-runtime/locales-drift-pending.json (последний снимок DOM
                        меню /, который JS-локализатор отправляет при появлении меню)
  POST /models-list   — тело = JSON {models: [{value,displayName,description,isActive}, ...]},
                        перезаписывает .claude/hooks-runtime/models-list.json (каталог моделей
                        из popup селектора, для построения внешнего переключателя моделей)
  GET  /ping          — возвращает {"status": "ok"} (alive-check)
  GET  /accounts      — список settings-файлов ~/.claude/settings*.json как
                        аккаунтов провайдеров (кнопка Accs в футере)
  POST /accounts      — тело = JSON {file: "settings_glm.json"}, делает этот
                        файл активным ~/.claude/settings.json
  POST /restart-exthost — кладёт заявку на перезапуск extension host
                        (.claude/hooks-runtime/restart-exthost-request.json);
                        сам перезапуск делает блок, инжектированный
                        patch-extension-csp.py в extension.js
  GET  /restart-exthost — приняло ли расширение последнюю заявку
                        ({token, acked}); ложь означает, что инжекция
                        не работает и смена аккаунта не применится
  GET  /list-projects — возвращает список папок проектов в ~/.claude/projects/ с числом сессий
                        и декодированным путём
  POST /move-session  — тело = JSON {session_id, source_project, target_project},
                        перемещает .jsonl сессии между папками проектов
  POST /create-project— тело = JSON {path}, создаёт папку для проекта в ~/.claude/projects/
                        с кодированием пути (не-alphanum → '-')
"""

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import unquote

# Разбор транскрипта — единственная тяжёлая операция сервера (первый
# проход по файлу в десятки мегабайт занимает около секунды). Лочим её,
# чтобы параллельные запросы не гонялись за файлом состояния.
_CACHE_LOCK = threading.Lock()

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    tomllib = None

# Разобранный claude-custom-config.toml, обновляется по mtime.
_CONFIG_CACHE = {"mtime": 0.0, "data": {}}
_CONFIG_LOCK = threading.Lock()

# Параметры, которые подтягиваются на лету и перезапуска НЕ требуют:
# первые три обновляет applyLiveConfig() в claude-custom.js через
# /custom-config, serverLog* перечитывает hook_log на каждой записи,
# serverConfigWatchSec читает сам наблюдатель на каждом цикле.
# Список обязан совпадать с тем, что там реально обновляется, иначе
# сервер будет либо зря перезапускаться, либо не перезапускаться,
# когда надо.
HOT_KEYS = frozenset({
    "cacheKeepaliveMinutes",
    "cacheKeepaliveMessage",
    "cacheKeepaliveMinContext",
    "cacheKeepaliveTtlMinutes",
    "serverLog",
    "serverLogMaxBytes",
    "serverConfigWatchSec",
})

DEFAULT_CONFIG_WATCH_SEC = 10

# Выставляется наблюдателем; main() после выхода из serve_forever()
# смотрит на него и решает, делать exec или просто завершиться.
_RESTART_REQUESTED = False

# cache_usage лежит рядом; при запуске скриптом sys.path[0] — эта папка,
# но вставляем явно, чтобы импорт не зависел от способа запуска.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cache_usage  # noqa: E402
import hook_log  # noqa: E402
import account_switcher  # noqa: E402


def _log(message: str) -> None:
    hook_log.log("server", message)

PORT = int(os.environ.get("CLAUDE_HTTP_PORT", "18923"))

# Время правки исходников на момент старта. Отдаётся в /ping, чтобы
# patch-claude-webview.py мог отличить «сервер жив» от «сервер жив, но
# поднят со старой версии кода». Без этого правки в файлах ниже молча
# не подхватываются до ручного убийства процесса.
#
# ВАЖНО: добавил серверу новый локальный импорт — впиши его сюда,
# иначе изменения в нём не будут поводом для перезапуска. Тот же список
# продублирован в _server_sources_mtime() внутри patch-claude-webview.py;
# они обязаны совпадать. Конфиг тоже здесь: правка serverLog должна
# доезжать до сервера без ручного перезапуска.
SOURCE_FILES = ("http-server.py", "cache_usage.py", "hook_log.py",
                "account_switcher.py")
CONFIG_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "patches", "claude-custom-config.toml",
)


def _sources_mtime() -> float:
    """Только код. Конфиг сюда НЕ входит намеренно: все, кому он нужен,
    перечитывают его на лету — hook_log на каждую запись, endpoint
    /custom-config по mtime. Держать конфиг в этом списке значило бы
    перезапускать сервер на каждую правку настройки, а каждый перезапуск
    — это окно, в котором webview получает «недоступен»."""
    here = os.path.dirname(os.path.abspath(__file__))
    newest = 0.0
    for name in SOURCE_FILES:
        try:
            newest = max(newest, os.path.getmtime(os.path.join(here, name)))
        except OSError:
            pass
    return newest


SCRIPT_MTIME = _sources_mtime()
PROJECT_DIR = os.environ.get("CLAUDE_PROJECT_DIR", "")
LOGS_DIR = os.path.join(PROJECT_DIR, ".claude", "hooks-runtime") if PROJECT_DIR else ""

# Корневая папка проектов Claude Code (где хранятся .jsonl сессии чатов).
CLAUDE_PROJECTS_DIR = os.path.expanduser("~/.claude/projects")

# Лимит на размер тела POST — защита от случайного DoS
MAX_BODY_BYTES = 1 * 1024 * 1024  # 1 MB

LOCALE_DRIFT_FILE = (
    os.path.join(LOGS_DIR, "locales-drift-pending.json") if LOGS_DIR else ""
)
MODELS_LIST_FILE = (
    os.path.join(LOGS_DIR, "models-list.json") if LOGS_DIR else ""
)

# Заявка на перезапуск extension host и подтверждение её приёма.
#
# Смена аккаунта провайдера подменяет ~/.claude/settings.json, но `env`
# оттуда применяет к себе CLI-процесс `claude` при старте, а стартует он
# один раз на активацию extension host (в логе расширения за 11 дней
# ровно 5 строк «Spawn-env probe captured» — по числу активаций, не по
# числу диалогов). Значит, чтобы смена подействовала, нужен новый
# процесс CLI, то есть перезапуск хоста.
#
# Webview вызвать команду VSCode не может, поэтому связь идёт через файл:
# сюда пишет заявку этот сервер, а читает её блок, который
# patch-extension-csp.py инжектит в extension.js. Он же на активации
# запоминает токен как базовый — новый токен означает «перезапустись»,
# а тот же самый (после перезапуска) — «уже сделано», иначе получился бы
# бесконечный цикл.
#
# Путь заявки привязан к проекту (LOGS_DIR), и инжектированный блок
# выводит его из корня воркспейса своего окна. Поэтому область действия
# сама собой оказывается правильной: чужие проекты заявку не видят,
# а окна с этим же проектом перезапустятся каждое по одному разу.
RESTART_REQUEST_FILE = (
    os.path.join(LOGS_DIR, "restart-exthost-request.json") if LOGS_DIR else ""
)
RESTART_ACK_FILE = (
    os.path.join(LOGS_DIR, "restart-exthost-ack.json") if LOGS_DIR else ""
)

# UUID4-формат для имени файла сессии — защита от directory traversal
# при перемещении файлов между папками.
SESSION_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

# Имя папки проекта в ~/.claude/projects/. Claude Code кодирует
# абсолютный путь проекта, заменяя каждый не [a-zA-Z0-9] символ на '-'.
# Примеры:
#   /home/vladimir → -home-vladimir
#   /home/vladimir/Документы/Projects/Flying_Player
#     → -home-vladimir-----------Projects-Flying-Player
PROJECT_DIR_NAME_RE = re.compile(r"^-[A-Za-z0-9-]+$")


def encode_project_path(abs_path: str) -> str:
    """Кодирует абсолютный путь проекта в имя папки ~/.claude/projects/.
    Каждый символ, не входящий в [a-zA-Z0-9], заменяется на '-'.
    """
    return re.sub(r"[^A-Za-z0-9]", "-", abs_path)


def _decode_for_display(encoded_name: str) -> str:
    """Fallback-отображение, когда настоящий путь проекта неизвестен
    (например, в папке нет ни одной сессии с правильным cwd).
    Заменяет ведущий '-' на '/' — даёт хотя бы 1:1 соответствие
    имени папки и строки в списке.
    """
    if encoded_name.startswith("-"):
        return "/" + encoded_name[1:]
    return encoded_name


CWD_MARKER_FILENAME = ".cwd"


def _real_path_from_cwd(encoded_name: str) -> str | None:
    """Возвращает реальный путь проекта для папки encoded_name.

    Источники в порядке приоритета:
      1. Файл `.cwd` в папке проекта (создаётся POST /create-project и
         любым переносом — это явный «маркер» пути, чтобы пустые папки
         тоже знали свой путь).
      2. `cwd` из любой .jsonl-сессии папки, при условии что
         `encode_project_path(cwd) == encoded_name` (защита от
         перенесённых сессий, чей старый cwd принадлежит другому
         проекту).

    Возвращает None, если ни один источник не дал валидный путь.
    """
    project_dir = os.path.join(CLAUDE_PROJECTS_DIR, encoded_name)
    if not os.path.isdir(project_dir):
        return None

    # 1. Маркер-файл .cwd
    marker = os.path.join(project_dir, CWD_MARKER_FILENAME)
    if os.path.isfile(marker):
        try:
            with open(marker, "r", encoding="utf-8") as f:
                value = f.read().strip()
            if (value and os.path.isabs(value)
                    and encode_project_path(value) == encoded_name):
                return value
        except OSError:
            pass

    # 2. cwd из любой .jsonl с совпадающим энкодингом
    try:
        files = [f for f in os.listdir(project_dir) if f.endswith(".jsonl")]
    except OSError:
        return None
    MAX_LINES_TO_SCAN = 20
    for fname in files:
        path = os.path.join(project_dir, fname)
        try:
            with open(path, "r", encoding="utf-8") as f:
                for _ in range(MAX_LINES_TO_SCAN):
                    line = f.readline()
                    if not line:
                        break
                    if '"cwd"' not in line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    cwd = obj.get("cwd") if isinstance(obj, dict) else None
                    if (isinstance(cwd, str) and cwd
                            and encode_project_path(cwd) == encoded_name):
                        return cwd
        except OSError:
            continue
    return None


def _write_cwd_marker(project_dir: str, abs_path: str) -> None:
    """Создаёт/обновляет файл .cwd в папке проекта.
    Молча игнорирует ошибки записи — функция вспомогательная.
    """
    marker = os.path.join(project_dir, CWD_MARKER_FILENAME)
    try:
        with open(marker, "w", encoding="utf-8") as f:
            f.write(abs_path)
    except OSError:
        pass


def _rewrite_cwd_in_jsonl(path: str, new_cwd: str) -> int:
    """Перезаписывает в jsonl корневое поле `cwd` всех валидных JSON-строк
    на `new_cwd`. Возвращает число изменённых строк.

    Атомарно через tmp-файл + os.replace. Строки без поля cwd либо
    нераспарсиваемые — переносятся как есть.
    """
    tmp = path + ".rewrite.tmp"
    changed = 0
    try:
        with open(path, "r", encoding="utf-8") as src, \
                open(tmp, "w", encoding="utf-8") as dst:
            for line in src:
                if '"cwd"' in line:
                    try:
                        obj = json.loads(line)
                        if isinstance(obj, dict) and "cwd" in obj:
                            if obj.get("cwd") != new_cwd:
                                obj["cwd"] = new_cwd
                                line = (json.dumps(obj, ensure_ascii=False)
                                        + "\n")
                                changed += 1
                    except json.JSONDecodeError:
                        pass
                dst.write(line)
        os.replace(tmp, path)
    except OSError:
        if os.path.isfile(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise
    return changed


def _atomic_write_json(path: str, payload: dict) -> None:
    """Атомарная запись JSON: tmp-файл рядом + os.replace.
    Защищает от частично записанного файла, если процесс упадёт во время write.
    """
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


_WEBVIEW_PATCHER = None


def _load_webview_patcher():
    """Лениво импортирует patch-claude-webview.py как модуль.

    Через importlib, потому что дефис в имени файла не даёт написать
    обычный import. Модуль на верхнем уровне только объявляет
    константы и функции (вся работа — под `if __name__`), так что
    импорт безопасен. Результат кэшируется: endpoint дёргается
    периодически.
    """
    global _WEBVIEW_PATCHER
    if _WEBVIEW_PATCHER is None:
        import importlib.util

        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "patch-claude-webview.py")
        spec = importlib.util.spec_from_file_location("claude_webview_patcher", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _WEBVIEW_PATCHER = module
    return _WEBVIEW_PATCHER


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # тишина в stdout: сервер запущен с DEVNULL

    def log_error(self, format, *args):
        # Ошибки запросов (битый HTTP, обрыв соединения) — в журнал,
        # а не в никуда. Именно здесь всплывёт, если webview рвёт
        # соединения по таймауту.
        try:
            _log("ошибка запроса: " + (format % args))
        except Exception:
            pass

    def handle_one_request(self):
        try:
            BaseHTTPRequestHandler.handle_one_request(self)
        except (ConnectionResetError, BrokenPipeError) as exc:
            # Обычное дело, когда webview закрыл вкладку в середине
            # запроса. Пишем на всякий случай, но без паники.
            _log(f"соединение разорвано клиентом: {type(exc).__name__}")

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

    def do_GET(self):
        if self.path == "/ping":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "ok",
                "pid": os.getpid(),
                "script_mtime": SCRIPT_MTIME,
            }).encode())
            return

        if self.path == "/list-projects":
            self._handle_list_projects()
            return

        if self.path == "/vscode-settings":
            self._handle_vscode_settings()
            return

        if self.path.split("?", 1)[0] == "/cache-usage":
            self._handle_cache_usage()
            return

        if self.path.split("?", 1)[0] == "/bypass":
            self._handle_bypass_get()
            return

        if self.path.split("?", 1)[0] == "/custom-config":
            self._handle_custom_config()
            return

        if self.path.split("?", 1)[0] == "/accounts":
            self._handle_accounts_get()
            return

        if self.path.split("?", 1)[0] == "/account-env":
            self._handle_account_env_get()
            return

        if self.path.split("?", 1)[0] == "/restart-exthost":
            self._handle_restart_exthost_get()
            return

        self.send_response(404)
        self.end_headers()

    def _handle_custom_config(self) -> None:
        """Отдаёт claude-custom-config.toml как JSON.

        Зачем: значения конфига попадают в webview через bootstrap,
        а тот читается ровно один раз — при загрузке окна. Правка
        настройки не действовала до Reload Window, и это регулярно
        сбивало с толку (порог поддержания показывался старый, хотя
        в файле стоял новый). Тот же приём уже применён для
        emojiButtonPlacement, здесь он распространён на весь конфиг.

        Разбор кэшируется по mtime: endpoint опрашивают несколько окон
        раз в несколько секунд, парсить TOML каждый раз незачем.
        """
        try:
            mtime = os.path.getmtime(CONFIG_FILE)
        except OSError:
            self._json_response(404, {"ok": False, "error": "конфиг не найден"})
            return

        with _CONFIG_LOCK:
            if mtime != _CONFIG_CACHE["mtime"]:
                if tomllib is None:
                    self._json_response(500, {
                        "ok": False, "error": "нужен Python 3.11+ (tomllib)",
                    })
                    return
                try:
                    with open(CONFIG_FILE, "rb") as fh:
                        _CONFIG_CACHE["data"] = tomllib.load(fh)
                except Exception as exc:
                    # Битый TOML — не повод отдавать мусор: пусть webview
                    # продолжит жить на значениях из bootstrap.
                    self._json_response(500, {
                        "ok": False, "error": f"TOML невалиден: {exc}",
                    })
                    return
                _CONFIG_CACHE["mtime"] = mtime
                _log(f"конфиг перечитан (mtime={mtime:.3f})")
            data = dict(_CONFIG_CACHE["data"])

        self._json_response(200, {"ok": True, "mtime": mtime, "config": data})

    # --- переключение аккаунтов ------------------------------------------
    #
    # Логика подмены ~/.claude/settings.json живёт в account_switcher.py;
    # здесь только транспорт для кнопки Accs в футере.

    def _handle_accounts_get(self) -> None:
        try:
            accounts = account_switcher.list_accounts()
        except Exception as exc:  # noqa: BLE001 — endpoint не роняет сервер
            self._json_response(500, {"ok": False, "error": str(exc)})
            return
        self._json_response(200, {"ok": True, "accounts": accounts})

    def _handle_accounts_post(self) -> None:
        body = self._read_body()
        if body is None:
            return
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            self._json_response(400, {"ok": False, "error": "тело не JSON"})
            return
        if not isinstance(payload, dict):
            self._json_response(400, {"ok": False, "error": "ожидался объект"})
            return

        target = str(payload.get("file") or "")
        try:
            ok, message = account_switcher.switch_account(target)
        except Exception as exc:  # noqa: BLE001
            self._json_response(500, {"ok": False, "error": str(exc)})
            return

        _log(f"переключение аккаунта на {target!r}: {message}")
        self._json_response(200 if ok else 400, {
            "ok": ok,
            "message": message,
            "accounts": account_switcher.list_accounts(),
        })

    # --- правка настроек аккаунта ----------------------------------------
    #
    # Панель Accs умеет не только выбирать аккаунт, но и править его
    # файл — по шестерёнке в строке. Разделов два: `env` (провайдер) и
    # скалярные настройки верхнего уровня (`model`, `language`, …), без
    # которых у аккаунта Anthropic редактор был бы пуст.
    #
    # Значения ходят как есть, включая токены: сервер слушает только
    # localhost, а webview, который их запрашивает, живёт на той же
    # машине. В журнал они не попадают — там только имена.
    #
    # Имя endpoint'а осталось прежним (`/account-env`), хотя разделов
    # теперь два: его знает webview, загруженный до этой правки, а
    # bootstrap перечитывается только при Reload Window. Переименование
    # оставило бы такие окна с мёртвой шестерёнкой.

    def _handle_account_env_get(self) -> None:
        filename = ""
        if "?" in self.path:
            for part in self.path.split("?", 1)[1].split("&"):
                if part.startswith("file="):
                    filename = unquote(part[len("file="):])
        try:
            ok, message, config = account_switcher.read_account_config(filename)
        except Exception as exc:  # noqa: BLE001 — endpoint не роняет сервер
            self._json_response(500, {"ok": False, "error": str(exc)})
            return
        if not ok:
            self._json_response(400, {"ok": False, "error": message})
            return
        # `env` отдаётся тем же ключом, что и раньше: старый webview
        # читает только его и продолжает работать.
        self._json_response(200, {
            "ok": True,
            "file": filename,
            "env": config["env"],
            "settings": config["settings"],
            "hints": account_switcher.hints(),
        })

    def _handle_account_env_post(self) -> None:
        body = self._read_body()
        if body is None:
            return
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            self._json_response(400, {"ok": False, "error": "тело не JSON"})
            return
        if not isinstance(payload, dict):
            self._json_response(400, {"ok": False, "error": "ожидался объект"})
            return

        filename = str(payload.get("file") or "")
        # Отсутствующий раздел — это «не трогать», а не «очистить»:
        # старый webview шлёт одно `env`, и его правка не должна
        # стирать настройки верхнего уровня.
        env = payload.get("env")
        settings = payload.get("settings")
        try:
            ok, message = account_switcher.write_account_config(
                filename, env, settings)
        except Exception as exc:  # noqa: BLE001
            self._json_response(500, {"ok": False, "error": str(exc)})
            return

        names = ", ".join(sorted(
            list(env if isinstance(env, dict) else [])
            + list(settings if isinstance(settings, dict) else [])
        )) or "—"
        _log(f"правка настроек {filename!r}: {message} [{names}]")
        self._json_response(200 if ok else 400, {
            "ok": ok,
            "message": message,
            "accounts": account_switcher.list_accounts(),
        })

    # --- перезапуск extension host ---------------------------------------
    #
    # Транспорт для модального окна, которое панель Accs показывает после
    # смены аккаунта. Сам перезапуск делает не сервер: он лишь кладёт
    # заявку, а команду `workbench.action.restartExtensionHost` вызывает
    # блок, инжектированный в extension.js (см. RESTART_REQUEST_FILE).

    @staticmethod
    def _read_restart_token(path: str) -> str:
        """Токен из файла заявки/подтверждения ('' при любой проблеме).

        Оба файла пишем мы сами, но читать их приходится вперемешку с
        записью из другого процесса, поэтому битый или наполовину
        записанный JSON здесь — штатная ситуация, а не ошибка.
        """
        if not path:
            return ""
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return ""
        token = data.get("token") if isinstance(data, dict) else None
        return token if isinstance(token, str) else ""

    def _handle_restart_exthost_post(self) -> None:
        """Кладёт заявку на перезапуск extension host.

        Тело запроса может содержать `sessionId` — идентификатор
        диалога, из которого нажали «Перезапустить». Он нужен, чтобы
        после рестарта открыть заново ИМЕННО эту сессию: панель,
        созданную умершим хостом, оживить нельзя, а новый хост о ней
        уже ничего не знает. Своё имя знает только сам webview
        (`window.__claudeSessionId`), поэтому он его и передаёт.
        """
        if not LOGS_DIR:
            self._json_response(500, {
                "ok": False, "error": "CLAUDE_PROJECT_DIR не задан",
            })
            return

        session_id = ""
        body = self._read_body()
        if body:
            try:
                payload = json.loads(body.decode("utf-8"))
                if isinstance(payload, dict):
                    value = payload.get("sessionId")
                    if isinstance(value, str):
                        session_id = value
            except Exception:
                # Тело необязательное: без sessionId заявка остаётся
                # рабочей, просто вкладку переоткрыть будет нечем.
                pass

        token = uuid.uuid4().hex
        try:
            os.makedirs(LOGS_DIR, exist_ok=True)
            # Через временный файл: наблюдатель в extension.js читает
            # заявку по таймеру и может попасть ровно в момент записи.
            tmp = RESTART_REQUEST_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"token": token, "ts": time.time(),
                           "project": PROJECT_DIR,
                           "sessionId": session_id}, fh)
            os.replace(tmp, RESTART_REQUEST_FILE)
        except OSError as exc:
            self._json_response(500, {"ok": False, "error": str(exc)})
            return

        _log(f"заявка на перезапуск extension host: token={token} "
             f"session={session_id or '—'}")
        self._json_response(200, {"ok": True, "token": token})

    def _handle_restart_exthost_get(self) -> None:
        """Приняло ли расширение последнюю заявку.

        Нужен для честного сообщения об отказе: если инжекция в
        extension.js не применилась (например, после обновления
        расширения), заявка так и останется без подтверждения — и
        модальное окно скажет об этом вместо того, чтобы висеть
        в ожидании перезапуска, которого не будет.
        """
        requested = self._read_restart_token(RESTART_REQUEST_FILE)
        acked = self._read_restart_token(RESTART_ACK_FILE)
        self._json_response(200, {
            "ok": True,
            "token": requested,
            "acked": bool(requested) and requested == acked,
        })

    # --- bypass-режим ---------------------------------------------------
    #
    # Ровно тот же marker-файл, что ставит bypass-magic-word.py по слову
    # `да!`: .claude/hooks-runtime/<session_id> с меткой времени внутри.
    # PreToolUse хук bypass-check.sh проверяет только факт существования
    # файла, поэтому кнопка в футере и магическое слово — два входа
    # в один механизм, а не два независимых состояния.

    def _bypass_marker(self, session_id: str) -> str:
        base = LOGS_DIR or os.path.join(PROJECT_DIR, ".claude", "hooks-runtime")
        return os.path.join(base, session_id)

    def _bypass_session_arg(self, source: str) -> str | None:
        """Достаёт и валидирует session id. None — ответ уже отправлен."""
        if not PROJECT_DIR:
            self._json_response(500, {"ok": False, "error": "CLAUDE_PROJECT_DIR не задан"})
            return None
        if not source or not SESSION_ID_RE.match(source):
            self._json_response(400, {"ok": False, "error": "некорректный session id"})
            return None
        return source

    def _handle_bypass_get(self) -> None:
        requested = ""
        if "?" in self.path:
            for part in self.path.split("?", 1)[1].split("&"):
                if part.startswith("session="):
                    requested = part[len("session="):]
        session_id = self._bypass_session_arg(requested)
        if session_id is None:
            return

        marker = self._bypass_marker(session_id)
        since = None
        if os.path.isfile(marker):
            try:
                with open(marker, encoding="utf-8") as fh:
                    since = float(fh.read().strip() or 0) or None
            except (OSError, ValueError):
                since = None
        self._json_response(200, {
            "ok": True,
            "active": os.path.isfile(marker),
            "since": since,
        })

    def _handle_bypass_post(self) -> None:
        body = self._read_body()
        if body is None:
            return
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            self._json_response(400, {"ok": False, "error": "тело не JSON"})
            return
        if not isinstance(payload, dict):
            self._json_response(400, {"ok": False, "error": "ожидался объект"})
            return

        session_id = self._bypass_session_arg(str(payload.get("session") or ""))
        if session_id is None:
            return

        marker = self._bypass_marker(session_id)
        want = bool(payload.get("active"))
        try:
            if want:
                os.makedirs(os.path.dirname(marker), exist_ok=True)
                with open(marker, "w", encoding="utf-8") as fh:
                    fh.write(f"{time.time()}\n")
            elif os.path.isfile(marker):
                os.remove(marker)
        except OSError as exc:
            self._json_response(500, {"ok": False, "error": str(exc)})
            return

        self._json_response(200, {"ok": True, "active": os.path.isfile(marker)})

    def _handle_webview_error(self) -> None:
        """Складывает исключения из webview в один журнал.

        Своих ошибок webview нам не показывал вовсе: devtools открыты не
        всегда, а в логи VSCode исключения страницы не попадают. Из-за
        этого поломку 2026-09-01 («вкладки пустые, бандл цел, наш JS
        исполняется») нечем было даже локализовать — журнал знал только
        про install/init, но не про падения.

        Пишем строками в один файл, а не файлом на ошибку, как
        /save-log: ошибка обычно повторяется десятками, и каталог
        засорился бы мгновенно. Размер ограничен — журнал ведётся ради
        последнего инцидента, а не вечно.
        """
        raw = self._read_body()
        if raw is None:
            return
        if not LOGS_DIR:
            self._json_response(500, {"ok": False, "error": "LOGS_DIR не найден"})
            return
        try:
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception:
            payload = {"raw": raw.decode("utf-8", errors="replace")[:2000]}

        path = os.path.join(LOGS_DIR, "webview-errors.log")
        try:
            os.makedirs(LOGS_DIR, exist_ok=True)
            # Простая ротация: перед записью подрезаем хвостом.
            if os.path.isfile(path) and os.path.getsize(path) > 512 * 1024:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    tail = fh.readlines()[-500:]
                with open(path, "w", encoding="utf-8") as fh:
                    fh.writelines(tail)
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(f"{stamp} {json.dumps(payload, ensure_ascii=False)}\n")
        except OSError as exc:
            self._json_response(500, {"ok": False, "error": str(exc)})
            return

        _log(f"webview-error: {str(payload.get('message'))[:200]}")
        self._json_response(200, {"ok": True})

    def _handle_cache_usage(self) -> None:
        """Статистика prompt-кэша сессии для кнопки Cache в футере.

        Сессия определяется по самому свежему .jsonl в папке проекта
        (~/.claude/projects/<кодированный cwd>/) — webview собственного
        session_id не знает, а свежий файл почти всегда и есть текущая
        сессия. Явный ?session=<uuid> перекрывает автоопределение:
        нужно, чтобы посмотреть уже закрытую сессию.

        Разбор инкрементальный, состояние — в hooks-runtime, поэтому
        повторные нажатия кнопки почти бесплатны даже на транскрипте
        в десятки мегабайт.
        """
        if not PROJECT_DIR:
            self._json_response(500, {
                "ok": False, "error": "CLAUDE_PROJECT_DIR не задан",
            })
            return

        requested = ""
        if "?" in self.path:
            for part in self.path.split("?", 1)[1].split("&"):
                if part.startswith("session="):
                    requested = part[len("session="):]

        project_dir = os.path.join(
            CLAUDE_PROJECTS_DIR, encode_project_path(PROJECT_DIR)
        )
        if not os.path.isdir(project_dir):
            self._json_response(404, {
                "ok": False, "error": "папка проекта не найдена",
            })
            return

        if requested:
            # SESSION_ID_RE — защита от directory traversal в имени файла.
            if not SESSION_ID_RE.match(requested):
                self._json_response(400, {
                    "ok": False, "error": "некорректный session id",
                })
                return
            transcript = os.path.join(project_dir, requested + ".jsonl")
            if not os.path.isfile(transcript):
                self._json_response(404, {
                    "ok": False, "error": "сессия не найдена",
                })
                return
        else:
            transcript = ""
            newest = -1.0
            try:
                entries = os.listdir(project_dir)
            except OSError as exc:
                self._json_response(500, {
                    "ok": False, "error": f"listdir failed: {exc}",
                })
                return
            for entry in entries:
                if not entry.endswith(".jsonl"):
                    continue
                full = os.path.join(project_dir, entry)
                try:
                    mtime = os.path.getmtime(full)
                except OSError:
                    continue
                if mtime > newest:
                    newest, transcript = mtime, full
            if not transcript:
                self._json_response(404, {
                    "ok": False, "error": "в проекте нет сессий",
                })
                return

        # TTL нужен уже при разборе: по нему промахи делятся на
        # неизбежные («отошёл надолго») и ранние, где кэш обязан был
        # выжить. Он же уезжает в ответ — панель Usage показывает его
        # рядом с вердиктом хода, второй запрос ей не нужен.
        ttl = _config_value("cacheKeepaliveTtlMinutes", 60)
        if not (isinstance(ttl, int) and not isinstance(ttl, bool) and ttl > 0):
            ttl = 60

        state_dir = LOGS_DIR or os.path.join(PROJECT_DIR, ".claude", "hooks-runtime")
        try:
            with _CACHE_LOCK:
                stats = cache_usage.collect(
                    transcript, state_dir=state_dir, ttl_minutes=ttl,
                )
        except Exception as exc:
            self._json_response(500, {"ok": False, "error": str(exc)})
            return

        self._json_response(200 if stats.get("ok") else 404, stats)

    def _handle_vscode_settings(self) -> None:
        """Отдаёт webview'у актуальные значения настроек из VSCode
        Settings UI (сейчас — только emojiButtonPlacement).

        Зачем: те же значения приезжают в bootstrap при следующем
        UserPromptSubmit, но применяются лишь после Reload Window.
        Опрос этого endpoint'а позволяет claude-custom.js подхватывать
        смену настройки почти сразу, как это уже сделано для CSS.

        Логика чтения settings.json (JSONC + приоритет workspace над
        user) живёт в patch-claude-webview.py и импортируется отсюда,
        чтобы не держать две расходящиеся копии парсера.
        """
        try:
            settings = _load_webview_patcher()._apply_vscode_settings({})
        except Exception as e:  # noqa: BLE001 — endpoint не должен ронять сервер
            self._json_response(500, {"error": f"settings read failed: {e}"})
            return
        self._json_response(200, settings)

    def _handle_list_projects(self) -> None:
        """Возвращает список папок-проектов в ~/.claude/projects/.

        Для каждой папки: encoded_name, число .jsonl-сессий, размер на диске
        и попытка декодирования обратно в реальный путь (если такая папка
        существует и кодировка совпадает — путь считается «надёжным»).
        """
        if not os.path.isdir(CLAUDE_PROJECTS_DIR):
            self._json_response(200, {"projects": []})
            return

        projects = []
        try:
            entries = sorted(os.listdir(CLAUDE_PROJECTS_DIR))
        except OSError as e:
            self._json_response(500, {"error": f"listdir failed: {e}"})
            return

        for entry in entries:
            full = os.path.join(CLAUDE_PROJECTS_DIR, entry)
            if not os.path.isdir(full):
                continue

            session_count = 0
            last_mtime = 0
            try:
                for fname in os.listdir(full):
                    if fname.endswith(".jsonl"):
                        session_count += 1
                        try:
                            mtime = os.path.getmtime(os.path.join(full, fname))
                            if mtime > last_mtime:
                                last_mtime = mtime
                        except OSError:
                            pass
            except OSError:
                continue

            real_path = _real_path_from_cwd(entry)
            projects.append({
                "encoded_name": entry,
                "real_path": real_path,
                "display_name": real_path or _decode_for_display(entry),
                "session_count": session_count,
                "last_mtime": last_mtime,
            })

        # Сортируем по последней активности (более свежие — выше)
        projects.sort(key=lambda p: p["last_mtime"], reverse=True)
        self._json_response(200, {"projects": projects})

    def _read_body(self) -> bytes | None:
        """Читает тело POST с защитой от слишком больших payload'ов."""
        length = int(self.headers.get("Content-Length", 0))
        if length > MAX_BODY_BYTES:
            self.send_response(413)
            self._cors_headers()
            self.end_headers()
            return None
        return self.rfile.read(length)

    def _json_response(self, code: int, payload: dict) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self._cors_headers()
        self.end_headers()
        self.wfile.write(json.dumps(payload, ensure_ascii=False).encode())

    def do_POST(self):
        if self.path == "/bypass":
            self._handle_bypass_post()
            return

        if self.path == "/accounts":
            self._handle_accounts_post()
            return

        if self.path == "/account-env":
            self._handle_account_env_post()
            return

        if self.path == "/restart-exthost":
            self._handle_restart_exthost_post()
            return

        if self.path == "/webview-error":
            self._handle_webview_error()
            return

        if self.path == "/save-log":
            raw = self._read_body()
            if raw is None:
                return
            body = raw.decode("utf-8", errors="replace")

            if not LOGS_DIR:
                self._json_response(500, {"error": "LOGS_DIR not found"})
                return
            os.makedirs(LOGS_DIR, exist_ok=True)

            ts = time.strftime("%Y-%m-%d_%H-%M-%S")
            filename = f"debug-log-{ts}.txt"
            filepath = os.path.join(LOGS_DIR, filename)
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(body)

            self._json_response(200, {
                "status": "saved",
                "file": filename,
                "path": filepath,
            })
            return

        if self.path == "/locale-drift":
            raw = self._read_body()
            if raw is None:
                return
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                self._json_response(400, {"error": f"invalid JSON: {e}"})
                return

            items = data.get("items") if isinstance(data, dict) else None
            if not isinstance(items, list):
                self._json_response(400, {
                    "error": "expected body {\"items\": [{section,label,title}, ...], \"texts\": [str, ...]}",
                })
                return
            # texts — необязательное поле; если не передано, оставим пустой список
            raw_texts = data.get("texts") if isinstance(data, dict) else None
            if raw_texts is not None and not isinstance(raw_texts, list):
                raw_texts = None

            # Нормализуем items
            cleaned_items = []
            for it in items:
                if not isinstance(it, dict):
                    continue
                cleaned_items.append({
                    "section": str(it.get("section") or "").strip() or None,
                    "label": str(it.get("label") or "").strip(),
                    "title": str(it.get("title") or "").strip(),
                })

            # Нормализуем texts: только строки, страпленные, дедуп с сохранением порядка
            cleaned_texts = []
            seen_texts = set()
            if raw_texts:
                for t in raw_texts:
                    if not isinstance(t, str):
                        continue
                    s = t.strip()
                    if not s or s in seen_texts:
                        continue
                    seen_texts.add(s)
                    cleaned_texts.append(s)

            if not LOCALE_DRIFT_FILE:
                self._json_response(500, {"error": "LOCALE_DRIFT_FILE not configured"})
                return
            os.makedirs(os.path.dirname(LOCALE_DRIFT_FILE), exist_ok=True)

            payload = {
                "collected_at": time.strftime("%Y-%m-%dT%H:%M:%S%z") or
                                time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "items": cleaned_items,
                "texts": cleaned_texts,
            }
            _atomic_write_json(LOCALE_DRIFT_FILE, payload)

            self._json_response(200, {
                "status": "saved",
                "items_count": len(cleaned_items),
                "texts_count": len(cleaned_texts),
                "path": LOCALE_DRIFT_FILE,
            })
            return

        if self.path == "/models-list":
            raw = self._read_body()
            if raw is None:
                return
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                self._json_response(400, {"error": f"invalid JSON: {e}"})
                return

            models = data.get("models") if isinstance(data, dict) else None
            if not isinstance(models, list):
                self._json_response(400, {
                    "error": "expected body {\"models\": [{value,displayName,description,isActive}, ...]}",
                })
                return

            cleaned = []
            for m in models:
                if not isinstance(m, dict):
                    continue
                value = m.get("value")
                cleaned.append({
                    "value": (str(value).strip() if value else None) or None,
                    "displayName": str(m.get("displayName") or "").strip(),
                    "description": str(m.get("description") or "").strip(),
                    "isActive": bool(m.get("isActive")),
                })

            if not MODELS_LIST_FILE:
                self._json_response(500, {"error": "MODELS_LIST_FILE not configured"})
                return
            os.makedirs(os.path.dirname(MODELS_LIST_FILE), exist_ok=True)

            payload = {
                "collected_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "models": cleaned,
            }
            _atomic_write_json(MODELS_LIST_FILE, payload)

            self._json_response(200, {
                "status": "saved",
                "count": len(cleaned),
                "path": MODELS_LIST_FILE,
            })
            return

        if self.path == "/move-session":
            self._handle_move_session()
            return

        if self.path == "/copy-session":
            self._handle_copy_session()
            return

        if self.path == "/create-project":
            self._handle_create_project()
            return

        if self.path == "/diag":
            # Самодиагностика webview: JS-патч присылает {tag, data},
            # сервер аппендит в .claude/hooks-runtime/session-mover-diag.log
            # с таймстампом. Используется только для отладки.
            raw = self._read_body()
            if raw is None:
                return
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                self._json_response(400, {"error": f"invalid JSON: {e}"})
                return
            log_path = os.path.join(LOGS_DIR or "/tmp",
                                    "session-mover-diag.log")
            try:
                os.makedirs(os.path.dirname(log_path), exist_ok=True)
                line = "[" + time.strftime("%H:%M:%S") + "] " + \
                       json.dumps(data, ensure_ascii=False) + "\n"
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(line)
            except OSError as e:
                self._json_response(500, {"error": str(e)})
                return
            self._json_response(200, {"status": "ok"})
            return

        self.send_response(404)
        self._cors_headers()
        self.end_headers()

    def _find_session_owner(self, session_id: str) -> str | None:
        """Сканирует ~/.claude/projects/* и возвращает имя папки, в которой
        лежит <session_id>.jsonl. Если файлов с таким UUID несколько —
        возвращает самый свежий (по mtime). None если не найдено.
        """
        if not os.path.isdir(CLAUDE_PROJECTS_DIR):
            return None
        candidates: list[tuple[float, str]] = []
        try:
            entries = os.listdir(CLAUDE_PROJECTS_DIR)
        except OSError:
            return None
        for entry in entries:
            path = os.path.join(CLAUDE_PROJECTS_DIR, entry, session_id + ".jsonl")
            if os.path.isfile(path):
                try:
                    candidates.append((os.path.getmtime(path), entry))
                except OSError:
                    candidates.append((0, entry))
        if not candidates:
            return None
        candidates.sort(reverse=True)
        return candidates[0][1]

    def _handle_move_session(self) -> None:
        """Перемещает .jsonl-файл сессии между папками проектов.

        Body: {"session_id": "<uuid>", "target_project": "<encoded>",
               "source_project": "<encoded>"?}
        source_project опционален: если не указан — ищется во всех
        папках ~/.claude/projects/, чтобы UI не нужно было знать
        текущий проект webview.
        """
        raw = self._read_body()
        if raw is None:
            return
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            self._json_response(400, {"error": f"invalid JSON: {e}"})
            return

        if not isinstance(data, dict):
            self._json_response(400, {"error": "expected JSON object"})
            return

        session_id = data.get("session_id")
        source = data.get("source_project")
        target = data.get("target_project")

        # Валидация: session_id должен быть UUID; source/target — имена
        # вида '-...' с только [A-Za-z0-9-]. Это защита от directory
        # traversal (../../etc/passwd и т.п.).
        if not isinstance(session_id, str) or not SESSION_ID_RE.match(session_id):
            self._json_response(400, {"error": "invalid session_id (expected UUID)"})
            return
        if not isinstance(target, str) or not PROJECT_DIR_NAME_RE.match(target):
            self._json_response(400, {"error": "invalid target_project"})
            return

        # source_project: если указан — валидируем; если нет — auto-detect
        # сканированием всех папок ~/.claude/projects/.
        if source is None:
            source = self._find_session_owner(session_id)
            if source is None:
                self._json_response(404, {
                    "error": f"session {session_id} not found in any project",
                })
                return
        else:
            if not isinstance(source, str) or not PROJECT_DIR_NAME_RE.match(source):
                self._json_response(400, {"error": "invalid source_project"})
                return

        if source == target:
            self._json_response(400, {"error": "source and target are the same"})
            return

        src_dir = os.path.join(CLAUDE_PROJECTS_DIR, source)
        dst_dir = os.path.join(CLAUDE_PROJECTS_DIR, target)
        src_file = os.path.join(src_dir, session_id + ".jsonl")
        dst_file = os.path.join(dst_dir, session_id + ".jsonl")

        if not os.path.isfile(src_file):
            self._json_response(404, {"error": "source session file not found"})
            return
        if not os.path.isdir(dst_dir):
            self._json_response(404, {"error": "target project folder not found"})
            return
        if os.path.exists(dst_file):
            self._json_response(409, {
                "error": "session with same id already exists in target",
                "dst_file": dst_file,
            })
            return

        # Перемещаем не только .jsonl, но и одноимённую папку с артефактами
        # сессии (state.json, contents/...), если она существует.
        moved_extras = []
        src_extras = os.path.join(src_dir, session_id)
        dst_extras = os.path.join(dst_dir, session_id)
        try:
            shutil.move(src_file, dst_file)
            if os.path.isdir(src_extras) and not os.path.exists(dst_extras):
                shutil.move(src_extras, dst_extras)
                moved_extras.append(session_id + "/")
        except (OSError, shutil.Error) as e:
            self._json_response(500, {"error": f"move failed: {e}"})
            return

        # Перезаписываем `cwd` внутри jsonl на путь нового проекта.
        # Путь берём через _real_path_from_cwd(target) — он смотрит сначала
        # на маркер-файл .cwd, потом на cwd любой сессии целевой папки.
        # Это убирает необходимость передавать target_path с UI.
        cwd_changed = 0
        rewrite_skipped_reason = None
        target_real = _real_path_from_cwd(target)
        if target_real:
            try:
                cwd_changed = _rewrite_cwd_in_jsonl(dst_file, target_real)
            except OSError as e:
                rewrite_skipped_reason = f"rewrite failed: {e}"
            # Обновим маркер на всякий случай (если его не было).
            _write_cwd_marker(dst_dir, target_real)
        else:
            rewrite_skipped_reason = (
                "no .cwd marker and no native session in target project — "
                "create the project via POST /create-project first"
            )

        self._json_response(200, {
            "status": "moved",
            "session_id": session_id,
            "source_project": source,
            "target_project": target,
            "from": src_file,
            "to": dst_file,
            "moved_extras": moved_extras,
            "cwd_rewritten_lines": cwd_changed,
            "new_cwd": target_real,
            "rewrite_skipped_reason": rewrite_skipped_reason,
        })

    def _handle_copy_session(self) -> None:
        """Копирует .jsonl-файл сессии в другой проект.

        В отличие от /move-session:
          - исходный файл остаётся на месте,
          - копии генерируется НОВЫЙ session_id (UUID v4), чтобы избежать
            коллизий с оригиналом и активной сессией,
          - в каждой строке копии sessionId и cwd заменяются на новые,
          - попутная папка-артефакт <session_id>/ тоже копируется
            и переименовывается в <new_session_id>/.

        Body: {"session_id": "<uuid>", "target_project": "<encoded>",
               "source_project": "<encoded>"?}
        """
        raw = self._read_body()
        if raw is None:
            return
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            self._json_response(400, {"error": f"invalid JSON: {e}"})
            return
        if not isinstance(data, dict):
            self._json_response(400, {"error": "expected JSON object"})
            return

        session_id = data.get("session_id")
        source = data.get("source_project")
        target = data.get("target_project")

        if not isinstance(session_id, str) or not SESSION_ID_RE.match(session_id):
            self._json_response(400, {"error": "invalid session_id"})
            return
        if not isinstance(target, str) or not PROJECT_DIR_NAME_RE.match(target):
            self._json_response(400, {"error": "invalid target_project"})
            return

        if source is None:
            source = self._find_session_owner(session_id)
            if source is None:
                self._json_response(404, {
                    "error": f"session {session_id} not found in any project",
                })
                return
        else:
            if not isinstance(source, str) or not PROJECT_DIR_NAME_RE.match(source):
                self._json_response(400, {"error": "invalid source_project"})
                return

        src_dir = os.path.join(CLAUDE_PROJECTS_DIR, source)
        dst_dir = os.path.join(CLAUDE_PROJECTS_DIR, target)
        src_file = os.path.join(src_dir, session_id + ".jsonl")

        if not os.path.isfile(src_file):
            self._json_response(404, {"error": "source session file not found"})
            return
        if not os.path.isdir(dst_dir):
            self._json_response(404, {"error": "target project folder not found"})
            return

        new_session_id = str(uuid.uuid4())
        dst_file = os.path.join(dst_dir, new_session_id + ".jsonl")

        target_real = _real_path_from_cwd(target)
        rewrite_skipped_reason = None
        if not target_real:
            rewrite_skipped_reason = (
                "no .cwd marker and no native session in target project — "
                "create the project via POST /create-project first"
            )

        # Копируем построчно, заменяя sessionId (всегда) и cwd
        # (если target_real известен). cwd в полях типа Bash-команд
        # не трогаем — обновляем только корневое поле объекта.
        lines_total = 0
        sid_changed = 0
        cwd_changed = 0
        try:
            with open(src_file, "r", encoding="utf-8") as src, \
                    open(dst_file, "w", encoding="utf-8") as dst:
                for line in src:
                    lines_total += 1
                    try:
                        obj = json.loads(line)
                        if isinstance(obj, dict):
                            mod = False
                            if "sessionId" in obj and obj.get("sessionId") != new_session_id:
                                obj["sessionId"] = new_session_id
                                sid_changed += 1
                                mod = True
                            if (target_real and "cwd" in obj
                                    and obj.get("cwd") != target_real):
                                obj["cwd"] = target_real
                                cwd_changed += 1
                                mod = True
                            if mod:
                                line = (json.dumps(obj, ensure_ascii=False)
                                        + "\n")
                    except json.JSONDecodeError:
                        pass
                    dst.write(line)
        except OSError as e:
            if os.path.isfile(dst_file):
                try:
                    os.remove(dst_file)
                except OSError:
                    pass
            self._json_response(500, {"error": f"copy failed: {e}"})
            return

        # Папка-артефакт сессии: копируем и переименовываем в new_session_id
        copied_extras = []
        src_extras = os.path.join(src_dir, session_id)
        dst_extras = os.path.join(dst_dir, new_session_id)
        if os.path.isdir(src_extras) and not os.path.exists(dst_extras):
            try:
                shutil.copytree(src_extras, dst_extras)
                copied_extras.append(new_session_id + "/")
            except (OSError, shutil.Error) as e:
                rewrite_skipped_reason = (
                    (rewrite_skipped_reason + "; " if rewrite_skipped_reason else "")
                    + f"copytree extras failed: {e}"
                )

        if target_real:
            _write_cwd_marker(dst_dir, target_real)

        self._json_response(200, {
            "status": "copied",
            "source_session_id": session_id,
            "new_session_id": new_session_id,
            "source_project": source,
            "target_project": target,
            "from": src_file,
            "to": dst_file,
            "lines_total": lines_total,
            "sessionId_rewritten_lines": sid_changed,
            "cwd_rewritten_lines": cwd_changed,
            "new_cwd": target_real,
            "copied_extras": copied_extras,
            "rewrite_skipped_reason": rewrite_skipped_reason,
        })

    def _handle_create_project(self) -> None:
        """Создаёт папку для нового проекта в ~/.claude/projects/.

        Body: {"path": "/abs/path/to/project"}.
        Папка проекта на диске НЕ создаётся — это делает пользователь.
        Здесь создаётся только директория в ~/.claude/projects/, чтобы
        туда можно было перемещать сессии.
        """
        raw = self._read_body()
        if raw is None:
            return
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            self._json_response(400, {"error": f"invalid JSON: {e}"})
            return

        path = data.get("path") if isinstance(data, dict) else None
        if not isinstance(path, str) or not path.strip():
            self._json_response(400, {"error": "expected {\"path\": \"<abs path>\"}"})
            return
        path = path.strip()
        if not os.path.isabs(path):
            self._json_response(400, {"error": "path must be absolute"})
            return

        encoded = encode_project_path(path)
        # Доп. проверка: имя должно начинаться с '-' (т.е. path с '/')
        # и состоять только из разрешённых символов.
        if not PROJECT_DIR_NAME_RE.match(encoded):
            self._json_response(400, {
                "error": f"encoded name invalid: {encoded!r}",
            })
            return

        target_dir = os.path.join(CLAUDE_PROJECTS_DIR, encoded)
        already_existed = os.path.isdir(target_dir)
        try:
            os.makedirs(target_dir, exist_ok=True)
        except OSError as e:
            self._json_response(500, {"error": f"mkdir failed: {e}"})
            return

        # Сохраняем введённый пользователем путь как маркер. Это
        # единственный источник истины для пустых только что созданных
        # папок — без него _real_path_from_cwd не смог бы определить
        # реальный путь, и UI показывал бы fallback /encoded[1:].
        _write_cwd_marker(target_dir, path)

        self._json_response(200, {
            "status": "ok",
            "encoded_name": encoded,
            "target_dir": target_dir,
            "already_existed": already_existed,
        })


def _bootstrap_cwd_markers() -> None:
    """Создаёт .cwd-маркер для каждой папки в ~/.claude/projects/,
    у которой его ещё нет, но из cwd её сессий можно вычислить путь.

    Выполняется один раз при старте сервера. Делает UI-список проектов
    сразу содержащим читаемые real_path'ы, без необходимости отдельной
    миграции от пользователя.
    """
    if not os.path.isdir(CLAUDE_PROJECTS_DIR):
        return
    try:
        entries = os.listdir(CLAUDE_PROJECTS_DIR)
    except OSError:
        return
    for entry in entries:
        if not PROJECT_DIR_NAME_RE.match(entry):
            continue
        project_dir = os.path.join(CLAUDE_PROJECTS_DIR, entry)
        if not os.path.isdir(project_dir):
            continue
        if os.path.isfile(os.path.join(project_dir, CWD_MARKER_FILENAME)):
            continue
        real = _real_path_from_cwd(entry)  # fallback на jsonl-сканирование
        if real:
            _write_cwd_marker(project_dir, real)


def _config_value(key: str, default):
    """Значение параметра из конфига через тот же mtime-кэш, что и
    endpoint /custom-config — правка видна без перезапуска сервера."""
    try:
        mtime = os.path.getmtime(CONFIG_FILE)
    except OSError:
        return default
    with _CONFIG_LOCK:
        if mtime != _CONFIG_CACHE["mtime"]:
            if tomllib is None:
                return default
            try:
                with open(CONFIG_FILE, "rb") as fh:
                    _CONFIG_CACHE["data"] = tomllib.load(fh)
                _CONFIG_CACHE["mtime"] = mtime
            except Exception:
                return default
        return _CONFIG_CACHE["data"].get(key, default)


def _read_config_dict() -> dict:
    if tomllib is None:
        return {}
    try:
        with open(CONFIG_FILE, "rb") as fh:
            data = tomllib.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        # Битый или недописанный TOML (редактор сохраняет не атомарно) —
        # молча пропускаем цикл, иначе наблюдатель принял бы это за
        # «все параметры исчезли» и устроил перезапуск на ровном месте.
        return {}


def _cold_snapshot(cfg: dict) -> dict:
    """Только те параметры, чья правка требует перезапуска."""
    return {k: v for k, v in cfg.items() if k not in HOT_KEYS}


def _rebuild_bootstrap() -> None:
    """Просит патчер пересобрать bootstrap webview.

    Сам по себе перезапуск сервера webview-параметры не применяет — они
    живут в bootstrap. Поэтому перед рестартом дёргаем патчер как
    подпроцесс, ровно тем же способом, каким его запускает harness:
    вызывать его внутренности из чужого процесса было бы хрупко.
    """
    patcher = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "patch-claude-webview.py"
    )
    if not os.path.isfile(patcher):
        return
    payload = json.dumps({
        "hook_event_name": "SessionStart",
        "session_id": "config-watch",
        "cwd": PROJECT_DIR,
    })
    try:
        subprocess.run(
            [sys.executable, patcher],
            input=payload, text=True, capture_output=True, timeout=30,
            env={**os.environ, "CLAUDE_PROJECT_DIR": PROJECT_DIR},
        )
        _log("bootstrap пересобран")
    except Exception as exc:
        _log(f"пересборка bootstrap не удалась: {exc}")


def _watch_config(server) -> None:
    """Следит за «холодными» параметрами конфига и перезапускает сервер.

    Горячие параметры (HOT_KEYS) игнорируются: их правка применяется
    без перезапуска, а лишний рестарт — это окно, в котором webview
    получает «недоступен».
    """
    baseline = _cold_snapshot(_read_config_dict())
    global _RESTART_REQUESTED
    while True:
        cfg = _read_config_dict()
        delay = cfg.get("serverConfigWatchSec", DEFAULT_CONFIG_WATCH_SEC)
        if not isinstance(delay, int) or isinstance(delay, bool) or delay <= 0:
            # Наблюдение выключено. Не выходим из потока: параметр сам
            # горячий, и его можно вернуть, не перезапуская сервер.
            time.sleep(DEFAULT_CONFIG_WATCH_SEC)
            continue
        time.sleep(delay)

        current = _cold_snapshot(_read_config_dict())
        if not current or current == baseline:
            continue

        changed = sorted(
            k for k in set(current) | set(baseline)
            if current.get(k) != baseline.get(k)
        )
        _log("конфиг: изменились " + ", ".join(changed)
             + " — пересобираю bootstrap и перезапускаюсь")
        _rebuild_bootstrap()
        _RESTART_REQUESTED = True
        # shutdown() из этого же потока безопасен: мы не внутри
        # serve_forever(), в отличие от обработчика сигнала.
        server.shutdown()
        return


def main():
    if not PROJECT_DIR:
        print("CLAUDE_PROJECT_DIR not set", file=sys.stderr)
        sys.exit(1)

    _bootstrap_cwd_markers()

    runtime_dir = os.path.join(PROJECT_DIR, ".claude", "hooks-runtime")
    os.makedirs(runtime_dir, exist_ok=True)
    pid_file = os.path.join(runtime_dir, "http-server.pid")

    # ThreadingHTTPServer, а не HTTPServer: webview опрашивает сервер
    # несколькими таймерами (состояние ByPass и настройки — каждые 5 с,
    # поддержание кэша — раз в минуту), плюс клики по кнопкам. На
    # однопоточном сервере первый разбор большого транскрипта (~1 с)
    # блокировал очередь, и опросы отваливались по таймауту — в webview
    # это выглядело как «http-server.py недоступен».
    # Занятый порт — не авария, а нормальный исход гонки: два окна
    # прислали сообщение одновременно, оба не увидели сервера и оба
    # его запустили. Победитель уже слушает, проигравший должен тихо
    # уйти — и, главное, НЕ трогать pid-файл. Поэтому pid пишется
    # только после успешного bind.
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as exc:
        _log(f"порт {PORT} занят ({exc}) — уже есть живой сервер, выхожу")
        return

    with open(pid_file, "w") as f:
        f.write(str(os.getpid()))

    # Сигналы логируем ДО выхода: иначе в журнале остаётся только
    # обрыв записей, и не отличить внешний SIGTERM (кто-то погасил)
    # от падения. Номер сигнала сразу показывает, кто инициатор:
    # 15 — штатная остановка хуком, 2 — Ctrl+C, 9 в лог не попадёт
    # никогда (SIGKILL не перехватывается) — и это тоже улика.
    def _on_signal(signum, _frame):
        _log(f"получен сигнал {signum} ({signal.Signals(signum).name}) — останавливаюсь")
        # shutdown() блокируется до выхода serve_forever(), а обработчик
        # сигнала выполняется ВНУТРИ него, в главном потоке: прямой
        # вызов — гарантированный дедлок (проверено, сервер повисал
        # с занятым портом и переставал принимать соединения).
        # Документированный способ — звать shutdown() из другого потока.
        threading.Thread(target=server.shutdown, daemon=True).start()

    for _sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(_sig, _on_signal)
        except (ValueError, OSError):
            pass

    threading.Thread(target=_watch_config, args=(server,), daemon=True).start()

    _log(
        f"старт: порт {PORT}, project_dir={PROJECT_DIR}, "
        f"script_mtime={SCRIPT_MTIME:.3f}, python={sys.version.split()[0]}"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _log("KeyboardInterrupt")
    except BaseException as exc:
        _log(f"падение serve_forever: {type(exc).__name__}: {exc}")
        raise
    finally:
        _log("завершение: закрываю сокет и убираю pid-файл")
        server.server_close()
        if os.path.isfile(pid_file):
            os.remove(pid_file)

    if _RESTART_REQUESTED:
        # exec вместо spawn: pid сохраняется, а сокет уже закрыт (и всё
        # равно помечен CLOEXEC), так что новый процесс займёт порт без
        # гонки с самим собой.
        _log("перезапуск по изменению конфига: exec")
        os.execv(sys.executable, [sys.executable, os.path.abspath(__file__)])


if __name__ == "__main__":
    main()
