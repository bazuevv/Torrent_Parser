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
resets_at; на новом окне счёт начинается заново. Наблюдение и сигнал
работают на любом активном аккаунте: сброса окна claude.ai ждут как
раз работая на стороннем провайдере (упёрся — переключился — ждёшь
звука). Провайдер гасит только повторы: кэш claude.ai на нём не
обновляется, и повтор звучал бы вечно.

Модуль читает claude-custom-config.toml сам (как hook_log), а не через
http-server: сервер импортирует limit_alert ради старта потока, и
обратный импорт был бы циклом. Ключи limitResetAlert* — горячие
(HOT_KEYS http-server): правка в окне настроек применяется следующим
циклом, без перезапуска. Дефолт в коде — «выключено»: битый TOML не
должен включать звук сам.
"""
import json
import os
import re
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
        "minVolume": 0,
        "duckOthers": False,
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
    cfg["minVolume"] = _int("limitResetAlertMinVolume", 0, 0, 100)
    duck = data.get("limitResetAlertDuckOthers", False)
    cfg["duckOthers"] = duck if isinstance(duck, bool) else False
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

    # Этот сброс уже прозвучал. Повтор — если просят, пора и разрешено:
    # на стороннем провайдере кэш claude.ai не обновится никогда, окно
    # «свободно» не сменится на «тратится», и повтор звучал бы вечно —
    # потому run_monitor ставит allowRepeat только на OAuth-логине.
    repeat_min = cfg.get("repeatMin", 0)
    last_play = state.get("lastPlayAt")
    if (repeat_min > 0 and cfg.get("allowRepeat", True)
            and last_play is not None and now - last_play >= repeat_min * 60):
        state["lastPlayAt"] = now
        return ("repeat", "repeat")
    return ("quiet", "already-signaled")


# --- звуковая сцена: громкость и чужие потоки ---------------------------
#
# На время сигнала глушатся чужие аудио-потоки (музыка, видео) и
# гарантируется слышимость: тихий дефолтный sink поднимается до
# limitResetAlertMinVolume. Единственный контроллер аудио здесь —
# wpctl (WirePlumber); pactl в системе нет. Всё толерантно: нет wpctl
# или странный вывод — сцена не трогается, сигнал играет как раньше.
#
# Мьют ≠ пауза: чужое видео продолжит идти без звука. Возврат —
# по завершении звука, кнопкой «Стоп» и, на случай смерти сервера,
# заявкой-файлом, которую подбирает следующий старт (recover_orphaned).

_WPCTL = {"path": None, "checked": False}
RESTORE_FILE = os.path.join(os.path.dirname(_HERE), "hooks-runtime",
                            "limit-alert-restore.json")


def _wpctl_binary():
    if not _WPCTL["checked"]:
        _WPCTL["path"] = shutil.which("wpctl")
        _WPCTL["checked"] = True
    return _WPCTL["path"]


def _run_wpctl(*args):
    """stdout успешного вызова; None — wpctl нет или вызов не удался."""
    binary = _wpctl_binary()
    if binary is None:
        return None
    try:
        proc = subprocess.run([binary, *args], capture_output=True,
                              text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 else None


def parse_wpctl_volume(text):
    """'Volume: 0.40 [MUTED]' → (0.40, True). None — не разобралось."""
    if not isinstance(text, str):
        return None
    m = re.search(r"Volume:\s*([0-9]*\.?[0-9]+)(\s*\[MUTED\])?", text)
    if not m:
        return None
    return (float(m.group(1)), bool(m.group(2)))


def parse_wpctl_streams(text):
    """Аудио-потоки из `wpctl status`: [(id, имя клиента)].

    Берутся только главные строки секции Streams блока Audio. У
    канальных строк (input_FL, monitor_FR и т.п.) отступ глубже —
    мьют применяется к узлу потока, а не к его портам; Sinks и
    Clients в секцию Streams не входят. id выровнены по правому
    краю, поэтому отступ главного знака — от 6 до 11 пробелов.
    """
    if not isinstance(text, str):
        return []
    lines = text.splitlines()
    start = end = None
    for i, line in enumerate(lines):
        if start is None and line.strip() == "Audio":
            start = i + 1
        elif start is not None and line and not line.startswith(" "):
            end = i
            break
    if start is None:
        return []
    block = lines[start:end] if end is not None else lines[start:]
    streams_at = None
    for i, line in enumerate(block):
        if "Streams:" in line:
            streams_at = i + 1
            break
    if streams_at is None:
        return []
    result = []
    for line in block[streams_at:]:
        if line and not line.startswith(" "):
            break
        m = re.match(r"^ {6,11}(\d+)\. +(.+?)\s*$", line)
        if m:
            # выравнивающий хвост (pid, версия) отрезается по двойному
            # пробелу — имени клиента он не принадлежит
            name = re.sub(r"\s{2,}.*$", "", m.group(2)).strip()
            result.append((int(m.group(1)), name))
    return result


def foreign_streams(streams):
    """Чужие потоки: наш ffplay в этот список не входит — он стартует
    ПОСЛЕ глушения и под мьют попасть не должен."""
    return [s for s in streams if s[1] != "ffplay"]


def duck(cfg):
    """Готовит сцену к сигналу; снимок — в restore()."""
    snap = {"volume": None, "muted": None, "streams": []}
    if cfg.get("duckOthers"):
        for sid, _name in foreign_streams(
                parse_wpctl_streams(_run_wpctl("status") or "")):
            if _run_wpctl("set-mute", str(sid), "1") is not None:
                snap["streams"].append(sid)
    min_volume = cfg.get("minVolume") or 0
    if min_volume > 0:
        got = parse_wpctl_volume(_run_wpctl("get-volume",
                                            "@DEFAULT_AUDIO_SINK@"))
        if got and (got[1] or got[0] < min_volume / 100.0):
            snap["volume"], snap["muted"] = got
            _run_wpctl("set-volume", "@DEFAULT_AUDIO_SINK@",
                       "%.2f" % (min_volume / 100.0))
            if got[1]:
                _run_wpctl("set-mute", "@DEFAULT_AUDIO_SINK@", "0")
    return snap


def restore(snap):
    """Возвращает сцену к исходному виду; толерантна к пропаже потоков."""
    if not isinstance(snap, dict):
        return
    for sid in snap.get("streams") or []:
        _run_wpctl("set-mute", str(sid), "0")
    if snap.get("volume") is not None:
        if snap.get("muted"):
            _run_wpctl("set-mute", "@DEFAULT_AUDIO_SINK@", "1")
        _run_wpctl("set-volume", "@DEFAULT_AUDIO_SINK@",
                   "%.2f" % snap["volume"])


def _scene_changed(snap):
    return bool(snap and (snap.get("streams")
                          or snap.get("volume") is not None))


def _write_restore_file(snap):
    """Заявка на случай смерти сервера до возврата сцены."""
    try:
        os.makedirs(os.path.dirname(RESTORE_FILE), exist_ok=True)
        tmp = RESTORE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(dict(snap, pid=os.getpid()), fh)
        os.replace(tmp, RESTORE_FILE)
    except OSError:
        pass


def _remove_restore_file():
    try:
        if os.path.isfile(RESTORE_FILE):
            os.remove(RESTORE_FILE)
    except OSError:
        pass


def _pid_is_server(pid):
    """Жив ли процесс с этим pid и наш ли это http-server.

    pid после os.execv сохраняется — заявка прежней жизни того же
    pid считается брошенной и восстанавливается.
    """
    try:
        os.kill(pid, 0)
        with open("/proc/%d/cmdline" % pid, "rb") as fh:
            cmd = fh.read().decode("utf-8", "replace")
        return "http-server.py" in cmd
    except (OSError, ValueError):
        return False


def recover_orphaned():
    """Подбирает заявку умершего сервера: чужие потоки не должны
    остаться замученными, а громкость — поднятой, навсегда."""
    try:
        with open(RESTORE_FILE, encoding="utf-8") as fh:
            snap = json.load(fh)
    except (OSError, ValueError):
        return False
    pid = snap.get("pid")
    if isinstance(pid, int) and pid != os.getpid() and _pid_is_server(pid):
        return False  # владелец жив (другой инстанс) — сцена ещё нужна
    restore(snap)
    _remove_restore_file()
    hook_log.log("limit-alert", "звуковая сцена восстановлена по заявке")
    return True


# --- воспроизведение --------------------------------------------------
#
# Один слот плеера: сигнал не должен накладываться сам на себя, поэтому
# перед новым запуском живой ffplay глушится. Закончившийся процесс
# подбирается здесь же (poll) — отдельный поток-жнец для одного
# процесса на полчаса был бы избыточен.

_PLAY_LOCK = threading.Lock()
_PLAYER = {"proc": None, "scene": None,
           "no_player_logged": False, "no_mp3_logged": False}


def _restore_scene() -> None:
    """Возвращает звуковую сцену, если она готовилась под сигнал."""
    scene = _PLAYER.pop("scene", None)
    if scene is not None:
        restore(scene)
        _remove_restore_file()


def _stop_player() -> None:
    proc = _PLAYER["proc"]
    if proc is None:
        _restore_scene()
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
    _restore_scene()


def reap_if_done() -> None:
    """Подбирает закончившийся ffplay, чтобы зомби не копился.

    Popen держит процесс до первого wait; если звук оборван извне
    (ручной pkill, завершение по -autoexit), без этой проверки
    defunct провисит до следующего сигнала. poll() и есть waitpid
    в режиме WNOHANG — вызов дешёвый, цикл может звать его каждые
    pollSec.
    """
    with _PLAY_LOCK:
        proc = _PLAYER["proc"]
        if proc is not None and proc.poll() is not None:
            _PLAYER["proc"] = None
            _restore_scene()


def play(cfg: dict, reason: str = "manual") -> bool:
    """Проигрывает notification.mp3; True — звук запущен.

    Перед запуском готовит звуковую сцену (duck): глушит чужие
    потоки и поднимает громкость — до старта ffplay, чтобы он сам
    под мьют не попал. Никогда не бросает исключений: монитор живёт
    в daemon-потоке сервера. Жалобы (нет плеера, нет файла) пишутся
    в журнал по одному разу за жизнь процесса.
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
        if cfg.get("playSec", 0) > 0:
            cmd += ["-t", str(cfg["playSec"])]

        scene = duck(cfg)
        try:
            proc = subprocess.Popen(cmd + ["-i", MP3_PATH],
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
        except OSError as exc:
            restore(scene)  # сцена подготовлена, а звука не будет
            hook_log.log("limit-alert", f"не удалось запустить ffplay: {exc}")
            return False
        _PLAYER["proc"] = proc
        _PLAYER["scene"] = scene
        if _scene_changed(scene):
            _write_restore_file(scene)
        hook_log.log("limit-alert",
                     f"звук запущен ({reason}, {cfg.get('playSec', 0) or 'целиком'} с, "
                     f"pid {proc.pid}, глушу потоков: {len(scene['streams'])})")
        return True


# --- состояние монитора -------------------------------------------------

_STATUS_LOCK = threading.Lock()
_MONITOR = {
    "enabled": False,
    "cfg": None,
    "snap": None,
    "state": {},
    "lastReason": None,
    "providerActive": False,
}


def _fmt(moment) -> str | None:
    if moment is None:
        return None
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(moment))


