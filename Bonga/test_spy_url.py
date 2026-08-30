"""Проверка ветки force=1 в chaturbate_stream: что делает сервер с ответом
агента после фатальной ошибки HLS у плеера. Агент и досье заглушаем.

Запуск: python3 Bonga/test_spy_url.py

Сторожит цену ошибки: пока сервер требовал от агента непременно ДРУГОЙ
адрес, совпадение стабильного пути origin.<ник>.<ULID> гасило оплаченный
показ через полминуты после входа, и токены уходили впустую.
"""
import os
import sys
import tempfile
import time

os.environ['BONGA_REC_DIR'] = tempfile.mkdtemp(prefix='spytest-')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402

ROOM = 'emmaember'
LIVE = ('https://edge17-hel.live.mmcdn.com/v1/edge/streams/'
        'origin.emmaember.01M18NFCRPBZZWV4XSA9W7B80P/playlist.m3u8')
OTHER = ('https://edge4-hel.live.mmcdn.com/v1/edge/streams/'
         'origin.emmaember.01M18NFCRPBZZWV4XSA9W7B80P/playlist.m3u8')

stops = []
server.cb_queue = lambda act, room: 'fake'
server.cb_cancel = lambda cid: None
server.chaturbate_dossier = lambda user: {'status': 'private', 'spy_price': 6}
server.cb_stop_now = lambda reason, wait=True: stops.append(reason)


def arm(url=LIVE, misses=0):
    """Ставим машину в состояние «идёт spy» с протухшим url_at, чтобы
    force-ветка сработала, а не отдала кэш."""
    stops.clear()
    server.cb_reset()
    server.CB_SPY.update({'state': 'spying', 'room': ROOM, 'price': 6,
                          'started': time.time(), 'url': url,
                          'url_at': time.time(), 'misses': misses})


def agent(answer):
    server.cb_wait_result = lambda cid, timeout: answer


def check(name, cond, extra=''):
    print(('  OK   ' if cond else '  ПРОВАЛ ') + name + (f' — {extra}' if extra else ''))
    return cond


ok = True

# 1. Агент отдал тот же адрес — раньше это гасило оплаченную сессию.
arm()
agent({'ok': True, 'url': LIVE, 'room_status': 'private'})
r = server.chaturbate_stream(ROOM, force=True)
ok &= check('тот же адрес: поток отдан', r.get('online') and r.get('url') == LIVE, repr(r)[:120])
ok &= check('тот же адрес: сессия жива', server.CB_SPY['state'] == 'spying')
ok &= check('тот же адрес: стопа не было', not stops, str(stops))
ok &= check('тот же адрес: refreshed=False', r.get('refreshed') is False)

# 2. Агент отдал другой адрес — принимаем и запоминаем.
arm()
agent({'ok': True, 'url': OTHER, 'room_status': 'private'})
r = server.chaturbate_stream(ROOM, force=True)
ok &= check('новый адрес: отдан плееру', r.get('url') == OTHER)
ok &= check('новый адрес: сохранён в CB_SPY', server.CB_SPY['url'] == OTHER)
ok &= check('новый адрес: refreshed=True', r.get('refreshed') is True)

# 3. Шоу кончилось: адреса нет, статус не private.
arm()
agent({'ok': False, 'url': '', 'room_status': 'public'})
r = server.chaturbate_stream(ROOM, force=True)
ok &= check('конец шоу: online=False', r.get('online') is False)
ok &= check('конец шоу: статус пробрасывается', r.get('status') == 'public', repr(r)[:120])
ok &= check('конец шоу: машина сброшена', server.CB_SPY['state'] == 'idle')

# 4. Первый промах: агент молчит — отдаём удержанный адрес, сессию не рвём.
arm()
agent(None)
r = server.chaturbate_stream(ROOM, force=True)
ok &= check('промах 1: отдан удержанный адрес', r.get('online') and r.get('url') == LIVE)
ok &= check('промах 1: сессия жива', server.CB_SPY['state'] == 'spying')
ok &= check('промах 1: счётчик = 1', server.CB_SPY['misses'] == 1, str(server.CB_SPY['misses']))
ok &= check('промах 1: стопа не было', not stops, str(stops))

# 5. Второй промах подряд — вот теперь останавливаем.
arm(misses=1)
agent({'ok': False, 'url': '', 'room_status': ''})
r = server.chaturbate_stream(ROOM, force=True)
ok &= check('промах 2: online=False', r.get('online') is False)
ok &= check('промах 2: стоп вызван', len(stops) == 1, str(stops))

# 6. Чужой хост принимать нельзя даже от агента.
arm()
agent({'ok': True, 'url': 'https://evil.example.com/playlist.m3u8', 'room_status': 'private'})
r = server.chaturbate_stream(ROOM, force=True)
ok &= check('чужой хост: не принят', r.get('url') != 'https://evil.example.com/playlist.m3u8',
            repr(r)[:120])
ok &= check('чужой хост: считается промахом', server.CB_SPY['misses'] == 1)

print('\nЖурнал сервера за прогон:')
with open(server.LOG_PATH, encoding='utf-8') as f:
    for line in f:
        print('   ', line.rstrip())

print('\nИТОГ:', 'всё сошлось' if ok else 'ЕСТЬ ПРОВАЛЫ')
sys.exit(0 if ok else 1)
