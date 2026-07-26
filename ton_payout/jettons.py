"""Поддержка джеттонов (USDT на TON) — чтение балансов и вычисление адресов.

Ключевые отличия джеттона от нативного TON:
  * у каждого владельца СВОЙ контракт «jetton-wallet» под конкретный токен;
    адрес выводится из (адрес владельца + мастер-контракт джеттона);
  * у USDT на TON **6 знаков** после запятой (у TON — 9);
  * перевод джеттона ВСЁ РАВНО требует TON на газ (~0.05 TON на перевод),
    поэтому у кошелька-отправителя должен быть и USDT, и TON.

Отправка джеттонов делается через JettonTransferBuilder (см. payout_core/web).
Здесь — только read-only часть и общие константы/конвертация.
"""

from __future__ import annotations

from decimal import Decimal

from ton_core import Address, NetworkGlobalID
from tonutils.clients import ToncenterClient
from tonutils.contracts import JettonMasterStablecoin, JettonWalletStablecoin

from . import config

# Официальный мастер-контракт USDT (Tether) в mainnet TON.
USDT_MASTER_MAINNET = "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs"

USDT_DECIMALS = 6
USDT_UNIT = Decimal(10) ** USDT_DECIMALS

# TON, прикладываемый к одному джеттон-переводу на газ. Неизрасходованный
# остаток возвращается отправителю; нетто-расход обычно ~0.02–0.04 TON.
JETTON_GAS_TON = Decimal("0.05")

SUPPORTED_ASSETS = ("TON", "USDT")


class JettonError(Exception):
    """Ошибка работы с джеттоном (не сконфигурирован, нет контракта и т.п.)."""


def usdt_master_address() -> str:
    """Адрес мастер-контракта USDT для текущей сети."""
    override = (config.USDT_MASTER_ADDRESS or "").strip()
    if override:
        return override
    if config.TON_NETWORK.lower() != "mainnet":
        raise JettonError(
            "Адрес мастер-контракта USDT для testnet не задан. Укажите "
            "USDT_MASTER_ADDRESS в ton_payout/config_secrets.py."
        )
    return USDT_MASTER_MAINNET


def to_usdt_units(amount: Decimal | str | float) -> int:
    """USDT -> минимальные единицы (6 знаков). 10.5 -> 10500000."""
    value = Decimal(str(amount))
    units = (value * USDT_UNIT).to_integral_value()
    return int(units)


def from_usdt_units(units: int) -> Decimal:
    """Минимальные единицы -> USDT. 10500000 -> 10.5"""
    return Decimal(units) / USDT_UNIT


def make_client() -> ToncenterClient:
    """Неподключённый клиент Toncenter для текущей сети."""
    net = (NetworkGlobalID.MAINNET
           if config.TON_NETWORK.lower() == "mainnet"
           else NetworkGlobalID.TESTNET)
    return ToncenterClient(
        network=net,
        api_key=config.TONCENTER_API_KEY or None,
        rps_limit=1,
    )


async def get_jetton_wallet_address(client: ToncenterClient, owner: str | Address) -> str:
    """Адрес USDT jetton-wallet для владельца (1 запрос к мастер-контракту)."""
    master = await JettonMasterStablecoin.from_address(client, Address(usdt_master_address()))
    jw = await master.get_wallet_address(Address(str(owner)) if not isinstance(owner, Address) else owner)
    return jw.to_str()


async def get_usdt_balance(client: ToncenterClient, jetton_wallet_address: str) -> dict:
    """Баланс USDT по адресу jetton-wallet.

    Если контракт ещё не создан (владельцу никогда не присылали USDT),
    возвращает нулевой баланс со state='nonexist' вместо ошибки.
    """
    try:
        wallet = await JettonWalletStablecoin.from_address(client, Address(jetton_wallet_address))
        return {
            "balance": f"{from_usdt_units(wallet.jetton_balance):.6f}",
            "units": wallet.jetton_balance,
            "state": wallet.state.value,
        }
    except Exception:
        # jetton-wallet разворачивается только при первом получении токена
        return {"balance": "0.000000", "units": 0, "state": "nonexist"}
