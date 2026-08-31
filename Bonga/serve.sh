#!/usr/bin/env bash
# Сервер player.html и его API.
# Через file:// браузер режет CORS-запросы к CDN, а простой http.server не умеет
# получать каталог и подписанные HLS-адреса Chaturbate.
# Слушаем 0.0.0.0, чтобы плеер открывался и с других устройств в локальной сети.
set -e

PORT=8777
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${BONGA_LOG_DIR:-${ROOT}/log}"
LAN_IP=$(hostname -I | awk '{print $1}')

mkdir -p "$LOG_DIR"

echo "Локально:  http://127.0.0.1:${PORT}/player.html"
echo "По сети:   http://${LAN_IP}:${PORT}/player.html"
echo

exec python3 "$ROOT/server.py" >>"$LOG_DIR/serve-stdout.log" 2>&1
