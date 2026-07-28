#!/usr/bin/env python3
"""Сокращённый формат из 4 групп: первая, две центральные, последняя."""

import os

from addr_to_number import to_full_number, to_account_number, encode_address

A = "UQBZ8NRvBw-5dRxoF3FWz5IrQ2pm23HWEYCHGvJVtZY0JQFl"


def split87(n: str, before: int) -> list[str]:
    """87 цифр → 22 группы: `before` по 4, затем 3, затем остаток по 4."""
    head = [n[i * 4:(i + 1) * 4] for i in range(before)]
    mid = n[before * 4:before * 4 + 3]
    rest = n[before * 4 + 3:]
    return head + [mid] + [rest[i:i + 4] for i in range(0, len(rest), 4)]


def short4(groups: list[str]) -> str:
    """Первая + две центральные + последняя (из 22 групп центр = #11 и #12)."""
    c = len(groups) // 2
    return "-".join([groups[0], groups[c - 1], groups[c], groups[-1]])


# --- сколько старших цифр 87-значного номера НЕ несут информации -------------
base = 0x51 << 280                       # UQ basechain, hash=0, crc=0
top = (0x51 << 280) | (2**272 - 1)       # hash и crc максимальны (вес байта 2 = 2**264)
b, t = str(base).zfill(87), str(top).zfill(87)
common = next(i for i, (x, y) in enumerate(zip(b, t)) if x != y)
print(f"UQ basechain, минимально возможный номер : {b[:16]}…")
print(f"UQ basechain, максимально возможный номер: {t[:16]}…")
print(f"→ первые {common} цифр ОДИНАКОВЫ у всех таких адресов (это байты флагов и workchain)")
print()

n87 = to_full_number(A)
for before, label in ((10, "A"), (11, "B")):
    g = split87(n87, before)
    s = short4(g)
    print(f"вариант {label}: {s}")
    print(f"           группы #1, #{len(g)//2}, #{len(g)//2+1}, #{len(g)} — {len(s.replace('-',''))} цифр, "
          f"из них информативных {len(s.replace('-','')) - common}")
print()

# --- эмпирика: коллизии ------------------------------------------------------
N = 300_000
for before, label in ((10, "A"), (11, "B")):
    seen = set()
    for _ in range(N):
        n = to_full_number(encode_address(0x51, 0, os.urandom(32)))
        seen.add(short4(split87(n, before)))
    print(f"вариант {label}: {N} адресов → {len(seen)} уникальных, коллизий {N - len(seen)}")

# --- исправленный вариант: строим из номера варианта 2 (только hash part) -----
print()
print("То же, но из 78-значного номера варианта 2 (без байтов флагов):")


def split78(n: str) -> list[str]:
    """78 цифр → 20 групп: 9 по 4, затем 2, затем 10 по 4."""
    return ([n[i * 4:(i + 1) * 4] for i in range(9)] + [n[36:38]]
            + [n[38 + i * 4:38 + (i + 1) * 4] for i in range(10)])


g2 = split78(to_account_number(A))
print(f"  {short4(g2)}")
seen = set()
for _ in range(N):
    n = to_account_number(encode_address(0x51, 0, os.urandom(32)))
    seen.add(short4(split78(n)))
print(f"  {N} адресов → {len(seen)} уникальных, коллизий {N - len(seen)}")
