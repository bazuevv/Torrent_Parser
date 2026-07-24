import os
import re
import hmac
import time
import queue
import signal
import secrets
import threading
import collections
import subprocess
from datetime import datetime
from decimal import Decimal, InvalidOperation

import requests as _req

from urllib.parse import quote
from flask import (
    Flask, render_template, jsonify, Response, request, session,
    send_from_directory,
)

import db
import config
import qb

# Модуль рассылки TON (вкладка «Переводы»). db/config не тянут tonutils —
# импортируются безопасно; payout_core (с tonutils) грузится лениво в /api/ton/wallet-info.
from ton_payout import db as ton_db
from ton_payout import config as ton_config

app = Flask(__name__)
# Секрет для подписи cookie-сессий (нужен вкладке «Переводы»). Берётся из
# секретов ton_payout; если не задан — эфемерный (сессии сбросятся при рестарте).
app.secret_key = ton_config.SECRET_KEY or secrets.token_hex(32)

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
VENV_PYTHON = os.path.join(PROJECT_DIR, "venv", "bin", "python3")
VIDEOS_DIR = "/home/ubuntu/Videos"
VIDEOS_BT_DIR = "/home/ubuntu/Videos/Bittorrent"
VIDEOS_DL_DIR = "/home/ubuntu/Downloads"
# Каталог скачанного медиа (media_downloader.py сохраняет сюда под basename).
# Совпадает с DATA_DIR загрузчика (env MEDIA_DATA_DIR или data/ в корне проекта).
MEDIA_DATA_DIR = os.environ.get("MEDIA_DATA_DIR") or os.path.join(PROJECT_DIR, "data")

_allowed_ips_cache: set[str] = set()
_allowed_ips_ts: float = 0.0
_ALLOWED_IPS_TTL = 10.0  # seconds


def _get_allowed_ips() -> set[str]:
    global _allowed_ips_cache, _allowed_ips_ts
    import json as _json, time as _time
    now = _time.monotonic()
    if now - _allowed_ips_ts > _ALLOWED_IPS_TTL:
        try:
            raw = db.get_setting("allowed_ips", "[]")
            try:
                entries = _json.loads(raw)
                _allowed_ips_cache = {e["ip"].strip() for e in entries if e.get("on") and e.get("ip")}
            except (ValueError, KeyError):
                # fallback: old comma-separated format
                _allowed_ips_cache = {ip.strip() for ip in raw.replace("\n", ",").split(",") if ip.strip()}
        except Exception:
            pass
        _allowed_ips_ts = now
    return _allowed_ips_cache


def _invalidate_allowed_ips_cache() -> None:
    global _allowed_ips_ts
    _allowed_ips_ts = 0.0


def _client_ip() -> str:
    return request.headers.get("X-Real-IP") or request.remote_addr or ""


def _settings_allowed() -> bool:
    ip = _client_ip()
    if ip in _get_allowed_ips():
        return True
    return config.ALLOW_LOCALHOST and ip in ("127.0.0.1", "::1")

_process_lock = threading.Lock()
_process: subprocess.Popen | None = None

_sub_lock = threading.Lock()
_subscribers: list[queue.Queue] = []
# История хранит (seq, line); seq — монотонный id строки для SSE Last-Event-ID,
# чтобы при переподключении отдавать только новые строки (без дублей).
_log_history: collections.deque[tuple[int, str]] = collections.deque(maxlen=1000)
_log_seq = 0


def _broadcast(line: str) -> None:
    global _log_seq
    _log_seq += 1
    item = (_log_seq, line)
    _log_history.append(item)
    with _sub_lock:
        dead = []
        for q in _subscribers:
            try:
                q.put_nowait(item)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _subscribers.remove(q)


def _broadcast_live(line: str) -> None:
    """Отправляет сообщение только живым подписчикам, без сохранения в историю (seq=None)."""
    item = (None, line)
    with _sub_lock:
        dead = []
        for q in _subscribers:
            try:
                q.put_nowait(item)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _subscribers.remove(q)


def _reader_thread(proc: subprocess.Popen) -> None:
    for raw in proc.stdout:
        _broadcast(raw.rstrip("\n"))
    proc.wait()
    _broadcast_live(f"__END__:{proc.returncode}")


# ── API ──────────────────────────────────────────────────────────────────────

_PROFILE_CACHE: dict[str, tuple[float, dict]] = {}
_PROFILE_CACHE_TTL = 600  # 10 минут

_LG_SESSION = _req.Session()
_LG_SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
})


