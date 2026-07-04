import os

BASE_URL = "https://xxxtorrents.pro"
TRACKER_URL = f"{BASE_URL}/tracker.php"

REQUEST_DELAY = 1.5  # секунды между HTTP-запросами

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

# Разрешить доступ к админ-панели с localhost (127.0.0.1)
ALLOW_LOCALHOST = True

# ── Секреты ───────────────────────────────────────────────────────────────────
# Пароли и хосты НЕ хранятся в этом файле (он в git). Значения берутся:
#   1) из переменной окружения с тем же именем (напр. DB_PASSWORD),
#   2) иначе из модуля config_secrets.py (он в .gitignore),
#   3) иначе — безопасная заглушка.
# Реальные значения держите в config_secrets.py (см. config_secrets.example.py).
try:
    import config_secrets as _secrets
except ImportError:
    _secrets = None


def _secret(name: str, default: str = "") -> str:
    env = os.getenv(name)
    if env is not None:
        return env
    if _secrets is not None and hasattr(_secrets, name):
        return str(getattr(_secrets, name))
    return default


# Настройки qBittorrent Web API
QB_HOST     = _secret("QB_HOST", "http://localhost:8080")
QB_USER     = _secret("QB_USER", "admin")
QB_PASSWORD = _secret("QB_PASSWORD")

# Настройки подключения к MySQL
DB_HOST     = _secret("DB_HOST", "localhost")
DB_PORT     = int(_secret("DB_PORT", "3306"))
DB_NAME     = _secret("DB_NAME", "torrent_parser")
DB_USER     = _secret("DB_USER", "torrent")
DB_PASSWORD = _secret("DB_PASSWORD")
