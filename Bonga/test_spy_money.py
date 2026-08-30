"""Проверка учёта расхода токенов за spy-сессию: точка отсчёта, накопление
и строка cb_money() для журнала. Агент заглушаем.

Запуск: python3 Bonga/test_spy_money.py

Сторожит два свойства, без которых по журналу нельзя сказать, за что списали:
точка отсчёта берётся из чтения агента перед платным входом (а не из кэша
CB_BALANCE, который мог быть снят в прошлой сессии), и пополнение баланса
посреди показа не уменьшает уже потраченное.
"""
import os
import sys
import tempfile
import time

os.environ['BONGA_REC_DIR'] = tempfile.mkdtemp(prefix='moneytest-')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402

ROOM = 'emmaember'
ok = True


def check(name, cond, extra=''):
    global ok
    print(('  OK   ' if cond else '  ПРОВАЛ ') + name + (f' — {extra}' if extra else ''))
    ok &= bool(cond)


def arm(price=6):
    server.cb_reset()
    server.CB_SPY.update({'state': 'starting', 'room': ROOM, 'price': price,
                          'started': time.time()})


# 1. Вход: баланс до входа становится точкой отсчёта, контрольное чтение
#    после входа сразу показывает первое списание.
arm()
before = server._cb_apply_balance({'balance': 217}, ROOM)
server.CB_SPY['balance_start'] = before
after = server._cb_apply_balance({'balance': 211}, ROOM)
check('вход: точка отсчёта = баланс до входа', server.CB_SPY['balance_start'] == 217,
      str(server.CB_SPY['balance_start']))
check('вход: контрольное чтение вернуло баланс', after == 211, str(after))
check('вход: первое списание учтено', server.CB_SPY['tokens_used'] == 6,
      str(server.CB_SPY['tokens_used']))

# 2. Опрос агента продолжает копить расход.
server._cb_apply_balance({'balance': 205}, ROOM)
check('опрос: расход накопился', server.CB_SPY['tokens_used'] == 12,
      str(server.CB_SPY['tokens_used']))

# 3. Пополнение посреди показа не стирает потраченное.
server._cb_apply_balance({'balance': 500}, ROOM)
check('пополнение: расход не уменьшился', server.CB_SPY['tokens_used'] == 12,
      str(server.CB_SPY['tokens_used']))
server._cb_apply_balance({'balance': 494}, ROOM)
check('пополнение: дальше считаем от нового баланса',
      server.CB_SPY['tokens_used'] == 18, str(server.CB_SPY['tokens_used']))

# 4. Строка для журнала называет цену, расход и баланс от-до.
line = server.cb_money()
check('журнал: цена в строке', '6 тк/мин' in line, line)
check('журнал: потрачено в строке', 'потрачено 18 тк' in line, line)
check('журнал: баланс от-до в строке', '217 → 494' in line, line)
check('журнал: длительность в секундах', 'показ 0 с' in line, line)

# 5. Контрольное чтение не состоялось — точка отсчёта уцелела, расход нулевой.
arm()
server.CB_SPY['balance_start'] = server._cb_apply_balance({'balance': 300}, ROOM)
check('баланс не прочитан: возвращает None',
      server._cb_apply_balance({'balance': None}, ROOM) is None)
check('баланс не прочитан: точка отсчёта цела', server.CB_SPY['balance_start'] == 300)
check('баланс не прочитан: расход не выдуман', server.CB_SPY['tokens_used'] == 0,
      str(server.CB_SPY['tokens_used']))

# 6. Чужая комната не должна попадать в расход этой сессии.
arm()
server.CB_SPY['balance_start'] = server._cb_apply_balance({'balance': 100}, ROOM)
server._cb_apply_balance({'balance': 40}, 'someoneelse')
check('чужая комната: расход не тронут', server.CB_SPY['tokens_used'] == 0,
      str(server.CB_SPY['tokens_used']))

print('\nИТОГ:', 'всё сошлось' if ok else 'ЕСТЬ ПРОВАЛЫ')
sys.exit(0 if ok else 1)
