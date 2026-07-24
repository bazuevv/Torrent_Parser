#!/usr/bin/env python3
"""Заполняет girls.ton_address РЕАЛЬНЫМИ TON-адресами (модель A).

Адрес каждой записи выводится детерминированно из master-сида
(TON_MASTER_MNEMONIC) с subwallet_id = girls.id. Свойства:
  * один и тот же id всегда даёт один и тот же адрес (обратная совместимость:
    существующие записи можно безопасно пере-вывести — адрес не изменится);
  * ключами от всех адресов владеет держатель master-сида (кастодиально),
    поэтому на эти адреса можно реально принимать и с них выводить средства.

Master-сид задаётся в ton_payout/config_secrets.py (или env TON_MASTER_MNEMONIC).

Примеры (из корня репозитория, venv с установленным tonutils):
    venv/bin/python3 gen_ton_addresses.py --dry-run    # показать план, не писать
    venv/bin/python3 gen_ton_addresses.py              # заполнить только пустые (NULL)
    venv/bin/python3 gen_ton_addresses.py --overwrite  # пере-вывести всем (идемпотентно)
    venv/bin/python3 gen_ton_addresses.py --limit 10   # не больше 10 записей
    venv/bin/python3 gen_ton_addresses.py --clear      # очистить все адреса (вернуть NULL)
"""

import argparse
import sys

import db
from ton_payout.addresses import AddressDeriverError, derive_address


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Детерминированный вывод реальных TON-адресов в girls.ton_address (модель A)."
    )
    p.add_argument("--overwrite", action="store_true",
                   help="пере-вывести адрес и у тех, у кого он уже задан (идемпотентно)")
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
    try:
        for r in rows:
            addr = derive_address(int(r["id"]))  # subwallet_id = girls.id
            if args.dry_run:
                print(f"[dry-run] #{r['id']} {r['username']} -> {addr}")
            else:
                upd.execute("UPDATE girls SET ton_address = %s WHERE id = %s", (addr, r["id"]))
                count += 1
    except AddressDeriverError as e:
        upd.close()
        conn.close()
        print(f"Ошибка: {e}", file=sys.stderr)
        return 1

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
