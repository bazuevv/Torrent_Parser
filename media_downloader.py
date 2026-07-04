"""
Загрузчик медиа из таблицы girl_media.

Для каждой строки скачивает файлы по полям thumb и full_url в папку DATA_DIR
(по умолчанию — data/ в корне проекта) под их оригинальным именем (basename пути
URL), после чего заменяет соответствующее поле в БД на имя сохранённого файла.

Пример: ссылка
    https://cdn.leakgallery.com/content6/twithabigd/watermark_2549991b21e64601a3fa77f113d2b68f_twithabigd_52516260095.jpg
сохраняется как
    data/watermark_2549991b21e64601a3fa77f113d2b68f_twithabigd_52516260095.jpg
а в БД поле становится
    watermark_2549991b21e64601a3fa77f113d2b68f_twithabigd_52516260095.jpg

Повторный запуск безопасен: обрабатываются только строки, где thumb/full_url ещё
являются URL (LIKE 'http%'); уже скачанные файлы не перекачиваются.

Использование:
    venv/bin/python3 media_downloader.py
    MEDIA_DATA_DIR=/tmp/data venv/bin/python3 media_downloader.py   # другая папка
"""

import json
import logging
import os
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, unquote

import requests
from requests.adapters import HTTPAdapter

import db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

PROJECT_DIR   = os.path.dirname(os.path.abspath(__file__))
# По умолчанию — папка data/ в корне проекта; переопределяется env MEDIA_DATA_DIR.
DATA_DIR      = os.environ.get("MEDIA_DATA_DIR") or os.path.join(PROJECT_DIR, "data")
REQUEST_DELAY = 0.3            # секунды между скачиваниями
CHUNK_SIZE    = 1 << 16        # 64 KiB на чтение потока

PROGRESS_FILE = os.environ.get("MEDIA_PROGRESS_FILE", "/tmp/media_downloader_progress.json")
PID_FILE      = os.environ.get("MEDIA_PID_FILE", "/tmp/media_downloader.pid")
_VIDEO_EXTS   = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".ts"}

_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
})

# ── Прогресс активных закачек ─────────────────────────────────────────────────
# key -> {name, type, percent, downloaded_mb, total_mb}; публикуется в PROGRESS_FILE
# для веб-панели (web.py /api/download-progress его читает).
_progress: dict[str, dict] = {}
_progress_lock = threading.Lock()
_last_write = 0.0
_total_bytes = 0    # суммарно скачано байт за текущий прогон (для расчёта скорости)

# Счётчики текущего прогона (для сводки при остановке/завершении)
_files_ok = 0       # реально скачано
_files_exist = 0    # уже были в папке (пропущены)
_files_err = 0      # ошибки загрузки


def _media_type(name: str) -> str:
    return "video" if os.path.splitext(name)[1].lower() in _VIDEO_EXTS else "image"


def _write_progress_locked() -> None:
    """Атомарно дампит активные закачки в PROGRESS_FILE. Вызывать под _progress_lock."""
    global _last_write
    data = {"updated_at": time.time(), "total_bytes": _total_bytes,
            "files": list(_progress.values())}
    tmp = PROGRESS_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, PROGRESS_FILE)
    except OSError:
        pass
    _last_write = time.time()


def _update_progress(key: str, entry: dict | None, force: bool = False) -> None:
    """entry=None убирает закачку из активных. Запись троттлится (~3/с);
    удаление и force пишутся сразу."""
    with _progress_lock:
        if entry is None:
            if _progress.pop(key, None) is not None:
                _write_progress_locked()
        else:
            _progress[key] = entry
            if force or (time.time() - _last_write) > 0.3:
                _write_progress_locked()


def _add_bytes(n: int) -> None:
    """Накапливает суммарно скачанные байты (потокобезопасно)."""
    global _total_bytes
    with _progress_lock:
        _total_bytes += n


def _clear_progress() -> None:
    with _progress_lock:
        _progress.clear()
        _write_progress_locked()


def _write_pid() -> None:
    try:
        with open(PID_FILE, "w") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass


def _remove_pid() -> None:
    try:
        os.remove(PID_FILE)
    except FileNotFoundError:
        pass


