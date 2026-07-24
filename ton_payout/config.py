import os

# ── Секреты ───────────────────────────────────────────────────────────────────
# Пароли, мнемоника и хосты НЕ хранятся в этом файле (он в git). Значения берутся:
#   1) из переменной окружения с тем же именем (напр. TON_MNEMONIC),
#   2) иначе из модуля config_secrets.py (он в .gitignore),
#   3) иначе — безопасная заглушка.
# Реальные значения держите в ton_payout/config_secrets.py (см. config_secrets.example.py).
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


# Подключение к MySQL — та же база, что и у Torrent_Parser (таблицы
# ton_recipients/ton_payout_runs/ton_payout_run_items живут в ней же).
DB_HOST     = _secret("DB_HOST", "localhost")
DB_PORT     = int(_secret("DB_PORT", "3306"))
DB_NAME     = _secret("DB_NAME", "torrent_parser")
DB_USER     = _secret("DB_USER", "torrent")
DB_PASSWORD = _secret("DB_PASSWORD")

# TON-кошелёк
TON_MNEMONIC       = _secret("TON_MNEMONIC")
TON_NETWORK        = _secret("TON_NETWORK", "mainnet")
TONCENTER_API_KEY  = _secret("TONCENTER_API_KEY")

# Веб-интерфейс
WEB_USERNAME = _secret("WEB_USERNAME", "admin")
WEB_PASSWORD = _secret("WEB_PASSWORD")
SECRET_KEY   = _secret("SECRET_KEY")
