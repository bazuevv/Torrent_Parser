#!/usr/bin/env python3
"""Сверка словарей с самой страницей.

Показывает, каких ключей в переводе не хватает, какие в нём лишние (остались
от удалённых кусков интерфейса) и где разошлись подстановки — {имя} в русском
и в переводе должны совпадать, иначе на месте значения останется само имя в
фигурных скобках.

    python3 lang/check.py          # все словари в папке
    python3 lang/check.py en       # только английский
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(os.path.dirname(HERE), 'player.html')


def russian():
    """Русский словарь читается прямо из страницы: он там и живёт."""
    src = open(PAGE, encoding='utf-8').read()
    block = re.search(r'const RU = \{\n(.*?)\n  \};', src, re.S)
    if not block:
        sys.exit('в player.html не нашёлся словарь RU')
    return dict(re.findall(r"^    '([^']+)': '(.*)',$", block.group(1), re.M))


def slots(line):
    return set(re.findall(r'\{(\w+)\}', line))


def check(code, ru):
    path = os.path.join(HERE, f'{code}.json')
    words = json.load(open(path, encoding='utf-8'))

    missing = [k for k in ru if k not in words]
    extra = [k for k in words if k not in ru]
    broken = [(k, slots(ru[k]), slots(words[k]))
              for k in words if k in ru and slots(ru[k]) != slots(words[k])]

    done = len(ru) - len(missing)
    print(f'{code}: переведено {done} из {len(ru)} ({done * 100 // len(ru)}%)')
    if extra:
        print(f'  лишних ключей: {len(extra)} — их нет в странице')
        for k in extra[:10]:
            print(f'     {k}')
    if broken:
        print(f'  разошлись подстановки: {len(broken)}')
        for k, want, got in broken[:10]:
            print(f'     {k}: в русском {sorted(want)}, в переводе {sorted(got)}')
    if missing:
        print(f'  не переведено: {len(missing)}')
        for k in missing[:10]:
            print(f'     {k} = {ru[k][:60]}')
        if len(missing) > 10:
            print(f'     … и ещё {len(missing) - 10}')
    return not extra and not broken


def main():
    ru = russian()
    codes = sys.argv[1:] or sorted(
        name[:-5] for name in os.listdir(HERE) if name.endswith('.json'))
    if not codes:
        print('словарей в папке нет; русский лежит в самой странице')
        return 0
    ok = all(check(code, ru) for code in codes)
    # Неполный перевод — не ошибка: недостающее берётся из русского. Ошибка —
    # лишние ключи и разъехавшиеся подстановки: первое значит мусор, второе
    # выведет человеку {имя} вместо числа.
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
