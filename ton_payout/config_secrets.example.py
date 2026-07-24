# Шаблон секретов для ton_payout. Скопируйте в config_secrets.py и впишите значения:
#     cp config_secrets.example.py config_secrets.py
# Файл config_secrets.py в .gitignore — в репозиторий не попадёт.
# Альтернатива: задать те же имена как переменные окружения (они имеют приоритет).

# ── MySQL ─────────────────────────────────────────────────────────────────────
# Та же база, что и у Torrent_Parser. Обычно совпадает с корневым config_secrets.py.
DB_HOST = "localhost"
DB_PORT = "3306"
DB_NAME = "torrent_parser"
DB_USER = "torrent"
DB_PASSWORD = "CHANGE_ME"

# ── TON-кошелёк ───────────────────────────────────────────────────────────────
# Мнемоника кошелька-отправителя (12/18/24 слова через пробел).
# ВНИМАНИЕ: это доступ к деньгам. Файл держите с правами 600, в git он не попадает.
TON_MNEMONIC = "word1 word2 word3 ... word24"
# mainnet или testnet
TON_NETWORK = "mainnet"
# Ключ Toncenter (не обязателен, но без него лимит 1 запрос/сек). https://t.me/toncenter
TONCENTER_API_KEY = ""

# ── Веб-интерфейс ─────────────────────────────────────────────────────────────
# Логин/пароль для входа в панель управления рассылкой.
WEB_USERNAME = "admin"
WEB_PASSWORD = "CHANGE_ME"
# Секретный ключ для подписи cookie-сессий Flask. Сгенерируйте случайную строку:
#     python3 -c "import secrets; print(secrets.token_hex(32))"
SECRET_KEY = "CHANGE_ME_TO_RANDOM_HEX"
