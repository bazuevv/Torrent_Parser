#!/usr/bin/env python3
"""Локальный HTTP-сервер для связи webview JS → файловая система.

Запускается хуком patch-claude-webview.py на SessionStart.
Убивается хуком на SessionEnd (через PID-файл).

Endpoints:
  POST /save-log      — тело = текст лога, сохраняет в .claude/hooks-runtime/debug-log-<ts>.txt
  POST /locale-drift  — тело = JSON {items: [{section,label,title}, ...]}, перезаписывает
                        .claude/hooks-runtime/locales-drift-pending.json (последний снимок DOM
                        меню /, который JS-локализатор отправляет при появлении меню)
  POST /models-list   — тело = JSON {models: [{value,displayName,description,isActive}, ...]},
                        перезаписывает .claude/hooks-runtime/models-list.json (каталог моделей
                        из popup селектора, для построения внешнего переключателя моделей)
  GET  /ping          — возвращает {"status": "ok"} (alive-check)
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
import sys
import time
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler

PORT = int(os.environ.get("CLAUDE_HTTP_PORT", "18923"))
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


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # тишина в stdout

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
            self.wfile.write(json.dumps({"status": "ok"}).encode())
            return

        if self.path == "/list-projects":
            self._handle_list_projects()
            return

        self.send_response(404)
        self.end_headers()

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


def main():
    if not PROJECT_DIR:
        print("CLAUDE_PROJECT_DIR not set", file=sys.stderr)
        sys.exit(1)

    _bootstrap_cwd_markers()

    # Записываем PID для остановки на SessionEnd
    runtime_dir = os.path.join(PROJECT_DIR, ".claude", "hooks-runtime")
    os.makedirs(runtime_dir, exist_ok=True)
    pid_file = os.path.join(runtime_dir, "http-server.pid")
    with open(pid_file, "w") as f:
        f.write(str(os.getpid()))

    server = HTTPServer(("127.0.0.1", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if os.path.isfile(pid_file):
            os.remove(pid_file)


if __name__ == "__main__":
    main()
