#!/bin/bash
# Надёжный перезапуск Flask
pkill -f "venv/bin/python3 web.py" 2>/dev/null
# Ждём, пока порт 5000 освободится (до 5 секунд)
for i in $(seq 1 10); do
    fuser 5000/tcp 2>/dev/null || break
    sleep 0.5
done
# Принудительно убить если ещё занят
fuser -k 5000/tcp 2>/dev/null
sleep 0.3
venv/bin/python3 web.py &
echo "Flask запущен (PID $!)"
