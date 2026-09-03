#!/usr/bin/env python3
"""
Статистика prompt-кэша сессии по локальному transcript-файлу.

Две точки входа:
  * `collect()` — библиотечная функция, которую дёргает http-server.py
    на GET /cache-usage (кнопка Cache в футере webview);
  * CLI `python3 cache_usage.py <transcript.jsonl>` — человекочитаемый
    отчёт для ручных прогонов и экспериментов с TTL.

Зачем: Claude Code не показывает ни размер контекста, ни попадания
и промахи кэша, ни во что это обходится. Всё это есть в JSONL
транскрипте сессии — каждый ответ ассистента несёт блок `usage`
с полями input_tokens / cache_creation_input_tokens /
cache_read_input_tokens / output_tokens. Здесь они агрегируются.

Как определяется попадание. API не сообщает вердикт напрямую, но его
видно из арифметики: при попадании `cache_read` очередного запроса
равен сумме `cache_read + cache_creation` предыдущего — весь прежний
префикс прочитался, дописалась только дельта хода. Если прочитано
заметно меньше — префикс не нашли и переписали заново.

Производительность. Транскрипты длинных сессий доходят до десятков
мегабайт, полный разбор на каждое нажатие кнопки был бы заметен.
Поэтому разбор инкрементальный: в hooks-runtime/cache-usage-<key>.json
хранится байтовый офсет и накопленные суммы, при следующем вызове
читается только дописанный хвост. При усечении или подмене файла
(размер меньше офсета) состояние сбрасывается и файл перечитывается
целиком. На 72 МБ первый прогон занимает ~1.0 с, последующие ~0.03 с.

Ограничение: множитель записи в кэш зависит от TTL (1.25x для пяти
минут, 2x для часа), а сам TTL в транскрипте не фиксируется. Берём 2x —
Claude Code по умолчанию использует часовой TTL. При уходе в usage
credits TTL падает до пяти минут, и тогда оценка записи завышена;
на итоговое соотношение это влияет слабо, потому что чтений на порядки
больше, чем записей.
"""
import json
import os
import sys
from datetime import datetime

HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(os.path.dirname(HOOKS_DIR), "hooks-runtime")

