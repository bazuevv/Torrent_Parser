import time
import mysql.connector
import config


def _retry_on_deadlock(fn, retries: int = 5, delay: float = 0.3):
    """Выполняет fn(), при дедлоке (1213) повторяет до retries раз."""
    for attempt in range(1, retries + 1):
        try:
            return fn()
        except mysql.connector.errors.InternalError as e:
            if e.errno == 1213 and attempt < retries:
                time.sleep(delay * attempt)
                continue
            raise


def get_connection():
    return mysql.connector.connect(
        host=config.DB_HOST,
        port=config.DB_PORT,
        user=config.DB_USER,
        password=config.DB_PASSWORD,
        charset="utf8mb4",
    )


def init_db():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        f"CREATE DATABASE IF NOT EXISTS `{config.DB_NAME}` "
        "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
    )
    cursor.execute(f"USE `{config.DB_NAME}`")

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS videos (
            id          INT AUTO_INCREMENT PRIMARY KEY,
            topic_id    INT NOT NULL,
            title       VARCHAR(1000) NOT NULL,
            magnet_link TEXT NOT NULL,
            quality     VARCHAR(20),
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uq_topic (topic_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS transcode_log (
            id           INT AUTO_INCREMENT PRIMARY KEY,
            filename     VARCHAR(1000) NOT NULL,
            action       ENUM('transcode','copy') NOT NULL,
            src_height   SMALLINT,
            src_size_mb  INT NOT NULL,
            dest_size_mb INT,
            started_at   DATETIME NOT NULL,
            finished_at  DATETIME,
            duration_sec INT,
            status       ENUM('running','done','error') NOT NULL DEFAULT 'running'
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS girls (
            id         INT AUTO_INCREMENT PRIMARY KEY,
            username   VARCHAR(255) NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uq_girl_username (username)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key_name    VARCHAR(100) PRIMARY KEY,
            value       VARCHAR(1000) NOT NULL,
            label       VARCHAR(200) NOT NULL,
            description VARCHAR(500),
            group_name  VARCHAR(50) NOT NULL DEFAULT 'general'
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )

    # Insert default settings (only if not already present)
    defaults = [
        # (key_name, value, label, description, group_name)
        ("allowed_ips",          '[{"ip":"46.243.1.70","on":true},{"ip":"45.89.66.225","on":true}]', "Разрешённые IP-адреса", "Список IP с возможностью включения/отключения", "access"),
        ("request_delay",        "1.5",                        "Задержка между запросами (сек)",      "Пауза между HTTP-запросами к трекеру", "parser"),
        ("max_pages",            "0",                          "Макс. страниц за сессию",             "0 = без ограничений",                  "parser"),
        ("transcode_max_height", "720",                        "Макс. высота после перекодирования",  "Видео выше этого значения перекодируются", "transcoder"),
        ("ffmpeg_crf",           "23",                         "CRF (качество, 0–51)",                "Меньше = лучше качество, больше файл", "transcoder"),
        ("ffmpeg_preset",        "medium",                     "Пресет кодирования",                  "ultrafast / fast / medium / slow / veryslow", "transcoder"),
        ("ffmpeg_threads",       "6",                          "Потоков ffmpeg",                      "Количество потоков кодирования",       "transcoder"),
        ("ffmpeg_cpu_cores",     "0-5",                        "Ядра CPU (taskset)",                  "Диапазон ядер, напр. 0-5 или 0,1,2",  "transcoder"),
        ("dest_dir",             "/home/ubuntu/Videos",        "Папка назначения",                    "Куда сохраняются перекодированные файлы", "transcoder"),
        ("qb_save_path",         "/home/ubuntu/Videos/Bittorrent", "Путь сохранения торрентов",       "Папка qBittorrent для новых закачек",  "qbittorrent"),
    ]
    cursor.executemany(
        "INSERT IGNORE INTO settings (key_name, value, label, description, group_name) VALUES (%s, %s, %s, %s, %s)",
        defaults,
    )

    # Migrations for existing installations
    for sql in [
        "ALTER TABLE videos ADD COLUMN torrent_hash VARCHAR(40)",
        "ALTER TABLE videos ADD COLUMN torrent_name VARCHAR(500)",
        "ALTER TABLE transcode_log ADD COLUMN queued_at DATETIME AFTER id",
        "ALTER TABLE transcode_log MODIFY COLUMN started_at DATETIME",
        "ALTER TABLE transcode_log MODIFY COLUMN status ENUM('queued','running','done','error') NOT NULL DEFAULT 'queued'",
        "ALTER TABLE transcode_log ADD COLUMN src_duration_sec INT AFTER src_height",
        "ALTER TABLE girls ADD COLUMN banner_url TEXT",
        "ALTER TABLE girls ADD COLUMN avatar_url TEXT",
        "ALTER TABLE girls ADD COLUMN onlyfans_url VARCHAR(512)",
        "ALTER TABLE girls ADD COLUMN media_count INT NOT NULL DEFAULT 0",
        "ALTER TABLE girls ADD COLUMN page_views INT NOT NULL DEFAULT 0",
        "ALTER TABLE girls ADD COLUMN lg_views VARCHAR(50) DEFAULT NULL",
        "ALTER TABLE girls ADD COLUMN subscribers VARCHAR(50) DEFAULT NULL",
        "ALTER TABLE girls ADD COLUMN is_active TINYINT(1) NOT NULL DEFAULT 1",
        "ALTER TABLE girl_media ADD COLUMN full_url TEXT AFTER thumb",
        """CREATE TABLE IF NOT EXISTS girl_media (
            id         INT AUTO_INCREMENT PRIMARY KEY,
            girl_id    INT NOT NULL,
            url        TEXT NOT NULL,
            thumb      TEXT,
            is_video   TINYINT(1) NOT NULL DEFAULT 0,
            position   SMALLINT NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            KEY idx_girl_media_girl_id (girl_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
    ]:
        try:
            cursor.execute(sql)
        except Exception:
            pass  # already applied

    conn.commit()
    cursor.close()
    conn.close()


def get_setting(key: str, default: str = "") -> str:
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM settings WHERE key_name = %s", (key,))
        row = cursor.fetchone()
        cursor.close()
        conn.close()
        return row[0] if row else default
    except Exception:
        return default


def get_all_settings() -> list[dict]:
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT key_name, value, label, description, group_name FROM settings ORDER BY group_name, key_name")
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        return rows
    except Exception:
        return []


def set_setting(key: str, value: str) -> None:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE settings SET value = %s WHERE key_name = %s", (value, key))
    conn.commit()
    cursor.close()
    conn.close()


def get_db_connection():
    conn = mysql.connector.connect(
        host=config.DB_HOST,
        port=config.DB_PORT,
        database=config.DB_NAME,
        user=config.DB_USER,
        password=config.DB_PASSWORD,
        charset="utf8mb4",
    )
    return conn


def topic_exists(conn, topic_id: int) -> bool:
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM videos WHERE topic_id = %s LIMIT 1", (topic_id,))
    result = cursor.fetchone()
    cursor.close()
    return result is not None


def save_video(conn, topic_id: int, title: str, magnet_link: str, quality: str | None):
    def _do():
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT IGNORE INTO videos (topic_id, title, magnet_link, quality)
            VALUES (%s, %s, %s, %s)
            """,
            (topic_id, title, magnet_link, quality),
        )
        conn.commit()
        affected = cursor.rowcount
        cursor.close()
        return affected > 0
    return _retry_on_deadlock(_do)


def update_torrent_info(conn, topic_id: int, torrent_hash: str, torrent_name: str | None = None):
    cursor = conn.cursor()
    cursor.execute(
        """UPDATE videos
           SET torrent_hash = %s,
               torrent_name = COALESCE(%s, torrent_name)
           WHERE topic_id = %s""",
        (torrent_hash, torrent_name, topic_id),
    )
    conn.commit()
    cursor.close()


def get_pending_torrent_hashes(conn) -> list[dict]:
    """Возвращает записи с хэшем, но без имени торрента (ожидают метаданных)."""
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT topic_id, torrent_hash FROM videos WHERE torrent_hash IS NOT NULL AND torrent_name IS NULL"
    )
    rows = cursor.fetchall()
    cursor.close()
    return rows


def set_torrent_name(conn, torrent_hash: str, torrent_name: str):
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE videos SET torrent_name = %s WHERE torrent_hash = %s AND torrent_name IS NULL",
        (torrent_name, torrent_hash),
    )
    conn.commit()
    cursor.close()


def save_girl(conn, username: str) -> bool:
    cursor = conn.cursor()
    cursor.execute(
        "INSERT IGNORE INTO girls (username) VALUES (%s)",
        (username,),
    )
    conn.commit()
    affected = cursor.rowcount
    cursor.close()
    return affected > 0


def save_girls_batch(conn, usernames: list[str]) -> int:
    """Пакетная вставка. Возвращает количество новых записей."""
    if not usernames:
        return 0
    cursor = conn.cursor()
    cursor.executemany(
        "INSERT IGNORE INTO girls (username) VALUES (%s)",
        [(u,) for u in usernames],
    )
    conn.commit()
    affected = cursor.rowcount
    cursor.close()
    return affected


def get_girl_profile_db(conn, username: str) -> dict | None | bool:
    """Читает профиль из БД.
    Возвращает None если данных нет (нужен парсинг),
    False если аккаунт неактивен (редирект на главную),
    dict если данные есть."""
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT id, banner_url, avatar_url, onlyfans_url, media_count, page_views,"
        " lg_views, subscribers, is_active FROM girls WHERE username = %s LIMIT 1",
        (username,),
    )
    girl = cursor.fetchone()
    if not girl:
        cursor.close()
        return None
    if girl["is_active"] == 0:
        cursor.close()
        return False  # неактивный аккаунт, не парсить
    if girl["banner_url"] is None and girl["avatar_url"] is None:
        cursor.close()
        return None
    cursor.execute(
        "SELECT url, thumb, full_url, is_video FROM girl_media WHERE girl_id = %s ORDER BY position",
        (girl["id"],),
    )
    media_rows = cursor.fetchall()
    # Если girl_media очищена — надо перепарсить
    if not media_rows:
        cursor.close()
        return None
    cursor.close()
    return {
        "banner_url": girl["banner_url"],
        "avatar_url": girl["avatar_url"],
        "onlyfans_url": girl["onlyfans_url"],
        "medias_count": str(girl["media_count"]),
        "page_views": girl["page_views"],
        "views": girl["lg_views"],
        "subscribers": girl["subscribers"],
        "media_items": [
            {"url": r["url"], "thumb": r["thumb"], "full_url": r["full_url"], "is_video": bool(r["is_video"])}
            for r in media_rows
        ],
    }


