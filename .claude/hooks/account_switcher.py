#!/usr/bin/env python3
"""Переключение между settings-файлами разных ИИ-провайдеров.

Claude Code читает ровно один файл — `~/.claude/settings.json`. Аккаунты
других провайдеров лежат рядом под своими именами (`settings_glm.json`,
`settings_api.json`, ...), и переключение — это подмена активного файла
копией выбранного.

Схема состояния (два служебных файла, у каждого одно назначение):

  settings.json.bak   полная копия ОРИГИНАЛЬНОГО settings.json. Создаётся
                      при первом уходе с него и больше не трогается, пока
                      не вернёмся обратно. Это единственный экземпляр
                      настроек Anthropic — затирать его нельзя.
  .active-account     имя активного файла (`settings_glm.json`).
                      Отсутствует ⇒ активен оригинальный settings.json.

Почему два файла, а не один: держать в `.bak` и содержимое, и имя
источника невозможно — одно затирает другое. Ранняя версия так и делала,
и возврат на Anthropic записывал в settings.json строку «settings_glm.json»
вместо настроек.

ВАЖНО: `env` из settings.json применяется процессом Claude Code при
старте, а стартует он один раз на активацию extension host. Поэтому
подмена файла сама по себе ничего не меняет в текущем окне — нужен
новый процесс CLI. Панель Accs предлагает это сразу после переключения:
перезапуск extension host (см. `[claude-exthost-restart]` в
extension.js и endpoint /restart-exthost в http-server.py). Он дешевле
Reload Window — редакторы, вкладки и терминалы остаются на месте.

`settings.local.json` не трогается: это отдельный пользовательский слой,
не относящийся к выбору провайдера.
"""

import datetime
import glob
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import urllib.request
from typing import Any

import codex_bridge_manager

CLAUDE_DIR = os.path.expanduser("~/.claude")
SETTINGS_FILE = os.path.join(CLAUDE_DIR, "settings.json")
BACKUP_FILE = SETTINGS_FILE + ".bak"
ACTIVE_MARKER = os.path.join(CLAUDE_DIR, ".active-account")

# Сроки подписок аккаунтов: {файл: {paidAt, days}}. Живут отдельно от
# settings-файлов (те принадлежат Claude Code, и служебный ключ в них —
# риск без выгоды), а ключи здесь — имена файлов, поэтому подписка
# следует за аккаунтом при любых переключениях.
SUBS_FILE = os.path.join(CLAUDE_DIR, ".account-subs.json")

# Журнал переключений — JSONL в hooks-runtime проекта.
#
# Зачем отдельный файл, когда о переключении и так пишет http-server.log:
# тот журнал человеческий и ротируется по размеру, а это данные, по
# которым панель Usage объясняет промахи кэша. Смена аккаунта означает
# другой провайдер, другой префикс и гарантированно холодный кэш —
# промах на следующем ходу закономерен, и винить за него сессию нельзя.
# Разбирать ради этого текстовые строки чужого журнала значило бы
# завязаться на его формат.
#
# Путь — рядом с состоянием разбора транскриптов, потому что читает
# журнал именно cache_usage.py. Каталог проекта берётся из переменной
# окружения (её ставит http-server.py), с запасным вариантом от
# расположения самого файла: скрипт запускают и напрямую.
_PROJECT_DIR = os.environ.get("CLAUDE_PROJECT_DIR") or os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
ACCOUNT_EVENTS_LOG = os.path.join(
    _PROJECT_DIR, ".claude", "hooks-runtime", "account-events.jsonl"
)

# Сколько событий держим. Одно событие — десятки байт, но файл живёт
# вечно, а истории панели хватает последних.
MAX_ACCOUNT_EVENTS = 500

# Глобальное состояние самого Claude Code — не настройки, а его рабочий
# файл. Нам оттуда нужен кэш лимитов подписки claude.ai
# (`cachedUsageUtilization`, см. anthropic_usage()) и почта логина.
CONFIG_JSON = os.path.expanduser("~/.claude.json")

# Токены OAuth-логина. Читаем ровно одно поле — тариф подписки;
# ни токены, ни их части наружу не уходят и в журнал не пишутся.
CREDENTIALS_JSON = os.path.join(CLAUDE_DIR, ".credentials.json")

# Тариф из credentials → как его называет сам Claude. Незнакомое
# значение показываем как есть с большой буквы: список планов меняется
# чаще, чем наш код.
PLAN_NAMES = {
    "pro": "Pro",
    "max": "Max",
    "team": "Team",
    "enterprise": "Enterprise",
    "free": "Free",
}

# Имя базового файла — он же аккаунт по умолчанию.
BASE_NAME = "settings.json"

# Не аккаунты: локальный слой настроек и наш собственный бэкап.
EXCLUDED = {"settings.local.json"}

# Разрешённое имя аккаунта. Заодно защита от directory traversal:
# имя приходит из webview и подставляется в путь.
ACCOUNT_NAME_RE = re.compile(r"^settings(_[A-Za-z0-9_-]+)?\.json$")

# Имя переменной окружения. Ограничение то же, что у самого shell:
# в settings.json попадёт только то, что процесс сможет прочитать.
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Имя настройки верхнего уровня. Схема Claude Code — camelCase; `$schema`
# в панель не попадает (см. SETTINGS_HIDDEN), но букву `$` разрешаем,
# чтобы правило описывало именно синтаксис ключа, а не наш фильтр.
SETTING_KEY_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_]*$")

# Корневые ключи, которых в редакторе быть не должно.
#   env         — правится отдельной секцией той же формы;
#   $schema     — служебная ссылка для редакторов JSON, к поведению
#                 Claude Code отношения не имеет.
# Всё остальное фильтруется по типу значения: панель правит скаляры,
# а `permissions`, `hooks`, `mcpServers` и прочие структуры — нет.
# Правило по типу, а не по списку: незнакомая настройка-скаляр должна
# появляться в панели сама, как и незнакомый settings-файл в списке.
SETTINGS_HIDDEN = {"env", "$schema"}

# --- подписка аккаунта ---------------------------------------------------
#
# Когда за аккаунт заплачено, знает только пользователь: ни Claude Code,
# ни провайдер этих данных панели не отдают. Дату оплаты вводят в
# настройках аккаунта (шестерёнка в панели Accs), срок считаем от неё.

# Срок по умолчанию — месяц: самый частый период подписки.
DEFAULT_SUB_DAYS = 30
# Верхняя граница срока: защита от опечатки в поле «дней».
MAX_SUB_DAYS = 3650

# «2026-09-05T14:30» — как его присылает input[type=datetime-local].
SUB_PAID_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?$")

# --- подписка Z.AI (GLM) через API ---------------------------------------
#
# У Z.AI нет ни OAuth-файлов, ни локального моста, но мониторинговые
# ручки открывает тот же API-ключ, что уже лежит в env аккаунта
# (пути — как у glm-for-copilot, ответ проверен на живом аккаунте):
#   GET /api/biz/subscription/list → productName и период подписки
# Китайская станция (open.bigmodel.cn) держит те же пути, но с
# raw-ключом вместо Bearer. Ответ кэшируется: список аккаунтов панель
# перечитывает часто, а ходить в сеть на каждый чих нельзя.
ZAI_SUB_PATH = "/api/biz/subscription/list"
ZAI_CACHE_FILE = os.path.join(
    _PROJECT_DIR, ".claude", "hooks-runtime", "zai-subs-cache.json"
)
ZAI_CACHE_TTL_SEC = 600      # удачный ответ
ZAI_FAIL_TTL_SEC = 120       # неудача: сеть лечится, панель не тормозит
ZAI_HTTP_TIMEOUT = 4.0

