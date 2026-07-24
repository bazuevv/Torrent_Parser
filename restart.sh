#!/bin/bash
# Надёжный перезапуск Flask через systemd (сервис torrent-parser.service).
# Процессом управляет systemd (автозапуск после перезагрузки + Restart=on-failure),
# поэтому перезапуск делаем только через него — иначе pkill + фоновый запуск
# конфликтуют с systemd (он поднимет свой процесс, а второй упрётся в занятый порт).
sudo systemctl restart torrent-parser.service

# Ждём, пока порт 5000 снова начнёт слушаться (до 5 секунд)
for i in $(seq 1 10); do
    ss -ltn 2>/dev/null | grep -q ':5000 ' && break
    sleep 0.5
done

if ss -ltn 2>/dev/null | grep -q ':5000 '; then
    echo "Flask перезапущен (systemd), порт 5000 слушается. PID $(systemctl show -p MainPID --value torrent-parser.service)"
else
    echo "ВНИМАНИЕ: порт 5000 не слушается. Смотри: journalctl -u torrent-parser -e"
fi
