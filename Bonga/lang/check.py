#!/usr/bin/env python3
"""Сверка словарей между собой и со страницей.

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
    """Русский — такой же файл, как остальные, и служит образцом для сверки."""
    return json.load(open(os.path.join(HERE, 'ru.json'), encoding='utf-8'))


def used_by_page(ru):
    """Ключи, которые страница на самом деле спрашивает.

    Нужны, чтобы ловить обратное расхождение: ключ есть в словарях, а страница
    его уже не просит — такой мусор иначе живёт годами.

    Две тонкости, на которых проверка врала:

    — комментарии выкидываем, иначе пример из пояснения к t() считается
      настоящим вызовом и требует несуществующего ключа;
    — часть ключей собирается склейкой: t('отчёт.' + key). Такие видны только
      как приставка, поэтому все ключи с ней считаем задействованными."""
    src = open(PAGE, encoding='utf-8').read()
    # Атрибуты ищем в исходном тексте: грубая вырезка комментариев съедала
    # заодно шаблон ячейки — он идёт сразу за пояснением, а «*/» встречается
    # и внутри строк. Вызовы t() ищем в очищенном: там пример из комментария
    # иначе сойдёт за настоящий вызов.
    keys = set(re.findall(r'data-i18n(?:-title|-ph|-aria|-tip)?="([^"]+)"', src))
    bare = re.sub(r'/\*.*?\*/', '', src, flags=re.S)
    bare = re.sub(r'(?m)^\s*//.*$', '', bare)
    keys |= set(re.findall(r"(?<![\w.$])t\('([^']+)'", bare))

    prefixes = {k for k in keys if k.endswith('.')}
    keys = {k for k in keys
            if re.match(r'^[а-яёA-Za-z]+\.', k) and not k.endswith('.')}
    for prefix in prefixes:
        keys |= {k for k in ru if k.startswith(prefix)}
    return keys


def slots(line):
    # Тот же шаблон, что в странице: «всё до закрывающей скобки». В Python \w
    # юникодный и поймал бы русские имена, в JavaScript — нет, и проверка,
    # написанная иначе, пропустила бы ровно ту ошибку, ради которой она есть.
    return set(re.findall(r'\{([^{}\s]+)\}', line))


# Знаки, которым в переводе взяться неоткуда: кириллица (кроме русского и
# украинского) и иероглифы — их ни один из наших языков не использует. Появиться
# они могут только по недосмотру, и глазами такое не находится: одна опечатка
# посреди двадцати трёх тысяч знаков.
CYRILLIC = re.compile(r'[А-Яа-яЁёІіЇїЄєҐґ]')
CJK = re.compile(r'[　-鿿가-힯]')
# Названия языков пишутся на них самих и в переводе не меняются: в английском
# списке тоже стоит «Русский», а не «Russian».
ENDONYMS = {'настройки.lang.вариант.ru'}


def strays(code, key, line):
    """Посторонние знаки — но вне {подстановок}: имена там русские всегда."""
    if key in ENDONYMS:
        return set()
    bare = re.sub(r'\{[^{}\s]+\}', '', line)
    found = set(CJK.findall(bare))
    if code not in ('ru', 'uk'):
        found |= set(CYRILLIC.findall(bare))
    return found


def check(code, ru):
    path = os.path.join(HERE, f'{code}.json')
    words = json.load(open(path, encoding='utf-8'))

    missing = [k for k in ru if k not in words]
    extra = [k for k in words if k not in ru]
    broken = [(k, slots(ru[k]), slots(words[k]))
              for k in words if k in ru and slots(ru[k]) != slots(words[k])]
    dirty = [(k, sorted(strays(code, k, words[k]))) for k in words
             if strays(code, k, words[k])]

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
    if dirty:
        print(f'  посторонние знаки: {len(dirty)}')
        for k, chars in dirty[:10]:
            print(f'     {k}: {" ".join(chars)} — {words[k][:60]}')
    if missing:
        print(f'  не переведено: {len(missing)}')
        for k in missing[:10]:
            print(f'     {k} = {ru[k][:60]}')
        if len(missing) > 10:
            print(f'     … и ещё {len(missing) - 10}')
    return not extra and not broken and not dirty


def main():
    ru = russian()
    used = used_by_page(ru)
    lost = sorted(used - set(ru))
    stale = sorted(set(ru) - used)
    if lost:
        print(f'страница просит ключи, которых нет в ru.json: {len(lost)}')
        for k in lost[:10]:
            print(f'   {k}')
    if stale:
        print(f'в ru.json есть ключи, которых страница не просит: {len(stale)}')
        for k in stale[:10]:
            print(f'   {k}')

    codes = sys.argv[1:] or sorted(
        name[:-5] for name in os.listdir(HERE)
        if name.endswith('.json') and name != 'ru.json')
    if not codes:
        print(f'ru.json: {len(ru)} ключей, других словарей в папке нет')
        return 0 if not lost and not stale else 1
    # Список, а не генератор: all() ленив и обрывается на первом «плохо» —
    # один сбойный словарь прятал бы все следующие, и правились бы они по
    # одному за прогон.
    ok = all([check(code, ru) for code in codes]) and not lost and not stale
    # Неполный перевод — не ошибка: недостающее берётся из русского. Ошибка —
    # лишние ключи и разъехавшиеся подстановки: первое значит мусор, второе
    # выведет человеку {имя} вместо числа.
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
