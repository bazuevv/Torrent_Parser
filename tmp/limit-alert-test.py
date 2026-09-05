#!/usr/bin/env python3
"""Стенд автомата сигнала сброса лимита (limit_alert.decide).

Вырезает ничего не надо: decide — чистая функция, стенд кормит её
фиктивными снимками с ручным `now` и проверяет переходы состояний.
Прогон: python3 tmp/limit-alert-test.py. Живой сброс окна ждать
пять часов нельзя — всё поведение проверяется здесь, на времени,
которое движется скачками.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", ".claude", "hooks"))

import limit_alert  # noqa: E402

FAILURES = []
CHECKS = 0


def check(name, cond, details=""):
    global CHECKS
    CHECKS += 1
    if cond:
        print(f"  ok  {name}")
    else:
        print(f" FAIL {name} {details}")
        FAILURES.append(name)


def cfg(mode="threshold", percent=95, repeatMin=0, playSec=0, allowRepeat=True):
    return {"mode": mode, "percent": percent, "repeatMin": repeatMin,
            "playSec": playSec, "pollSec": 30, "enabled": True,
            "allowRepeat": allowRepeat}


def snap(reset_at, percent=100, uuid="uuid-A", age_sec=60,
         signal_on_rollover=False):
    return {"percent": percent, "resetAt": reset_at,
            "accountUuid": uuid, "ageSec": age_sec,
            "signalOnRollover": signal_on_rollover}


T0 = 1_000_000.0           # «сейчас»
RESET1 = T0 + 3600.0       # сброс первого окна через час
RESET2 = T0 + 3600.0 + 5 * 3600.0  # сброс следующего окна

print("1. сброс после заполненного окна (threshold 95, было 97%)")
state = {}
act, why = limit_alert.decide(state, snap(RESET1, percent=97), cfg(), T0)
check("окно активно — тихо", act == "quiet" and why == "active", (act, why))
act, why = limit_alert.decide(state, snap(RESET1, percent=100), cfg(), T0 + 1800)
check("процент растёт — тихо", act == "quiet", (act, why))
act, why = limit_alert.decide(state, snap(RESET1, percent=100), cfg(), RESET1 + 45)
check("сброс настал — звук", act == "play" and why == "reset-threshold", (act, why))
check("запомнен сброс", state["lastSignaledReset"] == RESET1)
check("percentMax — 100", state["window"]["percentMax"] == 100)

print("2. повтор этого сброса — один раз (repeatMin=0)")
act, why = limit_alert.decide(state, snap(RESET1, percent=100), cfg(), RESET1 + 300)
check("повтора нет", act == "quiet" and why == "already-signaled", (act, why))

print("3. окно ниже порога (threshold 95, было 40%)")
state = {}
limit_alert.decide(state, snap(RESET1, percent=40), cfg(), T0)
act, why = limit_alert.decide(state, snap(RESET1, percent=40), cfg(), RESET1 + 10)
check("сброс тихий — ниже порога", act == "quiet" and why == "below-threshold", (act, why))

print("4. режим any: звук на любой заполненности")
state = {}
limit_alert.decide(state, snap(RESET1, percent=3), cfg(mode="any"), T0)
act, why = limit_alert.decide(state, snap(RESET1, percent=3), cfg(mode="any"), RESET1 + 10)
check("any — звук даже при 3%", act == "play" and why == "reset-any", (act, why))

print("5. no-witness: старт на уже истёкшем окне — тихо в обеих режимах")
state = {}
act, why = limit_alert.decide(state, snap(T0 - 7200, percent=100), cfg(), T0)
check("threshold — тихо", act == "quiet" and why == "no-witness", (act, why))
state = {}
act, why = limit_alert.decide(state, snap(T0 - 7200, percent=100), cfg(mode="any"), T0)
check("any — тоже тихо", act == "quiet" and why == "no-witness", (act, why))
check("сброс не запомнен (не заглушен будущий честный сигнал)",
      state["lastSignaledReset"] is None)

print("6. resetAt=None — тихо, состояние не трогаем")
state = {}
limit_alert.decide(state, snap(RESET1, percent=97), cfg(), T0)
window_before = dict(state["window"])
act, why = limit_alert.decide(state, snap(None, percent=97), cfg(), T0)
check("тихо без якоря", act == "quiet" and why == "no-reset-at", (act, why))
check("окно-свидетель уцелело", state["window"] == window_before)

print("7. переинициализация на новом resetAt (максимум не переносится)")
state = {}
limit_alert.decide(state, snap(RESET1, percent=97), cfg(), T0)
act, why = limit_alert.decide(state, snap(RESET2, percent=10), cfg(), RESET1 + 10)
check("новое окно активно", act == "quiet" and why == "active", (act, why))
check("percentMax начался заново", state["window"]["resetAt"] == RESET2
      and state["window"]["percentMax"] == 10)
act, why = limit_alert.decide(state, snap(RESET2, percent=10), cfg(), RESET2 + 5)
check("новое окно ниже порога — сброс тихий",
      act == "quiet" and why == "below-threshold", (act, why))

print("8. повтор по repeatMin и его прекращение новым активным окном")
state = {}
limit_alert.decide(state, snap(RESET1, percent=100), cfg(repeatMin=15), T0)
limit_alert.decide(state, snap(RESET1, percent=100), cfg(repeatMin=15), RESET1 + 5)
act, why = limit_alert.decide(state, snap(RESET1, percent=100),
                              cfg(repeatMin=15), RESET1 + 600)
check("через 10 минут — ещё тихо", act == "quiet", (act, why))
act, why = limit_alert.decide(state, snap(RESET1, percent=100),
                              cfg(repeatMin=15), RESET1 + 910)
check("через 15 минут — повтор", act == "repeat" and why == "repeat", (act, why))
act, why = limit_alert.decide(state, snap(RESET2, percent=5),
                              cfg(repeatMin=15), RESET1 + 920)
check("кэш обновился активным окном — тихо", act == "quiet" and why == "active", (act, why))
act, why = limit_alert.decide(state, snap(RESET2, percent=5),
                              cfg(repeatMin=15), RESET2 + 100)
check("новый сброс — новый звук по правилам",
      act == "quiet" and why == "below-threshold", (act, why))

print("9. смена accountUuid сбрасывает историю окна")
state = {}
limit_alert.decide(state, snap(RESET1, percent=100), cfg(), T0)
act, why = limit_alert.decide(state, snap(RESET1, percent=100, uuid="uuid-B"),
                              cfg(), RESET1 + 10)
check("чужой логин на том же сбросе — тихо без свидетеля",
      act == "quiet" and why == "no-witness", (act, why))
act, why = limit_alert.decide(state, snap(RESET1, percent=100, uuid="uuid-A"),
                              cfg(), RESET1 + 20)
check("вернулись на логин — истории больше нет",
      act == "quiet" and why == "no-witness", (act, why))

print("10. snap=None — тихо, состояние живёт")
state = {}
limit_alert.decide(state, snap(RESET1, percent=97), cfg(), T0)
act, why = limit_alert.decide(state, None, cfg(), T0)
check("нет данных — тихо", act == "quiet" and why == "no-data", (act, why))
check("окно-свидетель уцелело", state["window"]["resetAt"] == RESET1)

print("11. percent=None при threshold — тихий сброс с причиной")
state = {}
limit_alert.decide(state, snap(RESET1, percent=None), cfg(), T0)
act, why = limit_alert.decide(state, snap(RESET1, percent=None), cfg(), RESET1 + 5)
check("процент неизвестен — тихо", act == "quiet" and why == "no-percent", (act, why))
state = {}
limit_alert.decide(state, snap(RESET1, percent=None), cfg(mode="any"), T0)
act, why = limit_alert.decide(state, snap(RESET1, percent=None),
                              cfg(mode="any"), RESET1 + 5)
check("any при unknown percent — звук (заполненность не важна)",
      act == "play" and why == "reset-any", (act, why))

print("12. провайдер: сигнал звучит, повторы погашены (allowRepeat=False)")
state = {}
limit_alert.decide(state, snap(RESET1, percent=100),
                   cfg(repeatMin=10, allowRepeat=False), T0)
act, why = limit_alert.decide(state, snap(RESET1, percent=100),
                              cfg(repeatMin=10, allowRepeat=False), RESET1 + 5)
check("первичный звук на провайдере — есть",
      act == "play" and why == "reset-threshold", (act, why))
act, why = limit_alert.decide(state, snap(RESET1, percent=100),
                              cfg(repeatMin=10, allowRepeat=False), RESET1 + 1200)
check("повтор на провайдере — нет", act == "quiet", (act, why))
act, why = limit_alert.decide(state, snap(RESET1, percent=100),
                              cfg(repeatMin=10, allowRepeat=True), RESET1 + 1200)
check("на OAuth тот же срок — повтор есть",
      act == "repeat" and why == "repeat", (act, why))

print("13. парсеры wpctl: громкость и чужие потоки")
vol = limit_alert.parse_wpctl_volume("Volume: 0.40 [MUTED]")
check("громкость с мьютом", vol == (0.4, True), repr(vol))
vol = limit_alert.parse_wpctl_volume("Volume: 1.00")
check("громкость обычная", vol == (1.0, False), repr(vol))
check("мусор → None", limit_alert.parse_wpctl_volume(None) is None
      and limit_alert.parse_wpctl_volume("oops") is None)
WPCTL_STATUS = """
PipeWire 'pipewire-0' [1.0.5]
 └─ Clients:
        82. Firefox                             [1.0.5, pid:13364]
       101. VLC media player (LibVLC 3.0.20)    [1.0.5, pid:917700]

