# Шаблон секретов. Скопируйте в config_secrets.py и впишите реальные значения:
#     cp config_secrets.example.py config_secrets.py
# Файл config_secrets.py в .gitignore — в репозиторий не попадёт.
# Альтернатива: задать те же имена как переменные окружения (они имеют приоритет).
QB_HOST = "http://localhost:8080"
QB_USER = "admin"
QB_PASSWORD = "CHANGE_ME"

DB_HOST = "localhost"
DB_PORT = "3306"
DB_NAME = "torrent_parser"
DB_USER = "torrent"
DB_PASSWORD = "CHANGE_ME"
