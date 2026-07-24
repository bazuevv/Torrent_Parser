"""Веб-панель управления рассылкой TON.

Отдельное Flask-приложение (свой порт), защищённое логином/паролем. Возможности:
  * управление списком получателей (таблица ton_recipients в MySQL): добавить,
    изменить, включить/выключить, удалить;
  * запуск рассылки (dry-run или боевой; режим sequential/highload) — уходит в
    отдельный процесс `python3 -m ton_payout.run`, чтобы не блокировать веб;
  * журнал запусков с детализацией по каждому получателю и live-обновлением.

Запуск (из корня репозитория, тем же venv, где установлен tonutils):
    venv/bin/python3 -m ton_payout.web           # dev-сервер, 127.0.0.1:8091
    venv/bin/gunicorn -b 127.0.0.1:8091 ton_payout.web:app   # прод

Доступ ограничен логином/паролем (WEB_USERNAME/WEB_PASSWORD в config_secrets.py).
"""

from __future__ import annotations

import asyncio
import functools
import hmac
import logging
import os
import secrets
import subprocess
import sys
import time
from decimal import Decimal, InvalidOperation

from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from . import config, db

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")

log = logging.getLogger("ton_payout.web")

app = Flask(__name__)
app.secret_key = config.SECRET_KEY or secrets.token_hex(32)
if not config.SECRET_KEY:
    log.warning("SECRET_KEY не задан — сессии сбросятся при перезапуске. "
                "Задайте SECRET_KEY в config_secrets.py.")


@app.template_filter("rstrip_zeros")
def _rstrip_zeros(value: str) -> str:
    """Убирает хвостовые нули у десятичной строки: 1.500000000 -> 1.5, 2.000 -> 2."""
    if "." not in value:
        return value
    return value.rstrip("0").rstrip(".")


# ── Аутентификация и CSRF ───────────────────────────────────────────────────

def _get_csrf() -> str:
    token = session.get("csrf")
    if not token:
        token = secrets.token_hex(16)
        session["csrf"] = token
    return token


@app.context_processor
def _inject_globals():
    return {
        "csrf_token": _get_csrf(),
        "logged_in": session.get("logged_in", False),
    }


@app.before_request
def _csrf_protect():
    if request.method == "POST":
        form_token = request.form.get("csrf", "")
        if not hmac.compare_digest(form_token, session.get("csrf", "")):
            abort(400, "CSRF-токен неверен. Обновите страницу и повторите.")


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if not config.WEB_PASSWORD:
            flash("Пароль веб-панели не настроен (WEB_PASSWORD). Вход невозможен.", "error")
            return render_template("login.html")
        ok_user = hmac.compare_digest(username, config.WEB_USERNAME)
        ok_pass = hmac.compare_digest(password, config.WEB_PASSWORD)
        if ok_user and ok_pass:
            session["logged_in"] = True
            session.permanent = False
            nxt = request.args.get("next") or url_for("dashboard")
            return redirect(nxt)
        flash("Неверный логин или пароль.", "error")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


# ── Получатели ──────────────────────────────────────────────────────────────

def _parse_amount(raw: str) -> Decimal:
    try:
        amount = Decimal(raw.strip().replace(",", "."))
    except (InvalidOperation, AttributeError):
        raise ValueError(f"Некорректная сумма: {raw!r}")
    if not (Decimal(0) < amount < Decimal(1_000_000)):
        raise ValueError(f"Сумма вне допустимого диапазона (0 … 1 000 000): {amount}")
    # не больше 9 знаков после запятой (точность нанотона)
    if -amount.as_tuple().exponent > 9:
        raise ValueError("Слишком много знаков после запятой (максимум 9).")
    return amount


@app.route("/")
@login_required
def dashboard():
    recipients = db.list_recipients()
    active = [r for r in recipients if r["is_active"]]
    total_active = sum((Decimal(r["amount"]) for r in active), Decimal(0))
    return render_template(
        "dashboard.html",
        recipients=recipients,
        active_count=len(active),
        total_active=total_active,
        payout_running=db.has_running_payout(),
    )


@app.route("/recipients/add", methods=["POST"])
@login_required
def recipient_add():
    address = request.form.get("address", "").strip()
    comment = request.form.get("comment", "").strip()
    is_active = request.form.get("is_active") == "on"
    if not address:
        flash("Адрес не может быть пустым.", "error")
        return redirect(url_for("dashboard"))
    try:
        amount = _parse_amount(request.form.get("amount", ""))
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("dashboard"))
    try:
        db.add_recipient(address, amount, comment or None, is_active)
        flash(f"Получатель добавлен: {address}", "success")
    except Exception as e:  # noqa: BLE001 — например, дубликат адреса (UNIQUE)
        flash(f"Не удалось добавить: {e}", "error")
    return redirect(url_for("dashboard"))


@app.route("/recipients/<int:rid>/edit", methods=["POST"])
@login_required
def recipient_edit(rid: int):
    if not db.get_recipient(rid):
        abort(404)
    address = request.form.get("address", "").strip()
    comment = request.form.get("comment", "").strip()
    is_active = request.form.get("is_active") == "on"
    if not address:
        flash("Адрес не может быть пустым.", "error")
        return redirect(url_for("dashboard"))
    try:
        amount = _parse_amount(request.form.get("amount", ""))
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("dashboard"))
    try:
        db.update_recipient(rid, address, amount, comment or None, is_active)
        flash("Получатель обновлён.", "success")
    except Exception as e:  # noqa: BLE001
        flash(f"Не удалось обновить: {e}", "error")
    return redirect(url_for("dashboard"))


