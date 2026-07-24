"""CLI-запуск рассылки TON. Точка входа для cron и для веб-интерфейса.

Из корня репозитория:
    python3 -m ton_payout.run --mode highload
    python3 -m ton_payout.run --mode sequential --dry-run

Cron (раз в месяц, 3:00 первого числа) — секреты берутся из
ton_payout/config_secrets.py, так что в crontab их писать не нужно:
    0 3 1 * * cd /path/to/Torrent_Parser && venv/bin/python3 -m ton_payout.run --mode highload

Коды выхода: 0 — всё отправлено (или dry-run прошёл), 1 — ошибка,
2 — часть переводов не прошла (partial).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from . import db
from .payout_core import PayoutError, run_payout


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Рассылка TON по списку из БД")
    parser.add_argument(
        "--mode", choices=["sequential", "highload"], default="highload",
        help="sequential — по одному переводу (надёжно, медленно); "
             "highload — все одной транзакцией (быстро, дёшево). По умолчанию highload.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Только проверки и запись плана, без реальной отправки денег.",
    )
    parser.add_argument(
        "--triggered-by", default="cron",
        help="Метка инициатора для журнала (cron/web/...).",
    )
    args = parser.parse_args(argv)

    _configure_logging()

    try:
        run_id = asyncio.run(
            run_payout(
                mode=args.mode,
                dry_run=args.dry_run,
                triggered_by=args.triggered_by,
            )
        )
    except PayoutError as e:
        logging.getLogger("ton_payout").error("Рассылка не выполнена: %s", e)
        return 1

    run = db.get_run(run_id)
    status = run["status"] if run else "unknown"
    if status == "partial":
        return 2
    if status in ("error", "unknown"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
