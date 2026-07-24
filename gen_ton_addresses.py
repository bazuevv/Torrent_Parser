#!/usr/bin/env python3
"""Заполняет girls.ton_address случайными TON-адресами — для демонстрации того,
как выглядит заполненное поле адреса в профиле (вместо «Unknown»).

Адреса генерируются в корректном user-friendly формате (UQ…, 48 символов,
с валидной CRC16) через ton_core, но НЕ привязаны к реальным кошелькам:
это тестовые значения ТОЛЬКО для внешнего вида. Отправлять на них ничего нельзя.

Примеры (из корня репозитория, тем же venv, где установлен tonutils/ton_core):
    venv/bin/python3 gen_ton_addresses.py --dry-run    # показать план, ничего не писать
    venv/bin/python3 gen_ton_addresses.py              # заполнить только пустые (NULL)
    venv/bin/python3 gen_ton_addresses.py --overwrite  # перегенерировать всем
    venv/bin/python3 gen_ton_addresses.py --limit 10   # не больше 10 записей
    venv/bin/python3 gen_ton_addresses.py --clear      # очистить все адреса (вернуть NULL)
"""

import argparse
import os
import sys

import db
from ton_core import Address


def random_ton_address() -> str:
    """Случайный адрес в user-friendly non-bounceable формате (UQ…)."""
    return Address((0, os.urandom(32))).to_str(is_bounceable=False)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Генерация случайных TON-адресов в girls.ton_address (демо UI)."
    )
    p.add_argument("--overwrite", action="store_true",
                   help="перегенерировать адрес и у тех, у кого он уже задан")
    p.add_argument("--clear", action="store_true",
                   help="очистить ton_address у всех (вернуть NULL) и выйти")
    p.add_argument("--limit", type=int, default=0,
                   help="максимум записей за запуск (0 = без ограничения)")
    p.add_argument("--dry-run", action="store_true",
                   help="показать план, ничего не записывая в БД")
    args = p.parse_args(argv)

    conn = db.get_db_connection()

    # Режим очистки
    if args.clear:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM girls WHERE ton_address IS NOT NULL")
        n = cur.fetchone()[0]
        if args.dry_run:
            print(f"[dry-run] очистило бы ton_address у {n} записей")
        else:
            cur.execute("UPDATE girls SET ton_address = NULL WHERE ton_address IS NOT NULL")
            conn.commit()
            print(f"Очищено адресов: {n}")
        cur.close()
        conn.close()
        return 0

    # Выборка кандидатов
    where = "" if args.overwrite else "WHERE ton_address IS NULL"
    sql = f"SELECT id, username FROM girls {where} ORDER BY id"
    if args.limit > 0:
        sql += f" LIMIT {int(args.limit)}"

    cur = conn.cursor(dictionary=True)
    cur.execute(sql)
    rows = cur.fetchall()
    cur.close()

    if not rows:
        print("Нечего заполнять (нет подходящих записей).")
        conn.close()
        return 0

    upd = conn.cursor()
    count = 0
    for r in rows:
        addr = random_ton_address()
        if args.dry_run:
            print(f"[dry-run] #{r['id']} {r['username']} -> {addr}")
        else:
            upd.execute("UPDATE girls SET ton_address = %s WHERE id = %s", (addr, r["id"]))
            count += 1
    if not args.dry_run:
        conn.commit()
    upd.close()
    conn.close()

    if args.dry_run:
        print(f"[dry-run] обновило бы записей: {len(rows)}")
    else:
        print(f"Обновлено записей: {count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
