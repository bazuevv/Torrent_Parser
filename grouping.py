#!/usr/bin/env python3
"""Группировка 87-значного номера (вариант 1) по 4 цифры с 3-значной группой в центре.

87 = 21×4 + 3 → 22 группы. Точный центр недостижим (нужно было бы 10.5 групп
с каждой стороны), поэтому 3-значная группа ставится в позицию 11 или 12 из 22.
"""

import os

from addr_to_number import to_full_number, from_full_number, encode_address

DIGITS = 87
GROUPED_LEN = 108  # 87 цифр + 21 дефис


def group(number: str, before: int = 11) -> str:
    """Разбивает 87-значный номер: `before` групп по 4, затем 3 цифры, затем остаток.

    :param before: сколько 4-значных групп идёт до центральной 3-значной (10 или 11).
    """
    if len(number) != DIGITS:
        raise ValueError(f"ожидалось {DIGITS} цифр, получено {len(number)}")
    head = [number[i * 4:(i + 1) * 4] for i in range(before)]
    mid = number[before * 4:before * 4 + 3]
    rest = number[before * 4 + 3:]
    tail = [rest[i:i + 4] for i in range(0, len(rest), 4)]
    return "-".join(head + [mid] + tail)


def ungroup(grouped: str) -> str:
    """Обратно в 87 голых цифр."""
    n = grouped.replace("-", "")
    if len(n) != DIGITS or not n.isdigit():
        raise ValueError(f"после снятия дефисов ожидалось {DIGITS} цифр, получено {len(n)}")
    return n


def address_to_grouped(address: str, before: int = 11) -> str:
    return group(to_full_number(address), before)


def grouped_to_address(grouped: str) -> str:
    return from_full_number(ungroup(grouped))


if __name__ == "__main__":
    A = "UQBZ8NRvBw-5dRxoF3FWz5IrQ2pm23HWEYCHGvJVtZY0JQFl"
    n = to_full_number(A)

    for before, label in ((10, "вариант A — 10 групп | 3 | 11 групп"),
                          (11, "вариант B — 11 групп | 3 | 10 групп")):
        g = group(n, before)
        pos = before * 4 + 1
        print(label)
        print(f"  {g}")
        print(f"  длина {len(g)} символов, 3-значная группа занимает цифры {pos}–{pos + 2} из 87")
        print(f"  round-trip: {grouped_to_address(g) == A}")
        print()

    # EQ-адрес: 86 значащих цифр, padding до 87 → разметка не едет
    h = bytes.fromhex("59f0d46f070fb9751c68177156cf922b436a66db71d61180871af255b5963425")
    eq = encode_address(0x11, 0, h)
    print("EQ-адрес (86 значащих цифр, дополнен нулём):")
    print(f"  {eq}")
    print(f"  {address_to_grouped(eq)}")
    print(f"  round-trip: {grouped_to_address(address_to_grouped(eq)) == eq}")
    print()

    # массовая проверка
    bad = 0
    for _ in range(50000):
        a = encode_address(0x11 if os.urandom(1)[0] & 1 else 0x51, 0, os.urandom(32))
        g = address_to_grouped(a)
        if len(g) != GROUPED_LEN or grouped_to_address(g) != a:
            bad += 1
    print(f"50000 случайных адресов: длина всегда {GROUPED_LEN}, расхождений round-trip: {bad}")
