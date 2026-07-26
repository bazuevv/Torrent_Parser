import importlib.util
import os

# ── Секреты ───────────────────────────────────────────────────────────────────
# Пароли, мнемоника и хосты НЕ хранятся в этом файле (он в git). Значения берутся
# по приоритету:
#   1) переменная окружения с тем же именем (напр. TON_MNEMONIC),
#   2) ton_payout/config_secrets.py (свои секреты рассылки),
#   3) корневой config_secrets.py проекта (общие DB-креды),
#   4) безопасная заглушка.
# Файлы config_secrets.py грузятся по АБСОЛЮТНОМУ пути (importlib), поэтому
# находятся независимо от рабочей директории — что при запуске standalone
# (`python3 -m ton_payout.web`), что при импорте из корневого web.py на :5000.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)


def _load_secrets(path: str):
    if not os.path.exists(path):
        return None
    try:
        spec = importlib.util.spec_from_file_location(f"_secrets_{abs(hash(path))}", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


# ton_payout-локальные секреты имеют приоритет над корневыми
_ton_secrets = _load_secrets(os.path.join(_HERE, "config_secrets.py"))
_root_secrets = _load_secrets(os.path.join(_ROOT, "config_secrets.py"))


def _secret(name: str, default: str = "") -> str:
    env = os.getenv(name)
    if env is not None:
        return env
    for source in (_ton_secrets, _root_secrets):
        if source is not None and hasattr(source, name):
            return str(getattr(source, name))
    return default


# Подключение к MySQL — та же база, что и у Torrent_Parser (таблицы
# ton_recipients/ton_payout_runs/ton_payout_run_items живут в ней же).
DB_HOST     = _secret("DB_HOST", "localhost")
DB_PORT     = int(_secret("DB_PORT", "3306"))
DB_NAME     = _secret("DB_NAME", "torrent_parser")
DB_USER     = _secret("DB_USER", "torrent")
DB_PASSWORD = _secret("DB_PASSWORD")

# TON-кошелёк (рассылка)
TON_MNEMONIC       = _secret("TON_MNEMONIC")
TON_NETWORK        = _secret("TON_NETWORK", "mainnet")
TONCENTER_API_KEY  = _secret("TONCENTER_API_KEY")

# Мастер-контракт USDT (джеттон). Пусто = официальный адрес Tether в mainnet
# (см. jettons.USDT_MASTER_MAINNET). Для testnet задать явно.
USDT_MASTER_ADDRESS = _secret("USDT_MASTER_ADDRESS")

# Master-сид для детерминированного вывода адресов пользователей (модель A):
# адрес = WalletV4R2(subwallet_id = id пользователя). Из одного сида — сколько
# угодно реальных адресов. Держателю сида принадлежат ключи (кастодиальная модель).
TON_MASTER_MNEMONIC = _secret("TON_MASTER_MNEMONIC")

# Веб-интерфейс
WEB_USERNAME = _secret("WEB_USERNAME", "admin")
WEB_PASSWORD = _secret("WEB_PASSWORD")
SECRET_KEY   = _secret("SECRET_KEY")