def _sigterm_handler(signum, frame):
    """При остановке из панели (/api/stop → SIGTERM): сводка, чистка, выход."""
    logger.info("Остановлено. Загружено: %d | Уже были: %d | Ошибок при загрузке: %d",
                _files_ok, _files_exist, _files_err)
    _clear_progress()
    _remove_pid()
    os._exit(0)


def filename_from_url(url: str) -> str:
    """Оригинальное имя файла = basename пути URL (без query, декодированный).
    basename после unquote отсекает любые директории, поэтому обхода пути нет."""
    path = unquote(urlparse(url).path)
    return os.path.basename(path)


def _content_range_total(resp) -> int:
    """Полный размер файла из заголовка Content-Range (формат 'bytes start-end/total')."""
    cr = resp.headers.get("Content-Range", "")
    if "/" in cr:
        tail = cr.rsplit("/", 1)[-1].strip()
        if tail.isdigit():
            return int(tail)
    return 0


def download_file(url: str, dest_dir: str, progress_key: str | None = None) -> str | None:
    """Скачивает url в dest_dir под оригинальным именем. Возвращает имя файла
    или None при ошибке. Уже существующий непустой файл не перекачивается.
    Если задан progress_key — публикует прогресс закачки (имя, тип, %).

    Поддерживает докачку: недокачанные байты остаются в <файл>.part; при повторной
    попытке или запуске загрузка продолжается с этого места через HTTP Range
    (bytes=<уже_скачано>-). Если сервер Range не поддерживает — качает заново."""
    name = filename_from_url(url)
    if not name:
        logger.warning("Не удалось определить имя файла из URL: %s", url)
        return None

    dest = os.path.join(dest_dir, name)
    if os.path.isfile(dest) and os.path.getsize(dest) > 0:
        return name  # уже скачан ранее

    mtype = _media_type(name)
    tmp = dest + ".part"

    for attempt in range(1, 4):
        # сколько уже лежит в .part — с этого места докачиваем
        resume_from = os.path.getsize(tmp) if os.path.isfile(tmp) else 0
        headers = {"Range": f"bytes={resume_from}-"} if resume_from else {}
        try:
            with _SESSION.get(url, stream=True, timeout=60, headers=headers) as resp:
                # .part не меньше файла (докачивать нечего / битый) → начать заново
                if resp.status_code == 416:
                    try:
                        os.remove(tmp)
                    except FileNotFoundError:
                        pass
                    continue
                resp.raise_for_status()

                if resume_from and resp.status_code == 206:
                    mode = "ab"                       # сервер отдал хвост — дописываем
                    downloaded = resume_from
                    total = _content_range_total(resp) or (
                        resume_from + int(resp.headers.get("Content-Length", 0) or 0))
                    logger.info("Докачка %s с %.1f МБ", name, resume_from / 1048576)
                else:
                    mode = "wb"                       # 200: Range проигнорирован — с нуля
                    downloaded = 0
                    total = int(resp.headers.get("Content-Length", 0) or 0)

                total_mb = round(total / 1048576, 1) if total else None
                if progress_key:
                    _update_progress(progress_key, {
                        "name": name, "type": mtype,
                        "percent": min(100, round(downloaded * 100 / total)) if total else None,
                        "downloaded_mb": round(downloaded / 1048576, 1), "total_mb": total_mb,
                    }, force=True)

                with open(tmp, mode) as f:
                    for chunk in resp.iter_content(CHUNK_SIZE):
                        if not chunk:
                            continue
                        f.write(chunk)
                        downloaded += len(chunk)
                        _add_bytes(len(chunk))
                        if progress_key:
                            _update_progress(progress_key, {
                                "name": name, "type": mtype,
                                "percent": min(100, round(downloaded * 100 / total)) if total else None,
                                "downloaded_mb": round(downloaded / 1048576, 1),
                                "total_mb": total_mb,
                            })
                if progress_key:
                    # финальный флаш 100% (иначе для мелких файлов троттлинг съедает апдейт)
                    _update_progress(progress_key, {
                        "name": name, "type": mtype, "percent": 100,
                        "downloaded_mb": round(downloaded / 1048576, 1), "total_mb": total_mb,
                    }, force=True)
            os.replace(tmp, dest)  # атомарная замена
            return name
        except (requests.RequestException, OSError) as exc:
            logger.warning("Попытка %d/3 не удалась для %s: %s", attempt, url, exc)
            # .part НЕ удаляем — сохраняем прогресс для докачки на следующей попытке/запуске
            if attempt < 3:
                time.sleep(attempt * 2)
    return None


