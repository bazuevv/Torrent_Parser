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

import glob
import json
import os
import re
import shutil
import tempfile

CLAUDE_DIR = os.path.expanduser("~/.claude")
SETTINGS_FILE = os.path.join(CLAUDE_DIR, "settings.json")
BACKUP_FILE = SETTINGS_FILE + ".bak"
ACTIVE_MARKER = os.path.join(CLAUDE_DIR, ".active-account")

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


def _describe(path: str) -> dict:
    """Короткая сводка о провайдере: endpoint и модель."""
    env = _read_env(path)
    base_url = env.get("ANTHROPIC_BASE_URL") or "api.anthropic.com (по умолчанию)"
    model = (env.get("ANTHROPIC_MODEL")
             or env.get("ANTHROPIC_DEFAULT_OPUS_MODEL")
             or "")
    return {"baseUrl": base_url, "model": model}


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
    accounts = []
    for path in sorted(glob.glob(os.path.join(CLAUDE_DIR, "settings*.json"))):
        filename = os.path.basename(path)
        if filename in EXCLUDED or not ACCOUNT_NAME_RE.match(filename):
            continue
        info = _describe(source_path(filename))
        accounts.append({
            "file": filename,
            "name": _display_name(filename),
            "isActive": filename == current,
            "baseUrl": info["baseUrl"],
            "model": info["model"],
        })

    # Базовый аккаунт первым, остальные по алфавиту.
    accounts.sort(key=lambda a: (a["file"] != BASE_NAME, a["file"]))
    return accounts


def read_account_env(filename: str) -> tuple[bool, str, dict]:
    """Секция `env` аккаунта: (успех, сообщение, env).

    Возвращается именно `env`, а не весь файл: провайдера задают
    переменные окружения, а `permissions`, `model` и прочее — это
    пользовательские настройки, к выбору аккаунта не относящиеся.
    Трогать их через панель незачем, а испортить можно.
    """
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
    env = data.get("env")
    return True, "", env if isinstance(env, dict) else {}


def write_account_env(filename: str, env: dict) -> tuple[bool, str]:
    """Заменяет секцию `env` аккаунта, остальной файл не трогая.

    Пишет в источник (см. source_path), а если правился активный
    аккаунт — обновляет и активную копию `settings.json`. Без второго
    шага правка не дожила бы до перезапуска: копия осталась бы старой,
    а `env` читает именно её.
    """
    if not ACCOUNT_NAME_RE.match(filename or ""):
        return False, f"Недопустимое имя аккаунта: {filename!r}"
    if not isinstance(env, dict):
        return False, "env должен быть объектом"

    clean: dict[str, str] = {}
    for key, value in env.items():
        name = str(key).strip()
        if not name:
            continue
        if not ENV_KEY_RE.match(name):
            return False, f"Недопустимое имя переменной: {name!r}"
        # Значения в settings.json всегда строки — даже числовые
        # («API_TIMEOUT_MS»: «3000000»). Приводим сами, чтобы правка
        # через панель не меняла тип молча.
        clean[name] = "" if value is None else str(value)

    path = source_path(filename)
    if not os.path.isfile(path):
        return False, f"Файл {filename} не найден"
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        return False, f"{filename}: {exc}"
    if not isinstance(data, dict):
        return False, f"{filename}: ожидался объект JSON"

    if clean:
        data["env"] = clean
    else:
        # Пустую секцию не оставляем: у базового аккаунта `env` нет
        # вовсе, и пустой объект был бы отличием без разницы.
        data.pop("env", None)

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
