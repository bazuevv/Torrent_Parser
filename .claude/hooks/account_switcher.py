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
import json
import os
import re
import shutil
import tempfile
import time

CLAUDE_DIR = os.path.expanduser("~/.claude")
SETTINGS_FILE = os.path.join(CLAUDE_DIR, "settings.json")
BACKUP_FILE = SETTINGS_FILE + ".bak"
ACTIVE_MARKER = os.path.join(CLAUDE_DIR, ".active-account")

# Глобальное состояние самого Claude Code — не настройки, а его рабочий
# файл. Нам оттуда нужен только кэш лимитов подписки claude.ai
# (`cachedUsageUtilization`), см. anthropic_usage().
CONFIG_JSON = os.path.expanduser("~/.claude.json")

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
    return {"baseUrl": base_url, "model": model, "oauth": oauth}


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


def _resets_in_sec(value) -> int | None:
    """Сколько секунд осталось до сброса окна (None — время не разобрать)."""
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    left = (moment - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
    return max(0, int(left))


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
        windows.append({
            "key": key,
            "label": label,
            "title": title,
            "percent": percent,
            "resetsInSec": _resets_in_sec(window.get("resets_at")),
        })
    if not windows:
        return None

    fetched = cached.get("fetchedAtMs")
    age = None
    if isinstance(fetched, (int, float)) and not isinstance(fetched, bool):
        age = max(0, int(time.time() - fetched / 1000))
    return {"windows": windows, "ageSec": age}


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


def list_accounts() -> list[dict]:
    """Все settings-файлы в ~/.claude/ как список аккаунтов.

    Скан по маске, а не по жёсткому списку: добавленный руками
    `settings_foo.json` появляется в панели сам.
    """
    current = get_current_account()
    # Лимиты читаются один раз на список: файл общий, а аккаунтов
    # на OAuth-логине может оказаться больше одного.
    usage = anthropic_usage()
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
        }
        # Лимиты принадлежат логину claude.ai, а не активному аккаунту:
        # пока сессия идёт через стороннего провайдера, они не тратятся,
        # но остаются тем, что ждёт при возврате на Anthropic.
        if info["oauth"] and usage:
            entry["usage"] = usage
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

    Разделов два. `env` задаёт провайдера — адрес, ключ, подмену
    моделей. `settings` — скалярные настройки верхнего уровня (`model`,
    `language`, `effortLevel`, …): у аккаунта Anthropic секции `env` нет
    вовсе, и без них редактор для него был бы пуст.
    """
    ok, message, data = _load_account(filename)
    if not ok:
        return False, message, {}
    env = data.get("env")
    return True, "", {
        "env": env if isinstance(env, dict) else {},
        "settings": _visible_settings(data),
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


def switch_account(target: str) -> tuple[bool, str]:
    """Делает `target` активным settings.json.

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

    return True, (f"Активен {_display_name(target)}. "
                  "Применится после перезапуска расширения")


if __name__ == "__main__":
    for acc in list_accounts():
        mark = "*" if acc["isActive"] else " "
        print(f"{mark} {acc['file']:<24} {acc['name']:<14} {acc['baseUrl']}")