def _download_task(task: tuple) -> tuple:
    """Воркер пула: скачивает один файл и обновляет соответствующее поле в БД.
    Каждый воркер открывает свою короткоживущую connection (потокобезопасно).
    Возвращает (успех: bool, сообщение: str)."""
    media_id, field, url = task
    key = f"{media_id}:{field}"
    try:
        # Проверка по будущему имени файла: если он уже есть в папке медиа —
        # не качаем повторно, только обновляем БД.
        name = filename_from_url(url)
        dest = os.path.join(DATA_DIR, name) if name else ""
        if name and os.path.isfile(dest) and os.path.getsize(dest) > 0:
            filename, already = name, True
        else:
            # Сразу показываем закачку — ещё до ответа сервера (фаза подключения),
            # иначе активный воркер до первого байта не виден в списке.
            _update_progress(key, {"name": name or "?", "type": _media_type(name or ""),
                                   "percent": None, "downloaded_mb": 0.0, "total_mb": None},
                             force=True)
            filename, already = download_file(url, DATA_DIR, progress_key=key), False
            if REQUEST_DELAY:
                time.sleep(REQUEST_DELAY)

        if not filename:
            return False, f"не скачан ({field} id{media_id}): {url}", False
        try:
            conn = db.get_db_connection()
            db.set_media_local_file(conn, media_id, field, filename)
            conn.close()
        except Exception as exc:
            return False, f"ошибка БД ({field} id{media_id}): {exc}", False
        return True, filename, already
    finally:
        _update_progress(key, None)  # убрать из активных


def run():
    global _total_bytes
    db.init_db()
    os.makedirs(DATA_DIR, exist_ok=True)
    signal.signal(signal.SIGTERM, _sigterm_handler)
    _total_bytes = 0
    _clear_progress()
    _write_pid()

    workers = max(1, int(db.get_setting("download_workers", "4")))
    limit   = max(0, int(db.get_setting("download_limit", "0")))

    # Пул HTTP-соединений под число воркеров, чтобы он не стал узким местом
    # (по умолчанию requests держит maxsize=10). На реальную скорость влияет мало,
    # если упирается канал/лимит CDN, но снимает потолок пула при большом workers.
    adapter = HTTPAdapter(pool_connections=workers, pool_maxsize=workers)
    _SESSION.mount("http://", adapter)
    _SESSION.mount("https://", adapter)

    conn = db.get_db_connection()
    pending = db.get_pending_media(conn)
    conn.close()

    # Плоский список задач: по одной на каждое ещё не скачанное поле (thumb/full_url)
    tasks: list[tuple] = []
    for row in pending:
        for field in ("thumb", "full_url"):
            value = row.get(field)
            if value and value.startswith("http"):
                tasks.append((row["id"], field, value))

    total_all = len(tasks)
    if limit:
        tasks = tasks[:limit]
    total = len(tasks)

    suffix = f" (из {total_all}, лимит {limit})" if limit and total_all > total else ""
    logger.info("Файлов к скачиванию: %d%s | потоков: %d | папка: %s",
                total, suffix, workers, DATA_DIR)
    if total == 0:
        logger.info("Нечего скачивать — все медиа уже сохранены.")
        _remove_pid()
        return

    global _files_ok, _files_exist, _files_err
    _files_ok = 0
    _files_exist = 0
    _files_err = 0
    try:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(_download_task, t) for t in tasks]
            for i, fut in enumerate(as_completed(futures), 1):
                success, info, already = fut.result()
                if not success:
                    _files_err += 1
                    logger.warning("[%d/%d] ✗ %s", i, total, info)
                elif already:
                    _files_exist += 1
                    logger.info("[%d/%d] • уже есть: %s", i, total, info)
                else:
                    _files_ok += 1
                    logger.info("[%d/%d] ✓ %s", i, total, info)  # info = имя файла
    finally:
        _clear_progress()
        _remove_pid()

    logger.info("Завершено. Загружено: %d | Уже были: %d | Ошибок при загрузке: %d",
                _files_ok, _files_exist, _files_err)


if __name__ == "__main__":
    run()