def status() -> dict:
    """Снимок монитора для GET /limit-reset-alert (копия, под локом)."""
    playing = is_playing()
    with _PLAY_LOCK:
        ducked = bool(_PLAYER.get("scene"))
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
            "providerActive": _MONITOR["providerActive"],
            "playing": playing,
            "ducked": ducked,
            "volume": _MONITOR.get("volume"),
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
    started = play(cfg, reason="test")
    return {"ok": started, "playSec": cfg["playSec"]}


def is_playing() -> bool:
    """Играет ли звук прямо сейчас.

    Кнопка «Стоп» в webview опрашивает это поле и показывается только
    пока оно истинно: постоянная кнопка останавливать нечего.
    """
    with _PLAY_LOCK:
        proc = _PLAYER["proc"]
        return proc is not None and proc.poll() is None


def stop() -> bool:
    """Гасит играющий звук; True — звук был и остановлен.

    Тот же слот плеера, что и у монитора: остановка человеком и
    старт нового сигнала не конфликтуют.
    """
    with _PLAY_LOCK:
        was = _PLAYER["proc"] is not None and _PLAYER["proc"].poll() is None
        _stop_player()
    if was:
        hook_log.log("limit-alert", "звук остановлен вручную (кнопка «Стоп»)")
    return was


def run_monitor() -> None:
    """Цикл мониторинга; крутится в daemon-потоке http-server.

    Каждая итерация целиком в try/except: исключение (битый кэш, упавший
    ffplay) не имеет права останавливать мониторинг — гасится только
    итерация. Тихие причины логируются при смене: «окно активно» и час
    спустя — не новость, а смена причины — маркер перехода.
    """
    hook_log.log("limit-alert", "монитор запущен")
    recover_orphaned()
    poll = DEFAULT_POLL_SEC
    while True:
        try:
            reap_if_done()
            cfg = _monitor_config()
            poll = max(1, cfg["pollSec"])
            # Громкость — для статуса (кнопка «Стоп» опрашивает раз в
            # секунду): живой wpctl-вызов раз в pollSec, не чаще.
            with _STATUS_LOCK:
                _MONITOR["volume"] = parse_wpctl_volume(
                    _run_wpctl("get-volume", "@DEFAULT_AUDIO_SINK@"))

            if cfg["enabled"]:
                # Наблюдение — на любом активном аккаунте: сброса окна
                # claude.ai ждут как раз работая на стороннем провайдере
                # (упёрся — переключился — ждёшь звука). Провайдер гасит
                # только повторы: кэш claude.ai на нём не обновляется, и
                # «окно свободно» не сменится на «тратится» само.
                cfg["allowRepeat"] = account_switcher.active_account_is_oauth()
                snap = account_switcher.anthropic_usage_raw()
                now = time.time()
                with _STATUS_LOCK:
                    _MONITOR["cfg"] = cfg
                    _MONITOR["enabled"] = True
                    _MONITOR["providerActive"] = not cfg["allowRepeat"]
                    _MONITOR["snap"] = snap
                    action, reason = decide(_MONITOR["state"], snap, cfg, now)
                    if reason != _MONITOR["lastReason"]:
                        hook_log.log("limit-alert", f"состояние: {reason}")
                        _MONITOR["lastReason"] = reason
                if action in ("play", "repeat"):
                    play(cfg, reason=reason)
            else:
                with _STATUS_LOCK:
                    _MONITOR["cfg"] = cfg
                    _MONITOR["enabled"] = False
                    if _MONITOR["lastReason"] != "disabled":
                        hook_log.log("limit-alert", "состояние: disabled")
                        _MONITOR["lastReason"] = "disabled"
        except Exception as exc:
            hook_log.log("limit-alert", f"ошибка цикла мониторинга: {exc!r}")
        time.sleep(poll)
