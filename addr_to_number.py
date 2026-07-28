#!/usr/bin/env python3
"""Превращает TON-адрес в число тремя способами (варианты 1, 2, 3).

Структура user-friendly TON-адреса (48 символов base64url = 36 байт):
    [0]      флаги      0x11 EQ / 0x51 UQ / +0x80 testnet
    [1]      workchain  0x00 basechain, 0xFF masterchain (-1)
    [2:34]   hash part  32 байта — собственно уникальный идентификатор аккаунта
    [34:36]  CRC16-XMODEM от первых 34 байт
"""

import base64
import hashlib
import sys

FULL_DIGITS = 87      # 2**288-1 → 87 знаков
ACCOUNT_DIGITS = 78   # 2**256-1 → 78 знаков


def crc16_xmodem(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def decode_address(address: str) -> bytes:
    """base64url/base64 (48 символов) → 36 байт, с проверкой CRC."""
    s = address.strip().replace("+", "-").replace("/", "_")
    if len(s) != 48:
        raise ValueError(f"ожидалось 48 символов, получено {len(s)}")
    raw = base64.urlsafe_b64decode(s)
    crc_actual = int.from_bytes(raw[34:36], "big")
    crc_expected = crc16_xmodem(raw[:34])
    if crc_actual != crc_expected:
        raise ValueError(f"CRC не сходится: в адресе {crc_actual:#06x}, посчитано {crc_expected:#06x}")
    return raw


def encode_address(flags: int, workchain: int, hash_part: bytes) -> str:
    """36 байт → строка адреса (с пересчитанным CRC)."""
    body = bytes([flags, workchain & 0xFF]) + hash_part
    raw = body + crc16_xmodem(body).to_bytes(2, "big")
    return base64.urlsafe_b64encode(raw).decode()


# ---------------------------------------------------------------- вариант 1
def to_full_number(address: str) -> str:
    """Все 36 байт как одно число. Полностью обратимо, коллизий нет."""
    return str(int.from_bytes(decode_address(address), "big")).zfill(FULL_DIGITS)


def from_full_number(number: str) -> str:
    raw = int(number).to_bytes(36, "big")
    crc_expected = crc16_xmodem(raw[:34])
    if int.from_bytes(raw[34:36], "big") != crc_expected:
        raise ValueError("CRC не сходится — число повреждено")
    return base64.urlsafe_b64encode(raw).decode()


# ---------------------------------------------------------------- вариант 2
def to_account_number(address: str) -> str:
    """Только hash part (32 байта). UQ/EQ одного кошелька дают ОДНО число."""
    return str(int.from_bytes(decode_address(address)[2:34], "big")).zfill(ACCOUNT_DIGITS)


def from_account_number(number: str, workchain: int = 0,
                        bounceable: bool = False, testnet: bool = False) -> str:
    hash_part = int(number).to_bytes(32, "big")
    flags = (0x11 if bounceable else 0x51) | (0x80 if testnet else 0x00)
    return encode_address(flags, workchain, hash_part)


# ---------------------------------------------------------------- вариант 3
def luhn_check_digit(payload: str) -> str:
    total = 0
    for i, ch in enumerate(reversed(payload)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return str((10 - total % 10) % 10)


def luhn_valid(number: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(number)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def to_short_number(address: str, digits: int = 18, check_digit: bool = False) -> str:
    """Короткий номер через SHA-256 от hash part. НЕОБРАТИМО, возможны коллизии."""
    h = hashlib.sha256(decode_address(address)[2:34]).digest()
    payload = str(int.from_bytes(h, "big") % 10 ** digits).zfill(digits)
    return payload + luhn_check_digit(payload) if check_digit else payload


# ---------------------------------------------------------------- отчёт
def report(address: str) -> None:
    raw = decode_address(address)
    flags, wc = raw[0], raw[1]
    bounceable = not (flags & 0x40)
    testnet = bool(flags & 0x80)

    print(f"Адрес: {address}")
    print("=" * 78)
    print("РАЗБОР")
    print(f"  флаги      : {flags:#04x}  ({'bounceable EQ' if bounceable else 'non-bounceable UQ'}"
          f"{', testnet' if testnet else ', mainnet'})")
    print(f"  workchain  : {wc if wc != 0xFF else -1}")
    print(f"  hash part  : {raw[2:34].hex()}")
    print(f"  CRC16      : {int.from_bytes(raw[34:36], 'big'):#06x}  (сошёлся)")
    print(f"  raw-форма  : {wc if wc != 0xFF else -1}:{raw[2:34].hex()}")

    n1 = to_full_number(address)
    print()
    print(f"ВАРИАНТ 1 — все 36 байт ({len(n1)} цифр, обратимо, коллизий нет)")
    print(f"  {n1}")
    print(f"  обратно    : {from_full_number(n1)}")
    print(f"  совпадает  : {from_full_number(n1) == address.strip()}")

    n2 = to_account_number(address)
    print()
    print(f"ВАРИАНТ 2 — только hash part ({len(n2)} цифр, обратимо, UQ==EQ)")
    print(f"  {n2}")
    back = from_account_number(n2, workchain=(wc if wc != 0xFF else -1),
                               bounceable=bounceable, testnet=testnet)
    print(f"  обратно    : {back}")
    print(f"  совпадает  : {back == address.strip()}")
    eq = encode_address(0x11 | (0x80 if testnet else 0), wc, raw[2:34])
    uq = encode_address(0x51 | (0x80 if testnet else 0), wc, raw[2:34])
    print(f"  EQ-форма   : {eq}")
    print(f"  UQ-форма   : {uq}")
    print(f"  номер тот же у EQ и UQ: {to_account_number(eq) == to_account_number(uq) == n2}")

    print()
    print("ВАРИАНТ 3 — короткий номер SHA-256 (НЕОБРАТИМО, нужен UNIQUE в БД)")
    for d in (10, 12, 16, 18):
        print(f"  {d:>2} цифр   : {to_short_number(address, d)}")
    with_cd = to_short_number(address, 16, check_digit=True)
    print(f"  16+Луна   : {with_cd}   (проверка Луна: {luhn_valid(with_cd)})")
    print(f"  опечатка  : {with_cd[:-2] + str((int(with_cd[-2]) + 1) % 10) + with_cd[-1]}"
          f"   (проверка Луна: "
          f"{luhn_valid(with_cd[:-2] + str((int(with_cd[-2]) + 1) % 10) + with_cd[-1])} — опечатка поймана)")


if __name__ == "__main__":
    addr = sys.argv[1] if len(sys.argv) > 1 else "UQBZ8NRvBw-5dRxoF3FWz5IrQ2pm23HWEYCHGvJVtZY0JQFl"
    report(addr)
