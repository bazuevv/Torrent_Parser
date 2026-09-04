#!/usr/bin/env python3
"""
Сигнал сброса пятичасового окна лимита claude.ai (.claude/notification.mp3).

Звук звучит в момент, когда окно «5 ч» сбрасывается на 0% — «лимит, в
который упёрлись, снова доступен», а не при исчерпании. Момент сброса
абсолютен (resets_at в кэше CLI), поэтому сигнал работает даже по
замёрзшему кэшу: CLI на стороннем провайдере кэш не обновляет, а момент,
до которого окно жило, всё равно называет.

Данные — anthropic_usage_raw() из account_switcher (сырое окно, без
маскировки истёкших, какую делает anthropic_usage для полосок).

Правило свидетеля: сигнал звучит только для окна, которое монитор сам
видел активным. Иначе рестарт сервера (а он перезапускается по mtime на
каждую правку исходников и холодных ключей конфига) на замёрзшем кэше
с давно истёкшим resets_at играл бы звук о сбросе, случившемся часы
назад. Обратная сторона: рестарт ПОСЛЕ сброса пропускает сигнал этого
окна — принято, состояние на диск не пишется.

Заполненность окна = максимум наблюдённого процента в рамках одного
resets_at; на новом окне счёт начинается заново. Если активен сторонний
провайдер — монитор молчит: окно claude.ai в этот момент не тратится.

Модуль читает claude-custom-config.toml сам (как hook_log), а не через
http-server: сервер импортирует limit_alert ради старта потока, и
обратный импорт был бы циклом. Ключи limitResetAlert* — горячие
(HOT_KEYS http-server): правка в окне настроек применяется следующим
циклом, без перезапуска. Дефолт в коде — «выключено»: битый TOML не
должен включать звук сам.
"""
import os
import shutil
import subprocess
import sys
import threading
import time

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    tomllib = None

# Импорты соседей по каталогу: при запуске из http-server путь уже стоит
# в sys.path, при запуске стенда tmp/ — ставим сами (тот же приём, что
# у http-server).
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import hook_log  # noqa: E402
import account_switcher  # noqa: E402

CONFIG_PATH = hook_log.CONFIG_PATH
MP3_PATH = os.path.join(os.path.dirname(_HERE), "notification.mp3")

DEFAULT_POLL_SEC = 30


def _monitor_config() -> dict:
    """Нормализованные настройки монитора; при любой беде — «выключено»."""
    cfg = {
        "enabled": False,
        "mode": "threshold",
        "percent": 95,
        "pollSec": DEFAULT_POLL_SEC,
        "repeatMin": 0,
        "playSec": 0,
    }
    if tomllib is None or not os.path.isfile(CONFIG_PATH):
        return cfg
    try:
        with open(CONFIG_PATH, "rb") as fh:
            data = tomllib.load(fh)
    except Exception:
        return cfg
    if not isinstance(data, dict):
        return cfg

    enabled = data.get("limitResetAlert", False)
    cfg["enabled"] = enabled if isinstance(enabled, bool) else False

    mode = data.get("limitResetAlertMode", "threshold")
    cfg["mode"] = mode if mode in ("any", "threshold") else "threshold"

    def _int(key: str, default: int, low: int, high: int | None = None) -> int:
        value = data.get(key, default)
        # bool — тоже int; True как pollSec был бы тихой порчей.
        if not isinstance(value, int) or isinstance(value, bool) or value < low:
            return default
        if high is not None and value > high:
            return default
        return value

    cfg["percent"] = _int("limitResetAlertPercent", 95, 1, 100)
    cfg["pollSec"] = _int("limitResetAlertPollSec", DEFAULT_POLL_SEC, 1)
    cfg["repeatMin"] = _int("limitResetAlertRepeatMin", 0, 0)
    cfg["playSec"] = _int("limitResetAlertPlaySec", 0, 0)
    return cfg


