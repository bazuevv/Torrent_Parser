"""
Парсер профилей leakgallery.com.
Скачивает список username из sitemap-файлов и сохраняет в таблицу girls.

Использование:
    venv/bin/python3 leakgallery_parser.py
"""

import logging
import time

import requests
from bs4 import BeautifulSoup

import config
import db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

BASE_URL    = "https://leakgallery.com"
INDEX_URL   = f"{BASE_URL}/sitemap_profile_index.xml"
REQUEST_DELAY = 0.5  # секунды между запросами

_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
})


def _get(url: str, retries: int = 3) -> str | None:
    for attempt in range(1, retries + 1):
        try:
            resp = _SESSION.get(url, timeout=30)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as exc:
            logger.warning("Попытка %d/%d не удалась для %s: %s", attempt, retries, url, exc)
            if attempt < retries:
                time.sleep(attempt * 2)
    return None


def get_sitemap_files() -> list[str]:
    """Возвращает список URL файлов sitemap_profile_N.xml."""
    html = _get(INDEX_URL)
    if not html:
        return []
    soup = BeautifulSoup(html, "xml")
    return [loc.text.strip() for loc in soup.find_all("loc")]


def parse_sitemap_file(url: str) -> list[str]:
    """Парсит один sitemap-файл, возвращает список username."""
    html = _get(url)
    if not html:
        return []
    soup = BeautifulSoup(html, "xml")
    usernames = []
    for loc in soup.find_all("loc"):
        profile_url = loc.text.strip()
        # URL вида https://leakgallery.com/username
        parts = profile_url.rstrip("/").split("/")
        if len(parts) >= 4:
            username = parts[-1]
            # Пропускаем служебные страницы
            if username and not username.startswith("sitemap") and "/" not in username:
                usernames.append(username)
    return usernames


def run():
    logger.info("Инициализация БД...")
    db.init_db()
    conn = db.get_db_connection()

    logger.info("Получаю список sitemap-файлов: %s", INDEX_URL)
    sitemap_files = get_sitemap_files()
    if not sitemap_files:
        logger.error("Не удалось получить список sitemap-файлов")
        conn.close()
        return

    logger.info("Найдено sitemap-файлов: %d", len(sitemap_files))

    stats = {"saved": 0, "skipped": 0, "errors": 0}

    for i, sitemap_url in enumerate(sitemap_files, 1):
        logger.info("Обрабатываю файл %d/%d: %s", i, len(sitemap_files), sitemap_url)

        usernames = parse_sitemap_file(sitemap_url)
        if not usernames:
            logger.warning("Файл пуст или не удалось загрузить: %s", sitemap_url)
            stats["errors"] += 1
            continue

        logger.info("  Найдено профилей: %d", len(usernames))

        try:
            saved = db.save_girls_batch(conn, usernames)
            stats["saved"] += saved
            stats["skipped"] += len(usernames) - saved
        except Exception as exc:
            logger.error("Ошибка при пакетной вставке файла %d: %s", i, exc)
            stats["errors"] += 1
            continue

        logger.info(
            "  [%d/%d] +%d новых / %d дублей | Итого в БД: %d",
            i, len(sitemap_files),
            saved, len(usernames) - saved,
            stats["saved"],
        )

        if i < len(sitemap_files):
            time.sleep(REQUEST_DELAY)

    conn.close()
    logger.info(
        "Готово. Новых записей: %d | Дублей: %d | Ошибок: %d",
        stats["saved"], stats["skipped"], stats["errors"]
    )


if __name__ == "__main__":
    run()
