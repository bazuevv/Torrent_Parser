#!/usr/bin/env python3
"""Автозаполнение строк подписки OpenAI-аккаунтов панели из Codex id_token.

Панель Accs показывает срок подписки аккаунта из
``~/.claude/.account-subs.json`` (``{файл: {paidAt, days, email, plan}}``).
Для провайдера OpenAI (аккаунты через локальный Codex-мост) срок вносили
руками: «в протоколе Codex его нет». Но дата продления ChatGPT есть в
id_token из ``~/.codex/auth.json`` (``chatgpt_subscription_active_start``
/ ``_until`` в claim ``https://api.openai.com/auth``).

Работа ленивая, по требованию: хук перечисляет openai-аккаунты панели и
читает/декодирует токен ТОЛЬКО если у какого-то из них нет записи о
подписке или срок уже истёк. Аккаунты с действующей подпиской проверку
не запускают вовсе — повторная проверка откладывается до истечения
текущего срока. Свежие данные пишутся через
``account_switcher.write_subscription(automatic=True)`` — тем же кодом,
что и панель; ручной ввод сроков для OpenAI со стороны панели закрыт.

Все openai-аккаунты панели делят один вход Codex (``auth.json`` один,
мост один), поэтому данные текущего входа пишутся каждому требующему
обновления аккаунту без сверки почты.

Never raises: any failure exits 0 so the session keeps starting normally.
"""

from __future__ import annotations

import base64
import datetime
import glob
import json
import os
import sys

import account_switcher as accounts

AUTH_FILE = os.path.expanduser("~/.codex/auth.json")
AUTH_CLAIMS_KEY = "https://api.openai.com/auth"


def b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def jwt_payload(token: str) -> dict:
    parts = token.split(".")
    if len(parts) < 2:
        raise ValueError("not a JWT")
    return json.loads(b64url_decode(parts[1]))


def codex_login(token: str) -> dict:
    """Данные входа Codex из id_token: email, тариф и даты подписки."""
    claims = jwt_payload(token)
    auth = claims.get(AUTH_CLAIMS_KEY) or {}
    out = {
        "email": claims.get("email") or "",
        "plan": auth.get("chatgpt_plan_type") or "",
        "paid_at": None,
        "days": None,
    }
    try:
        start = datetime.datetime.fromisoformat(
            auth["chatgpt_subscription_active_start"])
        until = datetime.datetime.fromisoformat(
            auth["chatgpt_subscription_active_until"])
    except (KeyError, TypeError, ValueError):
        return out  # подписочного claim нет — только почта и тариф
    days = round((until - start).total_seconds() / 86400)
    if 1 <= days <= accounts.MAX_SUB_DAYS:
        # Локальное время без пояса: в SUBS_FILE так принято (см.
        # SUB_PAID_AT_RE), а панели важна разность paidAt+days−now,
        # пояс на неё не влияет.
        out["paid_at"] = start.astimezone().replace(
            tzinfo=None).isoformat(timespec="minutes")
        out["days"] = days
    return out


def _needs_update(record: dict | None) -> bool:
    """Нет срока подписки — или он уже истёк."""
    if not record:
        return True
    info = accounts.subscription_info(record)
    return info is None or info["expired"]


def sync(auth_file: str = AUTH_FILE) -> list[str]:
    """Обновить подписки openai-аккаунтов панели из id_token.

    Возвращает имена обновлённых файлов. Токен не читается вовсе, пока
    у всех openai-аккаунтов действующая подписка.
    """
    subs = accounts._read_subs()
    pending: list[tuple[str, dict | None]] = []
    for path in sorted(glob.glob(os.path.join(
            accounts.CLAUDE_DIR, "settings*.json"))):
        filename = os.path.basename(path)
        if filename in accounts.EXCLUDED or not accounts.ACCOUNT_NAME_RE.match(filename):
            continue
        if accounts._describe(accounts.source_path(filename))["provider"] != "openai":
            continue
        record = accounts.read_subscription(filename, subs)
        if _needs_update(record):
            pending.append((filename, record))
    if not pending:
        return []

    try:
        with open(auth_file, encoding="utf-8") as fh:
            token = json.load(fh)["tokens"]["id_token"]
        login = codex_login(token)
    except (OSError, ValueError, KeyError):
        return []

    updated = []
    for filename, record in pending:
        if login["paid_at"] is None and record and record.get("paidAt"):
            continue  # дат в токене нет — не стирать же имеющийся срок
        fresh = {
            key: value
            for key, value in (
                ("email", login["email"]),
                ("plan", login["plan"]),
                ("paidAt", login["paid_at"]),
                ("days", login["days"]),
            )
            if value not in (None, "")
        }
        old = {k: v for k, v in (record or {}).items() if k in fresh}
        if old == fresh:
            continue  # перепроверили после истечения — данных не прибавилось
        ok, _ = accounts.write_subscription(
            filename, login["paid_at"], login["days"],
            login["email"], login["plan"], automatic=True)
        if ok:
            updated.append(filename)
    return updated


def main() -> int:
    sync()
    return 0


if __name__ == "__main__":
    sys.exit(main())
