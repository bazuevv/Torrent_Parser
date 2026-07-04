import logging
import time

import config
import db
import parser as p

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def run():
    logger.info("Инициализация базы данных...")
    db.init_db()
    conn = db.get_db_connection()

    request_delay = float(db.get_setting("request_delay", str(config.REQUEST_DELAY)))
    max_pages_setting = int(db.get_setting("max_pages", "0"))
    logger.info("Настройки: задержка=%.1f сек, макс. страниц=%s",
                request_delay, max_pages_setting or "∞")

    stats = {"saved": 0, "skipped_quality": 0, "skipped_exists": 0, "no_magnet": 0}
    page_num = 0
    next_url = config.TRACKER_URL

    try:
        while next_url:
            page_num += 1
            if max_pages_setting and page_num > max_pages_setting:
                logger.info("Достигнут лимит страниц (%d). Завершение.", max_pages_setting)
                break

            logger.info("Загружаю страницу %d: %s", page_num, next_url)

            entries, next_url, _ = p.fetch_tracker_page(next_url)

            if not entries:
                logger.warning("Страница %d вернула пустой список. Завершение.", page_num)
                break

            logger.info("Получено записей: %d", len(entries))

            for entry in entries:
                topic_id = entry["topic_id"]
                title = entry["title"]
                quality = entry["quality"]

                if entry["skip"]:
                    logger.debug("Пропуск (качество > 1080p): [%s] %s", quality, title)
                    stats["skipped_quality"] += 1
                    continue

                if db.topic_exists(conn, topic_id):
                    logger.debug("Уже существует topic_id=%d", topic_id)
                    stats["skipped_exists"] += 1
                    continue

                magnet = p.fetch_magnet(topic_id)
                if not magnet:
                    logger.warning("Магнет-ссылка не найдена для topic_id=%d: %s", topic_id, title)
                    stats["no_magnet"] += 1
                    continue

                saved = db.save_video(conn, topic_id, title, magnet, quality)
                if saved:
                    logger.info("Сохранено [%s] %s", quality or "—", title[:80])
                    stats["saved"] += 1

            if next_url:
                time.sleep(request_delay)

    finally:
        conn.close()

    logger.info(
        "Готово. Сохранено: %d | Отфильтровано (>1080p): %d | "
        "Уже в БД: %d | Без магнета: %d",
        stats["saved"],
        stats["skipped_quality"],
        stats["skipped_exists"],
        stats["no_magnet"],
    )


if __name__ == "__main__":
    run()
