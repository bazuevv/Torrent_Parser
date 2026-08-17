#!/usr/bin/env python3
"""
UserPromptSubmit хук. Считает статистику prompt-кэша текущей сессии
по локальному transcript-файлу и отдаёт её модели в additionalContext.

Зачем: Claude Code не показывает пользователю ни размер контекста, ни
попадания/промахи кэша, ни во что это обходится. Всё это есть в JSONL
транскрипте сессии — каждый ответ ассистента несёт блок `usage` с
полями input_tokens / cache_creation_input_tokens /
cache_read_input_tokens / output_tokens. Хук их агрегирует.

Формат вывода (маркер `[cache-usage]`, три строки):

  [cache-usage] контекст 325.4k · последний ход: чтение 321.9k /
                запись 2.4k · попадание (пауза 6.2 мин)
  [cache-usage] сессия: 47 запросов, чтений 12.4M, записей 890.1k,
                промахов 3 (перезаписано 610.2k)
  [cache-usage] стоимость: с кэшем ~$7.12, без кэша было бы ~$62.30
                (экономия 8.7x)

Производительность. Транскрипты длинных сессий доходят до десятков
мегабайт, полный разбор на каждом сообщении был бы заметен. Поэтому
разбор инкрементальный: в hooks-runtime/cache-usage-<session>.json
хранится байтовый офсет и накопленные суммы, при следующем запуске
читается только дописанный хвост. При усечении/подмене файла (размер
меньше офсета) состояние сбрасывается и файл перечитывается целиком.

Ограничение: множитель записи в кэш зависит от TTL (1.25x для 5 минут,
2x для часа), а сам TTL в транскрипте не фиксируется. Берём 2x —
Claude Code по умолчанию использует часовой TTL. При уходе в usage
credits TTL падает до пяти минут, и тогда оценка записи завышена;
на итоговое соотношение это влияет слабо, потому что чтений на порядки
больше, чем записей.
"""
import json
import os
import sys

STATE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks-runtime"
)

# Ставки Claude API, $ за 1M токенов: (input, output).
# Ключ — префикс идентификатора модели, проверяется по самому длинному
# совпадению, чтобы claude-opus-4-8 не поймался правилом claude-opus.
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


def rate_for(model: str):
    best = None
    for prefix, value in RATES.items():
        if model.startswith(prefix):
            if best is None or len(prefix) > len(best[0]):
                best = (prefix, value)
    return best[1] if best else DEFAULT_RATE


def human(n: float) -> str:
    """1234567890 -> '1.2B', 1234567 -> '1.2M', 12345 -> '12.3k', 123 -> '123'."""
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(int(n))


def blank_state() -> dict:
    return {
        "offset": 0,
        "requests": 0,
        "read": 0,
        "write": 0,
        "fresh": 0,
        "output": 0,
        "misses": 0,
        "rewritten": 0,  # токены, переписанные после промахов
        "model": "",
        "prev": None,  # {"ts": iso, "rd": int, "wr": int, "sig": [...]}
        "last": None,  # то же для самого свежего запроса + verdict/gap
    }


def parse_ts(value: str):
    try:
        from datetime import datetime

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
    # следующий запуск, иначе поймаем половину JSON-объекта.
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

        verdict = "старт"
        gap_min = None
        if prev is not None:
            expected = prev["rd"] + prev["wr"]
            ts_now = parse_ts(rec.get("timestamp") or "")
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

        state["requests"] += 1
        state["read"] += rd
        state["write"] += wr
        state["fresh"] += fresh
        state["output"] += out
        model = msg.get("model")
        if isinstance(model, str) and model:
            state["model"] = model

        entry = {"ts": rec.get("timestamp") or "", "rd": rd, "wr": wr, "sig": sig}
        state["prev"] = entry
        state["last"] = {"rd": rd, "wr": wr, "verdict": verdict, "gap": gap_min}

    state["offset"] += consumed


def render(state: dict) -> str:
    if not state["requests"] or not state["last"]:
        return ""

    last = state["last"]
    ctx = last["rd"] + last["wr"]
    gap = last["gap"]
    gap_txt = f", пауза {gap:.1f} мин" if gap is not None else ""

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
    ratio = naive / cost if cost > 0 else 0.0

    lines = [
        f"[cache-usage] контекст {human(ctx)} · последний ход: чтение "
        f"{human(last['rd'])} / запись {human(last['wr'])} · "
        f"{last['verdict']}{gap_txt}",
        f"[cache-usage] сессия: {state['requests']} запросов, чтений "
        f"{human(state['read'])}, записей {human(state['write'])}, "
        f"промахов {state['misses']} (перезаписано "
        f"{human(state['rewritten'])})",
        f"[cache-usage] стоимость: с кэшем ~${cost:.2f}, без кэша было бы "
        f"~${naive:.2f} (экономия {ratio:.1f}x, модель "
        f"{state['model'] or 'н/д'})",
    ]
    return "\n".join(lines)


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0

    event = data.get("hook_event_name")
    if event and event != "UserPromptSubmit":
        return 0

    transcript = data.get("transcript_path")
    session_id = data.get("session_id") or "unknown"
    if not transcript or not os.path.isfile(transcript):
        return 0

    os.makedirs(STATE_DIR, exist_ok=True)
    state_path = os.path.join(STATE_DIR, f"cache-usage-{session_id}.json")

    state = blank_state()
    try:
        with open(state_path, encoding="utf-8") as fh:
            loaded = json.load(fh)
        if isinstance(loaded, dict) and "offset" in loaded:
            state.update(loaded)
    except Exception:
        pass  # нет файла или он битый — считаем с нуля

    try:
        consume(state, transcript)
    except Exception:
        return 0  # хук не имеет права ронять отправку сообщения

    try:
        tmp = state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False)
        os.replace(tmp, state_path)
    except Exception:
        pass

    context = render(state)
    if not context:
        return 0

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        }
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
