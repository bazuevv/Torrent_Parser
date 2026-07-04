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

import logging
import os
import time
from urllib.parse import urlparse, unquote

import requests

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
LOG_EVERY     = 50            # логировать прогресс каждые N строк

_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
})


def filename_from_url(url: str) -> str:
    """Оригинальное имя файла = basename пути URL (без query, декодированный).
    basename после unquote отсекает любые директории, поэтому обхода пути нет."""
    path = unquote(urlparse(url).path)
    return os.path.basename(path)


def download_file(url: str, dest_dir: str) -> str | None:
    """Скачивает url в dest_dir под оригинальным именем. Возвращает имя файла
    или None при ошибке. Уже существующий непустой файл не перекачивается."""
    name = filename_from_url(url)
    if not name:
        logger.warning("Не удалось определить имя файла из URL: %s", url)
        return None

    dest = os.path.join(dest_dir, name)
    if os.path.isfile(dest) and os.path.getsize(dest) > 0:
        return name  # уже скачан ранее

    tmp = dest + ".part"
    for attempt in range(1, 4):
        try:
            with _SESSION.get(url, stream=True, timeout=60) as resp:
                resp.raise_for_status()
                with open(tmp, "wb") as f:
                    for chunk in resp.iter_content(CHUNK_SIZE):
                        if chunk:
                            f.write(chunk)
            os.replace(tmp, dest)  # атомарная замена
            return name
        except (requests.RequestException, OSError) as exc:
            logger.warning("Попытка %d/3 не удалась для %s: %s", attempt, url, exc)
            try:
                os.remove(tmp)
            except FileNotFoundError:
                pass
            if attempt < 3:
                time.sleep(attempt * 2)
    return None


def run():
    os.makedirs(DATA_DIR, exist_ok=True)

    conn = db.get_db_connection()
    pending = db.get_pending_media(conn)
    conn.close()

    total = len(pending)
    logger.info("Строк с несохранёнными медиа: %d  →  папка %s", total, DATA_DIR)
    if total == 0:
        logger.info("Нечего скачивать — все медиа уже сохранены.")
        return

    files_ok = 0
    files_err = 0

    for i, row in enumerate(pending, 1):
        media_id = row["id"]
        for field in ("thumb", "full_url"):
            value = row.get(field)
            if not value or not value.startswith("http"):
                continue  # пусто или уже локальное имя файла

            name = download_file(value, DATA_DIR)
            if not name:
                files_err += 1
                logger.warning("[id %d] %s — не удалось скачать: %s", media_id, field, value)
                continue

            try:
                conn = db.get_db_connection()
                db.set_media_local_file(conn, media_id, field, name)
                conn.close()
                files_ok += 1
            except Exception as exc:
                files_err += 1
                logger.error("[id %d] %s — ошибка обновления БД: %s", media_id, field, exc)

            time.sleep(REQUEST_DELAY)

        if i % LOG_EVERY == 0 or i == total:
            logger.info("[%d/%d] Сохранено файлов: %d | Ошибок: %d", i, total, files_ok, files_err)

    logger.info("Завершено. Файлов сохранено: %d | Ошибок: %d", files_ok, files_err)


if __name__ == "__main__":
    run()
