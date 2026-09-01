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

ПОЧЕМУ ПАТЧИТСЯ И БЭКАП. `localize.py` при каждом запуске
восстанавливает package.json из `package.json.original` и заново
применяет переводы (см. _apply_package_json), затирая всё, чего в
бэкапе нет. Порядок хуков от этого не спасает: harness запускает
хуки одного события ПАРАЛЛЕЛЬНО, и позиция в массиве
`.claude/settings.json` ничего не гарантирует — 2026-08-12 наш
лёгкий хук стабильно финишировал на ~35 мс раньше тяжёлого
localize.py, и пункт настройки исчезал.

Поэтому свойство пишется в оба файла: в package.json — на языке
локали, в package.json.original — по-английски (localize.py сам
переведёт его по словарю static.settings, как и остальные строки).
Тогда результат одинаков при любом порядке выполнения.

Переводы берутся из `.claude/patches/locales/<locale>.json` — того же
файла, которым пользуется localize.py, чтобы формулировка не
разъезжалась по двум местам.

Идемпотентен: если свойство уже описано ровно так, как надо,
файл не переписывается.
"""

import glob
import json
import os
import sys
import time
import tomllib

# ext_patch лежит рядом; при запуске скриптом sys.path[0] — эта папка,
# но вставляем явно, чтобы импорт не зависел от способа запуска.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ext_patch  # noqa: E402

HOME = os.path.expanduser("~")
EXT_GLOB = os.path.join(HOME, ".vscode/extensions/anthropic.claude-code-*-linux-x64")

# Индекс установленных расширений. VSCode кэширует разобранные манифесты
# и считает кэш валидным, пока не изменился mtime ЭТОГО файла — правка
# package.json внутри каталога расширения его не инвалидирует. Поэтому
# после патча манифеста файл нужно «тронуть», иначе Settings UI будет
# показывать старый набор настроек даже после Reload Window.
EXTENSIONS_INDEX = os.path.join(HOME, ".vscode/extensions/extensions.json")

# Кэш разобранных манифестов (по профилю VSCode).
CACHE_GLOBS = [
    os.path.join(HOME, ".config/Code/CachedProfilesData/*/extensions.user.cache"),
    os.path.join(
        HOME, ".config/Code - Insiders/CachedProfilesData/*/extensions.user.cache"
    ),
]

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
# Канонические (английские) строки пункта. Русский вариант не хранится
# здесь: он живёт в `.claude/patches/locales/<locale>.json`, откуда его
# берёт и localize.py. Два источника правды разъехались бы при первой же
# правке формулировки.
DESCRIPTION_EN = (
    "Custom patch: where to show the emoji picker button in the chat input."
)
# markdownDescription VSCode рендерит как Markdown и предпочитает
# обычному description. Эмодзи + жирный заголовок визуально отделяют
# наши пункты от родных настроек расширения — покрасить строку
# средствами Settings UI нельзя, оболочка VSCode нам не подконтрольна.
MARKDOWN_DESCRIPTION_EN = (
    "🧩 **Added by local patch** (`.claude/patches/`). "
    "Where to show the emoji picker button in the chat input. "
    "Filter all patched settings: `@tag:claude-custom-patch`"
)
ENUM_DESCRIPTIONS_EN = [
    "Next to the microphone (top-right corner of the input)",
    "In the footer, next to the / menu button",
]
# Свой тег — по нему Settings UI умеет фильтровать: `@tag:claude-custom-patch`
# покажет ровно наши пункты и ничего больше.
SETTING_TAG = "claude-custom-patch"


def _read_locale() -> str:
    """Локаль из claude-custom-config.toml; 'en' при любой проблеме."""
    try:
        with open(CANONICAL_CONFIG, "rb") as f:
            cfg = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return "en"
    locale = cfg.get("locale")
    if isinstance(locale, str) and len(locale) >= 2:
        return locale
    return "en"


def _load_translations(locale: str) -> dict:
    """Словарь {английская строка: перевод} из static.settings локали.

    Тот же файл и та же секция, которыми пользуется localize.py, —
    поэтому перевод пункта достаточно добавить в одном месте.
    """
    if locale == "en":
        return {}
    path = os.path.join(
        PROJECT_DIR, ".claude", "patches", "locales", f"{locale}.json"
    )
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    settings = data.get("static", {}).get("settings", {})
    if not isinstance(settings, dict):
        return {}
    return {k: v for k, v in settings.items() if isinstance(v, str)}


def _desired_property(translations: dict) -> dict:
    """Описание пункта; строки переводятся, если перевод есть в словаре."""
    def tr(text: str) -> str:
        value = translations.get(text)
        return value if isinstance(value, str) and value else text

    return {
        "type": "string",
        "enum": ["mic", "footer"],
        "enumDescriptions": [tr(t) for t in ENUM_DESCRIPTIONS_EN],
        "default": "mic",
        "description": tr(DESCRIPTION_EN),
        "markdownDescription": tr(MARKDOWN_DESCRIPTION_EN),
        "tags": [SETTING_TAG],
    }


def _patch_manifest(pkg_path: str, desired: dict) -> str:
    """Вписывает свойство в contributes.configuration.properties.

    Возвращает status: "no_file" | "unreadable" | "bad_structure" |
    "already" | "patched" | "write_failed".
    """
    if not os.path.isfile(pkg_path):
        return "no_file"
    pkg = _read_json_retry(pkg_path)
    if not isinstance(pkg, dict):
        return "unreadable"
    try:
        with open(pkg_path, "r", encoding="utf-8") as f:
            raw = f.read()
    except OSError:
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
        # Манифест параллельно правит ещё и localize.py — писать можно
        # только атомарно, см. ext_patch.py.
        ext_patch.atomic_write(pkg_path, text)
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


def _read_json_retry(path: str, attempts: int = 3, delay: float = 0.15):
    """Читает JSON, переживая гонку с чужой неатомарной записью.

    localize.py переписывает package.json через open(w)+write, а хуки
    одного события идут параллельно — попасть в момент, когда файл
    пуст или обрезан, вполне реально. Один такой промах стоил нам
    маркера с пустой версией: он навсегда расходился с текущей,
    и предупреждение про Reload Window приходило каждую сессию.
    """
    for attempt in range(attempts):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            if attempt == attempts - 1:
                return None
            time.sleep(delay)
        except OSError:
            return None
    return None


def _read_ext_version(ext_dir: str) -> str:
    data = _read_json_retry(os.path.join(ext_dir, "package.json"))
    if not isinstance(data, dict):
        return ""
    return str(data.get("version", ""))


def _cache_is_stale() -> bool:
    """Есть ли кэш манифестов, отставший от нашего манифеста.

    Читаем как текст: структура кэша — внутреннее дело VSCode, а нам
    достаточно факта присутствия ключа и тега. Тег проверяется отдельно,
    потому что он появился позже самого пункта: без этой проверки кэш
    с уже знакомым ключом, но старым описанием, считался бы свежим.
    Если кэшей нет вообще (первый запуск, другой профиль) — считаем,
    что всё в порядке: VSCode построит кэш сам и прочитает актуальный
    манифест.
    """
    for pattern in CACHE_GLOBS:
        for path in glob.glob(pattern):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw = f.read()
            except OSError:
                continue
            if SETTING_KEY not in raw or SETTING_TAG not in raw:
                return True
    return False


def _invalidate_cache() -> bool:
    """Обновляет mtime extensions.json, чтобы VSCode пересобрал кэш
    манифестов при следующем старте окна. Содержимое не трогаем —
    файл принадлежит VSCode.
    """
    if not os.path.isfile(EXTENSIONS_INDEX):
        return False
    try:
        os.utime(EXTENSIONS_INDEX, None)
        return True
    except OSError:
        return False


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
    locale = _read_locale()
    desired = _desired_property(_load_translations(locale))
    desired_en = _desired_property({})
    messages: list[str] = []
    marker = _load_marker()
    marker_changed = False

    for ext_dir in glob.glob(EXT_GLOB):
        name = os.path.basename(ext_dir)
        pkg_path = os.path.join(ext_dir, "package.json")
        status = _patch_manifest(pkg_path, desired)
        # Тот же пункт — в бэкап, из которого localize.py восстанавливает
        # манифест. Без этого всё держалось бы на порядке хуков, а harness
        # запускает хуки одного события параллельно: localize.py успевает
        # откатить package.json уже ПОСЛЕ того, как мы его пропатчили,
        # и пункт пропадает (ровно это и произошло 2026-08-12).
        # В бэкапе — английский оригинал: localize.py переведёт его сам
        # по словарю, как и остальные строки настроек.
        _patch_manifest(pkg_path + ".original", desired_en)
        if status in ("patched", "already"):
            seen = marker.get(name)
            version = _read_ext_version(ext_dir)
            current = {"version": version, "property": desired}
            # Версию не удалось прочитать даже с ретраями — маркер не
            # трогаем: записанное «пусто» разошлось бы с реальной
            # версией и превратило разовое уведомление в постоянный шум.
            if not version:
                continue
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

    # Манифест может быть уже пропатчен, а Settings UI всё равно
    # показывать старый набор настроек — VSCode держит разобранные
    # манифесты в кэше и не перечитывает их, пока не изменится mtime
    # extensions.json. Проверяем кэш и при рассинхроне инвалидируем.
    # marker_changed ловит правку формулировок: ключ и тег в кэше на
    # месте, а тексты уже другие — сам кэш об этом не расскажет.
    if (_cache_is_stale() or marker_changed) and _invalidate_cache():
        messages.append(
            "[ext-settings WARNING] Кэш манифестов VSCode ещё не знает про "
            f"`{SETTING_KEY}` — Settings UI показывал бы старый список. "
            "Кэш инвалидирован (обновлён mtime extensions.json), нужен "
            "`Developer: Reload Window`, чтобы VSCode пересканировал расширения."
        )

    _emit_context(messages)
    return 0


if __name__ == "__main__":
    sys.exit(main())
