#!/usr/bin/env bash
# Локальный http-сервер для player.html.
# Через file:// браузер режет CORS-запросы к CDN — поэтому отдаём страницу по http.
# Слушаем 0.0.0.0, чтобы плеер открывался и с других устройств в локальной сети.
set -e

PORT=8777
LAN_IP=$(hostname -I | awk '{print $1}')

echo "Локально:  http://127.0.0.1:${PORT}/player.html"
echo "По сети:   http://${LAN_IP}:${PORT}/player.html"
echo

exec python3 -m http.server "${PORT}" --bind 0.0.0.0 --directory /mnt/Projects/Torrent_Parser/Bonga
