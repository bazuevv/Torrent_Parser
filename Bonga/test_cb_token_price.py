"""Цены каталога Chaturbate: −1 у сайта значит «приват выключен».

Запуск: python3 Bonga/test_cb_token_price.py
"""
import os
import sys
import tempfile

os.environ['BONGA_REC_DIR'] = tempfile.mkdtemp(prefix='cbprice-')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402


def check(name, cond, extra=''):
    print(('  OK   ' if cond else '  ПРОВАЛ ') + name + (f' — {extra}' if extra else ''))
    return cond


failed = 0
failed += not check('обычная ставка', server._cb_token_price({'private_price': 90}, 'private_price') == 90)
failed += not check('строка из JSON', server._cb_token_price({'private_price': '42'}, 'private_price') == 42)
failed += not check('минус один — выключен', server._cb_token_price({'private_price': -1}, 'private_price') == 0)
failed += not check('ноль', server._cb_token_price({'private_price': 0}, 'private_price') == 0)
failed += not check('нет поля', server._cb_token_price({}, 'private_price') == 0)
failed += not check('мусор', server._cb_token_price({'private_price': 'x'}, 'private_price') == 0)
failed += not check('spy_show_price', server._cb_token_price({'spy_show_price': 12}, 'spy_show_price') == 12)

print()
sys.exit(1 if failed else 0)
