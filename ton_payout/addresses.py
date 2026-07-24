"""Детерминированный вывод РЕАЛЬНЫХ TON-адресов из одного master-сида (модель A).

Адрес пользователя = кошелёк WalletV4R2, выведенный из master-мнемоники
(TON_MASTER_MNEMONIC) с subwallet_id = id пользователя (например, girls.id).

Свойства:
  * один секрет (master-мнемоника) → сколько угодно разных реальных адресов;
  * все адреса управляются этим же сидом (можно вывести средства с любого);
  * вывод детерминированный и ОФЛАЙН (в сеть не ходит) — тот же id всегда даёт
    тот же адрес, поэтому адрес не нужно хранить, его всегда можно пере-вывести.

subwallet_id — 32-битное поле, т.е. до ~4.29 млрд разных адресов из одного сида.

ВНИМАНИЕ: это кастодиальная модель — ключами владеет держатель master-сида.
Master-сид держите только в секретах (config_secrets.py / env), никогда в git.
"""

from __future__ import annotations

import threading

from ton_core import (
    NetworkGlobalID,
    PrivateKey,
    WalletV4Config,
    mnemonic_to_private_key,
)
from tonutils.clients import ToncenterClient
from tonutils.contracts import WalletV4R2

from . import config

MAX_SUBWALLET_ID = (1 << 32) - 1


class AddressDeriverError(Exception):
    """Невозможно вывести адрес (нет master-сида, некорректный id и т.п.)."""


_lock = threading.Lock()
_client: ToncenterClient | None = None       # неподключённый клиент, только для offline-вычислений
_private_key: PrivateKey | None = None       # ключ master-сида (PBKDF2 считается один раз)


def _ensure_ready() -> None:
    global _client, _private_key
    if _private_key is not None:
        return
    with _lock:
        if _private_key is not None:
            return
        mnemonic = (config.TON_MASTER_MNEMONIC or "").strip()
        if not mnemonic or mnemonic.startswith("word1"):
            raise AddressDeriverError(
                "TON_MASTER_MNEMONIC не задан. Укажите master-мнемонику в "
                "ton_payout/config_secrets.py (или в переменной окружения) — "
                "из неё детерминированно выводятся адреса пользователей."
            )
        words = mnemonic.split()
        if len(words) not in (12, 18, 24):
            raise AddressDeriverError(
                f"Некорректная master-мнемоника: ожидалось 12/18/24 слова, "
                f"получено {len(words)}."
            )
        net = (NetworkGlobalID.MAINNET
               if config.TON_NETWORK.lower() == "mainnet"
               else NetworkGlobalID.TESTNET)
        # Конструктор клиента в сеть не ходит; используется только как контейнер
        # для offline-вычисления адреса (from_private_key не обращается к сети).
        _client = ToncenterClient(network=net)
        # PBKDF2 (дорого) выполняется ОДИН раз: ключ master-сида один для всех id,
        # от id зависит только subwallet_id в конфиге (дешёвое вычисление адреса).
        _, priv_bytes = mnemonic_to_private_key(words)
        _private_key = PrivateKey(priv_bytes)


def derive_address(subwallet_id: int) -> str:
    """Возвращает реальный TON-адрес (UQ…, non-bounceable) для данного id.

    :param subwallet_id: идентификатор пользователя (0 … 2**32-1), напр. girls.id.
    :raises AddressDeriverError: если сид не задан/некорректен или id вне диапазона.
    """
    if not isinstance(subwallet_id, int) or isinstance(subwallet_id, bool):
        raise AddressDeriverError(f"subwallet_id должен быть int, получено {type(subwallet_id).__name__}")
    if not (0 <= subwallet_id <= MAX_SUBWALLET_ID):
        raise AddressDeriverError(
            f"subwallet_id вне диапазона 0..{MAX_SUBWALLET_ID}: {subwallet_id}"
        )
    _ensure_ready()
    cfg = WalletV4Config(subwallet_id=subwallet_id)
    wallet = WalletV4R2.from_private_key(_client, _private_key, config=cfg)
    return wallet.address.to_str(is_bounceable=False)


def is_configured() -> bool:
    """True, если master-сид задан и корректен (без выброса исключения)."""
    try:
        _ensure_ready()
        return True
    except AddressDeriverError:
        return False
