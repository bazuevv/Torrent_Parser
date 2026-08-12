#!/usr/bin/env python3
"""SessionStart-хук: добавляет наши кастомные пункты в настройки
расширения Claude Code, чтобы их можно было менять из VSCode
Settings UI (Settings → Extensions → Claude Code), а не только
правкой `.claude/patches/claude-custom-config.toml`.

Сейчас добавляется один пункт:

  claudeCode.emojiButtonPlacement — где показывать кнопку 😀:
    "mic"    — слева от микрофона, в правом верхнем углу поля ввода;
    "footer" — в футере, рядом с кнопкой меню `/`.

Значение читает не этот хук, а `patch-claude-webview.py`: он берёт
его из settings.json VSCode и кладёт в `window.__CLAUDE_CUSTOM_CONFIG__`
для инжектируемого claude-custom.js. Здесь только регистрация пункта
в манифесте — без неё VSCode считает ключ неизвестным и не рисует
его в UI.

ПОРЯДОК ХУКОВ ВАЖЕН. `localize.py` при каждом запуске восстанавливает
package.json из `package.json.original` и заново применяет переводы
(см. _apply_package_json). Любая наша правка, сделанная ДО него,
будет затёрта. Поэтому в `.claude/settings.json` этот хук
зарегистрирован последним в цепочке SessionStart — после localize.py.

Идемпотентен: если свойство уже описано ровно так, как надо,
файл не переписывается.
"""

import glob
import json
import os
import sys
import tomllib

HOME = os.path.expanduser("~")
EXT_GLOB = os.path.join(HOME, ".vscode/extensions/anthropic.claude-code-*-linux-x64")

PROJECT_DIR = os.environ.get("CLAUDE_PROJECT_DIR")
if not PROJECT_DIR:
    PROJECT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

CANONICAL_CONFIG = os.path.join(
    PROJECT_DIR, ".claude", "patches", "claude-custom-config.toml"
)

SETTING_KEY = "claudeCode.emojiButtonPlacement"

# Описания на двух языках: расширение локализуется хуком localize.py
# по параметру `locale`, и наш пункт не должен выбиваться из общего
# языка настроек.
DESCRIPTIONS = {
    "ru": {
        "description": (
            "Кастомный патч: где показывать кнопку вставки смайликов "
            "в поле ввода чата."
        ),
        "enumDescriptions": [
            "Рядом с микрофоном (правый верхний угол поля ввода)",
            "В футере, рядом с кнопкой меню /",
        ],
    },
    "en": {
        "description": (
            "Custom patch: where to show the emoji picker button "
            "in the chat input."
        ),
        "enumDescriptions": [
            "Next to the microphone (top-right corner of the input)",
            "In the footer, next to the / menu button",
        ],
    },
}


def _read_locale() -> str:
    """Локаль из claude-custom-config.toml; 'en' при любой проблеме."""
    try:
        with open(CANONICAL_CONFIG, "rb") as f:
            cfg = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return "en"
    locale = cfg.get("locale")
    if isinstance(locale, str) and locale in DESCRIPTIONS:
        return locale
    return "en"


def _desired_property(locale: str) -> dict:
    texts = DESCRIPTIONS.get(locale, DESCRIPTIONS["en"])
    return {
        "type": "string",
        "enum": ["mic", "footer"],
        "enumDescriptions": texts["enumDescriptions"],
        "default": "mic",
        "description": texts["description"],
    }


def _patch_manifest(pkg_path: str, desired: dict) -> str:
    """Вписывает свойство в contributes.configuration.properties.

    Возвращает status: "no_file" | "unreadable" | "bad_structure" |
    "already" | "patched" | "write_failed".
    """
    if not os.path.isfile(pkg_path):
        return "no_file"
    try:
        with open(pkg_path, "r", encoding="utf-8") as f:
            raw = f.read()
        pkg = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return "unreadable"

    contributes = pkg.get("contributes")
    if not isinstance(contributes, dict):
        return "bad_structure"
    configuration = contributes.get("configuration")
    # В манифесте Claude Code это объект, но спецификация VSCode
    # допускает и массив блоков — тогда берём первый с properties.
    if isinstance(configuration, list):
        configuration = next(
            (b for b in configuration if isinstance(b, dict) and "properties" in b),
            None,
        )
    if not isinstance(configuration, dict):
        return "bad_structure"
    properties = configuration.get("properties")
    if not isinstance(properties, dict):
        return "bad_structure"

    if properties.get(SETTING_KEY) == desired:
        return "already"

    properties[SETTING_KEY] = desired

    # Манифест отформатирован табами — сохраняем стиль, иначе diff
    # против package.json.original становится нечитаемым.
    text = json.dumps(pkg, ensure_ascii=False, indent="\t")
    if raw.endswith("\n"):
        text += "\n"
    try:
        with open(pkg_path, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        return "write_failed"
    return "patched"


def _marker_path() -> str:
    return os.path.join(
        PROJECT_DIR, ".claude", "hooks-runtime", "ext-settings-applied.json"
    )


def _load_marker() -> dict:
    """Слепок того, что мы уже вписывали в манифест, по каталогам
    расширений: {<ext_dir_name>: {"version": ..., "property": {...}}}.

    Нужен, чтобы не поднимать шум на пустом месте. localize.py
    откатывает package.json из бэкапа на КАЖДОМ SessionStart, и наш
    хук каждый раз вписывает пункт заново — но пользователю это
    неинтересно. Сообщение про `Developer: Reload Window` уместно
    только когда пункт действительно новый: расширение обновилось
    или изменилось само описание настройки.
    """
    try:
        with open(_marker_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_marker(marker: dict) -> None:
    path = _marker_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(marker, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError:
        pass


def _read_ext_version(ext_dir: str) -> str:
    try:
        with open(os.path.join(ext_dir, "package.json"), "r", encoding="utf-8") as f:
            return str(json.load(f).get("version", ""))
    except (OSError, json.JSONDecodeError):
        return ""


def _emit_context(lines: list[str]) -> None:
    if not lines:
        return
    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "\n".join(lines),
        }
    }
    try:
        print(json.dumps(output, ensure_ascii=False))
    except OSError:
        pass


def main() -> int:
    desired = _desired_property(_read_locale())
    messages: list[str] = []
    marker = _load_marker()
    marker_changed = False

    for ext_dir in glob.glob(EXT_GLOB):
        name = os.path.basename(ext_dir)
        status = _patch_manifest(os.path.join(ext_dir, "package.json"), desired)
        if status in ("patched", "already"):
            seen = marker.get(name)
            current = {"version": _read_ext_version(ext_dir), "property": desired}
            if seen != current:
                marker[name] = current
                marker_changed = True
                messages.append(
                    f"[ext-settings WARNING] В `{name}/package.json` добавлен пункт "
                    f"`{SETTING_KEY}`. Чтобы он появился в Settings UI, нужен "
                    "`Developer: Reload Window` — VSCode читает манифесты расширений "
                    "при старте."
                )
        elif status in ("bad_structure", "unreadable"):
            messages.append(
                f"[ext-settings WARNING] `{name}/package.json` не удалось "
                f"обработать ({status}) — пункт `{SETTING_KEY}` НЕ добавлен, "
                "в Settings UI его не будет. Проверь "
                "`.claude/hooks/patch-extension-settings.py`."
            )
        elif status == "write_failed":
            messages.append(
                f"[ext-settings WARNING] `{name}/package.json` не удалось "
                "записать — проверь права на файл."
            )

    if marker_changed:
        _save_marker(marker)
    _emit_context(messages)
    return 0


if __name__ == "__main__":
    sys.exit(main())
