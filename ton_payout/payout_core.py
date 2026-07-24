"""Единая логика рассылки TON: читает активных получателей из БД, отправляет
средства и пишет результат в журнал (ton_payout_runs / ton_payout_run_items).

Два режима (одинаковый интерфейс, разные кошельки):
  * "sequential" — WalletV4R2, по одному переводу за транзакцию, каждый
    подтверждается через SeqnoGuard. Надёжно и с поштучным tx-hash, но медленно;
    подходит для небольшого числа адресов.
  * "highload"   — WalletHighloadV3R1, все получатели одной транзакцией
    (до 64516 сообщений). Быстро и дёшево на десятках/сотнях адресов, но
    подтверждение — на уровне всей транзакции, а не отдельного адреса.

Важно: sequential и highload — РАЗНЫЕ контракты с разными адресами даже из
одной мнемоники. Пополнять нужно тот адрес, которым будете слать.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Callable

from ton_core import Address, NetworkGlobalID, to_nano
from tonutils.clients import ToncenterClient
from tonutils.contracts import (
    SeqnoGuard,
    TONTransferBuilder,
    WalletHighloadV3R1,
    WalletV4R2,
)

from . import config, db

NANO = Decimal(10) ** 9

WALLET_CLASSES = {
    "sequential": WalletV4R2,
    "highload": WalletHighloadV3R1,
}

# резерв на комиссию сети сверх суммы выплат, TON на одного получателя
FEE_RESERVE_PER_RECIPIENT = {
    "sequential": Decimal("0.01"),   # своя комиссия за каждую транзакцию
    "highload": Decimal("0.005"),    # одна транзакция, комиссия делится на всех
}

log = logging.getLogger("ton_payout")


class PayoutError(Exception):
    """Ошибка, из-за которой рассылка не может быть выполнена (до отправки денег)."""


def _resolve_mnemonic() -> str:
    mnemonic = (config.TON_MNEMONIC or "").strip()
    if not mnemonic or mnemonic.startswith("word1"):
        raise PayoutError(
            "Мнемоника кошелька не задана (TON_MNEMONIC). Укажите её в "
            "ton_payout/config_secrets.py или через переменную окружения."
        )
    if len(mnemonic.split()) not in (12, 18, 24):
        raise PayoutError(
            f"Некорректная мнемоника: ожидалось 12/18/24 слова, "
            f"получено {len(mnemonic.split())}."
        )
    return mnemonic


def _validate_recipients(recipients: list[dict]) -> list[tuple[dict, object]]:
    """Разбирает адреса получателей. Возвращает пары (получатель, Address).

    Некорректные адреса не отбрасываются молча — по ним бросается PayoutError
    до создания запуска, чтобы не отправить часть денег и застрять.
    """
    parsed: list[tuple[dict, object]] = []
    errors: list[str] = []
    for r in recipients:
        try:
            parsed.append((r, Address(r["address"])))
        except Exception as e:  # noqa: BLE001 — любой парс-фейл адреса
            errors.append(f"#{r['id']} {r['address']}: {e}")
    if errors:
        raise PayoutError("Некорректные адреса получателей:\n" + "\n".join(errors))
    return parsed


async def run_payout(
    mode: str,
    dry_run: bool,
    triggered_by: str = "web",
    progress: Callable[[str], None] | None = None,
) -> int:
    """Выполняет рассылку. Возвращает id созданного запуска (ton_payout_runs.id).

    :param mode: "sequential" | "highload".
    :param dry_run: True — только проверки и запись плана, без отправки.
    :param triggered_by: кто инициировал ("web" / "cron" / ...).
    :param progress: необязательный колбэк для строк прогресса (в дополнение к логу).
    :raises PayoutError: если рассылка невозможна (проверки не пройдены).
    """
    if mode not in WALLET_CLASSES:
        raise PayoutError(f"Неизвестный режим: {mode!r}. Ожидалось sequential/highload.")

    def emit(msg: str) -> None:
        log.info(msg)
        if progress:
            progress(msg)

    db.init_db()

    recipients = db.list_recipients(active_only=True)
    if not recipients:
        raise PayoutError("Нет активных получателей — рассылать некому.")

    mnemonic = _resolve_mnemonic()
    parsed = _validate_recipients(recipients)

    wallet_cls = WALLET_CLASSES[mode]
    if len(recipients) > wallet_cls.MAX_MESSAGES:
        raise PayoutError(
            f"Получателей {len(recipients)}, лимит режима {mode} — "
            f"{wallet_cls.MAX_MESSAGES} за транзакцию. Уменьшите список или "
            f"смените режим."
        )

    network_str = "mainnet" if config.TON_NETWORK.lower() == "mainnet" else "testnet"
    network = NetworkGlobalID.MAINNET if network_str == "mainnet" else NetworkGlobalID.TESTNET

    total = sum((Decimal(r["amount"]) for r in recipients), Decimal(0))
    fee_reserve = FEE_RESERVE_PER_RECIPIENT[mode] * len(recipients)

    client = ToncenterClient(
        network=network,
        api_key=config.TONCENTER_API_KEY or None,
        rps_limit=1,
    )
    await client.connect()

    run_id: int | None = None
    try:
        wallet, _, _, _ = wallet_cls.from_mnemonic(client, mnemonic)
        await wallet.refresh()
        wallet_addr = wallet.address.to_str(is_bounceable=False)
        balance = Decimal(wallet.balance) / NANO

        emit(
            f"Режим {mode}, сеть {network_str}. Кошелёк {wallet_addr} "
            f"(state={wallet.state.value}), баланс {balance:.4f} TON. "
            f"К рассылке {total:.4f} TON на {len(recipients)} адрес(ов), "
            f"резерв на комиссию {fee_reserve:.4f} TON."
        )

        # Журнал запуска + план по каждому получателю (статус pending)
        run_id = db.create_run(mode, network_str, dry_run, len(recipients), total, triggered_by)
        db.set_run_wallet(run_id, wallet_addr)
        item_ids: dict[int, int] = {}
        for r in recipients:
            item_ids[r["id"]] = db.add_run_item(
                run_id, r["id"], r["address"], r["amount"], r.get("comment")
            )

        # Проверка баланса — до любой отправки
        if balance < total + fee_reserve:
            msg = (
                f"Недостаточно средств: на балансе {balance:.4f} TON, требуется "
                f"{total + fee_reserve:.4f} TON (выплаты + резерв). Пополните {wallet_addr}."
            )
            db.finish_run(run_id, "error", error_message=msg)
            raise PayoutError(msg)

        if dry_run:
            for r in recipients:
                db.update_run_item(item_ids[r["id"]], "skipped")
                emit(f"[DRY RUN] отправил бы {Decimal(r['amount']):.4f} TON на {r['address']}")
            db.finish_run(run_id, "done")
            emit(f"[DRY RUN] проверки пройдены, деньги не отправлены. Запуск #{run_id}.")
            return run_id

        if mode == "sequential":
            return await _run_sequential(wallet, recipients, item_ids, run_id, emit)
        return await _run_highload(wallet, recipients, item_ids, run_id, total, emit)
    except PayoutError:
        raise
    except Exception as e:  # noqa: BLE001 — фиксируем любой сбой в журнал
        if run_id is not None:
            db.finish_run(run_id, "error", error_message=str(e))
        log.exception("Сбой рассылки")
        raise PayoutError(f"Сбой рассылки: {e}") from e
    finally:
        await client.close()


async def _run_sequential(wallet, recipients, item_ids, run_id, emit) -> int:
    # SeqnoGuard дожидается подтверждения seqno в блокчейне перед каждой
    # следующей отправкой — иначе быстрые последовательные переводы конфликтуют.
    guard = SeqnoGuard(wallet, timeout=60.0, poll_interval=2.0)
    sent = failed = 0
    for r in recipients:
        amount = Decimal(r["amount"])
        try:
            msg = await guard.transfer(
                destination=Address(r["address"]),
                amount=to_nano(amount),
                body=(r.get("comment") or None),
            )
            db.update_run_item(item_ids[r["id"]], "sent", tx_hash=msg.normalized_hash)
            sent += 1
            emit(f"OK   {amount:.4f} TON -> {r['address']} (hash={msg.normalized_hash})")
        except Exception as e:  # noqa: BLE001 — сбой одного перевода не рушит остальные
            db.update_run_item(item_ids[r["id"]], "failed", error_message=str(e))
            failed += 1
            emit(f"FAIL {amount:.4f} TON -> {r['address']}: {e}")

    status = "done" if failed == 0 else ("partial" if sent else "error")
    db.finish_run(run_id, status)
    emit(f"Готово: успешно {sent}, ошибок {failed}. Статус запуска: {status}.")
    return run_id


async def _run_highload(wallet, recipients, item_ids, run_id, total, emit) -> int:
    builders = [
        TONTransferBuilder(
            destination=Address(r["address"]),
            amount=to_nano(Decimal(r["amount"])),
            body=(r.get("comment") or None),
        )
        for r in recipients
    ]
    # Один вызов = одна внешняя транзакция со всеми получателями внутри.
    # query_id/created_at для anti-replay генерируются автоматически.
    msg = await wallet.batch_transfer_message(builders)
    for r in recipients:
        db.update_run_item(item_ids[r["id"]], "sent", tx_hash=msg.normalized_hash)
    db.finish_run(run_id, "done", tx_hash=msg.normalized_hash)
    emit(
        f"Отправлено ОДНОЙ транзакцией: {len(recipients)} получателей, "
        f"{total:.4f} TON, hash={msg.normalized_hash}. Индивидуальное подтверждение "
        f"по каждому адресу — отдельная проверка (баланс/эксплорер)."
    )
    return run_id
