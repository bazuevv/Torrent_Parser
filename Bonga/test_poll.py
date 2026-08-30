"""Проверка long-poll агента: когда он обязан вернуться раньше срока, а когда
досидеть до конца.

Запуск: python3 Bonga/test_poll.py

Сторожит свежесть цифр расхода. Прецедент 30.08: poll отдавал снимок только
по истечении CB_POLL_HOLD, и в журнале плеера «потрачено 1 тк» держалось
минуту, скакнув до 3 лишь на выходе, — то есть ровно во время оборвавшегося
показа цифры врали. Обратная крайность не лучше: возврат на каждое движение
времени превратил бы long-poll в busy-loop, поэтому неизменный снимок обязан
досиживать до конца.
"""
import os
import sys
import tempfile
import threading
import time

os.environ['BONGA_REC_DIR'] = tempfile.mkdtemp(prefix='polltest-')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402

server.CB_POLL_HOLD = 2.0          # штатные 25 с сделали бы прогон невыносимым
ROOM = 'emmaember'
ok = True


def check(name, cond, extra=''):
    global ok
    print(('  OK   ' if cond else '  ПРОВАЛ ') + name + (f' — {extra}' if extra else ''))
    ok &= bool(cond)


def poll_in_thread(agent_id='player-hub', username='adm211'):
    """Запускает long-poll в потоке; возвращает функцию ожидания результата.

    Пустое username — опрос без живой закладки за ним: плеер шлёт его именно
    так, когда вкладка Chaturbate потеряна.
    """
    box = {}

    def run():
        start = time.time()
        box['answer'] = server.cb_agent_poll(agent_id, username, False,
                                            server.CB_AGENT_VERSION)
        box['took'] = time.time() - start

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    time.sleep(0.15)               # даём потоку дойти до ожидания

    def wait(limit=5.0):
        thread.join(limit)
        return box

    return wait


def arm():
    with server.CB_LOCK:
        server.cb_reset()
        server.CB_SPY.update({'state': 'spying', 'room': ROOM, 'price': 6,
                              'started': time.time(), 'last_balance': 300,
                              'balance_start': 300})
    server.CB_CMDS.clear()


def session(answer):
    """Сессия в ответе poll-а лежит уровнем ниже: answer['spy'] — весь снимок
    ({'agent': …, 'spy': …}), ровно так его и разбирает агент."""
    return answer['spy']['spy']


def bump(balance):
    with server.CB_COND:
        server._cb_apply_balance({'balance': balance}, ROOM)
        server.CB_COND.notify_all()


# 1. Ничего не менялось — досиживаем до конца, а не крутимся вхолостую.
arm()
wait = poll_in_thread()
box = wait()
check('без изменений: держит до конца срока', box.get('took', 0) >= 1.5,
      f'вернулся через {box.get("took", 0):.2f} с')
check('без изменений: команды нет', box['answer']['cmd'] is None)

# 2. Расход изменился — возвращаемся сразу, не дожидаясь конца.
arm()
wait = poll_in_thread()
time.sleep(0.3)
bump(294)
box = wait()
check('расход изменился: вернулся досрочно', box.get('took', 9) < 1.2,
      f'вернулся через {box.get("took", 9):.2f} с')
check('расход изменился: в снимке новые цифры',
      session(box['answer'])['tokens'] == 6
      and session(box['answer'])['balance_now'] == 294,
      str(session(box['answer'])))

# 3. Появилась команда — возвращаемся с ней немедленно.
arm()
wait = poll_in_thread()
time.sleep(0.3)
server.cb_queue('url_get', ROOM)
box = wait()
check('команда: вернулся досрочно', box.get('took', 9) < 1.2,
      f'вернулся через {box.get("took", 9):.2f} с')
check('команда: доставлена агенту',
      (box['answer']['cmd'] or {}).get('act') == 'url_get', str(box['answer']['cmd']))

# 4. Смена состояния сессии тоже будит ожидание.
arm()
wait = poll_in_thread()
time.sleep(0.3)
with server.CB_COND:
    server.CB_SPY['state'] = 'stopping'
    server.CB_COND.notify_all()
box = wait()
check('смена состояния: вернулся досрочно', box.get('took', 9) < 1.2,
      f'вернулся через {box.get("took", 9):.2f} с')
check('смена состояния: снимок это показывает',
      session(box['answer'])['state'] == 'stopping')

# 5. Опрос без живой закладки команду забирать не вправе.
arm()
wait = poll_in_thread(agent_id='deadhub', username='')
time.sleep(0.3)
server.cb_queue('spy_stop', ROOM)
box = wait()
check('без закладки: команду не отдали', box['answer']['cmd'] is None,
      str(box['answer']['cmd']))
check('без закладки: команда осталась в очереди', len(server.CB_CMDS) == 1,
      str(list(server.CB_CMDS)))

# 6. …и она достаётся следующему опросу, за которым закладка есть.
wait = poll_in_thread()
box = wait()
check('живая закладка: получила ту же команду',
      (box['answer']['cmd'] or {}).get('act') == 'spy_stop',
      str(box['answer']['cmd']))

# 7. Само течение времени поводом для возврата быть не должно.
arm()
wait = poll_in_thread()
time.sleep(0.6)                    # spy['seconds'] за это время вырос
box = wait()
check('течение времени: не будит ожидание', box.get('took', 0) >= 1.5,
      f'вернулся через {box.get("took", 0):.2f} с')

# 8. Стоп из плеера не блокируется на агенте: команда сразу в ответе,
#    плеер шлёт её вкладке, не дожидаясь следующего long-poll
#    (blondie_dirty_squirt 30.08 16:40: abort poll терял уже claimed стоп).
arm()
result = server.cb_stop_now('кнопка в плеере', wait=False)
check('стоп без ожидания: сразу ok, не idle',
      result.get('ok') and result.get('stopping') == ROOM and not result.get('idle'),
      str(result))
cid = result.get('stop_cmd')
cmd = server.CB_CMDS.get(cid) if cid else None
check('стоп без ожидания: команда в очереди и в ответе',
      bool(cid) and cmd and cmd.get('act') == 'spy_stop' and cmd.get('room') == ROOM,
      str({'result': result, 'cmd': cmd}))

print('\nИТОГ:', 'всё сошлось' if ok else 'ЕСТЬ ПРОВАЛЫ')
sys.exit(0 if ok else 1)