def decide(state: dict, snap: dict | None, cfg: dict, now: float) -> tuple[str, str]:
    """Один шаг автомата: («play» | «repeat» | «quiet», причина).

    Чистая функция: мутирует только переданный state, время и данные
    приходят аргументами — на этом держится стенд tmp/limit-alert-test.py.

    state: {"account", "window": {"resetAt", "percentMax"} | None,
            "lastSignaledReset", "lastPlayAt"} — переживляет смены
    снимков; snap — выдача anthropic_usage_raw().
    """
    if snap is None:
        return ("quiet", "no-data")

    # Кэш принадлежит логину: окно чужого аккаунта (или проценты,
    # оставшиеся от прежнего) — не наше дело, история начинается заново.
    account = snap.get("accountUuid")
    if state.get("account") != account:
        state.clear()
        state.update({
            "account": account,
            "window": None,
            "lastSignaledReset": None,
            "lastPlayAt": None,
        })

    reset_at = snap.get("resetAt")
    if reset_at is None:
        # Без якоря сброса сигнал невозможен в принципе. Живой кэш
        # отдаёт resets_at всегда — это защита от деградации формата.
        # Состояние не трогаем: якорь может появиться со свежим кэшем.
        return ("quiet", "no-reset-at")

    if reset_at > now:
        # Окно активно — копим максимум процента в рамках ЭТОГО окна.
        # Именно максимум: utilization к концу окна может упасть, если
        # старая трата выпала из скользящих 5 часов, а «упирался ли»
        # выражает высшая точка.
        window = state.get("window")
        percent = snap.get("percent")
        if window is None or window.get("resetAt") != reset_at:
            state["window"] = {"resetAt": reset_at, "percentMax": percent}
        elif percent is not None:
            if window.get("percentMax") is None or percent > window["percentMax"]:
                window["percentMax"] = percent
        return ("quiet", "active")

    # Момент сброса прошёл.
    window = state.get("window")
    if window is None or window.get("resetAt") != reset_at:
        # Свидетеля нет: монитор не видел это окно живым — старт после
        # сброса или рестарт на замёрзшем кэше. Звук был бы враньём
        # о времени события; lastSignaledReset не ставим, чтобы не
        # заглушить возможный честный сигнал после свежего кэша.
        return ("quiet", "no-witness")

    if state.get("lastSignaledReset") != reset_at:
        if cfg.get("mode") == "any":
            state["lastSignaledReset"] = reset_at
            state["lastPlayAt"] = now
            return ("play", "reset-any")
        percent_max = window.get("percentMax")
        if percent_max is None:
            # Окно видели активным, но процента в нём так и не узнали.
            return ("quiet", "no-percent")
        if percent_max >= cfg.get("percent", 95):
            state["lastSignaledReset"] = reset_at
            state["lastPlayAt"] = now
            return ("play", "reset-threshold")
        return ("quiet", "below-threshold")

    # Этот сброс уже прозвучал. Повтор — если просят и пора: пока кэш
    # не обновился новым активным окном, «окно свободно, а работы нет»
    # остаётся правдой. Прекращение повторов наступает само, как только
    # resets_at станет будущим (пользователь начал работать).
    repeat_min = cfg.get("repeatMin", 0)
    last_play = state.get("lastPlayAt")
    if repeat_min > 0 and last_play is not None and now - last_play >= repeat_min * 60:
        state["lastPlayAt"] = now
        return ("repeat", "repeat")
    return ("quiet", "already-signaled")


# --- воспроизведение --------------------------------------------------
#
# Один слот плеера: сигнал не должен накладываться сам на себя, поэтому
# перед новым запуском живой ffplay глушится. Закончившийся процесс
# подбирается здесь же (poll) — отдельный поток-жнец для одного
# процесса на полчаса был бы избыточен.

_PLAY_LOCK = threading.Lock()
_PLAYER = {"proc": None, "no_player_logged": False, "no_mp3_logged": False}


def _stop_player() -> None:
    proc = _PLAYER["proc"]
    if proc is None:
        return
    if proc.poll() is None:  # ещё играет
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
    _PLAYER["proc"] = None