# Квоты Coding Plan: окно 5 ч и недельная квота. Endpoint клиентский
# (его страница подписки в кабинете Z.AI рисует прогресс квоты),
# формат окон — percentage + nextResetTime (epoch ms).
ZAI_QUOTA_PATH = "/api/monitor/usage/quota/limit"
ZAI_USAGE_CACHE_FILE = os.path.join(
    _PROJECT_DIR, ".claude", "hooks-runtime", "zai-usage-cache.json"
)
ZAI_USAGE_TTL_SEC = 300      # квота тратится на глазах: чаще подписки
# Подписи окон: ключи те же, что у claude.ai, — панель рисует полоски
# одним кодом. (unit, number) → (key, label, title).
ZAI_WINDOW_KINDS = {
    (3, 5): ("five_hour", "5 ч", "Пятичасовое окно"),
    (6, 1): ("seven_day", "7 дн", "Недельная квота"),
}
ZAI_UNIT_NAMES = {3: "ч", 6: "нед"}

# Диапазон действия подписки в ответе Z.AI: «2026-09-19 15:55:07-2026-10-19 15:55:07».
ZAI_VALID_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})-(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})$"
)

# Описания переменных окружения. Показываются по значку «?» в строке;
# незнакомой переменной значка нет — врать про неё нечего.
ENV_HINTS = {
    "ANTHROPIC_API_KEY":
        "Ключ Anthropic API — уходит в заголовок x-api-key. "
        "У сторонних провайдеров чаще используют ANTHROPIC_AUTH_TOKEN.",
    "ANTHROPIC_AUTH_TOKEN":
        "Токен авторизации — заголовок Authorization: Bearer. "
        "Основной способ входа к стороннему провайдеру.",
    "ANTHROPIC_BASE_URL":
        "Адрес API вместо api.anthropic.com. Именно этим и подключают "
        "сторонних провайдеров.",
    "ANTHROPIC_MODEL":
        "Модель сессии. Перебивает выбор, сделанный командой /model.",
    "ANTHROPIC_SMALL_FAST_MODEL":
        "Устаревшее имя дешёвой быстрой модели для фоновых задач. "
        "Сейчас её задаёт ANTHROPIC_DEFAULT_HAIKU_MODEL.",
    "ANTHROPIC_DEFAULT_OPUS_MODEL":
        "Чем провайдер подменяет класс Opus.",
    "ANTHROPIC_DEFAULT_SONNET_MODEL":
        "Чем провайдер подменяет класс Sonnet.",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL":
        "Чем провайдер подменяет класс Haiku — дешёвую модель для "
        "фоновой работы (заголовки чатов, вспомогательные вызовы).",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW":
        "Размер контекстного окна в токенах, от которого считается "
        "автокомпактификация.",
    "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE":
        "Порог автокомпактификации в процентах от окна.",
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS":
        "Потолок токенов в одном ответе модели.",
    "CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS":
        "Потолок токенов на вывод одного чтения файла.",
    "MAX_THINKING_TOKENS":
        "Бюджет размышления модели в токенах.",
    "CLAUDE_CODE_EFFORT_LEVEL":
        "Уровень усилий процесса CLI: low, medium, high, xhigh или max "
        "(так max и сохраняют — ключ effortLevel его не принимает). "
        "Псевдонимы: med=medium, unset/auto — снять. Перебивает всё "
        "на сессию, включая effortLevel и ultracode.",
    "API_TIMEOUT_MS":
        "Таймаут одного запроса к API, миллисекунды.",
    "BASH_DEFAULT_TIMEOUT_MS":
        "Таймаут Bash-команды по умолчанию, миллисекунды.",
    "BASH_MAX_TIMEOUT_MS":
        "Верхняя граница таймаута Bash-команды, миллисекунды.",
    "BASH_MAX_OUTPUT_LENGTH":
        "Сколько символов вывода Bash доходит до модели.",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC":
        "1 — отключить необязательные обращения к серверам Anthropic: "
        "телеметрию, автообновление, фоновые вызовы моделей.",
    "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY":
        "1 — не показывать опрос об удовлетворённости.",
    "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS":
        "1 — включить экспериментальные команды агентов.",
    "DISABLE_TELEMETRY": "1 — не отправлять телеметрию.",
    "DISABLE_ERROR_REPORTING": "1 — не отправлять отчёты об ошибках.",
    "DISABLE_AUTOUPDATER": "1 — не обновлять CLI автоматически.",
    "DISABLE_BUG_COMMAND": "1 — убрать команду /bug.",
    "DISABLE_COST_WARNINGS": "1 — не предупреждать о расходах.",
    "DISABLE_NON_ESSENTIAL_MODEL_CALLS":
        "1 — отключить вспомогательные вызовы моделей "
        "(заголовки чатов и прочая фоновая работа).",
    "HTTP_PROXY": "HTTP-прокси для запросов к API.",
    "HTTPS_PROXY": "HTTPS-прокси для запросов к API.",
}

# Описания и типы настроек верхнего уровня. `type` определяет, каким
# полем настройку рисует панель: bool — переключателем, enum — списком,
# остальное — текстом. Тексты — перевод `.describe()` из zod-схемы
# внутри extension.js: там же и проверяется значение, так что список
# вариантов enum обязан ей соответствовать.
SETTING_HINTS = {
    "model": {
        "type": "text",
        "hint": "Модель по умолчанию: алиас (opus, sonnet, haiku) или "
                "полный id (claude-opus-5). Суффикс [1m] просит вариант "
                "с контекстным окном в 1M токенов.",
    },
    "language": {
        "type": "text",
        "hint": "Язык ответов Claude и голосового ввода.",
    },
    "effortLevel": {
        "type": "enum",
        "options": ["low", "medium", "high", "xhigh"],
        "hint": "Глубина рассуждений на поддерживающих её моделях. "
                "max здесь не принимается — оно сессионное и в файле "
                "молча отбрасывается; постоянный max задаёт только "
                "переменная CLAUDE_CODE_EFFORT_LEVEL=max.",
    },
    "ultracode": {
        "type": "bool",
        "hint": "Ультракод: усилие xhigh плюс постоянная оркестрация "
                "динамическими workflow на всю сессию. Требует "
                "включённых workflow и xhigh-способной модели. "
                "Интерактивный /effort ultracode не сохраняется — "
                "только этот ключ в файле.",
    },
    "workflowKeywordTriggerEnabled": {
        "type": "bool",
        "hint": "Триггер по слову «ultracode» в промпте: такой ход "
                "уходит в Workflow-инструмент. По умолчанию включён; "
                "false — отключить триггер.",
    },
    "alwaysThinkingEnabled": {
        "type": "bool",
        "hint": "Размышления модели: true или отсутствие ключа — "
                "включены автоматически на поддерживающих моделях, "
                "false — выключены.",
    },
    "preferredNotifChannel": {
        "type": "enum",
        "options": ["auto", "iterm2", "terminal_bell", "iterm2_with_bell",
                    "kitty", "ghostty", "notifications_disabled"],
        "hint": "Канал системных уведомлений.",
    },
    "switchModelsOnFlag": {
        "type": "bool",
        "hint": "Если сообщение помечено защитными фильтрами — перейти "
                "на другую модель и продолжить разговор. Выключено — "
                "сессия вместо этого встаёт.",
    },
    "agentPushNotifEnabled": {
        "type": "bool",
        "hint": "Разрешить Claude присылать push-уведомления на телефон "
                "по своей инициативе.",
    },
    "inputNeededNotifEnabled": {
        "type": "bool",
        "hint": "Слать push на телефон, когда ждём ответа на запрос "
                "разрешения или вопрос.",
    },
    "verbose": {
        "type": "bool",
        "hint": "Показывать вывод инструментов целиком, а не сокращённо.",
    },
    "autoCompactEnabled": {
        "type": "bool",
        "hint": "Автоматически сжимать переписку, когда контекст "
                "заполняется.",
    },
    "fileCheckpointingEnabled": {
        "type": "bool",
        "hint": "Снимать копии файлов перед правкой, чтобы /rewind мог "
                "их вернуть.",
    },
    "todoFeatureEnabled": {
        "type": "bool",
        "hint": "Включить панель задач (todo).",
    },
    "showTurnDuration": {
        "type": "bool",
        "hint": "Показывать длительность каждого хода ассистента.",
    },
    "showMessageTimestamps": {
        "type": "bool",
        "hint": "Ставить каждому сообщению время получения.",
    },
    "showThinkingSummaries": {
        "type": "bool",
        "hint": "Запрашивать у API сводки размышления и показывать их "
                "в переписке.",
    },
    "autoMemoryEnabled": {
        "type": "bool",
        "hint": "Разрешить авто-память проекта: чтение и запись файлов "
                "в каталоге памяти.",
    },
    "skipDangerousModePermissionPrompt": {
        "type": "bool",
        "hint": "Отметка о том, что диалог про bypass-режим разрешений "
                "уже принят.",
    },
    "autoUploadSessions": {
        "type": "bool",
        "hint": "Зеркалить сессии на claude.ai только для просмотра.",
    },
    "remoteControlAtStartup": {
        "type": "bool",
        "hint": "Поднимать мост Remote Control при старте сессии.",
    },
    "theme": {
        "type": "text",
        "hint": "Цветовая тема интерфейса.",
    },
    "editorMode": {
        "type": "text",
        "hint": "Режим клавиш в поле ввода (например, vim).",
    },
}