def fetch_girl_profile(username: str) -> dict | None:
    # 1. Проверяем in-memory кэш
    entry = _PROFILE_CACHE.get(username)
    if entry and (time.time() - entry[0]) < _PROFILE_CACHE_TTL:
        return entry[1]

    # 2. Проверяем БД
    conn = db.get_db_connection()
    db_data = db.get_girl_profile_db(conn, username)
    conn.close()
    if db_data is False:  # неактивный аккаунт — не парсить
        _PROFILE_CACHE[username] = (time.time(), None)
        return None
    if db_data is not None:
        _PROFILE_CACHE[username] = (time.time(), db_data)
        return db_data

    # 3. Парсим с leakgallery.com через Angular transfer state + API
    import leakgallery_media_parser as _lm

    url = f"https://leakgallery.com/{username}"
    try:
        resp = _LG_SESSION.get(url, timeout=20)
        if resp.status_code != 200:
            return None
        # Редирект на главную = аккаунт удалён/неактивен
        if resp.url.rstrip("/") == "https://leakgallery.com":
            try:
                conn = db.get_db_connection()
                db.mark_girl_inactive_db(conn, username)
                conn.close()
            except Exception:
                pass
            _PROFILE_CACHE[username] = (time.time(), None)
            return None
    except Exception:
        return None

    html = resp.text

    parsed = _lm.parse_transfer_states(html, username)
    banner_url   = parsed["banner_url"]
    avatar_url   = parsed["avatar_url"]
    media_count  = parsed["media_count"]
    media_items  = parsed["media_items"]
    views        = parsed["views"]
    subscribers  = parsed["subscribers"]
    token_str    = parsed["app_token"]
    medias_count = str(media_count) or None

    # Профиль не найден: leakgallery отдаёт 200 с содержимым главной (клиентский
    # редирект), блока профиля в transfer state нет. Помечаем аккаунт неактивным
    # только если страница реально загрузилась (есть APP_TOKEN) — иначе это,
    # вероятно, временный сбой, и профиль не трогаем (будет повторная попытка).
    if not parsed["profile_found"]:
        if token_str:
            try:
                conn = db.get_db_connection()
                db.mark_girl_inactive_db(conn, username)
                conn.close()
            except Exception:
                pass
            _PROFILE_CACHE[username] = (time.time(), None)
        return None

    # Подгружаем оставшиеся страницы через API
    if token_str and media_count > len(media_items):
        import math as _math
        _lm._token_cache = (token_str, time.monotonic() + 800)
        seen = {m["url"] for m in media_items}
        total_pages = _math.ceil(media_count / _lm.API_PAGE_SIZE)
        for page in range(2, total_pages + 1):
            page_data = _lm._api_get_page(username, page, token_str)
            if not page_data:
                break
            for item in page_data.get("medias", []):
                entry = _lm._media_item_from_api(item, username)
                if entry["url"] not in seen:
                    seen.add(entry["url"])
                    media_items.append(entry)
            time.sleep(0.3)

    data = {
        "username": username,
        "banner_url": banner_url,
        "avatar_url": avatar_url,
        "views": views,
        "medias_count": medias_count or str(len(media_items)),
        "subscribers": subscribers,
        "page_views": 0,
        "media_items": media_items,
    }

    # 4. Сохраняем в БД и кэш
    try:
        conn = db.get_db_connection()
        db.save_girl_profile_db(conn, username, banner_url, avatar_url, media_items,
                                lg_views=views, subscribers=subscribers)
        conn.close()
    except Exception:
        pass
    _PROFILE_CACHE[username] = (time.time(), data)
    return data


@app.get("/")
def index():
    return render_template("index.html", admin=_settings_allowed())


@app.get("/<username>")
def girl_profile(username: str):
    conn = db.get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT username, created_at, ton_address FROM girls WHERE username = %s LIMIT 1", (username,))
    girl = cursor.fetchone()
    cursor.close()
    conn.close()
    if not girl:
        return "Профиль не найден", 404
    profile = fetch_girl_profile(username)
    # Инкрементируем счётчик просмотров нашей страницы
    try:
        conn = db.get_db_connection()
        new_page_views = db.increment_page_views_db(conn, username)
        conn.close()
        if profile is not None:
            profile = dict(profile)
            profile["page_views"] = new_page_views
    except Exception:
        pass
    return render_template("girl_profile.html", girl=girl, profile=profile)


@app.get("/media/<path:filename>")
def serve_media(filename: str):
    """Отдаёт скачанное медиа из data/. send_from_directory защищает от
    path traversal и сам выставляет Content-Type по расширению."""
    return send_from_directory(MEDIA_DATA_DIR, filename, max_age=86400)


@app.template_global()
def media_src(value: str | None) -> str:
    """URL для media_items: http(s) — как есть (не скачано, remote CDN),
    иначе локальное имя файла — через маршрут отдачи /media/<файл>."""
    if not value:
        return ""
    if value.startswith("http://") or value.startswith("https://"):
        return value
    return "/media/" + quote(value)


@app.get("/api/status")
def api_status():
    with _process_lock:
        running = _process is not None and _process.poll() is None
        pid = _process.pid if running else None
    if not running:
        # Загрузчик мог пережить рестарт Flask / работать без _process — по PID-файлу
        dl_pid = _read_pid(_DL_PID_FILE)
        if dl_pid:
            running, pid = True, dl_pid
    return jsonify({"running": running, "pid": pid})


