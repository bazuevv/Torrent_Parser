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

import asyncio
import logging
from decimal import Decimal
from typing import Callable

from ton_core import Address, NetworkGlobalID, to_nano
from tonutils.clients import ToncenterClient
from tonutils.contracts import (
    JettonTransferBuilder,
    SeqnoGuard,
    TONTransferBuilder,
    WalletHighloadV3R1,
    WalletV4R2,
)

from . import config, db, jettons

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
    asset: str = "TON",
) -> int:
    """Выполняет рассылку. Возвращает id созданного запуска (ton_payout_runs.id).

    :param mode: "sequential" | "highload".
    :param dry_run: True — только проверки и запись плана, без отправки.
    :param triggered_by: кто инициировал ("web" / "cron" / ...).
    :param progress: необязательный колбэк для строк прогресса (в дополнение к логу).
    :param asset: "TON" (нативная монета) или "USDT" (джеттон).
    :raises PayoutError: если рассылка невозможна (проверки не пройдены).
    """
    if mode not in WALLET_CLASSES:
        raise PayoutError(f"Неизвестный режим: {mode!r}. Ожидалось sequential/highload.")
    if asset not in jettons.SUPPORTED_ASSETS:
        raise PayoutError(f"Неизвестный актив: {asset!r}. Ожидалось TON/USDT.")

    def emit(msg: str) -> None:
        log.info(msg)
        if progress:
            progress(msg)

    db.init_db()

    # Рассылаем только получателей выбранного актива — суммы TON и USDT
    # нельзя смешивать в одном запуске (разные единицы и разный газ).
    recipients = db.list_recipients(active_only=True, asset=asset)
    if not recipients:
        raise PayoutError(f"Нет активных получателей с активом {asset} — рассылать некому.")

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
    if asset == "TON":
        # Комиссия платится тем же активом, что и выплаты.
        ton_needed = total + FEE_RESERVE_PER_RECIPIENT[mode] * len(recipients)
    else:
        # Джеттон: выплаты идут в USDT, но газ ВСЕГДА в TON — ~0.05 TON
        # на каждый перевод (неизрасходованное возвращается отправителю).
        ton_needed = jettons.JETTON_GAS_TON * len(recipients)

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

        # Для USDT дополнительно нужен баланс джеттона на jetton-wallet отправителя.
        # Без API-ключа публичный Toncenter даёт ~1 запрос/с — разносим запросы
        # паузой, иначе ловим 429 (refresh + runGetMethod + баланс подряд).
        usdt_balance = None
        jetton_wallet_addr = None
        if asset == "USDT":
            delay = 0.0 if config.TONCENTER_API_KEY else 1.2
            if delay:
                await asyncio.sleep(delay)
            jetton_wallet_addr = await jettons.get_jetton_wallet_address(client, wallet_addr)
            if delay:
                await asyncio.sleep(delay)
            info = await jettons.get_usdt_balance(client, jetton_wallet_addr)
            usdt_balance = Decimal(info["balance"])

        if asset == "TON":
            emit(
                f"Режим {mode}, сеть {network_str}, актив TON. Кошелёк {wallet_addr} "
                f"(state={wallet.state.value}), баланс {balance:.4f} TON. "
                f"К рассылке {total:.4f} TON на {len(recipients)} адрес(ов), "
                f"нужно с комиссией {ton_needed:.4f} TON."
            )
        else:
            emit(
                f"Режим {mode}, сеть {network_str}, актив USDT. Кошелёк {wallet_addr} "
                f"(TON на газ: {balance:.4f}), USDT-кошелёк {jetton_wallet_addr} "
                f"(баланс {usdt_balance:.6f} USDT). К рассылке {total:.6f} USDT на "
                f"{len(recipients)} адрес(ов), газа нужно ~{ton_needed:.4f} TON."
            )

        # Журнал запуска + план по каждому получателю (статус pending)
        run_id = db.create_run(mode, network_str, dry_run, len(recipients), total,
                               triggered_by, asset=asset)
        db.set_run_wallet(run_id, wallet_addr)
        item_ids: dict[int, int] = {}
        for r in recipients:
            item_ids[r["id"]] = db.add_run_item(
                run_id, r["id"], r["address"], r["amount"], r.get("comment"), asset=asset
            )

        # Проверки балансов — до любой отправки
        if balance < ton_needed:
            msg = (
                f"Недостаточно TON: на балансе {balance:.4f} TON, требуется "
                f"{ton_needed:.4f} TON ("
                + ("выплаты + комиссия" if asset == "TON" else "газ для джеттон-переводов")
                + f"). Пополните {wallet_addr}."
            )
            db.finish_run(run_id, "error", error_message=msg)
            raise PayoutError(msg)

        if asset == "USDT" and usdt_balance < total:
            msg = (
                f"Недостаточно USDT: на {jetton_wallet_addr} {usdt_balance:.6f} USDT, "
                f"требуется {total:.6f} USDT. Пополните USDT-кошелёк отправителя."
            )
            db.finish_run(run_id, "error", error_message=msg)
            raise PayoutError(msg)

        prec = 4 if asset == "TON" else 6
        if dry_run:
            for r in recipients:
                db.update_run_item(item_ids[r["id"]], "skipped")
                emit(f"[DRY RUN] отправил бы {Decimal(r['amount']):.{prec}f} {asset} на {r['address']}")
            db.finish_run(run_id, "done")
            emit(f"[DRY RUN] проверки пройдены, средства не отправлены. Запуск #{run_id}.")
            return run_id

        if mode == "sequential":
            return await _run_sequential(wallet, recipients, item_ids, run_id, emit, asset)
        return await _run_highload(wallet, recipients, item_ids, run_id, total, emit, asset)
    except PayoutError:
        raise
    except Exception as e:  # noqa: BLE001 — фиксируем любой сбой в журнал
        if run_id is not None:
            db.finish_run(run_id, "error", error_message=str(e))
        log.exception("Сбой рассылки")
        raise PayoutError(f"Сбой рассылки: {e}") from e
    finally:
        await client.close()