def hints() -> dict:
    """Справочник для панели: описания env-переменных и настроек."""
    return {"env": ENV_HINTS, "settings": SETTING_HINTS}

# Человекочитаемые имена для известных суффиксов. Незнакомый суффикс
# показывается как есть — список не обязан быть полным.
DISPLAY_NAMES = {
    "settings.json": "Anthropic",
    "settings_glm.json": "Z.AI (GLM)",
    "settings_api.json": "East API",
    "settings_mac.json": "Mac",
    "settings_openai.json": "OpenAI (ChatGPT)",
}


def _display_name(filename: str) -> str:
    if filename in DISPLAY_NAMES:
        return DISPLAY_NAMES[filename]
    stem = filename[len("settings_"):-len(".json")] if filename.startswith("settings_") else filename
    return stem.replace("_", " ").upper()


def _read_env(path: str) -> dict:
    """Возвращает секцию `env` из settings-файла ({} при любой ошибке).

    Файл пользовательский и может быть в любом состоянии — битый JSON
    не должен ронять список аккаунтов.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    env = data.get("env") if isinstance(data, dict) else None
    return env if isinstance(env, dict) else {}


# Ключи env, по которым видно, что аккаунт ходит не в claude.ai, а к
# стороннему провайдеру. Аккаунт без единого из них работает на OAuth-
# логине Claude Code — значит, к нему относятся лимиты подписки.
# Правило по наличию ключей, а не по имени файла: `settings_mac.json`
# в этом же каталоге тоже смотрит на east-api, хотя по имени не скажешь.
THIRD_PARTY_ENV_KEYS = (
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
)


def _describe(path: str) -> dict:
    """Короткая сводка о провайдере: endpoint, модель и тип логина."""
    env = _read_env(path)
    base_url = env.get("ANTHROPIC_BASE_URL") or "api.anthropic.com (по умолчанию)"
    model = (env.get("ANTHROPIC_MODEL")
             or env.get("ANTHROPIC_DEFAULT_OPUS_MODEL")
             or "")
    oauth = not any(env.get(key) for key in THIRD_PARTY_ENV_KEYS)
    provider = "openai" if base_url.rstrip("/") == (
        f"http://{codex_bridge_manager.BRIDGE_HOST}:"
        f"{codex_bridge_manager.BRIDGE_PORT}"
    ) else ("anthropic" if oauth else "custom")
    return {"baseUrl": base_url, "model": model, "oauth": oauth,
            "provider": provider}


def _openai_usage(rate_limits: Any) -> dict | None:
    if not isinstance(rate_limits, dict):
        return None
    windows = []
    for key, label, title in (
        ("primary", "5 ч", "Сессия"),
        ("secondary", "7 дн", "Неделя"),
    ):
        window = rate_limits.get(key)
        if not isinstance(window, dict):
            continue
        percent = window.get("usedPercent")
        if isinstance(percent, bool) or not isinstance(percent, (int, float)):
            continue
        resets_at = window.get("resetsAt")
        left = None
        if isinstance(resets_at, (int, float)) and not isinstance(resets_at, bool):
            left = int(resets_at - time.time())
        duration = window.get("windowDurationMins")
        if isinstance(duration, (int, float)) and duration > 0:
            if duration < 24 * 60:
                label = f"{int(duration // 60)} ч"
            else:
                label = f"{int(duration // (24 * 60))} дн"
        windows.append({
            "key": f"openai_{key}", "label": label, "title": title,
            "percent": percent, "resetsInSec": max(0, left) if left is not None else None,
            "expired": left is not None and left <= 0,
        })
    if not windows:
        return None
    return {"windows": windows, "ageSec": 0,
            "sourceLabel": "Данные Codex App Server"}


def openai_usage_raw(snapshot: dict | None = None) -> dict | None:
    """Raw primary Codex limit window for the reset-sound monitor.

    Unlike ``_openai_usage``, this keeps the absolute reset timestamp and
    identifies the ChatGPT login without exposing its email.  The rollover
    flag lets the monitor recognize a reset even when Codex immediately
    replaces the expired window with the next active one.
    """
    if snapshot is None:
        snapshot = codex_bridge_manager.account_snapshot(timeout=4.0)
    if not isinstance(snapshot, dict):
        return None
    limits = snapshot.get("rateLimits")
    primary = limits.get("primary") if isinstance(limits, dict) else None
    if not isinstance(primary, dict):
        return None
    percent = primary.get("usedPercent")
    if isinstance(percent, bool) or not isinstance(percent, (int, float)):
        percent = None
    reset_at = primary.get("resetsAt")
    if (isinstance(reset_at, bool)
            or not isinstance(reset_at, (int, float))):
        reset_at = None

    account = snapshot.get("account")
    identity = "chatgpt"
    if isinstance(account, dict):
        identity = "|".join(str(account.get(key) or "") for key in (
            "type", "email", "planType",
        ))
    account_key = "openai:" + hashlib.sha256(identity.encode()).hexdigest()
    return {
        "percent": percent,
        "resetAt": reset_at,
        "accountUuid": account_key,
        "ageSec": 0,
        "provider": "openai",
        "signalOnRollover": True,
    }


def _openai_runtime(value: Any) -> dict | None:
    """Public per-thread model/effort/token details from the local bridge."""
    if not isinstance(value, dict):
        return None
    result: dict = {}
    for key in ("model", "effort"):
        item = value.get(key)
        if isinstance(item, str) and item:
            result[key] = item
    last = value.get("last")
    if isinstance(last, dict):
        safe_last = {}
        for key in (
            "input_tokens", "cached_input_tokens", "cache_write_input_tokens",
            "output_tokens", "reasoning_output_tokens", "total_tokens",
        ):
            amount = last.get(key)
            if isinstance(amount, int) and not isinstance(amount, bool) and amount >= 0:
                safe_last[key] = amount
        if safe_last:
            result["last"] = safe_last
    context = value.get("model_context_window")
    if isinstance(context, int) and not isinstance(context, bool) and context > 0:
        result["modelContextWindow"] = context
    return result or None


def openai_account() -> dict:
    snapshot = codex_bridge_manager.account_snapshot()
    if not snapshot:
        return {"bridgeReady": False}
    account = snapshot.get("account")
    models = snapshot.get("models")
    limits = snapshot.get("rateLimits")
    runtime = _openai_runtime(snapshot.get("bridgeUsage"))
    result: dict = {"bridgeReady": True}
    if isinstance(account, dict):
        email = account.get("email")
        plan = account.get("planType")
        if isinstance(email, str):
            result["email"] = email
        if isinstance(plan, str):
            result["plan"] = plan.replace("_", " ").title()
    if runtime:
        result["runtime"] = runtime
        if isinstance(runtime.get("model"), str):
            result["model"] = runtime["model"]
    elif isinstance(models, list):
        default = next((m for m in models if isinstance(m, dict)
                        and m.get("isDefault")), None)
        if isinstance(default, dict) and isinstance(default.get("id"), str):
            result["model"] = default["id"]
    if isinstance(limits, dict):
        usage = _openai_usage(limits)
        if usage:
            result["usage"] = usage
    return result


# --- лимиты подписки claude.ai ---------------------------------------
#
# Окна лимитов в порядке показа: ключ в кэше CLI, подпись у полоски,
# полное название для подсказки. Список явный, а не «все непустые окна»:
# рядом с five_hour в том же объекте лежат окна под кодовыми именами
# (nimbus_quill, cinder_cove, ...) — что они означают, мы не знаем,
# и рисовать их полоской значило бы выдумывать смысл.
USAGE_WINDOWS = (
    ("five_hour", "5 ч", "Сессия (5 часов)"),
    ("seven_day", "7 дн", "Неделя (7 дней)"),
    ("seven_day_opus", "Opus", "Неделя, Opus"),
    ("seven_day_sonnet", "Sonnet", "Неделя, Sonnet"),
)


def anthropic_identity() -> dict:
    """Почта и тариф логина claude.ai — то, чем аккаунт узнаётся в лицо.

    Endpoint у такого аккаунта всегда `api.anthropic.com`, и в строке
    панели он не говорит ни о чём: одинаков у любого OAuth-аккаунта.
    Почта с тарифом отличают его от соседнего, поэтому подпись строки
    строится из них. У аккаунта провайдера наоборот — адрес и модель
    там и есть всё различие, их и показываем.

    Почта лежит в рабочем файле CLI, тариф — в его же credentials рядом
    с токенами. Берём оттуда ровно одно поле; ни токены, ни их части
    наружу не уходят и в журнал не пишутся.

    Пустой словарь означает «показывать нечего» — панель в этом случае
    возвращается к endpoint'у.
    """
    identity: dict = {}

    try:
        with open(CONFIG_JSON, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        account = data.get("oauthAccount") if isinstance(data, dict) else None
        email = account.get("emailAddress") if isinstance(account, dict) else None
        if isinstance(email, str) and email.strip():
            identity["email"] = email.strip()
    except (OSError, json.JSONDecodeError):
        pass

    try:
        with open(CREDENTIALS_JSON, "r", encoding="utf-8") as fh:
            creds = json.load(fh)
        oauth = creds.get("claudeAiOauth") if isinstance(creds, dict) else None
        plan = oauth.get("subscriptionType") if isinstance(oauth, dict) else None
        if isinstance(plan, str) and plan.strip():
            key = plan.strip().lower()
            identity["plan"] = PLAN_NAMES.get(key, key.capitalize())
    except (OSError, json.JSONDecodeError):
        pass

    return identity


def _reset_moment(value) -> float | None:
    """Абсолютный момент сброса окна (epoch); None — не разобрать.

    Живой кэш отдаёт ISO-строку со смещением `+00:00`. Наивная строка
    без смещения считается UTC: время в кэше всегда про UTC, смещения
    локальной зоны у него не бывает.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return moment.timestamp()


