"""Слой БД для рассылки TON.

Три таблицы в той же MySQL, что и у Torrent_Parser:
  * ton_recipients      — управляемый список получателей (адрес, сумма, коммент);
  * ton_payout_runs     — журнал запусков рассылки (когда, каким кошельком, итог);
  * ton_payout_run_items — построчный результат по каждому получателю в запуске.

Суммы хранятся в DECIMAL(20,9) — 9 знаков после запятой соответствуют нанотонам
(1 TON = 1e9 nanoton), без потерь точности float.
"""

from __future__ import annotations

from decimal import Decimal

import mysql.connector

from . import config


def get_connection():
    return mysql.connector.connect(
        host=config.DB_HOST,
        port=config.DB_PORT,
        database=config.DB_NAME,
        user=config.DB_USER,
        password=config.DB_PASSWORD,
        charset="utf8mb4",
    )


def init_db() -> None:
    """Создаёт таблицы, если их ещё нет. Безопасно вызывать многократно."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS ton_recipients (
            id         INT AUTO_INCREMENT PRIMARY KEY,
            address    VARCHAR(70)     NOT NULL,
            amount     DECIMAL(20,9)   NOT NULL,
            comment    VARCHAR(500),
            is_active  TINYINT(1)      NOT NULL DEFAULT 1,
            created_at TIMESTAMP       DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP       DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uq_ton_recipient_address (address)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS ton_payout_runs (
            id               INT AUTO_INCREMENT PRIMARY KEY,
            started_at       DATETIME        NOT NULL,
            finished_at      DATETIME,
            mode             ENUM('sequential','highload') NOT NULL,
            network          ENUM('mainnet','testnet')     NOT NULL,
            dry_run          TINYINT(1)      NOT NULL DEFAULT 0,
            status           ENUM('running','done','error','partial') NOT NULL DEFAULT 'running',
            total_recipients INT             NOT NULL DEFAULT 0,
            total_amount     DECIMAL(20,9)   NOT NULL DEFAULT 0,
            wallet_address   VARCHAR(70),
            tx_hash          VARCHAR(100),
            error_message    TEXT,
            triggered_by     VARCHAR(50)     NOT NULL DEFAULT 'web'
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS ton_payout_run_items (
            id            INT AUTO_INCREMENT PRIMARY KEY,
            run_id        INT             NOT NULL,
            recipient_id  INT,
            address       VARCHAR(70)     NOT NULL,
            amount        DECIMAL(20,9)   NOT NULL,
            comment       VARCHAR(500),
            status        ENUM('pending','sent','failed','skipped') NOT NULL DEFAULT 'pending',
            tx_hash       VARCHAR(100),
            error_message TEXT,
            KEY idx_run_items_run_id (run_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )

    conn.commit()
    cursor.close()
    conn.close()


# ── Получатели ──────────────────────────────────────────────────────────────

def list_recipients(active_only: bool = False) -> list[dict]:
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    sql = "SELECT * FROM ton_recipients"
    if active_only:
        sql += " WHERE is_active = 1"
    sql += " ORDER BY id"
    cursor.execute(sql)
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return rows


def get_recipient(recipient_id: int) -> dict | None:
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM ton_recipients WHERE id = %s", (recipient_id,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row


def add_recipient(address: str, amount: Decimal | str, comment: str | None,
                  is_active: bool = True) -> int:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO ton_recipients (address, amount, comment, is_active) "
        "VALUES (%s, %s, %s, %s)",
        (address, str(amount), comment or None, 1 if is_active else 0),
    )
    conn.commit()
    new_id = cursor.lastrowid
    cursor.close()
    conn.close()
    return new_id


def update_recipient(recipient_id: int, address: str, amount: Decimal | str,
                     comment: str | None, is_active: bool) -> None:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE ton_recipients SET address = %s, amount = %s, comment = %s, "
        "is_active = %s WHERE id = %s",
        (address, str(amount), comment or None, 1 if is_active else 0, recipient_id),
    )
    conn.commit()
    cursor.close()
    conn.close()


def set_recipient_active(recipient_id: int, is_active: bool) -> None:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE ton_recipients SET is_active = %s WHERE id = %s",
        (1 if is_active else 0, recipient_id),
    )
    conn.commit()
    cursor.close()
    conn.close()


def delete_recipient(recipient_id: int) -> None:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM ton_recipients WHERE id = %s", (recipient_id,))
    conn.commit()
    cursor.close()
    conn.close()


# ── Запуски рассылки ────────────────────────────────────────────────────────

def create_run(mode: str, network: str, dry_run: bool, total_recipients: int,
               total_amount: Decimal | str, triggered_by: str = "web") -> int:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO ton_payout_runs "
        "(started_at, mode, network, dry_run, status, total_recipients, "
        " total_amount, triggered_by) "
        "VALUES (NOW(), %s, %s, %s, 'running', %s, %s, %s)",
        (mode, network, 1 if dry_run else 0, total_recipients,
         str(total_amount), triggered_by),
    )
    conn.commit()
    run_id = cursor.lastrowid
    cursor.close()
    conn.close()
    return run_id


def set_run_wallet(run_id: int, wallet_address: str) -> None:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE ton_payout_runs SET wallet_address = %s WHERE id = %s",
        (wallet_address, run_id),
    )
    conn.commit()
    cursor.close()
    conn.close()


def finish_run(run_id: int, status: str, tx_hash: str | None = None,
               error_message: str | None = None) -> None:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE ton_payout_runs SET finished_at = NOW(), status = %s, "
        "tx_hash = %s, error_message = %s WHERE id = %s",
        (status, tx_hash, error_message, run_id),
    )
    conn.commit()
    cursor.close()
    conn.close()


def add_run_item(run_id: int, recipient_id: int | None, address: str,
                 amount: Decimal | str, comment: str | None) -> int:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO ton_payout_run_items "
        "(run_id, recipient_id, address, amount, comment) "
        "VALUES (%s, %s, %s, %s, %s)",
        (run_id, recipient_id, address, str(amount), comment or None),
    )
    conn.commit()
    item_id = cursor.lastrowid
    cursor.close()
    conn.close()
    return item_id


def update_run_item(item_id: int, status: str, tx_hash: str | None = None,
                    error_message: str | None = None) -> None:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE ton_payout_run_items SET status = %s, tx_hash = %s, "
        "error_message = %s WHERE id = %s",
        (status, tx_hash, error_message, item_id),
    )
    conn.commit()
    cursor.close()
    conn.close()


def list_runs(limit: int = 50) -> list[dict]:
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT * FROM ton_payout_runs ORDER BY id DESC LIMIT %s", (limit,)
    )
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return rows


def get_run(run_id: int) -> dict | None:
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM ton_payout_runs WHERE id = %s", (run_id,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row


def get_run_items(run_id: int) -> list[dict]:
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT * FROM ton_payout_run_items WHERE run_id = %s ORDER BY id",
        (run_id,),
    )
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return rows


def has_running_payout() -> bool:
    """Есть ли незавершённый (не dry-run) запуск — для защиты от двойного старта."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT 1 FROM ton_payout_runs WHERE status = 'running' AND dry_run = 0 LIMIT 1"
    )
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row is not None
