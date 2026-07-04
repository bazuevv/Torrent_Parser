"""
Парсер медиа-данных профилей leakgallery.com.
Для каждого профиля в таблице girls (без данных) скачивает:
  - banner_url, avatar_url
  - список ВСЕХ медиа (превью + ссылки) через API с пагинацией
и сохраняет в БД (girls.banner_url, girls.avatar_url, таблица girl_media).

Использование:
    venv/bin/python3 leakgallery_media_parser.py
"""

import json
import logging
import re
import sys
import time

import requests

import db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

BASE_URL      = "https://leakgallery.com"
API_URL       = "https://api.leakgallery.com"
CDN_URL       = "https://cdn.leakgallery.com"
REQUEST_DELAY = 0.5   # секунды между запросами к сайту
API_PAGE_SIZE = 40    # сайт отдаёт по 40 элементов на страницу
LOG_EVERY     = 50    # логировать прогресс каждые N профилей


def _media_item_from_api(item: dict, username: str) -> dict:
    """Конвертирует медиа-объект из API в формат для БД."""
    media_id   = item["id"]
    is_video   = bool(item.get("is_video"))
    thumb_path = item.get("thumbnail_path", "")
    file_path  = item.get("file_path", "")

    thumb    = f"{CDN_URL}/{thumb_path}" if thumb_path else None
    full_url = f"{CDN_URL}/{file_path}"  if file_path  else None
    url      = f"{BASE_URL}/{username}/{media_id}"

    return {"url": url, "thumb": thumb, "full_url": full_url, "is_video": is_video}


def parse_transfer_states(html: str, username: str) -> dict:
    """Извлекает данные профиля из Angular transfer-state <script>-блоков.

    Общая логика для media-парсера и веб-приложения. Возвращает dict с ключами:
    banner_url, avatar_url, media_count, views, subscribers, app_token, media_items,
    profile_found (был ли в transfer state блок профиля для этого username; False
    означает, что профиль не существует — сайт отдаёт 200 с содержимым главной).
    Пагинацию (страницы 2+) вызывающий делает сам — стратегия токена у них разная.
    """
    states = re.findall(
        r'<script[^>]*type=["\'](?:application/json|ng-state)["\'][^>]*>(.*?)</script>',
        html, re.DOTALL,
    )

    banner_url = avatar_url = None
    views = subscribers = None
    app_token = None
    media_count = 0
    media_items: list[dict] = []
    profile_found = False

    for content in states:
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            continue

        if "APP_TOKEN" in data:
            app_token = data["APP_TOKEN"]

        # Banner, avatar, счётчики и первая страница медиа
        profile_key = f"httpGet:profile/{username}?type=All&sort=MostRecent&fake=true"
        if profile_key in data:
            profile_found = True
            prof = data[profile_key]
            p = prof.get("profile", {})
            banner_url  = p.get("banner_pic") or banner_url
            avatar_url  = p.get("profile_pic") or avatar_url
            media_count = prof.get("mediaCount", 0)
            views       = str(prof.get("profileViews", "")) or views
            subscribers = str(prof.get("profileSubscribers", "")) or subscribers
            for item in prof.get("medias", []):
                media_items.append(_media_item_from_api(item, username))

        # Также проверяем вложенный ключ (вариант 3810946911.b)
        for v in data.values():
            if isinstance(v, dict) and isinstance(v.get("b"), dict):
                b = v["b"]
                if "medias" in b and isinstance(b.get("profile"), dict):
                    p = b["profile"]
                    if p.get("username", "").lower() == username.lower():
                        profile_found = True
                        banner_url  = p.get("banner_pic") or banner_url
                        avatar_url  = p.get("profile_pic") or avatar_url
                        media_count = b.get("mediaCount", media_count) or media_count
                        if not media_items:
                            for item in b.get("medias", []):
                                media_items.append(_media_item_from_api(item, username))

    return {
        "banner_url": banner_url,
        "avatar_url": avatar_url,
        "media_count": media_count,
        "views": views,
        "subscribers": subscribers,
        "app_token": app_token,
        "media_items": media_items,
        "profile_found": profile_found,
    }


_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
})

# Кэш токена: (token_str, expires_at_monotonic)
_token_cache: tuple[str, float] | None = None
# Флаг: последний _get_token вернул None из-за редиректа (неактивный аккаунт)
_last_inactive: bool = False


def _get_token(username: str) -> str | None:
    """Извлекает APP_TOKEN из Angular transfer state страницы профиля."""
    global _token_cache, _last_inactive
    _last_inactive = False
    now = time.monotonic()
    if _token_cache and _token_cache[1] > now + 60:
        return _token_cache[0]

    try:
        resp = _SESSION.get(f"{BASE_URL}/{username}", timeout=20)
        if resp.status_code == 404:
            return None
        # Редирект на главную = аккаунт неактивен
        if resp.url.rstrip("/") == BASE_URL:
            _last_inactive = True
            return None
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Ошибка загрузки страницы %s: %s", username, exc)
        return None

    # Сохраняем HTML для дальнейшего использования
    _SESSION._last_html = resp.text

    states = re.findall(
        r'<script[^>]*type=["\'](?:application/json|ng-state)["\'][^>]*>(.*?)</script>',
        resp.text, re.DOTALL,
    )
    for content in states:
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            continue
        if "APP_TOKEN" in data:
            token = data["APP_TOKEN"]
            # Декодируем exp из JWT без внешних библиотек
            try:
                import base64
                payload_b64 = token.split(".")[1]
                payload_b64 += "=" * (-len(payload_b64) % 4)
                payload = json.loads(base64.b64decode(payload_b64))
                exp_monotonic = now + (payload["exp"] - time.time())
            except Exception:
                exp_monotonic = now + 600  # fallback: 10 минут
            _token_cache = (token, exp_monotonic)
            return token
    return None