def _build_transfer(recipient: dict, asset: str):
    """Строит билдер перевода под нужный актив.

    TON  — прямой перевод нативной монеты.
    USDT — джеттон-перевод: сообщение уходит на jetton-wallet отправителя,
           к нему прикладывается TON на газ (JETTON_GAS_TON).
    """
    destination = Address(recipient["address"])
    comment = recipient.get("comment") or None
    amount = Decimal(recipient["amount"])

    if asset == "TON":
        return TONTransferBuilder(
            destination=destination,
            amount=to_nano(amount),
            body=comment,
        )
    return JettonTransferBuilder(
        destination=destination,
        jetton_amount=jettons.to_usdt_units(amount),
        jetton_master_address=Address(jettons.usdt_master_address()),
        forward_payload=comment,
        amount=to_nano(jettons.JETTON_GAS_TON),
    )


async def _run_sequential(wallet, recipients, item_ids, run_id, emit, asset="TON") -> int:
    # SeqnoGuard дожидается подтверждения seqno в блокчейне перед каждой
    # следующей отправкой — иначе быстрые последовательные переводы конфликтуют.
    guard = SeqnoGuard(wallet, timeout=60.0, poll_interval=2.0)
    prec = 4 if asset == "TON" else 6
    sent = failed = 0
    for r in recipients:
        amount = Decimal(r["amount"])
        try:
            msg = await guard.transfer_message(_build_transfer(r, asset))
            db.update_run_item(item_ids[r["id"]], "sent", tx_hash=msg.normalized_hash)
            sent += 1
            emit(f"OK   {amount:.{prec}f} {asset} -> {r['address']} (hash={msg.normalized_hash})")
        except Exception as e:  # noqa: BLE001 — сбой одного перевода не рушит остальные
            db.update_run_item(item_ids[r["id"]], "failed", error_message=str(e))
            failed += 1
            emit(f"FAIL {amount:.{prec}f} {asset} -> {r['address']}: {e}")

    status = "done" if failed == 0 else ("partial" if sent else "error")
    db.finish_run(run_id, status)
    emit(f"Готово: успешно {sent}, ошибок {failed}. Статус запуска: {status}.")
    return run_id


async def _run_highload(wallet, recipients, item_ids, run_id, total, emit, asset="TON") -> int:
    builders = [_build_transfer(r, asset) for r in recipients]
    # Один вызов = одна внешняя транзакция со всеми получателями внутри.
    # query_id/created_at для anti-replay генерируются автоматически.
    msg = await wallet.batch_transfer_message(builders)
    for r in recipients:
        db.update_run_item(item_ids[r["id"]], "sent", tx_hash=msg.normalized_hash)
    db.finish_run(run_id, "done", tx_hash=msg.normalized_hash)
    prec = 4 if asset == "TON" else 6
    emit(
        f"Отправлено ОДНОЙ транзакцией: {len(recipients)} получателей, "
        f"{total:.{prec}f} {asset}, hash={msg.normalized_hash}. Индивидуальное "
        f"подтверждение по каждому адресу — отдельная проверка (баланс/эксплорер)."
    )
    return run_id