def play(play_sec: int = 0, reason: str = "manual") -> bool:
    """Проигрывает notification.mp3; True — звук запущен.

    Никогда не бросает исключений: монитор живёт в daemon-потоке
    сервера. Жалобы (нет плеера, нет файла) пишутся в журнал по одному
    разу за жизнь процесса — цикл раз в pollSec не должен заполнять
    журнал одной и той же строкой.
    """
    with _PLAY_LOCK:
        _stop_player()

        binary = shutil.which("ffplay")
        if binary is None:
            if not _PLAYER["no_player_logged"]:
                hook_log.log("limit-alert", "ffplay не найден в PATH — звук невозможен")
                _PLAYER["no_player_logged"] = True
            return False
        if not os.path.isfile(MP3_PATH):
            if not _PLAYER["no_mp3_logged"]:
                hook_log.log("limit-alert", f"нет файла {MP3_PATH}")
                _PLAYER["no_mp3_logged"] = True
            return False

        cmd = [binary, "-nodisp", "-autoexit", "-loglevel", "quiet"]
        if play_sec > 0:
            cmd += ["-t", str(play_sec)]
        cmd += ["-i", MP3_PATH]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
        except OSError as exc:
            hook_log.log("limit-alert", f"не удалось запустить ffplay: {exc}")
            return False
        _PLAYER["proc"] = proc
        hook_log.log("limit-alert",
                     f"звук запущен ({reason}, {play_sec or 'целиком'} с, pid {proc.pid})")
        return True


# --- состояние монитора -------------------------------------------------

_STATUS_LOCK = threading.Lock()
_MONITOR = {
    "enabled": False,
    "cfg": None,
    "snap": None,
    "state": {},
    "lastReason": None,
    "providerPause": False,
}


def _fmt(moment) -> str | None:
    if moment is None:
        return None
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(moment))


def status() -> dict:
    """Снимок монитора для GET /limit-reset-alert (копия, под локом)."""
    with _STATUS_LOCK:
        snap = dict(_MONITOR["snap"]) if _MONITOR["snap"] else None
        if snap and snap.get("resetAt") is not None:
            snap["resetAtIso"] = _fmt(snap["resetAt"])
        state = dict(_MONITOR["state"])
        for key in ("lastSignaledReset", "lastPlayAt"):
            if state.get(key) is not None:
                state[key + "Iso"] = _fmt(state[key])
        window = state.get("window")
        if isinstance(window, dict):
            state["window"] = dict(window)
        return {
            "enabled": _MONITOR["enabled"],
            "providerPause": _MONITOR["providerPause"],
            "cfg": dict(_MONITOR["cfg"]) if _MONITOR["cfg"] else None,
            "snap": snap,
            "state": state,
            "lastReason": _MONITOR["lastReason"],
            "mp3Present": os.path.isfile(MP3_PATH),
            "playerFound": shutil.which("ffplay") is not None,
        }


def test_play() -> dict:
    """Проверочный звук для POST /limit-reset-alert-test."""
    cfg = _monitor_config()
    started = play(cfg["playSec"], reason="test")
    return {"ok": started, "playSec": cfg["playSec"]}


def run_monitor() -> None:
    """Цикл мониторинга; крутится в daemon-потоке http-server.

    Каждая итерация целиком в try/except: исключение (битый кэш, упавший
    ffplay) не имеет права останавливать мониторинг — гасится только
    итерация. Тихие причины логируются при смене: «окно активно» и час
    спустя — не новость, а смена причины — маркер перехода.
    """
    hook_log.log("limit-alert", "монитор запущен")
    poll = DEFAULT_POLL_SEC
    while True:
        try:
            cfg = _monitor_config()
            poll = max(1, cfg["pollSec"])
            with _STATUS_LOCK:
                _MONITOR["cfg"] = cfg
                _MONITOR["enabled"] = cfg["enabled"]

            if cfg["enabled"] and account_switcher.active_account_is_oauth():
                snap = account_switcher.anthropic_usage_raw()
                now = time.time()
                with _STATUS_LOCK:
                    _MONITOR["providerPause"] = False
                    _MONITOR["snap"] = snap
                    action, reason = decide(_MONITOR["state"], snap, cfg, now)
                    if reason != _MONITOR["lastReason"]:
                        hook_log.log("limit-alert", f"состояние: {reason}")
                        _MONITOR["lastReason"] = reason
                if action in ("play", "repeat"):
                    play(cfg["playSec"], reason=reason)
            else:
                with _STATUS_LOCK:
                    _MONITOR["providerPause"] = cfg["enabled"]  # пауза только если включён
                    if cfg["enabled"]:
                        _MONITOR["snap"] = account_switcher.anthropic_usage_raw()
                    reason = "disabled" if not cfg["enabled"] else "provider"
                    if reason != _MONITOR["lastReason"]:
                        hook_log.log("limit-alert", f"состояние: {reason}")
                        _MONITOR["lastReason"] = reason
        except Exception as exc:
            hook_log.log("limit-alert", f"ошибка цикла мониторинга: {exc!r}")
        time.sleep(poll)