# Ставки Claude API, $ за 1M токенов: (input, output).
# Ключ — префикс идентификатора модели, выбирается самое длинное
# совпадение, чтобы claude-opus-4-8 не поймался правилом claude-opus.
RATES = {
    "claude-fable-5": (10.0, 50.0),
    "claude-mythos-5": (10.0, 50.0),
    "claude-mythos-preview": (10.0, 50.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4": (5.0, 25.0),
    "claude-opus-3": (15.0, 75.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-haiku-4": (1.0, 5.0),
}
DEFAULT_RATE = (5.0, 25.0)

CACHE_WRITE_MULT = 2.0  # часовой TTL; пятиминутный дал бы 1.25
CACHE_READ_MULT = 0.1

MAX_TRACKED_MISSES = 20  # хвост промахов, который отдаём в UI

# Длина ленты истории для панели. Больше промахов, потому что к ним
# добавляются переключения модели и аккаунта: при двадцати промахах
# и десятке переключений хвост в сорок записей показывает всё
# и не заставляет панель расти без конца.
MAX_HISTORY_ITEMS = 40

# Паузы перед промахами храним отдельно и дольше: по ним считается
# доля ранних потерь кэша (индикатор Mood). Двадцати записей miss_log
# для этого мало — в длинной сессии доля вышла бы заниженной, а по
# одному числу на промах хвост в полтысячи записей ничего не весит.
MAX_TRACKED_GAPS = 500

# Сколько ходов держим в ряду для истории Mood. Одна запись — четыре
# числа, так что три тысячи ходов это десятки килобайт: дешевле, чем
# перечитывать транскрипт на каждое открытие окна.
MAX_TRACKED_TURNS = 3000

# Версия схемы файла состояния. Инкрементировать при любом изменении
# набора полей: старое состояние тогда отбрасывается и транскрипт
# перечитывается целиком. Без этого добавленное поле молча остаётся
# пустым на всех сессиях, у которых состояние уже накоплено.
STATE_VERSION = 6


def rate_for(model: str):
    best = None
    for prefix, value in RATES.items():
        if model.startswith(prefix):
            if best is None or len(prefix) > len(best[0]):
                best = (prefix, value)
    return best[1] if best else DEFAULT_RATE


def human(n: float) -> str:
    """1234567890 -> '1.2B', 1234567 -> '1.2M', 12345 -> '12.3k'."""
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(int(n))


def blank_state() -> dict:
    return {
        "v": STATE_VERSION,
        "offset": 0,
        "requests": 0,
        "read": 0,
        "write": 0,
        "fresh": 0,
        "output": 0,
        "misses": 0,
        "rewritten": 0,  # токены, переписанные после промахов
        "miss_log": [],  # хвост последних промахов для панели
        "miss_gaps": [],  # паузы перед промахами, мин (для доли ранних)
        "context_peak": 0,  # самый большой контекст хода за сессию
        # Ряд ходов для истории Mood: по записи на ход. Храним сырьё
        # (был ли промах, какая пауза, какой контекст), а не готовое
        # значение шкалы — сравнение паузы с TTL делается при выдаче,
        # поэтому правка cacheKeepaliveTtlMinutes пересчитывает и уже
        # накопленную историю, ровно как текущее положение стрелки.
        "turns": [],
        "model": "",
        "started": "",
        "prev": None,  # {"ts": iso, "rd": int, "wr": int, "sig": [...], "model": str}
        "last": None,  # тот же ход + verdict/gap
    }


def parse_ts(value: str):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def consume(state: dict, path: str) -> None:
    """Дочитывает транскрипт с сохранённого офсета и обновляет суммы."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return

    if size < state["offset"]:
        # файл усечён или подменён — начинаем заново
        state.update(blank_state())

    if size == state["offset"]:
        return

    with open(path, "rb") as fh:
        fh.seek(state["offset"])
        chunk = fh.read()

    # Хвост без перевода строки — недописанная запись, оставляем на
    # следующий вызов, иначе поймаем половину JSON-объекта.
    trailing = chunk.rfind(b"\n")
    if trailing == -1:
        return
    consumed = trailing + 1
    text = chunk[:consumed].decode("utf-8", errors="replace")

    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = rec.get("message")
        if not isinstance(msg, dict):
            continue
        usage = msg.get("usage")
        if not isinstance(usage, dict):
            continue

        rd = usage.get("cache_read_input_tokens") or 0
        wr = usage.get("cache_creation_input_tokens") or 0
        fresh = usage.get("input_tokens") or 0
        out = usage.get("output_tokens") or 0
        if not (rd or wr or fresh):
            continue  # служебная запись с нулевой usage

        sig = [fresh, wr, rd, out]
        prev = state["prev"]
        if prev is not None and prev["sig"] == sig:
            continue  # дубль той же записи (стрим отдаёт её дважды)

        # Модель этого хода. `<synthetic>` — служебная отметка самого
        # CLI (сообщения об ошибках API), а не модель; сегодня такие
        # записи и так отсеиваются нулевой usage выше, но принять их
        # за смену модели было бы обидной ошибкой.
        turn_model = msg.get("model")
        if not isinstance(turn_model, str) or turn_model in ("", "<synthetic>"):
            turn_model = state.get("model") or ""

        ts_raw = rec.get("timestamp") or ""
        verdict = "старт"
        gap_min = None
        if prev is not None:
            expected = prev["rd"] + prev["wr"]
            ts_now = parse_ts(ts_raw)
            ts_prev = parse_ts(prev["ts"])
            if ts_now and ts_prev:
                gap_min = (ts_now - ts_prev).total_seconds() / 60.0
            if expected <= 0:
                verdict = "н/д"
            elif rd >= expected * 0.95:
                verdict = "попадание"
            elif rd >= expected * 0.5:
                verdict = "частичное"
            else:
                verdict = "промах"
            if verdict in ("промах", "частичное"):
                state["misses"] += 1
                state["rewritten"] += wr
                state["miss_log"].append({
                    "ts": ts_raw,
                    "gap": round(gap_min, 1) if gap_min is not None else None,
                    "expected": expected,
                    "read": rd,
                    "written": wr,
                    "verdict": verdict,
                    # Модель хода и предыдущего: по их расхождению
                    # история объясняет промах сменой модели.
                    "model": turn_model,
                    "prev_model": prev.get("model") or "",
                })
                del state["miss_log"][:-MAX_TRACKED_MISSES]
                if gap_min is not None:
                    # Промахи без разобранной метки времени пропускаем:
                    # неизвестную паузу нельзя сравнить с TTL, а
                    # записать её в ранние — значит соврать.
                    state["miss_gaps"].append(round(gap_min, 2))
                    del state["miss_gaps"][:-MAX_TRACKED_GAPS]

        if not state["started"] and ts_raw:
            state["started"] = ts_raw
        # Контекст хода — то, что модель прочитала плюс дописала.
        # Держим максимум за сессию: «сколько сейчас» видно из last,
        # а вот докуда окно вообще раскрывалось, по последнему ходу
        # уже не восстановить — компактификация обнуляет контекст.
        state["context_peak"] = max(state["context_peak"], rd + wr)
        state["turns"].append({
            "ts": ts_raw,
            # Промах в смысле Mood — потерянный кэш, включая частичный.
            "miss": verdict in ("промах", "частичное"),
            "gap": round(gap_min, 2) if gap_min is not None else None,
            "ctx": rd + wr,
            # Первый ход шанса не имел: кэшу неоткуда было взяться.
            "chance": prev is not None,
            # Модель хода. Хранится у каждого хода, а не только
            # последняя (state["model"]): смена модели рвёт префикс,
            # и промах сразу после неё закономерен. Но решать, какой
            # промах чем объяснён, положено при выдаче — здесь только
            # сырьё, как с паузой и TTL.
            "model": turn_model,
        })
        del state["turns"][:-MAX_TRACKED_TURNS]
        state["requests"] += 1
        state["read"] += rd
        state["write"] += wr
        state["fresh"] += fresh
        state["output"] += out
        if turn_model:
            state["model"] = turn_model

        state["prev"] = {"ts": ts_raw, "rd": rd, "wr": wr, "sig": sig,
                         "model": turn_model}
        state["last"] = {
            "ts": ts_raw,
            "read": rd,
            "write": wr,
            "verdict": verdict,
            "gap": round(gap_min, 1) if gap_min is not None else None,
        }

    state["offset"] += consumed


def _load_state(state_path: str) -> dict:
    state = blank_state()
    try:
        with open(state_path, encoding="utf-8") as fh:
            loaded = json.load(fh)
    except Exception:
        return state  # нет файла или он битый — считаем с нуля
    if not isinstance(loaded, dict) or "offset" not in loaded:
        return state
    if loaded.get("v") != STATE_VERSION:
        return state  # схема сменилась — перечитываем транскрипт целиком
    state.update(loaded)
    return state


def _save_state(state_path: str, state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(state_path), exist_ok=True)
        tmp = state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False)
        os.replace(tmp, state_path)
    except Exception:
        pass  # без кэша состояния просто перечитаем файл целиком


def collect(transcript_path: str, state_dir: str = STATE_DIR,
            use_state: bool = True, ttl_minutes: float = 60.0) -> dict:
    """Считает статистику кэша по транскрипту и возвращает плоский dict.

    `use_state=False` отключает инкрементальный кэш — файл читается
    целиком. Нужно для одноразовых прогонов, где не хочется мусорить
    в hooks-runtime.

    `ttl_minutes` — время жизни кэша. По нему промахи делятся на
    неизбежные (пауза длиннее TTL: кэш истёк бы сам) и ранние (пауза
    короче: кэш был жив, но не сработал). Сравнение делается здесь,
    а не в state, чтобы правка cacheKeepaliveTtlMinutes применялась
    к уже накопленной сессии, а не только к будущим промахам.
    """
    if not transcript_path or not os.path.isfile(transcript_path):
        return {"ok": False, "error": "transcript not found"}

    key = os.path.basename(transcript_path)
    if key.endswith(".jsonl"):
        key = key[:-6]
    state_path = os.path.join(state_dir, f"cache-usage-{key}.json")

    state = _load_state(state_path) if use_state else blank_state()
    try:
        consume(state, transcript_path)
    except Exception as exc:
        return {"ok": False, "error": f"parse failed: {exc}"}
    if use_state:
        _save_state(state_path, state)

    if not state["requests"] or not state["last"]:
        return {"ok": False, "error": "no usage records yet"}

    in_rate, out_rate = rate_for(state["model"])
    cost = (
        state["write"] / 1e6 * in_rate * CACHE_WRITE_MULT
        + state["read"] / 1e6 * in_rate * CACHE_READ_MULT
        + state["fresh"] / 1e6 * in_rate
        + state["output"] / 1e6 * out_rate
    )
    # Без кэша всё, что читалось и писалось, шло бы как свежий input.
    naive = (
        (state["read"] + state["write"] + state["fresh"]) / 1e6 * in_rate
        + state["output"] / 1e6 * out_rate
    )

    # Ранние промахи и знаменатель считаются по размеченному ряду ходов
    # — тому же, что уходит в историю Mood. Раньше здесь был отдельный
    # счёт по `miss_gaps` и `requests`, и два источника могли разойтись:
    # пауз хранится 500, ходов 3000. Теперь источник один.
    events = account_events()
    marked = explain_turns(state.get("turns") or [], events, ttl_minutes)
    early = sum(1 for t in marked if t.get("early"))
    # Знаменатель — ходы, на которых кэш вообще мог сработать. Первый
    # запрос сессии не в счёт: читать ему ещё нечего, и промахом он
    # не считается (verdict «старт»). Ходы после смены модели или
    # аккаунта тоже: у них шанса не было.
    chances = sum(1 for t in marked if t.get("counts"))
    explained = sum(1 for t in marked if t.get("miss") and t.get("explain"))

    last = state["last"]
    return {
        "ok": True,
        "session": key,
        "model": state["model"],
        "started": state["started"],
        "requests": state["requests"],
        "context": last["read"] + last["write"],
        "context_peak": state["context_peak"],
        "last": last,
        # Промахи, которых не должно было быть: кэш ещё жил, но ход
        # его не застал. Отдаём и знаменатель — по ним индикатор Mood
        # считает, насколько часто кэш теряется впустую.
        "early_misses": early,
        "early_chances": chances,
        # Промахи, у которых нашлась причина: сменилась модель или
        # аккаунт. В early они не идут — но знать, сколько их, полезно:
        # это разница между «кэш течёт» и «я много переключался».
        "explained_misses": explained,
        "ttl_minutes": ttl_minutes,
        "read": state["read"],
        "write": state["write"],
        "fresh": state["fresh"],
        "output": state["output"],
        "misses": state["misses"],
        "rewritten": state["rewritten"],
        # miss_log остаётся ради webview, загруженных до этой правки:
        # bootstrap перечитывается только при Reload Window, и такое
        # окно живёт до ближайшей перезагрузки. Новый UI читает history.
        "miss_log": state["miss_log"],
        "history": build_history(state, marked, events),
        "cost": round(cost, 2),
        "cost_naive": round(naive, 2),
        "ratio": round(naive / cost, 1) if cost > 0 else 0.0,
        # Потеря — разница между уплаченным (запись) и тем, что стоило
        # бы попадание (чтение). Полная стоимость записи завышала бы
        # оценку: даже при попадании префикс не бесплатен.
        "wasted": round(
            state["rewritten"] / 1e6 * in_rate
            * (CACHE_WRITE_MULT - CACHE_READ_MULT), 2
        ),
        "rates": {
            "input": in_rate,
            "output": out_rate,
            "write_mult": CACHE_WRITE_MULT,
            "read_mult": CACHE_READ_MULT,
        },
    }


def build_history(state: dict, marked: list, events: list) -> list[dict]:
    """Лента событий сессии: промахи вперемешку с переключениями.

    Раньше панель показывала только промахи, и они выглядели
    беспричинными: «переписано 900k» без единого намёка, что за минуту
    до этого сменили аккаунт. Причина и следствие теперь стоят рядом
    в одной ленте, отсортированной по времени.

    Что сюда НЕ попадает:

    * промахи с нулевой перезаписью — это частичные попадания, кэш
      сработал и терять было нечего; в ленте они только шумят;
    * объяснённые промахи — сразу после смены модели или аккаунта кэш
      переписывается ГАРАНТИРОВАННО, это не наблюдение, а следствие
      самого переключения. Отдельная строка о нём не сообщает ничего
      сверх того, что уже сказано строкой события, только дублирует
      её тем же временем. Строка события остаётся, строка промаха — нет;
    * откаты переключения — их отфильтровал `read_account_events()`:
      для пользователя такого переключения не было.

    Событие переключения, случившееся после последнего хода, всё равно
    показывается: переключились, открыли панель — и увидели, что это
    зафиксировано, не дожидаясь следующего запроса к модели.
    """
    items = []
    by_ts = {t.get("ts"): t for t in marked if t.get("ts")}

    for miss in state.get("miss_log") or []:
        if (miss.get("written") or 0) <= 0:
            continue
        turn = by_ts.get(miss.get("ts")) or {}
        if turn.get("explain"):
            # Гарантированное следствие переключения — не наблюдение.
            # Само переключение уже добавится ниже отдельной строкой.
            continue
        items.append({
            "kind": "miss",
            "ts": miss.get("ts") or "",
            "gap": miss.get("gap"),
            "written": miss.get("written") or 0,
            "verdict": miss.get("verdict") or "",
        })

    seen_events = set()
    for turn in marked:
        if turn.get("explain") == "model":
            items.append({
                "kind": "model",
                "ts": turn.get("ts") or "",
                "from": turn.get("prev_model") or "",
                "to": turn.get("model") or "",
            })
        elif turn.get("explain") == "account" and turn.get("event"):
            ev = turn["event"]
            seen_events.add(ev.get("ts"))
            items.append({
                "kind": "account",
                "ts": ev.get("ts") or "",
                "from": ev.get("from_name") or ev.get("from") or "",
                "to": ev.get("to_name") or ev.get("to") or "",
            })

    # Переключения, до которых ход ещё не дошёл. Ограничиваем началом
    # сессии: журнал общий на проект, и события чужих сессий здесь
    # были бы враньём.
    started = parse_ts(state.get("started") or "")
    for ev in events or []:
        if ev.get("ts") in seen_events:
            continue
        when = parse_ts(ev.get("ts") or "")
        if not when or (started and when < started):
            continue
        items.append({
            "kind": "account",
            "ts": ev.get("ts") or "",
            "from": ev.get("from_name") or ev.get("from") or "",
            "to": ev.get("to_name") or ev.get("to") or "",
            # Ход после переключения ещё не сделан — кэш пока не
            # проверялся. Панель показывает это отдельной пометкой,
            # иначе событие выглядело бы как обошедшееся без промаха.
            "pending": True,
        })

    # Событие (model/account) сортируется раньше всего остального с той
    # же меткой времени: у переключения и хода, который его вызвал,
    # секунда обычно совпадает, и порядок «причина, потом остальное»
    # не должен зависеть от порядка вставки в items.
    items.sort(key=lambda it: (it.get("ts") or "",
                               0 if it.get("kind") in ("model", "account") else 1))
    return items[-MAX_HISTORY_ITEMS:]


def account_events() -> list:
    """События переключения аккаунтов из журнала account_switcher.

    Импорт ленивый и под try: этот модуль запускают и как CLI из
    произвольного места, а без журнала разбор обязан работать —
    просто без объяснений «сменили аккаунт».
    """
    try:
        import account_switcher
        return account_switcher.read_account_events()
    except Exception:
        return []


def explain_turns(turns: list, events: list, ttl_minutes: float) -> list[dict]:
    """Размечает ходы: чем объяснён промах и считается ли он ранним.

    Промах бывает трёх сортов, и путать их нельзя.

    * **Неизбежный по времени** — пауза длиннее TTL: кэш истёк бы сам,
      платить за перезапись пришлось бы при любом раскладе.
    * **Объяснённый** — между ходами сменилась модель или аккаунт.
      Другая модель это другой префикс, другой аккаунт — вообще другой
      провайдер; кэш в этот момент холодный по определению. Такой
      промах не говорит ни о чём, кроме того, что пользователь
      переключился, и записывать его в потери значило бы штрафовать
      за собственное решение.
    * **Ранний** — всё остальное: кэш был жив, но ход его не застал.
      Только это и есть потеря на ровном месте.

    Объяснённый ход выбывает и из знаменателя: шанса у него не было,
    а оставить его там — значит занизить долю потерь ровно в тех
    сессиях, где переключались чаще всего.

    Приоритет у аккаунта: смена аккаунта обычно тянет за собой и смену
    модели, и назвать причиной модель означало бы указать на следствие.

    Разметка делается при выдаче, а не при разборе, — как и сравнение
    с TTL. Поэтому и правка `cacheKeepaliveTtlMinutes`, и появление
    новых записей в журнале аккаунтов пересчитывают уже накопленную
    историю, а не только будущие ходы.
    """
    stamped = []
    for ev in events or []:
        when = parse_ts(ev.get("ts") or "")
        if when:
            stamped.append((when, ev))
    stamped.sort(key=lambda pair: pair[0])

    out = []
    prev_ts = None
    prev_model = ""
    for turn in turns or []:
        ts = parse_ts(turn.get("ts") or "")
        model = turn.get("model") or ""
        gap = turn.get("gap")
        miss = bool(turn.get("miss"))

        explain = None
        event = None
        if prev_ts and ts:
            for when, ev in stamped:
                if prev_ts < when <= ts:
                    explain = "account"
                    event = ev
                    break
        if explain is None and prev_model and model and model != prev_model:
            explain = "model"

        item = dict(turn)
        item["explain"] = explain
        item["prev_model"] = prev_model
        item["event"] = event
        # Ранний промах — необъяснённый и уложившийся в TTL. Промах без
        # разобранной паузы в ранние не идёт: сравнить его не с чем,
        # а записать вслепую — соврать.
        item["early"] = bool(
            miss and explain is None and gap is not None and gap < ttl_minutes
        )
        # Шанс был, если ход не первый и кэшу ничто не мешало заведомо.
        item["counts"] = bool(turn.get("chance")) and explain is None
        out.append(item)

        prev_ts = ts or prev_ts
        if model:
            prev_model = model
    return out


def mood_series(state: dict, ttl_minutes: float) -> list[dict]:
    """История Mood по ходам: накопленные числа на момент каждого хода.

    Отдаём не готовое значение шкалы, а те же сырые числа, что и
    `/cache-usage` (`early_misses`, `early_chances`, `context_peak`).
    Формула положения стрелки живёт в JS, и считать её здесь значило бы
    завести вторую копию: кривая истории и живая стрелка обязаны
    получаться из одного кода, иначе они однажды разойдутся и объяснить
    расхождение будет нечем.

    Сравнение паузы с TTL делается здесь, при выдаче, а не при разборе —
    поэтому правка `cacheKeepaliveTtlMinutes` пересчитывает всю
    накопленную историю, как и текущее показание.
    """
    series = []
    early = 0
    chances = 0
    peak = 0
    # Разметка общая с `collect()` — та же функция, тот же журнал
    # аккаунтов. Вторая копия правил однажды разошлась бы с первой,
    # и кривая истории спорила бы со стрелкой об одном и том же ходе.
    for turn in explain_turns(state.get("turns") or [],
                              account_events(), ttl_minutes):
        ctx = turn.get("ctx") or 0
        peak = max(peak, ctx)
        if turn.get("counts"):
            chances += 1
        if turn.get("early"):
            early += 1
        series.append({
            "ts": turn.get("ts") or "",
            "early_misses": early,
            "early_chances": chances,
            "context_peak": peak,
            "context": ctx,
            "miss": bool(turn.get("miss")),
            "gap": turn.get("gap"),
            # Чем объяснён промах, если объяснён: по этому полю окно
            # истории Mood отмечает точки переключений.
            "explain": turn.get("explain"),
        })
    return series


def collect_series(transcript_path: str, state_dir: str = STATE_DIR,
                   ttl_minutes: float = 60.0) -> dict:
    """История Mood по транскрипту: разбор плюс ряд по ходам.

    Отдельная функция, а не поле в `collect()`: ряд нужен только при
    открытии окна истории, а `/cache-usage` опрашивается индикатором
    каждые двадцать секунд — таскать в нём сотни точек значило бы
    гонять их вхолостую.
    """
    stats = collect(transcript_path, state_dir=state_dir,
                    ttl_minutes=ttl_minutes)
    if not stats.get("ok"):
        return stats

    key = os.path.basename(transcript_path)
    if key.endswith(".jsonl"):
        key = key[:-6]
    state = _load_state(os.path.join(state_dir, f"cache-usage-{key}.json"))
    return {
        "ok": True,
        "session": stats.get("session"),
        "started": stats.get("started"),
        "ttl_minutes": ttl_minutes,
        "series": mood_series(state, ttl_minutes),
    }


def format_report(st: dict) -> str:
    """Человекочитаемый отчёт для CLI."""
    if not st.get("ok"):
        return f"нет данных: {st.get('error', 'неизвестная ошибка')}"

    last = st["last"]
    gap = f", пауза {last['gap']} мин" if last["gap"] is not None else ""
    lines = [
        f"сессия {st['session']}  ·  модель {st['model'] or 'н/д'}",
        f"контекст сейчас : {human(st['context'])}",
        f"пик за сессию   : {human(st['context_peak'])}",
        f"последний ход   : чтение {human(last['read'])} / "
        f"запись {human(last['write'])} · {last['verdict']}{gap}",
        "",
        f"запросов        : {st['requests']}",
        f"прочитано из кэша: {st['read']:,}",
        f"записано в кэш  : {st['write']:,}",
        f"свежий input    : {st['fresh']:,}",
        f"output          : {st['output']:,}",
        f"промахов        : {st['misses']} (перезаписано {st['rewritten']:,} "
        f"= ${st['wasted']})",
        f"из них ранних   : {st['early_misses']} из {st['early_chances']} ходов "
        f"(пауза короче TTL {st['ttl_minutes']:g} мин)",
        "",
        f"с кэшем         : ${st['cost']}",
        f"без кэша было бы: ${st['cost_naive']}",
        f"экономия        : {st['ratio']}x",
    ]
    if st["miss_log"]:
        lines.append("")
        lines.append("последние промахи:")
        for m in st["miss_log"]:
            ts = m["ts"][11:16] if len(m["ts"]) > 16 else m["ts"]
            gap_txt = f"{m['gap']:>7} мин" if m["gap"] is not None else "      —"
            lines.append(
                f"  {ts} {gap_txt}  ожидалось {m['expected']:>9,} · "
                f"прочитано {m['read']:>9,} · переписано {m['written']:>9,}"
            )
    return "\n".join(lines)


def main(argv) -> int:
    if len(argv) < 2:
        print(f"usage: {os.path.basename(argv[0])} <transcript.jsonl>",
              file=sys.stderr)
        return 1
    st = collect(argv[1], use_state=False)
    print(format_report(st))
    return 0 if st.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