Audio
 ├─ Devices:
 │      46. GP106 High Definition Audio Controller [alsa]
 ├─ Sinks:
 │      33. Built-in Audio Digital Stereo (IEC958) [vol: 1.00]
 │  *   48. GP106 High Definition Audio Controller Digital Stereo (HDMI) [vol: 1.00]
 ├─ Sink endpoints:
 ├─ Sources:
 │      42. Built-in Audio Analog Stereo        [vol: 1.00]
 └─ Streams:
        63. gnome-remote-desktop-daemon
             67. input_FL        < ALC887-VD Digital:monitor_FL	[init]
             69. monitor_FR
        84. speech-dispatcher-dummy
             87. output_FR       > HDMI 0:playback_FR	[init]
             88. output_FL       > HDMI 0:playback_FL	[init]
       105. VLC media player (LibVLC 3.0.20)
            102. output_FR       > HDMI 0:playback_FR	[paused]
            106. output_FL       > HDMI 0:playback_FL	[paused]
       120. ffplay
            121. output_FL       > HDMI 0:playback_FL	[active]
            122. output_FR       > HDMI 0:playback_FR	[active]

Video
 └─ Streams:
        99. firefox
"""
streams = limit_alert.parse_wpctl_streams(WPCTL_STATUS)
ids = [s[0] for s in streams]
check("только playback-потоки собраны",
      63 not in ids and 84 in ids and 105 in ids and 120 in ids, repr(streams))
check("capture/monitor и канальные строки не попали",
      63 not in ids and 67 not in ids and 69 not in ids)
check("Sinks и Clients не попали",
      33 not in ids and 48 not in ids and 82 not in ids and 101 not in ids)
check("видео-потоки не попали", 99 not in ids)
check("3-значный id с меньшим отступом распознан",
      (105, "VLC media player (LibVLC 3.0.20)") in streams, repr(streams))
foreign = limit_alert.foreign_streams(streams)
check("свой ffplay отфильтрован",
      120 not in [s[0] for s in foreign] and len(foreign) == 2, repr(foreign))

print("14. OpenAI: новое окно подтверждает сброс старого")
state = {}
limit_alert.decide(
    state, snap(RESET1, percent=98, uuid="openai-A",
                signal_on_rollover=True), cfg(), T0,
)
act, why = limit_alert.decide(
    state, snap(RESET2, percent=0, uuid="openai-A",
                signal_on_rollover=True), cfg(), RESET1 + 5,
)
check("rollover после порога — звук",
      act == "play" and why == "reset-rollover-threshold", (act, why))
check("свидетель уже относится к новому окну",
      state["window"]["resetAt"] == RESET2
      and state["window"]["percentMax"] == 0, state)

print("15. конфиг: битый/отсутствующий TOML — монитор выключен")
saved = limit_alert.CONFIG_PATH
limit_alert.CONFIG_PATH = os.path.join(os.path.dirname(saved), "нет-такого.toml")
c = limit_alert._monitor_config()
check("нет файла — выключено", c["enabled"] is False)
limit_alert.CONFIG_PATH = saved

print()
if FAILURES:
    print(f"ПРОВАЛЕНО: {len(FAILURES)} из {CHECKS}: {', '.join(FAILURES)}")
    sys.exit(1)
print(f"OK, {CHECKS} проверок")