@app.route("/recipients/<int:rid>/toggle", methods=["POST"])
@login_required
def recipient_toggle(rid: int):
    r = db.get_recipient(rid)
    if not r:
        abort(404)
    db.set_recipient_active(rid, not r["is_active"])
    return redirect(url_for("dashboard"))


@app.route("/recipients/<int:rid>/delete", methods=["POST"])
@login_required
def recipient_delete(rid: int):
    if not db.get_recipient(rid):
        abort(404)
    db.delete_recipient(rid)
    flash("Получатель удалён.", "success")
    return redirect(url_for("dashboard"))


# ── Запуск рассылки ─────────────────────────────────────────────────────────

@app.route("/payout/run", methods=["POST"])
@login_required
def payout_run():
    mode = request.form.get("mode", "highload")
    dry_run = request.form.get("dry_run") == "on"
    if mode not in ("sequential", "highload"):
        flash("Неизвестный режим рассылки.", "error")
        return redirect(url_for("dashboard"))

    if not dry_run and db.has_running_payout():
        flash("Уже выполняется рассылка — дождитесь её завершения.", "error")
        return redirect(url_for("runs"))

    active = db.list_recipients(active_only=True)
    if not active:
        flash("Нет активных получателей — рассылать некому.", "error")
        return redirect(url_for("dashboard"))

    prev_max = 0
    latest = db.list_runs(1)
    if latest:
        prev_max = latest[0]["id"]

    os.makedirs(LOG_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(LOG_DIR, f"payout_{ts}.log")
    cmd = [
        sys.executable, "-m", "ton_payout.run",
        "--mode", mode, "--triggered-by", "web",
    ]
    if dry_run:
        cmd.append("--dry-run")

    logf = open(log_path, "w", encoding="utf-8")
    subprocess.Popen(
        cmd, cwd=PROJECT_DIR, stdout=logf, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    flash(
        ("Пробный прогон запущен." if dry_run else "Рассылка запущена.")
        + " Ниже — статус (обновляется автоматически).",
        "success",
    )
    return redirect(url_for("runs_waiting", after=prev_max))


@app.route("/runs/waiting/<int:after>")
@login_required
def runs_waiting(after: int):
    latest = db.list_runs(1)
    if latest and latest[0]["id"] > after:
        return redirect(url_for("run_detail", run_id=latest[0]["id"]))
    return render_template("runs_waiting.html", after=after)


@app.route("/api/runs/latest")
@login_required
def api_latest_run():
    after = request.args.get("after", 0, type=int)
    latest = db.list_runs(1)
    if latest and latest[0]["id"] > after:
        return jsonify({"ready": True, "run_id": latest[0]["id"]})
    return jsonify({"ready": False})


# ── Журнал запусков ─────────────────────────────────────────────────────────

@app.route("/runs")
@login_required
def runs():
    return render_template("runs.html", runs=db.list_runs(100))


@app.route("/runs/<int:run_id>")
@login_required
def run_detail(run_id: int):
    run = db.get_run(run_id)
    if not run:
        abort(404)
    return render_template(
        "run_detail.html", run=run, items=db.get_run_items(run_id)
    )


@app.route("/api/runs/<int:run_id>")
@login_required
def api_run(run_id: int):
    run = db.get_run(run_id)
    if not run:
        abort(404)
    items = db.get_run_items(run_id)

    def _ser(d: dict) -> dict:
        return {k: (str(v) if isinstance(v, Decimal) else
                    (v.isoformat() if hasattr(v, "isoformat") else v))
                for k, v in d.items()}

    return jsonify({
        "run": _ser(run),
        "items": [_ser(i) for i in items],
        "finished": run["status"] != "running",
    })


# ── Балансы кошельков (read-only обращение к сети) ───────────────────────────

@app.route("/api/wallet-info")
@login_required
def api_wallet_info():
    try:
        info = asyncio.run(_wallet_overview())
        return jsonify(info)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 200


async def _wallet_overview() -> dict:
    from ton_core import NetworkGlobalID
    from tonutils.clients import ToncenterClient
    from tonutils.contracts import WalletHighloadV3R1, WalletV4R2

    from .payout_core import NANO, _resolve_mnemonic

    mnemonic = _resolve_mnemonic()  # бросит PayoutError, если не задана
    network_str = "mainnet" if config.TON_NETWORK.lower() == "mainnet" else "testnet"
    network = NetworkGlobalID.MAINNET if network_str == "mainnet" else NetworkGlobalID.TESTNET

    client = ToncenterClient(network=network, api_key=config.TONCENTER_API_KEY or None, rps_limit=1)
    await client.connect()
    out = {"network": network_str, "wallets": {}}
    try:
        for mode, cls in (("sequential", WalletV4R2), ("highload", WalletHighloadV3R1)):
            wallet, _, _, _ = cls.from_mnemonic(client, mnemonic)
            await wallet.refresh()
            out["wallets"][mode] = {
                "address": wallet.address.to_str(is_bounceable=False),
                "balance": f"{Decimal(wallet.balance) / NANO:.4f}",
                "state": wallet.state.value,
            }
    finally:
        await client.close()
    return out


if __name__ == "__main__":
    db.init_db()
    app.run(host="127.0.0.1", port=8091, debug=False)