def save_girl_profile_db(conn, username: str, banner_url: str | None,
                         avatar_url: str | None, media_items: list[dict],
                         onlyfans_url: str | None = None,
                         lg_views: str | None = None,
                         subscribers: str | None = None) -> None:
    """Сохраняет/обновляет banner, avatar и медиа-список в БД."""
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE girls SET banner_url = %s, avatar_url = %s, onlyfans_url = %s,"
        " media_count = %s, lg_views = %s, subscribers = %s WHERE username = %s",
        (banner_url, avatar_url, onlyfans_url, len(media_items), lg_views, subscribers, username),
    )
    cursor.execute("SELECT id FROM girls WHERE username = %s LIMIT 1", (username,))
    row = cursor.fetchone()
    if not row:
        cursor.close()
        conn.commit()
        return
    girl_id = row[0]
    cursor.execute("DELETE FROM girl_media WHERE girl_id = %s", (girl_id,))
    if media_items:
        cursor.executemany(
            "INSERT INTO girl_media (girl_id, url, thumb, full_url, is_video, position) VALUES (%s,%s,%s,%s,%s,%s)",
            [
                (girl_id, m["url"], m.get("thumb"), m.get("full_url"), 1 if m.get("is_video") else 0, i)
                for i, m in enumerate(media_items)
            ],
        )
    conn.commit()
    cursor.close()


def increment_page_views_db(conn, username: str) -> int:
    """Увеличивает счётчик просмотров профиля на нашем сайте. Возвращает новое значение."""
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE girls SET page_views = page_views + 1 WHERE username = %s",
        (username,),
    )
    cursor.execute("SELECT page_views FROM girls WHERE username = %s LIMIT 1", (username,))
    row = cursor.fetchone()
    conn.commit()
    cursor.close()
    return row[0] if row else 0


def mark_girl_inactive_db(conn, username: str) -> None:
    """Помечает аккаунт как неактивный (редирект на главную leakgallery)."""
    cursor = conn.cursor()
    cursor.execute("UPDATE girls SET is_active = 0 WHERE username = %s", (username,))
    conn.commit()
    cursor.close()