def _api_get_page(username: str, page: int, token: str) -> dict | None:
    """Запрашивает одну страницу медиа через API."""
    url = f"{API_URL}/profile/{username}/{page}?type=All&sort=MostRecent&fake=true"
    headers = {
        "Authorization": f"Bearer {token}",
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/",
    }
    for attempt in range(1, 4):
        try:
            resp = _SESSION.get(url, headers=headers, timeout=20)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            logger.warning("API стр.%d попытка %d/3 для %s: %s", page, attempt, username, exc)
            if attempt < 3:
                time.sleep(attempt * 2)
    return None


def parse_profile(username: str) -> dict | None:
    """Парсит профиль через Angular transfer state + API пагинацию.
    Возвращает dict {banner_url, avatar_url, media_items} или None если профиль
    не найден (404, сетевая ошибка или несуществующий профиль)."""
    global _last_inactive
    # 1. Загружаем страницу и получаем token + начальные данные
    token = _get_token(username)
    if token is None:
        return None  # 404 или ошибка сети

    html = getattr(_SESSION, "_last_html", "")
    parsed = parse_transfer_states(html, username)
    # Профиль не существует: сайт вернул 200 с содержимым главной, блока профиля
    # в transfer state нет → сигналим run(), что аккаунт надо пометить неактивным.
    if not parsed["profile_found"]:
        _last_inactive = True
        return None
    banner_url  = parsed["banner_url"]
    avatar_url  = parsed["avatar_url"]
    media_count = parsed["media_count"]
    media_items = parsed["media_items"]

    # 2. Если медиа больше чем поместилось на первой странице — грузим остальные
    seen_ids = {m["url"] for m in media_items}
    if media_count > len(media_items):
        total_pages = (media_count + API_PAGE_SIZE - 1) // API_PAGE_SIZE
        for page in range(2, total_pages + 1):
            # Обновляем токен если истёк
            if _token_cache is None or _token_cache[1] < time.monotonic() + 60:
                new_token = _get_token(username)
                if new_token:
                    token = new_token

            page_data = _api_get_page(username, page, token)
            if not page_data:
                logger.warning("Нет данных на стр.%d для %s", page, username)
                break

            for item in page_data.get("medias", []):
                entry = _media_item_from_api(item, username)
                if entry["url"] not in seen_ids:
                    seen_ids.add(entry["url"])
                    media_items.append(entry)

            time.sleep(REQUEST_DELAY)

    return {
        "banner_url": banner_url,
        "avatar_url": avatar_url,
        "media_items": media_items,
    }


def get_pending_usernames(conn) -> list[str]:
    """Возвращает usernames профилей, у которых ещё нет данных в БД (активные)."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT username FROM girls"
        " WHERE is_active = 1 AND banner_url IS NULL AND avatar_url IS NULL ORDER BY id"
    )
    rows = cursor.fetchall()
    cursor.close()
    return [r[0] for r in rows]


def run():
    conn = db.get_db_connection()
    pending = get_pending_usernames(conn)
    conn.close()

    total = len(pending)
    logger.info("Профилей без данных: %d", total)

    if total == 0:
        logger.info("Все профили уже обработаны.")
        return

    done = 0
    errors = 0

    for i, username in enumerate(pending, 1):
        data = parse_profile(username)

        if data is None:
            errors += 1
            if _last_inactive:
                logger.info("[%d/%d] %s — неактивен (профиль не найден)", i, total, username)
                try:
                    conn = db.get_db_connection()
                    db.mark_girl_inactive_db(conn, username)
                    conn.close()
                except Exception:
                    pass
            else:
                logger.warning("[%d/%d] %s — не удалось загрузить (404 или ошибка сети)", i, total, username)
        else:
            try:
                conn = db.get_db_connection()
                db.save_girl_profile_db(
                    conn, username,
                    data["banner_url"],
                    data["avatar_url"],
                    data["media_items"],
                )
                conn.close()
                done += 1
            except Exception as exc:
                errors += 1
                logger.error("[%d/%d] %s — ошибка БД: %s", i, total, username, exc)

        if i % LOG_EVERY == 0 or i == total:
            remaining = total - i
            logger.info(
                "[%d/%d] Готово: %d | Ошибок: %d | Осталось: %d",
                i, total, done, errors, remaining,
            )

        time.sleep(REQUEST_DELAY)

    logger.info("Завершено. Обработано: %d / %d, ошибок: %d", done, total, errors)


if __name__ == "__main__":
    run()
