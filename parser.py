import re
import time
import logging
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

import config

logger = logging.getLogger(__name__)

# Разрешения, которые считаются "выше 1080p" и должны быть исключены
_HIGH_RES_PATTERN = re.compile(
    r"\b(4k|2k|uhd|2160p|1440p|1600p|2048p|4096p)\b", re.IGNORECASE
)
# Извлекаем числовое значение разрешения: [1080p] или [..., All Sex, 1080p]
_QUALITY_PATTERN = re.compile(r"\b(\d{3,4})p\b", re.IGNORECASE)

session = requests.Session()
session.headers.update(config.HEADERS)


def extract_quality(title: str) -> tuple[str | None, bool]:
    """Возвращает (строка качества или None, флаг исключения).

    Флаг исключения True означает, что видео нужно пропустить (качество > 1080p).
    """
    if _HIGH_RES_PATTERN.search(title):
        return None, True

    match = _QUALITY_PATTERN.search(title)
    if match:
        pixels = int(match.group(1))
        quality_str = f"{pixels}p"
        if pixels > 1080:
            return quality_str, True
        return quality_str, False

    return None, False


def _get(url: str, retries: int = 3) -> requests.Response | None:
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(url, timeout=30)
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            logger.warning("Попытка %d/%d не удалась для %s: %s", attempt, retries, url, exc)
            if attempt < retries:
                time.sleep(attempt * 2)
    return None


def parse_tracker_page(html: str) -> list[dict]:
    """Разбирает страницу tracker.php, возвращает список записей.

    Каждая запись: {'topic_id': int, 'title': str, 'quality': str|None, 'skip': bool}
    """
    soup = BeautifulSoup(html, "html.parser")
    results = []

    for link in soup.find_all("a", href=re.compile(r"viewtopic\.php\?t=\d+")):
        title = link.get_text(strip=True)
        if not title:
            continue

        qs = parse_qs(urlparse(link["href"]).query)
        topic_ids = qs.get("t")
        if not topic_ids:
            continue
        topic_id = int(topic_ids[0])

        quality, skip = extract_quality(title)
        results.append({"topic_id": topic_id, "title": title, "quality": quality, "skip": skip})

    return results


def extract_next_page_url(html: str, current_url: str) -> str | None:
    """Находит ссылку на следующую страницу пагинации."""
    soup = BeautifulSoup(html, "html.parser")

    # Ищем ссылку с текстом «Следующая» или символом → / »
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True)
        if text in ("»", "→", "Следующая", "Next", ">"):
            href = a["href"]
            return urljoin(config.BASE_URL + "/", href.lstrip("./"))

    # Альтернатива: ищем ссылку tracker.php?...&start=N где N > текущего start
    current_start = 0
    parsed_current = urlparse(current_url)
    qs_current = parse_qs(parsed_current.query)
    if "start" in qs_current:
        current_start = int(qs_current["start"][0])

    max_start = current_start
    next_url = None
    for a in soup.find_all("a", href=re.compile(r"tracker\.php.*start=\d+")):
        href = a["href"]
        qs = parse_qs(urlparse(href).query)
        start = int(qs.get("start", [0])[0])
        if start > max_start:
            max_start = start
            next_url = urljoin(config.BASE_URL + "/", href.lstrip("./"))

    if max_start > current_start:
        return next_url

    return None


def parse_magnet(html: str) -> str | None:
    """Извлекает магнет-ссылку со страницы топика."""
    soup = BeautifulSoup(html, "html.parser")
    tag = soup.find("a", href=re.compile(r"^magnet:", re.IGNORECASE))
    if tag:
        return tag["href"]
    return None


def fetch_tracker_page(url: str) -> tuple[list[dict], str | None, str]:
    """Загружает одну страницу трекера.

    Возвращает (записи, url следующей страницы или None, итоговый url).
    """
    resp = _get(url)
    if resp is None:
        logger.error("Не удалось загрузить страницу трекера: %s", url)
        return [], None, url

    html = resp.text
    entries = parse_tracker_page(html)
    next_url = extract_next_page_url(html, resp.url)
    return entries, next_url, resp.url


def fetch_magnet(topic_id: int) -> str | None:
    """Загружает страницу топика и извлекает магнет-ссылку."""
    url = f"{config.BASE_URL}/viewtopic.php?t={topic_id}"
    time.sleep(config.REQUEST_DELAY)
    resp = _get(url)
    if resp is None:
        logger.warning("Не удалось загрузить топик %d", topic_id)
        return None
    return parse_magnet(resp.text)
