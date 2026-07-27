"""Котировки конвертации TON ⇄ USDT через DEX STON.fi (только чтение).

Этот модуль НИЧЕГО не отправляет в блокчейн — он лишь спрашивает у публичного
API STON.fi, сколько получится при обмене: курс, комиссию пула, влияние на цену
и минимум к получению с учётом проскальзывания.

Почему нужен минимум (min_ask): курс в пуле плавающий. Между расчётом и
исполнением он может измениться, поэтому в реальный своп закладывают нижнюю
границу — если получилось бы меньше, обмен откатывается вместо потери средств.

Реальная отправка свопов — отдельный шаг (см. README), здесь её сознательно нет.
"""

from __future__ import annotations

from decimal import Decimal

import requests

from . import jettons

# STON.fi обозначает нативный TON «псевдо-джеттоном» pTON — у него свой адрес.
PTON_ADDRESS = "EQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAM9c"

API_BASE = "https://api.ston.fi/v1"
REQUEST_TIMEOUT = 25

TON_DECIMALS = 9
DEFAULT_SLIPPAGE = Decimal("0.01")  # 1%


class SwapQuoteError(Exception):
    """Не удалось получить котировку (сеть, лимиты, нет пула и т.п.)."""


def _units(amount: Decimal, asset: str) -> int:
    """Сумма -> минимальные единицы актива (TON 9 знаков, USDT 6)."""
    if asset == "TON":
        return int((amount * (Decimal(10) ** TON_DECIMALS)).to_integral_value())
    return jettons.to_usdt_units(amount)


def _from_units(units: int | str, asset: str) -> Decimal:
    """Минимальные единицы -> сумма актива."""
    value = Decimal(str(units))
    if asset == "TON":
        return value / (Decimal(10) ** TON_DECIMALS)
    return value / jettons.USDT_UNIT


def _asset_address(asset: str) -> str:
    """Адрес контракта актива в терминах STON.fi."""
    return PTON_ADDRESS if asset == "TON" else jettons.usdt_master_address()


def get_quote(
    from_asset: str,
    to_asset: str,
    amount: Decimal | str,
    slippage: Decimal | str = DEFAULT_SLIPPAGE,
) -> dict:
    """Котировка обмена from_asset -> to_asset на сумму amount.

    :param from_asset: "TON" или "USDT" — что отдаём.
    :param to_asset: "TON" или "USDT" — что получаем.
    :param amount: сумма в единицах from_asset (например, 1.5).
    :param slippage: допустимое проскальзывание долей единицы (0.01 = 1%).
    :raises SwapQuoteError: если направление некорректно или API недоступен.
    """
    from_asset = str(from_asset).upper()
    to_asset = str(to_asset).upper()
    for a in (from_asset, to_asset):
        if a not in jettons.SUPPORTED_ASSETS:
            raise SwapQuoteError(f"Неизвестный актив: {a}. Ожидалось TON/USDT.")
    if from_asset == to_asset:
        raise SwapQuoteError("Отдаваемый и получаемый активы совпадают.")

    amount = Decimal(str(amount))
    if amount <= 0:
        raise SwapQuoteError("Сумма должна быть больше нуля.")

    params = {
        "offer_address": _asset_address(from_asset),
        "ask_address": _asset_address(to_asset),
        "units": str(_units(amount, from_asset)),
        "slippage_tolerance": str(slippage),
    }
    try:
        resp = requests.post(
            f"{API_BASE}/swap/simulate",
            params=params,
            headers={"accept": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as e:
        raise SwapQuoteError(f"Не удалось связаться со STON.fi: {e}") from e

    if resp.status_code != 200:
        raise SwapQuoteError(
            f"STON.fi ответил {resp.status_code}: {resp.text[:200]}"
        )
    try:
        data = resp.json()
    except ValueError as e:
        raise SwapQuoteError(f"Некорректный ответ STON.fi: {e}") from e

    ask = _from_units(data["ask_units"], to_asset)
    min_ask = _from_units(data["min_ask_units"], to_asset)
    prec_to = 4 if to_asset == "TON" else 6
    prec_from = 4 if from_asset == "TON" else 6

    gas = data.get("gas_params") or {}
    # forward_gas — сколько TON прикладывается к свопу (часть возвращается)
    gas_ton = _from_units(gas.get("forward_gas", 0), "TON")

    fee_units = data.get("fee_units")
    fee_asset = to_asset  # комиссия удерживается из получаемого актива

    return {
        "from_asset": from_asset,
        "to_asset": to_asset,
        "offer": f"{amount:.{prec_from}f}",
        "receive": f"{ask:.{prec_to}f}",
        "min_receive": f"{min_ask:.{prec_to}f}",
        "rate": data.get("swap_rate"),
        "price_impact_pct": _pct(data.get("price_impact")),
        "fee_percent": _pct(data.get("fee_percent")),
        "fee_amount": (f"{_from_units(fee_units, fee_asset):.{prec_to}f}"
                       if fee_units is not None else None),
        "fee_asset": fee_asset,
        "slippage_pct": _pct(slippage),
        "gas_ton": f"{gas_ton:.4f}",
        "router": data.get("router_address"),
        "pool": data.get("pool_address"),
        "dex": "STON.fi",
    }


def _pct(value) -> str | None:
    """Долю (0.003) -> проценты ('0.30')."""
    if value is None:
        return None
    try:
        return f"{Decimal(str(value)) * 100:.2f}"
    except Exception:  # noqa: BLE001 — формат от внешнего API
        return None