@app.post("/api/start")
def api_start():
    if not _settings_allowed():
        return jsonify({"ok": False, "error": "Forbidden"}), 403
    data = request.get_json(silent=True) or {}
    parser = data.get("parser", "main")
    scripts = {
        "main": "main.py",
        "leakgallery": "leakgallery_parser.py",
        "leakgallery_media": "leakgallery_media_parser.py",
        "media_downloader": "media_downloader.py",
    }
    script = scripts.get(parser)
    if not script:
        return jsonify({"ok": False, "error": "Неизвестный парсер"}), 400
    global _process
    with _process_lock:
        if _process is not None and _process.poll() is None:
            return jsonify({"ok": False, "error": "Парсер уже запущен"}), 400
        proc = subprocess.Popen(
            [VENV_PYTHON, "-u", script],
            cwd=PROJECT_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        _process = proc
    threading.Thread(target=_reader_thread, args=(proc,), daemon=True).start()
    return jsonify({"ok": True, "pid": proc.pid})


@app.post("/api/stop")
def api_stop():
    if not _settings_allowed():
        return jsonify({"ok": False, "error": "Forbidden"}), 403
    global _process
    with _process_lock:
        proc_alive = _process is not None and _process.poll() is None
        if proc_alive:
            _process.terminate()
    if proc_alive:
        return jsonify({"ok": True})
    # Загрузчик без _process (пережил рестарт Flask) — гасим по PID-файлу
    dl_pid = _read_pid(_DL_PID_FILE)
    if dl_pid:
        try:
            os.kill(dl_pid, signal.SIGTERM)
            return jsonify({"ok": True})
        except ProcessLookupError:
            pass
    return jsonify({"ok": False, "error": "Парсер не запущен"}), 400


@app.get("/api/stream")
def api_stream():
    if not _settings_allowed():
        return jsonify({"error": "Forbidden"}), 403
    # ?after=<seq> — клиент передаёт последний полученный id, чтобы при
    # переподключении не получать повторно всю историю (защита от дублей).
    try:
        after = int(request.args.get("after", 0))
    except (TypeError, ValueError):
        after = 0

    def generate():
        for seq, line in list(_log_history):
            if seq > after:
                yield f"id: {seq}\ndata: {line}\n\n"

        q: queue.Queue = queue.Queue(maxsize=500)
        with _sub_lock:
            _subscribers.append(q)
        try:
            while True:
                try:
                    seq, line = q.get(timeout=25)
                    if seq is not None:
                        yield f"id: {seq}\ndata: {line}\n\n"
                    else:
                        yield f"data: {line}\n\n"
                    if line.startswith("__END__:"):
                        break
                except queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            with _sub_lock:
                if q in _subscribers:
                    _subscribers.remove(q)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/videos")
def api_videos():
    page = max(1, int(request.args.get("page", 1)))
    per_page = 50
    quality = request.args.get("quality", "").strip()
    search = request.args.get("search", "").strip()

    try:
        conn = db.get_db_connection()
        cursor = conn.cursor(dictionary=True)

        where, params = [], []
        if quality == "__null__":
            where.append("quality IS NULL")
        elif quality:
            where.append("quality = %s")
            params.append(quality)
        if search:
            where.append("title LIKE %s")
            params.append(f"%{search}%")

        where_sql = ("WHERE " + " AND ".join(where)) if where else ""

        cursor.execute(f"SELECT COUNT(*) AS cnt FROM videos {where_sql}", params)
        total = cursor.fetchone()["cnt"]

        offset = (page - 1) * per_page
        cursor.execute(
            f"""SELECT topic_id, title, quality, magnet_link,
                       torrent_hash, torrent_name, created_at
                FROM videos {where_sql}
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s""",
            params + [per_page, offset],
        )
        rows = cursor.fetchall()
        cursor.close()
        conn.close()

        for row in rows:
            if isinstance(row["created_at"], datetime):
                row["created_at"] = row["created_at"].strftime("%Y-%m-%d %H:%M")

        return jsonify(
            {
                "total": total,
                "page": page,
                "per_page": per_page,
                "pages": max(1, (total + per_page - 1) // per_page),
                "items": rows,
            }
        )
    except Exception as exc:
        return jsonify({"error": str(exc), "total": 0, "page": 1, "pages": 1, "items": []}), 500


@app.get("/api/stats")
def api_stats():
    try:
        conn = db.get_db_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute("SELECT COUNT(*) AS total FROM videos")
        total = cursor.fetchone()["total"]

        cursor.execute(
            """SELECT COALESCE(quality, 'без метки') AS quality, COUNT(*) AS cnt
               FROM videos GROUP BY quality ORDER BY cnt DESC"""
        )
        by_quality = cursor.fetchall()

        cursor.execute("SELECT COUNT(*) AS total FROM girls")
        girls_total = cursor.fetchone()["total"]

        cursor.execute("SELECT COUNT(*) AS total FROM girls WHERE banner_url IS NULL AND avatar_url IS NULL")
        girls_no_media = cursor.fetchone()["total"]

        cursor.execute("SELECT COUNT(DISTINCT girl_id) AS total FROM girl_media")
        girls_with_media = cursor.fetchone()["total"]

        cursor.execute("SELECT COUNT(*) AS n FROM girl_media WHERE thumb LIKE 'http%' OR full_url LIKE 'http%'")
        media_pending = cursor.fetchone()["n"]

        cursor.close()
        conn.close()

        return jsonify({
            "total": total,
            "by_quality": by_quality,
            "girls_total": girls_total,
            "girls_no_media": girls_no_media,
            "girls_with_media": girls_with_media,
            "media_pending": media_pending,
        })
    except Exception as exc:
        return jsonify({"total": 0, "by_quality": [], "error": str(exc)})


@app.get("/api/girls")
def api_girls():
    page = max(1, int(request.args.get("page", 1)))
    per_page = 50
    search = request.args.get("search", "").strip()

    try:
        conn = db.get_db_connection()
        cursor = conn.cursor(dictionary=True)

        where, params = [], []
        if search:
            where.append("username LIKE %s")
            params.append(f"%{search}%")

        where_sql = ("WHERE " + " AND ".join(where)) if where else ""

        cursor.execute(f"SELECT COUNT(*) AS cnt FROM girls {where_sql}", params)
        total = cursor.fetchone()["cnt"]

        offset = (page - 1) * per_page
        cursor.execute(
            f"""SELECT username, created_at FROM girls {where_sql}
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s""",
            params + [per_page, offset],
        )
        rows = cursor.fetchall()
        cursor.close()
        conn.close()

        for row in rows:
            if isinstance(row["created_at"], datetime):
                row["created_at"] = row["created_at"].strftime("%Y-%m-%d %H:%M")

        return jsonify(
            {
                "total": total,
                "page": page,
                "per_page": per_page,
                "pages": max(1, (total + per_page - 1) // per_page),
                "items": rows,
            }
        )
    except Exception as exc:
        return jsonify({"error": str(exc), "total": 0, "page": 1, "pages": 1, "items": []}), 500


@app.post("/api/add-magnet")
def api_add_magnet():
    body = request.get_json(silent=True) or {}
    magnet = body.get("magnet", "").strip()
    topic_id = body.get("topic_id")
    if not magnet.startswith("magnet:"):
        return jsonify({"ok": False, "error": "Некорректная магнет-ссылка"}), 400

    ok, msg = qb.add_magnet(magnet)
    if not ok:
        return jsonify({"ok": False, "message": msg}), 502

    torrent_hash = qb.extract_hash(magnet)
    torrent_name = None

    if torrent_hash:
        # Пробуем получить имя из qBittorrent сразу (метаданные могут прийти быстро)
        info_map = qb.get_torrents_info([torrent_hash])
        t = info_map.get(torrent_hash, {})
        name = t.get("name", "")
        if name and name.lower() != torrent_hash:
            torrent_name = name

        if topic_id:
            try:
                conn = db.get_db_connection()
                db.update_torrent_info(conn, int(topic_id), torrent_hash, torrent_name)
                conn.close()
            except Exception:
                pass

    return jsonify({
        "ok": True,
        "message": msg,
        "torrent_hash": torrent_hash,
        "torrent_name": torrent_name,
    })


@app.get("/api/torrent-status")
def api_torrent_status():
    """Принимает ?hashes=h1,h2,... — возвращает статус и имя файла из qBittorrent.
    Попутно сохраняет torrent_name в БД для тех, у кого его ещё нет."""
    hashes_raw = request.args.get("hashes", "").strip()
    if not hashes_raw:
        return jsonify({})

    hashes = [h.strip().lower() for h in hashes_raw.split(",") if h.strip()]
    info_map = qb.get_torrents_info(hashes)

    result: dict = {}
    updates: list[tuple[str, str]] = []

    for h, t in info_map.items():
        name = t.get("name", "")
        if name.lower() == h:
            name = ""  # qBittorrent ещё не получил метаданные
        state = t.get("state", "")
        progress = t.get("progress", 0.0)
        complete = state in qb.COMPLETE_STATES and progress >= 1.0
        file_url = qb.file_url(t) if complete else None

        result[h] = {
            "name": name,
            "state": state,
            "progress": round(progress * 100),
            "complete": complete,
            "file_url": file_url,
        }
        if name:
            updates.append((h, name))

    # Обновляем имена в БД для торрентов, у которых они ещё не сохранены
    if updates:
        try:
            conn = db.get_db_connection()
            for h, name in updates:
                db.set_torrent_name(conn, h, name)
            conn.close()
        except Exception:
            pass

    return jsonify(result)


@app.post("/api/add-all-magnets")
def api_add_all_magnets():
    try:
        conn = db.get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT topic_id, magnet_link FROM videos WHERE torrent_hash IS NULL")
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

    if not rows:
        return jsonify({"ok": True, "added": 0, "total": 0, "message": "Нет новых файлов для загрузки"})

    # Отфильтровываем записи с невалидным BTIH-хэшем (битые ссылки с трекера)
    valid = [(r, h) for r in rows if (h := qb.extract_hash(r["magnet_link"]))]
    if not valid:
        return jsonify({"ok": True, "added": 0, "total": len(rows),
                        "message": "Все оставшиеся ссылки имеют некорректный формат"})

    magnets = [r["magnet_link"] for r, _ in valid]
    ok, msg = qb.add_magnets_batch(magnets)
    if not ok:
        return jsonify({"ok": False, "error": msg}), 502

    try:
        conn = db.get_db_connection()
        for r, h in valid:
            db.update_torrent_info(conn, r["topic_id"], h)
        conn.close()
    except Exception:
        pass

    return jsonify({"ok": True, "added": len(valid), "total": len(rows), "message": msg})


@app.get("/api/transcode-log")
def api_transcode_log():
    if not _settings_allowed():
        return jsonify({"error": "Forbidden", "total": 0, "page": 1, "pages": 1, "items": []}), 403
    page = max(1, int(request.args.get("page", 1)))
    per_page = 50
    try:
        conn = db.get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT COUNT(*) AS cnt FROM transcode_log")
        total = cursor.fetchone()["cnt"]
        offset = (page - 1) * per_page
        cursor.execute(
            """SELECT tl.id, tl.queued_at, tl.filename, tl.action, tl.src_height,
                      tl.src_size_mb, tl.dest_size_mb, tl.started_at, tl.finished_at,
                      tl.duration_sec, tl.status, v.topic_id
               FROM transcode_log tl
               LEFT JOIN videos v ON v.torrent_name = tl.filename
               ORDER BY tl.id DESC
               LIMIT %s OFFSET %s""",
            [per_page, offset],
        )
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        for row in rows:
            for col in ("queued_at", "finished_at"):
                if isinstance(row.get(col), datetime):
                    row[col] = row[col].strftime("%Y-%m-%d %H:%M")
            if isinstance(row.get("started_at"), datetime):
                row["started_at"] = row["started_at"].strftime("%Y-%m-%dT%H:%M:%S")
        return jsonify({
            "total": total, "page": page, "per_page": per_page,
            "pages": max(1, (total + per_page - 1) // per_page),
            "items": rows,
        })
    except Exception as exc:
        return jsonify({"error": str(exc), "total": 0, "page": 1, "pages": 1, "items": []}), 500


_VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm"}


def _make_video_resp(redirect: str, name: str) -> Response:
    resp = Response()
    resp.headers["X-Accel-Redirect"] = redirect
    resp.headers["Content-Type"] = "video/mp4"
    resp.headers["Content-Disposition"] = f"inline; filename*=UTF-8''{quote(os.path.basename(name))}"
    return resp


def _video_response(name: str):
    """Return X-Accel-Redirect response for a video filename.
    Checks processed dir, Bittorrent root, Bittorrent subdirs, then Downloads."""
    import glob as _glob
    # 1. Processed/transcoded directory
    if os.path.exists(os.path.join(VIDEOS_DIR, name)):
        return _make_video_resp(f"/videos-internal/{quote(name)}", name)
    # 2. Bittorrent root
    if os.path.exists(os.path.join(VIDEOS_BT_DIR, name)):
        return _make_video_resp(f"/videos-bt-internal/{quote(name)}", name)
    # 3. Bittorrent subdirectories (multi-file torrents)
    matches = _glob.glob(os.path.join(VIDEOS_BT_DIR, "**", _glob.escape(name)), recursive=True)
    if matches:
        rel = os.path.relpath(matches[0], VIDEOS_BT_DIR)
        return _make_video_resp(f"/videos-bt-internal/{quote(rel, safe='/')}", name)
    # 4. Downloads fallback
    if os.path.exists(os.path.join(VIDEOS_DL_DIR, name)):
        return _make_video_resp(f"/videos-dl-internal/{quote(name)}", name)
    return None


def _list_torrent_files(torrent_name: str) -> list[dict]:
    """Return list of video files for a torrent (handles both single-file and dir)."""
    import glob as _glob
    bt_path = os.path.join(VIDEOS_BT_DIR, torrent_name)
    done_path = os.path.join(VIDEOS_DIR, torrent_name)

    results = []
    if os.path.isdir(bt_path):
        for root, _, files in os.walk(bt_path):
            for f in sorted(files):
                if os.path.splitext(f)[1].lower() in _VIDEO_EXTS:
                    rel = os.path.relpath(os.path.join(root, f), VIDEOS_BT_DIR)
                    # Check if transcoded version exists
                    transcoded = os.path.exists(os.path.join(VIDEOS_DIR, f))
                    results.append({"name": f, "rel": rel, "transcoded": transcoded})
    elif os.path.isfile(bt_path) or os.path.isfile(done_path):
        transcoded = os.path.isfile(done_path)
        results.append({"name": torrent_name, "rel": torrent_name, "transcoded": transcoded})
    return results


@app.get("/api/torrent-files/<int:topic_id>")
def api_torrent_files(topic_id):
    """Returns list of video files for a torrent (single file or directory)."""
    try:
        conn = db.get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT torrent_name FROM videos WHERE topic_id = %s", (topic_id,))
        row = cursor.fetchone()
        cursor.close()
        conn.close()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    if not row or not row["torrent_name"]:
        return jsonify({"files": []}), 404

    files = _list_torrent_files(row["torrent_name"])
    return jsonify({"torrent_name": row["torrent_name"], "files": files})


@app.get("/api/serve-bt-file")
def api_serve_bt_file():
    """Serve a specific file by relative path within the Bittorrent directory."""
    relpath = request.args.get("path", "").strip("/")
    if not relpath or ".." in relpath:
        return "", 400
    name = os.path.basename(relpath)
    # Prefer transcoded version
    if os.path.exists(os.path.join(VIDEOS_DIR, name)):
        return _make_video_resp(f"/videos-internal/{quote(name)}", name)
    full = os.path.join(VIDEOS_BT_DIR, relpath)
    if os.path.isfile(full):
        return _make_video_resp(f"/videos-bt-internal/{quote(relpath, safe='/')}", name)
    return "", 404


@app.get("/api/video/<int:topic_id>")
def api_video(topic_id):
    try:
        conn = db.get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT torrent_name FROM videos WHERE topic_id = %s", (topic_id,))
        row = cursor.fetchone()
        cursor.close()
        conn.close()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    if not row or not row["torrent_name"]:
        return "", 404

    resp = _video_response(row["torrent_name"])
    return resp if resp is not None else ("", 404)


@app.get("/api/transcode-video/<int:log_id>")
def api_transcode_video(log_id):
    try:
        conn = db.get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT filename FROM transcode_log WHERE id = %s AND status = 'done'",
            (log_id,),
        )
        row = cursor.fetchone()
        cursor.close()
        conn.close()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    if not row:
        return "", 404

    resp = _video_response(row["filename"])
    return resp if resp is not None else ("", 404)


_TC_PID_FILE    = "/tmp/transcode_queue.pid"
_TC_FFMPEG_PID  = "/tmp/transcode_ffmpeg.pid"
_TC_PAUSE_FLAG  = "/tmp/transcode_paused"
_TC_PROGRESS    = "/tmp/ffmpeg_progress.txt"
_TC_CURRENT     = "/tmp/transcode_current.json"


def _read_pid(path: str) -> int | None:
    try:
        with open(path) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)   # check alive
        return pid
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
        return None


def _tc_state() -> str:
    if _read_pid(_TC_PID_FILE) is None:
        return "stopped"
    if os.path.exists(_TC_PAUSE_FLAG):
        return "paused"
    return "running"


@app.get("/api/transcode-progress")
def api_transcode_progress():
    if not _settings_allowed():
        return jsonify({"error": "Forbidden"}), 403

    import json as _json, time as _time

    current = {}
    try:
        with open(_TC_CURRENT) as f:
            current = _json.load(f)
    except (FileNotFoundError, ValueError):
        pass

    # parse ffmpeg -progress file
    prog = {}
    try:
        with open(_TC_PROGRESS) as f:
            for line in f:
                line = line.strip()
                if "=" in line:
                    k, _, v = line.partition("=")
                    prog[k.strip()] = v.strip()
    except FileNotFoundError:
        pass

    out_time_us = int(prog.get("out_time_us", 0) or 0)
    total_sec = current.get("src_duration_sec") or 0
    elapsed_sec = _time.time() - current.get("started_at", _time.time())

    percent = None
    eta_sec = None
    if total_sec and out_time_us:
        done_sec = out_time_us / 1_000_000
        percent = min(100.0, done_sec / total_sec * 100)
        speed_str = prog.get("speed", "").replace("x", "")
        try:
            speed = float(speed_str)
            if speed > 0:
                remaining_src = total_sec - done_sec
                eta_sec = int(remaining_src / speed)
        except (ValueError, TypeError):
            pass

    return jsonify({
        "current": current,
        "frame": prog.get("frame"),
        "fps": prog.get("fps"),
        "bitrate": prog.get("bitrate"),
        "speed": prog.get("speed"),
        "out_time": prog.get("out_time"),
        "out_time_us": out_time_us,
        "total_size": prog.get("total_size"),
        "percent": round(percent, 1) if percent is not None else None,
        "eta_sec": eta_sec,
        "elapsed_sec": int(elapsed_sec),
    })


@app.get("/api/transcode-status")
def api_transcode_status():
    if not _settings_allowed():
        return jsonify({"error": "Forbidden"}), 403
    state = _tc_state()
    queue_cnt = 0
    current = None
    try:
        conn = db.get_db_connection()
        c = conn.cursor(dictionary=True)
        c.execute("SELECT COUNT(*) AS n FROM transcode_log WHERE status='queued'")
        queue_cnt = c.fetchone()["n"]
        c.execute("SELECT filename FROM transcode_log WHERE status='running' LIMIT 1")
        row = c.fetchone()
        current = row["filename"] if row else None
        c.close()
        conn.close()
    except Exception:
        pass
    return jsonify({"state": state, "queue_remaining": queue_cnt, "current_file": current})


@app.post("/api/transcode-control")
def api_transcode_control():
    if not _settings_allowed():
        return jsonify({"ok": False, "error": "Forbidden"}), 403
    action = (request.get_json(silent=True) or {}).get("action", "")
    if action == "stop":
        pid = _read_pid(_TC_PID_FILE)
        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        # also kill ffmpeg directly in case SIGTERM didn't propagate fast enough
        fpid = _read_pid(_TC_FFMPEG_PID)
        if fpid:
            try:
                os.kill(fpid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            os.remove(_TC_PAUSE_FLAG)
        except FileNotFoundError:
            pass
        return jsonify({"ok": True})
    elif action == "pause":
        open(_TC_PAUSE_FLAG, "w").close()
        fpid = _read_pid(_TC_FFMPEG_PID)
        if fpid:
            try:
                os.kill(fpid, signal.SIGSTOP)
            except ProcessLookupError:
                pass
        return jsonify({"ok": True})
    elif action == "resume":
        fpid = _read_pid(_TC_FFMPEG_PID)
        if fpid:
            try:
                os.kill(fpid, signal.SIGCONT)
            except ProcessLookupError:
                pass
        try:
            os.remove(_TC_PAUSE_FLAG)
        except FileNotFoundError:
            pass
        return jsonify({"ok": True})
    elif action == "start":
        if _read_pid(_TC_PID_FILE) is not None:
            return jsonify({"ok": False, "error": "Уже запущено"})
        import subprocess as _sp
        proj = os.path.dirname(os.path.abspath(__file__))
        py = os.path.join(proj, "venv", "bin", "python3")
        script = os.path.join(proj, "process_queue.py")
        _sp.Popen([py, script], start_new_session=True,
                  stdout=open("/var/log/torrent_parser/transcode.log", "a"),
                  stderr=subprocess.STDOUT)
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": f"Unknown action: {action}"}), 400


_DL_PROGRESS_FILE = os.environ.get("MEDIA_PROGRESS_FILE", "/tmp/media_downloader_progress.json")
_DL_PID_FILE = os.environ.get("MEDIA_PID_FILE", "/tmp/media_downloader.pid")


@app.get("/api/download-progress")
def api_download_progress():
    """Активные закачки media_downloader: имя, тип, процент по каждому файлу.
    Прогресс показываем, пока процесс жив (по PID) либо файл свежий. По устареванию
    mtime не прячем — иначе на паузах в передаче (CDN тормозит) карточки мигали бы."""
    if not _settings_allowed():
        return jsonify({"error": "Forbidden", "files": []}), 403
    import json as _json, time as _time
    empty = {"files": [], "total_bytes": 0, "updated_at": 0}
    try:
        with open(_DL_PROGRESS_FILE) as f:
            data = _json.load(f)
    except (FileNotFoundError, ValueError):
        return jsonify(empty)
    alive = _read_pid(_DL_PID_FILE) is not None
    try:
        fresh = (_time.time() - os.path.getmtime(_DL_PROGRESS_FILE)) <= 8
    except OSError:
        fresh = False
    if not (alive or fresh):
        return jsonify(empty)
    return jsonify({
        "files": data.get("files", []),
        "total_bytes": data.get("total_bytes", 0),
        "updated_at": data.get("updated_at", 0),
    })


@app.get("/api/settings-access")
def api_settings_access():
    return jsonify({"allowed": _settings_allowed()})


@app.get("/api/settings")
def api_settings_get():
    if not _settings_allowed():
        return jsonify({"error": "Forbidden"}), 403
    rows = db.get_all_settings()
    return jsonify(rows)


@app.post("/api/settings")
def api_settings_post():
    if not _settings_allowed():
        return jsonify({"ok": False, "error": "Forbidden"}), 403
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"ok": False, "error": "Ожидается объект {key: value}"}), 400
    try:
        for key, value in body.items():
            db.set_setting(str(key), str(value))
        _invalidate_allowed_ips_cache()
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


# ── Вкладка «Переводы» (рассылка TON) ─────────────────────────────────────────
# Модель доступа:
#   1) быть admin-eligible: localhost ИЛИ Разрешённый IP (та же проверка
#      _settings_allowed(), что и у остального админ-функционала);
#   2) плюс войти по логину/паролю (WEB_USERNAME/WEB_PASSWORD из ton_payout) —
#      пароль спрашивается ВСЕГДА, в т.ч. с localhost.
# Изменяющие состояние POST-эндпоинты дополнительно требуют CSRF-токен
# (заголовок X-TON-CSRF), выдаваемый после входа в /api/ton/access.

def _ton_admin() -> bool:
    """Клиент на доверенной сети (localhost или Разрешённый IP)."""
    return _settings_allowed()


def _ton_authed() -> bool:
    return bool(session.get("ton_authed"))


def _ton_csrf() -> str:
    token = session.get("ton_csrf")
    if not token:
        token = secrets.token_hex(16)
        session["ton_csrf"] = token
    return token


def _ton_guard(require_csrf: bool = False):
    """Возвращает (ошибочный Response, код) либо None, если доступ разрешён."""
    if not _ton_admin():
        return jsonify({"ok": False, "error": "Forbidden"}), 403
    if not _ton_authed():
        return jsonify({"ok": False, "error": "Требуется вход", "need_auth": True}), 401
    if require_csrf:
        sent = request.headers.get("X-TON-CSRF", "")
        if not hmac.compare_digest(sent, session.get("ton_csrf", "")):
            return jsonify({"ok": False, "error": "Неверный CSRF-токен"}), 400
    return None


_ton_db_ready = False
_ton_db_lock = threading.Lock()


def _ton_ensure_db():
    """Лениво создаёт таблицы рассылки (init_db) при первом обращении к данным.
    Основное приложение init_db для ton_payout сам не вызывает."""
    global _ton_db_ready
    if _ton_db_ready:
        return None
    with _ton_db_lock:
        if not _ton_db_ready:
            try:
                ton_db.init_db()
                _ton_db_ready = True
            except Exception as e:
                return jsonify({"ok": False, "error": f"БД недоступна: {e}"}), 500
    return None


@app.before_request
def _ton_ensure_db_before():
    p = request.path
    if p.startswith("/api/ton/") and p not in (
        "/api/ton/access", "/api/ton/login", "/api/ton/logout", "/api/ton/wallet-info",
    ):
        # Инициализация нужна только тем, кто реально пройдёт guard — но безвредна
        # и для остальных (при отсутствии доступа сначала отработает _ton_guard).
        if _ton_admin() and _ton_authed():
            return _ton_ensure_db()
    return None


@app.get("/api/ton/access")
def api_ton_access():
    """Состояние доступа к вкладке для фронтенда."""
    admin = _ton_admin()
    authed = admin and _ton_authed()
    resp = {
        "admin": admin,
        "authed": authed,
        "username": ton_config.WEB_USERNAME if authed else None,
        "network": ton_config.TON_NETWORK,
        "password_configured": bool(ton_config.WEB_PASSWORD),
    }
    if authed:
        resp["csrf"] = _ton_csrf()
    return jsonify(resp)


@app.post("/api/ton/login")
def api_ton_login():
    if not _ton_admin():
        return jsonify({"ok": False, "error": "Forbidden"}), 403
    if not ton_config.WEB_PASSWORD:
        return jsonify({"ok": False, "error": "Пароль вкладки не настроен (WEB_PASSWORD)."}), 503
    body = request.get_json(silent=True) or {}
    username = str(body.get("username", ""))
    password = str(body.get("password", ""))
    ok_user = hmac.compare_digest(username, ton_config.WEB_USERNAME)
    ok_pass = hmac.compare_digest(password, ton_config.WEB_PASSWORD)
    if ok_user and ok_pass:
        session["ton_authed"] = True
        return jsonify({"ok": True, "csrf": _ton_csrf()})
    return jsonify({"ok": False, "error": "Неверный логин или пароль"}), 401


@app.post("/api/ton/logout")
def api_ton_logout():
    session.pop("ton_authed", None)
    session.pop("ton_csrf", None)
    return jsonify({"ok": True})


# ── Переводы: данные и действия ───────────────────────────────────────────────

def _ton_ser(d: dict) -> dict:
    """Сериализует строку БД для JSON: Decimal -> str, datetime -> ISO."""
    out = {}
    for k, v in d.items():
        if isinstance(v, Decimal):
            out[k] = str(v)
        elif hasattr(v, "isoformat"):
            out[k] = v.isoformat(sep=" ", timespec="seconds")
        else:
            out[k] = v
    return out


def _ton_parse_amount(raw) -> Decimal:
    try:
        amount = Decimal(str(raw).strip().replace(",", "."))
    except (InvalidOperation, AttributeError):
        raise ValueError(f"Некорректная сумма: {raw!r}")
    if not (Decimal(0) < amount < Decimal(1_000_000)):
        raise ValueError("Сумма вне допустимого диапазона (0 … 1 000 000).")
    if -amount.as_tuple().exponent > 9:
        raise ValueError("Слишком много знаков после запятой (максимум 9).")
    return amount


@app.get("/api/ton/recipients")
def api_ton_recipients():
    guard = _ton_guard()
    if guard:
        return guard
    rows = [_ton_ser(r) for r in ton_db.list_recipients()]
    active = [r for r in rows if int(r["is_active"])]
    total = sum((Decimal(r["amount"]) for r in active), Decimal(0))
    return jsonify({
        "ok": True,
        "recipients": rows,
        "active_count": len(active),
        "total_active": f"{total:.9f}".rstrip("0").rstrip("."),
        "payout_running": ton_db.has_running_payout(),
    })


@app.post("/api/ton/recipients/add")
def api_ton_recipient_add():
    guard = _ton_guard(require_csrf=True)
    if guard:
        return guard
    body = request.get_json(silent=True) or {}
    address = str(body.get("address", "")).strip()
    comment = str(body.get("comment", "")).strip()
    is_active = bool(body.get("is_active", True))
    if not address:
        return jsonify({"ok": False, "error": "Адрес не может быть пустым"}), 400
    try:
        amount = _ton_parse_amount(body.get("amount", ""))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    try:
        rid = ton_db.add_recipient(address, amount, comment or None, is_active)
        return jsonify({"ok": True, "id": rid})
    except Exception as e:  # напр. дубликат адреса (UNIQUE)
        return jsonify({"ok": False, "error": str(e)}), 400


@app.post("/api/ton/recipients/<int:rid>/edit")
def api_ton_recipient_edit(rid: int):
    guard = _ton_guard(require_csrf=True)
    if guard:
        return guard
    if not ton_db.get_recipient(rid):
        return jsonify({"ok": False, "error": "Получатель не найден"}), 404
    body = request.get_json(silent=True) or {}
    address = str(body.get("address", "")).strip()
    comment = str(body.get("comment", "")).strip()
    is_active = bool(body.get("is_active", True))
    if not address:
        return jsonify({"ok": False, "error": "Адрес не может быть пустым"}), 400
    try:
        amount = _ton_parse_amount(body.get("amount", ""))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    try:
        ton_db.update_recipient(rid, address, amount, comment or None, is_active)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.post("/api/ton/recipients/<int:rid>/toggle")
def api_ton_recipient_toggle(rid: int):
    guard = _ton_guard(require_csrf=True)
    if guard:
        return guard
    r = ton_db.get_recipient(rid)
    if not r:
        return jsonify({"ok": False, "error": "Получатель не найден"}), 404
    ton_db.set_recipient_active(rid, not int(r["is_active"]))
    return jsonify({"ok": True})


@app.post("/api/ton/recipients/<int:rid>/delete")
def api_ton_recipient_delete(rid: int):
    guard = _ton_guard(require_csrf=True)
    if guard:
        return guard
    if not ton_db.get_recipient(rid):
        return jsonify({"ok": False, "error": "Получатель не найден"}), 404
    ton_db.delete_recipient(rid)
    return jsonify({"ok": True})


@app.post("/api/ton/run")
def api_ton_run():
    guard = _ton_guard(require_csrf=True)
    if guard:
        return guard
    body = request.get_json(silent=True) or {}
    mode = str(body.get("mode", "highload"))
    dry_run = bool(body.get("dry_run", True))
    if mode not in ("sequential", "highload"):
        return jsonify({"ok": False, "error": "Неизвестный режим"}), 400
    if not dry_run and ton_db.has_running_payout():
        return jsonify({"ok": False, "error": "Рассылка уже выполняется"}), 409
    if not ton_db.list_recipients(active_only=True):
        return jsonify({"ok": False, "error": "Нет активных получателей"}), 400

    prev_max = 0
    latest = ton_db.list_runs(1)
    if latest:
        prev_max = latest[0]["id"]

    log_dir = os.path.join(PROJECT_DIR, "ton_payout", "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f"payout_{time.strftime('%Y%m%d_%H%M%S')}.log")
    cmd = [VENV_PYTHON, "-m", "ton_payout.run",
           "--mode", mode, "--triggered-by", "web"]
    if dry_run:
        cmd.append("--dry-run")
    try:
        logf = open(log_path, "w", encoding="utf-8")
        subprocess.Popen(cmd, cwd=PROJECT_DIR, stdout=logf,
                         stderr=subprocess.STDOUT, start_new_session=True)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Не удалось запустить: {e}"}), 500
    return jsonify({"ok": True, "after": prev_max, "dry_run": dry_run})


@app.get("/api/ton/runs")
def api_ton_runs():
    guard = _ton_guard()
    if guard:
        return guard
    return jsonify({"ok": True, "runs": [_ton_ser(r) for r in ton_db.list_runs(100)]})


@app.get("/api/ton/runs/latest")
def api_ton_runs_latest():
    guard = _ton_guard()
    if guard:
        return guard
    after = request.args.get("after", 0, type=int)
    latest = ton_db.list_runs(1)
    if latest and latest[0]["id"] > after:
        return jsonify({"ok": True, "ready": True, "run_id": latest[0]["id"]})
    return jsonify({"ok": True, "ready": False})


@app.get("/api/ton/runs/<int:run_id>")
def api_ton_run_detail(run_id: int):
    guard = _ton_guard()
    if guard:
        return guard
    run = ton_db.get_run(run_id)
    if not run:
        return jsonify({"ok": False, "error": "Запуск не найден"}), 404
    items = [_ton_ser(i) for i in ton_db.get_run_items(run_id)]
    return jsonify({
        "ok": True,
        "run": _ton_ser(run),
        "items": items,
        "finished": run["status"] != "running",
    })


@app.get("/api/ton/wallet-info")
def api_ton_wallet_info():
    guard = _ton_guard()
    if guard:
        return guard
    try:
        import asyncio
        info = asyncio.run(_ton_wallet_overview())
        return jsonify({"ok": True, **info})
    except Exception as e:  # tonutils не установлен / мнемоника не задана / сеть
        return jsonify({"ok": False, "error": str(e)})


async def _ton_wallet_overview() -> dict:
    from ton_core import NetworkGlobalID
    from tonutils.clients import ToncenterClient
    from tonutils.contracts import WalletHighloadV3R1, WalletV4R2

    from ton_payout.payout_core import NANO, _resolve_mnemonic

    mnemonic = _resolve_mnemonic()
    net_str = "mainnet" if ton_config.TON_NETWORK.lower() == "mainnet" else "testnet"
    network = NetworkGlobalID.MAINNET if net_str == "mainnet" else NetworkGlobalID.TESTNET
    client = ToncenterClient(network=network,
                             api_key=ton_config.TONCENTER_API_KEY or None, rps_limit=1)
    await client.connect()
    out = {"network": net_str, "wallets": {}}
    try:
        for mode, cls in (("sequential", WalletV4R2), ("highload", WalletHighloadV3R1)):
            wallet, _, _, _ = cls.from_mnemonic(client, mnemonic)
            await wallet.refresh()
            out["wallets"][mode] = {
                "address": wallet.address.to_str(is_bounceable=False),
                "balance": f"{Decimal(wallet.balance) / NANO:.4f}",
                "state": wallet.state.value,
            }
    finally:
        await client.close()
    return out


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
