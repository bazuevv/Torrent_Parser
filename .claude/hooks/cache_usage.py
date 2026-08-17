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

# Версия схемы файла состояния. Инкрементировать при любом изменении
# набора полей: старое состояние тогда отбрасывается и транскрипт
# перечитывается целиком. Без этого добавленное поле молча остаётся
# пустым на всех сессиях, у которых состояние уже накоплено.
STATE_VERSION = 2


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
        "model": "",
        "started": "",
        "prev": None,  # {"ts": iso, "rd": int, "wr": int, "sig": [...]}
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
                })
                del state["miss_log"][:-MAX_TRACKED_MISSES]

        if not state["started"] and ts_raw:
            state["started"] = ts_raw
        state["requests"] += 1
        state["read"] += rd
        state["write"] += wr
        state["fresh"] += fresh
        state["output"] += out
        model = msg.get("model")
        if isinstance(model, str) and model:
            state["model"] = model

        state["prev"] = {"ts": ts_raw, "rd": rd, "wr": wr, "sig": sig}
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
            use_state: bool = True) -> dict:
    """Считает статистику кэша по транскрипту и возвращает плоский dict.

    `use_state=False` отключает инкрементальный кэш — файл читается
    целиком. Нужно для одноразовых прогонов, где не хочется мусорить
    в hooks-runtime.
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

    last = state["last"]
    return {
        "ok": True,
        "session": key,
        "model": state["model"],
        "started": state["started"],
        "requests": state["requests"],
        "context": last["read"] + last["write"],
        "last": last,
        "read": state["read"],
        "write": state["write"],
        "fresh": state["fresh"],
        "output": state["output"],
        "misses": state["misses"],
        "rewritten": state["rewritten"],
        "miss_log": state["miss_log"],
        "cost": round(cost, 2),
        "cost_naive": round(naive, 2),
        "ratio": round(naive / cost, 1) if cost > 0 else 0.0,
        "wasted": round(state["rewritten"] / 1e6 * in_rate * CACHE_WRITE_MULT, 2),
        "rates": {
            "input": in_rate,
            "output": out_rate,
            "write_mult": CACHE_WRITE_MULT,
            "read_mult": CACHE_READ_MULT,
        },
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