def _resets_in_sec(value) -> int | None:
    """Секунды до сброса окна; отрицательное — момент уже прошёл.

    Знак здесь значим, поэтому к нулю НЕ прижимаем: прошедшее время
    сброса означает, что окно уже перевалило, и показывать по нему
    старые проценты нельзя (см. anthropic_usage).
    """
    moment = _reset_moment(value)
    if moment is None:
        return None
    return int(moment - time.time())


def _read_usage_cache() -> dict | None:
    """Кэш лимитов из ~/.claude.json, если он принадлежит текущему логину.

    Общая часть anthropic_usage() и anthropic_usage_raw(): вычитка файла
    и сверка подписи. None — нет файла, нет кэша или в нём проценты
    чужого аккаунта.
    """
    try:
        with open(CONFIG_JSON, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None

    cached = data.get("cachedUsageUtilization")
    if not isinstance(cached, dict):
        return None
    util = cached.get("utilization")
    if not isinstance(util, dict):
        return None

    # Кэш подписан аккаунтом. После смены логина в нём какое-то время
    # лежат проценты прежнего — показать их как свои значило бы соврать.
    account = data.get("oauthAccount")
    expected = account.get("accountUuid") if isinstance(account, dict) else None
    got = cached.get("accountUuid")
    if expected and got and expected != got:
        return None
    return cached


def anthropic_usage() -> dict | None:
    """Загрузка лимитов подписки claude.ai (пятичасовое окно и недельное).

    Данные берутся из кэша, который ведёт сам Claude Code: он ходит
    в `api.anthropic.com/api/oauth/usage` и кладёт ответ в
    `~/.claude.json` под ключом `cachedUsageUtilization`. Свой запрос
    к API мы не делаем сознательно: для него нужен OAuth-токен из
    `~/.claude/.credentials.json`, а он живёт часами и требует
    обновления — заниматься этим параллельно с CLI значит соперничать
    с ним за один и тот же файл ради чисел, которые он и так сохранил.

    Обратная сторона — данные ровно настолько свежие, насколько давно
    CLI их обновлял. Поэтому наружу уходит и возраст записи: показать
    вчерашние проценты как сегодняшние нельзя.

    None означает «показывать нечего» (нет файла, нет кэша, чужой
    аккаунт) — панель в этом случае просто не рисует полоски.
    """
    cached = _read_usage_cache()
    if cached is None:
        return None
    util = cached["utilization"]

    windows = []
    for key, label, title in USAGE_WINDOWS:
        window = util.get(key)
        if not isinstance(window, dict):
            continue
        percent = window.get("utilization")
        # bool — тоже int, а полоска на True шириной 100% была бы
        # красивой неправдой.
        if isinstance(percent, bool) or not isinstance(percent, (int, float)):
            continue
        left = _resets_in_sec(window.get("resets_at"))

        # Окно, чьё время сброса прошло, уже началось заново, и
        # прежние проценты к нему не относятся. Кэш об этом не знает:
        # обновляет его CLI, а на стороннем провайдере он к claude.ai
        # не обращается вовсе — потому и застревало «100%, меньше
        # минуты» на сутки.
        #
        # Показываем 0%: на стороннем аккаунте лимит claude.ai не
        # тратится, так что после сброса окно и правда пустое. Это
        # расчёт, а не измерение, поэтому окно помечается expired —
        # панель говорит «сброшен» вместо обратного отсчёта и пишет
        # в подсказке, что свежих данных нет.
        expired = left is not None and left <= 0
        windows.append({
            "key": key,
            "label": label,
            "title": title,
            "percent": 0 if expired else percent,
            "resetsInSec": None if expired else left,
            "expired": expired,
        })
    if not windows:
        return None

    fetched = cached.get("fetchedAtMs")
    age = None
    if isinstance(fetched, (int, float)) and not isinstance(fetched, bool):
        age = max(0, int(time.time() - fetched / 1000))
    return {"windows": windows, "ageSec": age}


def anthropic_usage_raw() -> dict | None:
    """Сырое пятичасовое окно — для монитора сигнала сброса лимита.

    anthropic_usage() прячет окно с прошедшим resets_at (percent=0,
    resetsInSec=None): панели так правильно, но монитору нужны именно
    «процент, который был в окне до сброса» и «абсолютный момент
    сброса». Момент абсолютен, поэтому сигнал работает даже по
    замёрзшему кэшу: CLI, не работающий на OAuth-логине, кэш не
    обновляет, а resets_at в нём всё равно называет, когда окно
    жило до.

    None — показывать нечего (нет файла/кэша, чужой аккаунт, окна
    five_hour в кэше нет).
    """
    cached = _read_usage_cache()
    if cached is None:
        return None
    window = cached["utilization"].get("five_hour")
    if not isinstance(window, dict):
        return None

    percent = window.get("utilization")
    if isinstance(percent, bool) or not isinstance(percent, (int, float)):
        percent = None
    account = cached.get("accountUuid")

    fetched = cached.get("fetchedAtMs")
    age = None
    if isinstance(fetched, (int, float)) and not isinstance(fetched, bool):
        age = max(0, int(time.time() - fetched / 1000))
    return {
        "percent": percent,
        "resetAt": _reset_moment(window.get("resets_at")),
        "accountUuid": account if isinstance(account, str) else None,
        "ageSec": age,
    }


def get_current_account() -> str:
    """Имя активного settings-файла.

    Маркер валидируется: если он указывает на исчезнувший файл, считаем
    активным базовый — иначе UI показал бы выбранным несуществующий
    аккаунт, а вернуться было бы некуда.
    """
    try:
        with open(ACTIVE_MARKER, "r", encoding="utf-8") as fh:
            name = fh.read().strip()
    except OSError:
        return BASE_NAME
    if (name and ACCOUNT_NAME_RE.match(name)
            and os.path.isfile(os.path.join(CLAUDE_DIR, name))):
        return name
    return BASE_NAME


def current_account_runtime() -> dict:
    """Safe active provider/model metadata without usage or identity fetches."""
    filename = get_current_account()
    info = _describe(source_path(filename))
    return {
        "file": filename,
        "provider": info.get("provider") or "",
        "model": info.get("model") or "",
    }


def source_path(filename: str) -> str:
    """Файл, в котором лежат НАСТОЯЩИЕ настройки аккаунта.

    Для аккаунтов провайдеров это сам `settings_x.json`. С базовым
    сложнее: `settings.json` — это активная копия, и пока активен чужой
    аккаунт, оригинал Anthropic лежит в `settings.json.bak`. Читать и
    править базовый аккаунт в такой момент нужно именно там, иначе
    в панели он показывал бы чужой endpoint, а правки затёрлись бы
    при ближайшем возврате на него.
    """
    if filename != BASE_NAME:
        return os.path.join(CLAUDE_DIR, filename)
    if get_current_account() != BASE_NAME and os.path.isfile(BACKUP_FILE):
        return BACKUP_FILE
    return SETTINGS_FILE


def active_account_is_oauth() -> bool:
    """Работает ли текущая сессия на OAuth-логине claude.ai.

    Монитору сброса лимитов этого достаточно, чтобы молчать на стороннем
    провайдере: окно claude.ai в этот момент не тратится, а кэш лимитов
    не обновляется — сигнал сброса был бы рассказом о чужой паузе.
    """
    return _describe(source_path(get_current_account()))["oauth"]


def list_accounts() -> list[dict]:
    """Все settings-файлы в ~/.claude/ как список аккаунтов.

    Скан по маске, а не по жёсткому списку: добавленный руками
    `settings_foo.json` появляется в панели сам.
    """
    current = get_current_account()
    # Лимиты и данные логина читаются один раз на список: файлы общие,
    # а аккаунтов на OAuth-логине может оказаться больше одного.
    usage = anthropic_usage()
    identity = anthropic_identity()
    subs = _read_subs()
    openai_identity: dict | None = None
    accounts = []
    for path in sorted(glob.glob(os.path.join(CLAUDE_DIR, "settings*.json"))):
        filename = os.path.basename(path)
        if filename in EXCLUDED or not ACCOUNT_NAME_RE.match(filename):
            continue
        info = _describe(source_path(filename))
        entry = {
            "file": filename,
            "name": _display_name(filename),
            "isActive": filename == current,
            "baseUrl": info["baseUrl"],
            "model": info["model"],
            "oauth": info["oauth"],
            "provider": info["provider"],
        }
        # Срок подписки и данные логина — по-разному в зависимости от
        # того, кто их знает:
        #   claude.ai  — лимиты/почта/тариф из кэша и credentials CLI;
        #   OpenAI     — то же из локального Codex App Server, срок
        #                подписки — из Codex id_token (запись в
        #                SUBS_FILE ведёт codex_id_token_sync.py);
        #   Z.AI       — тариф, период и окна квот из его API (ключ уже
        #                в env аккаунта, ответ кэшируется);
        #   остальные  — только ручная запись из SUBS_FILE.
        # Приоритет API над ручной записью: в собственном биллинге
        # провайдер не ошибается, а рука — может.
        record = read_subscription(filename, subs)
        if info["oauth"]:
            if usage:
                entry["usage"] = usage
            entry.update(identity)
        elif info["provider"] == "openai":
            if openai_identity is None:
                openai_identity = openai_account()
            entry.update(openai_identity)
            # Срока подписки в протоколе Codex нет, зато он есть в
            # id_token из ~/.codex/auth.json: хук codex_id_token_sync.py
            # лениво обновляет запись в SUBS_FILE, как только срок
            # истёк. Поэтому `record` здесь уже актуален — ручной
            # строки для OpenAI больше не существует.
            if record:
                info_sub = subscription_info(record)
                if info_sub:
                    entry["subscription"] = info_sub
        else:
            env = _read_env(source_path(filename))
            usage = zai_usage(env)
            if usage:
                entry["usage"] = usage
                entry["usageZai"] = True
            zai = zai_subscription(env)
            if zai:
                entry["subscription"] = zai
                entry["plan"] = zai["plan"]
            elif record:
                info_sub = subscription_info(record)
                if info_sub:
                    entry["subscription"] = info_sub
            # Почта — только ручная: через API её не отдают. Ручной
            # тариф — запас на случай, когда API молчит.
            if record:
                for key in ("email", "plan"):
                    if record.get(key) and key not in entry:
                        entry[key] = record[key]
        accounts.append(entry)

    # Базовый аккаунт первым, остальные по алфавиту.
    accounts.sort(key=lambda a: (a["file"] != BASE_NAME, a["file"]))
    return accounts


def _load_account(filename: str) -> tuple[bool, str, dict]:
    """Разбор settings-файла аккаунта: (успех, сообщение, данные)."""
    if not ACCOUNT_NAME_RE.match(filename or ""):
        return False, f"Недопустимое имя аккаунта: {filename!r}", {}
    path = source_path(filename)
    if not os.path.isfile(path):
        return False, f"Файл {filename} не найден", {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        # Битый JSON правкой через панель не починить: неизвестно, что
        # в нём было. Честнее сказать и отправить в редактор руками.
        return False, f"{filename}: {exc}", {}
    if not isinstance(data, dict):
        return False, f"{filename}: ожидался объект JSON", {}
    return True, "", data


def _visible_settings(data: dict) -> dict:
    """Скалярные настройки верхнего уровня — то, что правит панель.

    Структуры (`permissions`, `hooks`, `mcpServers`) отсеиваются по
    типу: полем «ключ→значение» их не отредактировать, а показать и
    сохранить как текст значило бы их испортить.
    """
    out = {}
    for key, value in data.items():
        if key in SETTINGS_HIDDEN:
            continue
        if isinstance(value, (str, bool, int, float)):
            out[key] = value
    return out


def read_account_config(filename: str) -> tuple[bool, str, dict]:
    """Правимая часть файла аккаунта: (успех, сообщение, разделы).

    Разделов три. `env` задаёт провайдера — адрес, ключ, подмену
    моделей. `settings` — скалярные настройки верхнего уровня (`model`,
    `language`, `effortLevel`, …): у аккаунта Anthropic секции `env` нет
    вовсе, и без них редактор для него был бы пуст. `subscription` —
    дата оплаты подписки и срок: их вводят руками, отдельного
    хранилища в settings-файле для них нет (см. SUBS_FILE). Для
    OpenAI раздел — None: подписка пишется автоматически из Codex
    id_token (codex_id_token_sync.py), ручного поля быть не должно.
    """
    ok, message, data = _load_account(filename)
    if not ok:
        return False, message, {}
    env = data.get("env")
    subscription = None
    if _describe(source_path(filename))["provider"] != "openai":
        subscription = read_subscription(filename)
    return True, "", {
        "env": env if isinstance(env, dict) else {},
        "settings": _visible_settings(data),
        "subscription": subscription,
    }


def _coerce_setting(key: str, value, previous):
    """Значение настройки в том типе, в каком его ждёт Claude Code.

    Форма отдаёт всё строками (кроме переключателей), а в JSON у
    `switchModelsOnFlag` должен остаться булев тип, у числовых настроек
    — числовой. Тип берём из справочника, при незнакомом ключе — из
    прежнего значения, и лишь потом гадаем по самой строке: так правка
    чужой настройки не превращает её в строку молча.
    """
    if isinstance(value, bool):
        return value
    kind = SETTING_HINTS.get(key, {}).get("type")
    text = str("" if value is None else value).strip()

    if kind == "bool" or isinstance(previous, bool):
        return text.lower() in ("1", "true", "yes", "on", "да")
    numeric = isinstance(previous, (int, float)) and not isinstance(previous, bool)
    if kind == "number" or numeric or (kind is None and previous is None
                                       and re.fullmatch(r"-?\d+(\.\d+)?", text)):
        try:
            return int(text) if re.fullmatch(r"-?\d+", text) else float(text)
        except ValueError:
            return text
    if kind is None and previous is None and text.lower() in ("true", "false"):
        return text.lower() == "true"
    return text


def write_account_config(filename: str, env, settings) -> tuple[bool, str]:
    """Заменяет секции `env` и/или `settings` аккаунта.

    `None` вместо раздела означает «не трогать»: webview, загруженный
    до появления второй секции, шлёт только `env`, и его правка не
    должна стирать настройки верхнего уровня.

    Пишет в источник (см. source_path), а если правился активный
    аккаунт — обновляет и активную копию `settings.json`. Без второго
    шага правка не дожила бы до перезапуска: копия осталась бы старой,
    а `env` читают именно из неё.
    """
    if env is None and settings is None:
        return False, "Нечего сохранять: не передан ни env, ни settings"
    if env is not None and not isinstance(env, dict):
        return False, "env должен быть объектом"
    if settings is not None and not isinstance(settings, dict):
        return False, "settings должен быть объектом"

    clean_env: dict[str, str] = {}
    if env is not None:
        for key, value in env.items():
            name = str(key).strip()
            if not name:
                continue
            if not ENV_KEY_RE.match(name):
                return False, f"Недопустимое имя переменной: {name!r}"
            # Значения в env всегда строки — даже числовые
            # («API_TIMEOUT_MS»: «3000000»). Приводим сами, чтобы правка
            # через панель не меняла тип молча.
            clean_env[name] = "" if value is None else str(value)

    ok, message, data = _load_account(filename)
    if not ok:
        return False, message

    if env is not None:
        if clean_env:
            data["env"] = clean_env
        else:
            # Пустую секцию не оставляем: у базового аккаунта `env` нет
            # вовсе, и пустой объект был бы отличием без разницы.
            data.pop("env", None)

    if settings is not None:
        clean_settings = {}
        for key, value in settings.items():
            name = str(key).strip()
            if not name:
                continue
            if name in SETTINGS_HIDDEN or not SETTING_KEY_RE.match(name):
                return False, f"Недопустимое имя настройки: {name!r}"
            clean_settings[name] = _coerce_setting(name, value, data.get(name))
        # Удаляем только то, что панель показывала: невидимые ей ключи
        # (структуры, `$schema`) она удалить не просила и не могла.
        for name in _visible_settings(data):
            if name not in clean_settings:
                data.pop(name, None)
        data.update(clean_settings)

    path = source_path(filename)
    try:
        _write_json_atomic(path, data)
        if filename == get_current_account() and path != SETTINGS_FILE:
            _write_json_atomic(SETTINGS_FILE, data)
    except OSError as exc:
        return False, f"Ошибка записи: {exc}"

    suffix = (" Применится после перезапуска расширения."
              if filename == get_current_account() else "")
    return True, f"Настройки {_display_name(filename)} сохранены.{suffix}"


def _write_json_atomic(path: str, data: dict) -> None:
    """Запись через временный файл в той же директории + replace.

    Тот же довод, что и у _copy_atomic: settings.json читается
    процессом Claude Code в произвольный момент, и прямая запись
    оставила бы окно, в котором файл наполовину пуст.
    """
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".env-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        if os.path.exists(path):
            shutil.copymode(path, tmp)
        os.replace(tmp, path)
    except OSError:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def _copy_atomic(src: str, dst: str) -> None:
    """Копирование через временный файл в той же директории + replace.

    settings.json читается процессом Claude Code в произвольный момент;
    прямая запись оставила бы окно, в котором файл наполовину пуст.
    """
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dst), prefix=".switch-")
    os.close(fd)
    try:
        shutil.copyfile(src, tmp)
        shutil.copymode(src, tmp)
        os.replace(tmp, dst)
    except OSError:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def _read_subs() -> dict:
    """Все записи о подписках: {файл: {paidAt, days}}. Битые — мимо."""
    try:
        with open(SUBS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _parse_paid_at(value) -> datetime.datetime | None:
    """«2026-09-05T14:30» → datetime; None, если значение не дата."""
    if not isinstance(value, str) or not SUB_PAID_AT_RE.match(value):
        return None
    try:
        return datetime.datetime.fromisoformat(value)
    except ValueError:
        return None


def read_subscription(filename: str, subs: dict | None = None) -> dict | None:
    """Запись о подписке аккаунта: {paidAt, days, email, plan} или None.

    `subs` — уже прочитанный файл записей: list_accounts читает его
    один раз на все аккаунты. Запись валидна, если есть хоть что-то:
    дата оплаты, тариф или почта. Молча дотягивает срок до
    предельного: показывать в панели пустоту из-за опечатки в ручном
    JSON хуже, чем показать с запасным.
    """
    if not ACCOUNT_NAME_RE.match(filename or ""):
        return None
    if subs is None:
        subs = _read_subs()
    record = subs.get(filename)
    if not isinstance(record, dict):
        return None
    out: dict = {}
    if _parse_paid_at(record.get("paidAt")) is not None:
        out["paidAt"] = record["paidAt"]
        days = record.get("days")
        if (not isinstance(days, int) or isinstance(days, bool)
                or not 1 <= days <= MAX_SUB_DAYS):
            days = DEFAULT_SUB_DAYS
        out["days"] = days
    for key in ("email", "plan"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            out[key] = value.strip()[:128]
    return out or None


def write_subscription(filename: str, paid_at, days, email, plan,
                       automatic: bool = False) -> tuple[bool, str]:
    """Сохраняет запись о подписке или удаляет её, когда пусто всё.

    Пустая дата — не ошибка: запись может жить ради тарифа и почты
    (у Z.AI срок всё равно приходит из его API). Дата без срока —
    месяц по умолчанию.

    Для OpenAI-аккаунта запись ведёт только автоматика: срок подписки
    ChatGPT берётся из Codex id_token (хук codex_id_token_sync.py),
    ручной ввод был бы затёр при ближайшем обновлении. `automatic=True`
    открывает запись этому хуку.
    """
    if not ACCOUNT_NAME_RE.match(filename or ""):
        return False, f"Недопустимое имя аккаунта: {filename!r}"
    if not automatic and _describe(source_path(filename))["provider"] == "openai":
        return False, ("Срок подписки OpenAI-аккаунта поддерживается "
                       "автоматически — из Codex id_token")

    text = "" if paid_at is None else str(paid_at).strip()
    moment = _parse_paid_at(text) if text else None
    if text and moment is None:
        return False, "Дата оплаты не разобрана: нужен формат ГГГГ-ММ-ДД ЧЧ:ММ"

    labels: dict[str, str] = {}
    for key, value in (("email", email), ("plan", plan)):
        clean = "" if value is None else str(value).strip()
        if len(clean) > 128:
            return False, f"Поле «{key}»: не длиннее 128 символов"
        if clean:
            labels[key] = clean

    subs = _read_subs()
    if moment is None and not labels:
        if filename not in subs:
            return True, "Данных подписки у аккаунта и не было"
        del subs[filename]
        try:
            _write_json_atomic(SUBS_FILE, subs)
        except OSError as exc:
            return False, f"Ошибка записи: {exc}"
        return True, "Данные подписки убраны — строка срока исчезнет из списка"

    number = DEFAULT_SUB_DAYS
    if moment is not None:
        if days is not None and not isinstance(days, bool):
            try:
                number = int(str(days).strip())
            except (TypeError, ValueError):
                return False, f"Срок подписки — число дней, а не {days!r}"
        if not 1 <= number <= MAX_SUB_DAYS:
            return False, f"Срок подписки — от 1 до {MAX_SUB_DAYS} дней"

    record: dict = dict(labels)
    if moment is not None:
        record["paidAt"] = moment.isoformat(timespec="minutes")
        record["days"] = number
    subs[filename] = record
    try:
        _write_json_atomic(SUBS_FILE, subs)
    except OSError as exc:
        return False, f"Ошибка записи: {exc}"

    bits = []
    if moment is not None:
        bits.append("оплачена " + moment.strftime("%d.%m.%Y %H:%M")
                    + f", срок {number} дн")
    if labels:
        bits.append("тариф и почта" if len(labels) == 2
                    else next(iter(labels)))
    return True, ("Данные подписки " + _display_name(filename) + ": "
                  + "; ".join(bits))


def subscription_info(record: dict) -> dict | None:
    """Валидная запись подписки, дополненная рассчитанным сроком.

    Пока период не кончился, отдаём остаток; после конца — признак
    `expired`, чтобы панель честно написала «истекла», а не ноль дней.
    Время локальное и без пояса с обеих сторон — разность корректна.
    """
    moment = _parse_paid_at(record.get("paidAt"))
    if moment is None:
        return None
    days = record.get("days", DEFAULT_SUB_DAYS)
    until = moment + datetime.timedelta(days=days)
    left = int((until - datetime.datetime.now()).total_seconds())
    return {
        "paidAt": record["paidAt"],
        "days": days,
        "until": until.isoformat(timespec="minutes"),
        "leftSec": left,
        "expired": left <= 0,
    }


def _zai_host(env: dict) -> str | None:
    """Хост Z.AI из env аккаунта; None — провайдер не Z.AI."""
    base = str(env.get("ANTHROPIC_BASE_URL") or "")
    host = base.split("//", 1)[-1].split("/", 1)[0].lower()
    return host if host in ("api.z.ai", "open.bigmodel.cn") else None


def _zai_cache_payload(token: str, path: str, version: int,
                       ok_ttl: float, fail_ttl: float) -> dict | None:
    """Свежий кэш-файл для этого токена: {result, savedAt} или None.

    Общая часть запросов к Z.AI: вычитка, сверка подписи и TTL.
    `savedAt` нужен наружу для возраста данных.
    """
    stamp = hashlib.sha256(token.encode()).hexdigest()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    # Версия: правка разбора ответа должна протолкнуть старый кэш,
    # иначе ответ прежней логики доживал бы свой TTL.
    if (not isinstance(data, dict) or data.get("token") != stamp
            or data.get("v") != version):
        return None
    ttl = ok_ttl if data.get("result") else fail_ttl
    if time.time() - float(data.get("savedAt") or 0) > ttl:
        return None
    return data


def _zai_cache_age(payload: dict) -> int:
    """Возраст кэш-записи в секундах, от нуля и выше."""
    return max(0, int(time.time() - float(payload.get("savedAt") or 0)))


def _read_zai_cache(token: str, path: str = ZAI_CACHE_FILE,
                    version: int = 2) -> dict | None:
    """Свежий результат из кэша (None — нет/просрочен)."""
    payload = _zai_cache_payload(
        token, path, version, ZAI_CACHE_TTL_SEC, ZAI_FAIL_TTL_SEC)
    if payload is None:
        return None
    result = payload.get("result")
    # Отрицательный кэш — это «ничего не нашлось», а не «не Z.AI».
    return result if isinstance(result, dict) else None


def _write_zai_cache(token: str, result: dict | None,
                     path: str = ZAI_CACHE_FILE, version: int = 2) -> None:
    stamp = hashlib.sha256(token.encode()).hexdigest()
    payload = {"v": version, "token": stamp, "savedAt": time.time(),
               "result": result}
    try:
        _write_json_atomic(path, payload)
    except OSError:
        pass  # кэш вспомогательный: без него просто лишний запрос


def zai_subscription(env: dict) -> dict | None:
    """Тариф и срок Coding Plan с сервера Z.AI, в формате подписки.

    Формат тот же, что у ручной записи из subscription_info, плюс
    источник: панель показывает срок одинаково, а подсказку — честнее.
    None — провайдер не Z.AI, ключа нет или спросить не вышло; тогда
    строку срока даёт ручная запись, если она есть.
    """
    host = _zai_host(env)
    token = env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY")
    if not host or not token:
        return None
    cached = _read_zai_cache(token)
    if cached is not None:
        return cached

    # Китайская станция ждёт raw-ключ, международная — Bearer.
    auth = token if host == "open.bigmodel.cn" else "Bearer " + token
    request = urllib.request.Request(
        "https://" + host + ZAI_SUB_PATH,
        headers={"Authorization": auth, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=ZAI_HTTP_TIMEOUT) as resp:
            payload = json.load(resp)
    except (OSError, ValueError):
        _write_zai_cache(token, None)
        return None

    items = payload.get("data") if isinstance(payload, dict) else None
    first = None
    if isinstance(items, list):
        first = next((i for i in items
                      if isinstance(i, dict) and i.get("status") == "VALID"), None)
    valid = str(first.get("valid") or "") if first else ""
    span = ZAI_VALID_RE.match(valid)
    plan = first.get("productName") if first else None
    if not span or not isinstance(plan, str) or not plan:
        _write_zai_cache(token, None)
        return None

    # Начало — время покупки. Конец оплаченного — день следующего
    # списания: `valid` показывает горизонт контракта (следующий
    # период), а оплачен только текущий. Прецедент: покупка 19.08,
    # nextRenewTime 19.09, valid 19.09–19.10 — тариф действует
    # до 19.09, и только потом спишут за следующий.
    start = datetime.datetime.strptime(
        str(first.get("purchaseTime") or span.group(1)), "%Y-%m-%d %H:%M:%S")
    renew = str(first.get("nextRenewTime") or "")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", renew):
        until = datetime.datetime.strptime(
            renew + " " + start.strftime("%H:%M:%S"), "%Y-%m-%d %H:%M:%S")
    else:
        until = datetime.datetime.strptime(span.group(2), "%Y-%m-%d %H:%M:%S")
    left = int((until - datetime.datetime.now()).total_seconds())
    result = {
        "paidAt": start.isoformat(timespec="minutes"),
        "days": int((until - start).total_seconds() // 86400),
        "until": until.isoformat(timespec="minutes"),
        "leftSec": left,
        "expired": left <= 0,
        "source": "zai",
        "plan": plan,
        "autoRenew": first.get("autoRenew") == 1,
    }
    _write_zai_cache(token, result)
    return result


def zai_usage(env: dict) -> dict | None:
    """Лимиты квот GLM Coding Plan: окна 5 ч и неделя, полосками как claude.ai.

    Endpoint `/api/monitor/usage/quota/limit` — клиентская ручка, которой
    страница подписки в кабинете Z.AI рисует прогресс квоты: ключ и хост
    те же, что у zai_subscription. Наружу — формат anthropic_usage
    (windows + ageSec), панель рисует полоски одним кодом. Неудача тоже
    кэшируется на короткий TTL, чтобы панель не долбила лежащий API.

    None — провайдер не Z.AI, ключа нет или данных не нашлось; панель
    в этом случае полосок не рисует.
    """
    host = _zai_host(env)
    token = env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY")
    if not host or not token:
        return None

    payload = _zai_cache_payload(
        token, ZAI_USAGE_CACHE_FILE, 1, ZAI_USAGE_TTL_SEC, ZAI_FAIL_TTL_SEC)
    if payload is not None:
        result = payload.get("result")
        if isinstance(result, dict):
            return {**result, "ageSec": _zai_cache_age(payload)}
        return None  # неудача свежа — сеть не зовём

    # Китайская станция ждёт raw-ключ, международная — Bearer.
    auth = token if host == "open.bigmodel.cn" else "Bearer " + token
    request = urllib.request.Request(
        "https://" + host + ZAI_QUOTA_PATH,
        headers={"Authorization": auth, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=ZAI_HTTP_TIMEOUT) as resp:
            body = json.load(resp)
    except (OSError, ValueError):
        _write_zai_cache(token, None, ZAI_USAGE_CACHE_FILE, 1)
        return None

    data = body.get("data") if isinstance(body, dict) else None
    items = data.get("limits") if isinstance(data, dict) else None
    windows = []
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            percent = item.get("percentage")
            # bool — тоже int, а полоска на True шириной 100% была бы
            # красивой неправдой.
            if isinstance(percent, bool) or not isinstance(percent, (int, float)):
                continue
            kind = ZAI_WINDOW_KINDS.get((item.get("unit"), item.get("number")))
            if kind is not None:
                key, label, title = kind
            else:
                key = None
                unit = ZAI_UNIT_NAMES.get(item.get("unit"),
                                          "×" + str(item.get("unit")))
                label = f"{item.get('number')} {unit}"
                title = label
            reset_ms = item.get("nextResetTime")
            if isinstance(reset_ms, bool) or not isinstance(reset_ms, (int, float)):
                left = None
            else:
                left = int(reset_ms / 1000 - time.time())
            expired = left is not None and left <= 0
            windows.append({
                "key": key,
                "label": label,
                "title": title,
                "percent": 0 if expired else percent,
                "resetsInSec": None if expired else left,
                "expired": expired,
            })
    if not windows:
        _write_zai_cache(token, None, ZAI_USAGE_CACHE_FILE, 1)
        return None
    result = {"windows": windows, "sourceLabel": "Данные Z.AI"}
    _write_zai_cache(token, result, ZAI_USAGE_CACHE_FILE, 1)
    return result


def log_account_event(from_file: str, to_file: str, revert: bool = False) -> None:
    """Записывает переключение аккаунта в журнал событий.

    `revert=True` — это возврат прежнего аккаунта после отказа от
    перезапуска расширения. Такое событие пишется тоже, но помечается:
    для пользователя переключения не было, процесс CLI как работал на
    старом аккаунте, так и работает, и кэш ничего не терял. Показывать
    такую пару в истории значило бы объяснять промахи тем, чего не
    происходило. А не писать вовсе — потерять след, когда откат
    сорвался и на диске остался чужой аккаунт.
    """
    record = {
        "ts": datetime.datetime.now(datetime.timezone.utc)
        .isoformat(timespec="seconds").replace("+00:00", "Z"),
        "kind": "account",
        "from": from_file,
        "to": to_file,
        "from_name": _display_name(from_file) if from_file else "",
        "to_name": _display_name(to_file) if to_file else "",
        "revert": bool(revert),
    }
    try:
        os.makedirs(os.path.dirname(ACCOUNT_EVENTS_LOG), exist_ok=True)
        lines = []
        if os.path.isfile(ACCOUNT_EVENTS_LOG):
            with open(ACCOUNT_EVENTS_LOG, encoding="utf-8") as fh:
                lines = fh.readlines()[-(MAX_ACCOUNT_EVENTS - 1):]
        lines.append(json.dumps(record, ensure_ascii=False) + "\n")
        tmp = ACCOUNT_EVENTS_LOG + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.writelines(lines)
        os.replace(tmp, ACCOUNT_EVENTS_LOG)
    except OSError:
        # Журнал — вспомогательный: без него панель просто не объяснит
        # промах, а переключение всё равно должно состояться.
        pass


def read_account_events(limit: int = MAX_ACCOUNT_EVENTS) -> list[dict]:
    """Читает журнал переключений. Откаты отбрасывает — см. выше."""
    try:
        with open(ACCOUNT_EVENTS_LOG, encoding="utf-8") as fh:
            lines = fh.readlines()[-limit:]
    except OSError:
        return []
    events = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict) and not rec.get("revert"):
            events.append(rec)
    return events


def switch_account(target: str, revert: bool = False) -> tuple[bool, str]:
    """Делает `target` активным settings.json.

    `revert` — признак того, что это возврат после отказа от
    перезапуска; уходит в журнал событий и там означает «переключения
    для пользователя не было».

    Возвращает (успех, сообщение для пользователя).
    """
    if not ACCOUNT_NAME_RE.match(target or ""):
        return False, f"Недопустимое имя аккаунта: {target!r}"

    target_path = os.path.join(CLAUDE_DIR, target)
    if not os.path.isfile(target_path):
        return False, f"Файл {target} не найден"

    current = get_current_account()
    if current == target:
        return True, f"{_display_name(target)} уже активен"

    try:
        if target == BASE_NAME:
            # Возврат на оригинал: восстанавливаем из бэкапа.
            if not os.path.isfile(BACKUP_FILE):
                return False, ("Бэкап settings.json.bak не найден — "
                               "восстановить оригинал нечем")
            _copy_atomic(BACKUP_FILE, SETTINGS_FILE)
            os.remove(BACKUP_FILE)
            if os.path.isfile(ACTIVE_MARKER):
                os.remove(ACTIVE_MARKER)
        else:
            # Уход с оригинала: сохраняем его один раз. Повторное
            # копирование затёрло бы бэкап настройками чужого провайдера.
            if not os.path.isfile(BACKUP_FILE):
                if not os.path.isfile(SETTINGS_FILE):
                    return False, "settings.json отсутствует — нечего сохранять"
                _copy_atomic(SETTINGS_FILE, BACKUP_FILE)
            _copy_atomic(target_path, SETTINGS_FILE)
            with open(ACTIVE_MARKER, "w", encoding="utf-8") as fh:
                fh.write(target)
    except OSError as exc:
        return False, f"Ошибка переключения: {exc}"

    # Пишем только состоявшуюся подмену: неудачная ветка выходит выше,
    # а «уже активен» — вообще не событие.
    log_account_event(current, target, revert=revert)

    return True, (f"Активен {_display_name(target)}. "
                  "Применится после перезапуска расширения")


if __name__ == "__main__":
    for acc in list_accounts():
        mark = "*" if acc["isActive"] else " "
        print(f"{mark} {acc['file']:<24} {acc['name']:<14} {acc['baseUrl']}")
