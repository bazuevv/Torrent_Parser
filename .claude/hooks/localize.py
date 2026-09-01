#!/usr/bin/env python3
"""Единая точка локализации расширения Claude Code.

Хук читает locale из `.claude/patches/claude-custom-config.toml`,
загружает `.claude/patches/locales/<locale>.json` и применяет переводы.

Структура файла переводов: два мета-раздела `static` и `runtime`.

- `static.settings`, `static.ui` — строки, заменяемые ПРЯМО В ФАЙЛЕ
  package.json расширения (через бэкап `.original`). Сюда попадают
  только те UI-ключи, которые ФИЗИЧЕСКИ присутствуют в package.json
  (например, `contributes.commands`, описания скиллов).
- `runtime.permissions`, `runtime.ui` — строки, заменяемые ВО ВРЕМЯ
  работы webview через JS-локализатор, инжектируемый в
  `webview/index.js`. UI-строки, не попадающие в package.json
  (динамические меню, тултипы, плейсхолдеры, mode-селектор), живут
  только здесь.

Дубликатов между static.ui и runtime.ui нет — каждая UI-строка
лежит ровно в одном разделе по факту присутствия в package.json.

Применяет переводы двумя способами:

1. **Статически** — патчит `package.json` расширения (settings + ui):
   описания настроек и slash-команд VSCode читает один раз при загрузке
   расширения, поэтому здесь нужен патч на диске.
2. **Динамически** — инжектит JS-локализатор в `webview/index.js`
   собственным bootstrap-блоком с маркером `/* claude-localizer */`.
   Он сам читает свой `LOCALE`/`TRANSLATIONS` из инлайн-литерала
   (без зависимости от bootstrap'а из patch-claude-webview.py),
   ставит MutationObserver на `document.body` и заменяет тексты
   permission-диалогов, кнопок, тултипов и атрибутов
   `title`/`aria-label`/`placeholder`. Зону чата
   (`[data-testid="assistant-message"]`, `[class*="messageContainer_"]`)
   не трогает.

При locale="en" или отсутствии файла переводов хук удаляет блок
локализатора из `webview/index.js` и восстанавливает английский
`package.json` из бэкапа, чтобы вернуться к английскому UI.

Переключение между локалями реализовано через `package.json.original`:
при первом запуске хук сохраняет английский оригинал рядом с самим
`package.json`. Перед применением каждой новой локали файл всегда
восстанавливается из бэкапа — это даёт корректные переходы en → ru,
ru → de, ru → en и т.д. Если расширение обновилось (отличается поле
`version`), бэкап автоматически пересоздаётся из новой английской
версии.
"""

import glob
import json
import os
import re
import sys
import time
import tomllib

# ext_patch лежит рядом; при запуске скриптом sys.path[0] — эта папка,
# но вставляем явно, чтобы импорт не зависел от способа запуска.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ext_patch  # noqa: E402

HOME = os.path.expanduser("~")
EXT_GLOB = os.path.join(HOME, ".vscode/extensions/anthropic.claude-code-*-linux-x64")

PROJECT_DIR = os.environ.get("CLAUDE_PROJECT_DIR")
if not PROJECT_DIR:
    PROJECT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

PATCHES_DIR = os.path.join(PROJECT_DIR, ".claude", "patches")
CONFIG_PATH = os.path.join(PATCHES_DIR, "claude-custom-config.toml")

LOC_MARKER_BEGIN = "/* claude-localizer */"
LOC_MARKER_END = "/* /claude-localizer */"

_LOC_BLOCK_RE = re.compile(
    re.escape(LOC_MARKER_BEGIN) + r".*?" + re.escape(LOC_MARKER_END),
    re.DOTALL,
)

# Самостоятельный bootstrap-блок локализатора. Подставляются:
#   __LOCALE_JSON__         — JSON-строка (например, "ru")
#   __TRANSLATIONS_JSON__   — JSON-объект с ключами permissions/ui
LOCALIZER_TEMPLATE = """\
/* claude-localizer */
;(function(){
  if (window.__claudeLocalizerInstalled) return;
  window.__claudeLocalizerInstalled = true;

  var LOCALE = __LOCALE_JSON__;
  var TRANSLATIONS = __TRANSLATIONS_JSON__;
  var permTranslations = TRANSLATIONS.permissions || {};
  var uiTranslations = TRANSLATIONS.ui || {};
  var allTranslations = {};
  var trSources = [permTranslations, uiTranslations];
  for (var ti = 0; ti < trSources.length; ti++) {
    var src = trSources[ti];
    for (var key in src) {
      if (src.hasOwnProperty(key)) allTranslations[key] = src[key];
    }
  }
  // Длинные ключи первыми: "Toggle fast mode (Opus 4.6 only)" не должен
  // съедаться коротким "Toggle fast mode".
  var translationKeys = Object.keys(allTranslations).sort(function(a,b){return b.length-a.length;});
  var hasTranslations = LOCALE !== 'en' && translationKeys.length > 0;
  if (!hasTranslations) return;

  var CHAT_SELECTORS = '[data-testid="assistant-message"], [class*="messageContainer_"]';

  function isInsideChat(node) {
    var el = node.nodeType === 1 ? node : node.parentElement;
    if (!el) return false;
    try { return !!el.closest(CHAT_SELECTORS); } catch (_) { return false; }
  }

  function localizeNode(node) {
    if (node.nodeType === 3) {
      if (isInsideChat(node)) return;
      var parentTag = node.parentElement && node.parentElement.tagName;
      if (parentTag === 'TEXTAREA' || parentTag === 'INPUT') return;
      var text = node.textContent;
      for (var ki = 0; ki < translationKeys.length; ki++) {
        var eng = translationKeys[ki];
        if (text.indexOf(eng) !== -1) {
          node.textContent = text.replace(eng, allTranslations[eng]);
          return;
        }
      }
    } else if (node.nodeType === 1) {
      if (isInsideChat(node)) return;
      var attrs = ['title', 'aria-label', 'placeholder'];
      for (var ai = 0; ai < attrs.length; ai++) {
        var attrVal = node.getAttribute && node.getAttribute(attrs[ai]);
        if (attrVal) {
          for (var ki2 = 0; ki2 < translationKeys.length; ki2++) {
            var engAttr = translationKeys[ki2];
            if (attrVal.indexOf(engAttr) !== -1) {
              node.setAttribute(attrs[ai], attrVal.replace(engAttr, allTranslations[engAttr]));
              break;
            }
          }
        }
      }
      if (node.childNodes) {
        for (var i = 0; i < node.childNodes.length; i++) {
          localizeNode(node.childNodes[i]);
        }
      }
    }
  }

  var lastLocalizeScan = 0;
  function localizeDOM() {
    var now = Date.now();
    if (now - lastLocalizeScan < 500) return;
    lastLocalizeScan = now;
    var permContainers = document.querySelectorAll('[class*="permissionRequest"]');
    for (var pi = 0; pi < permContainers.length; pi++) localizeNode(permContainers[pi]);
    var inputs = document.querySelectorAll('input[placeholder], textarea[placeholder], [title], [aria-label]');
    for (var ii = 0; ii < inputs.length; ii++) {
      var el = inputs[ii];
      if (el.placeholder) {
        for (var eng2 in allTranslations) {
          if (el.placeholder.indexOf(eng2) !== -1) {
            el.placeholder = el.placeholder.replace(eng2, allTranslations[eng2]);
          }
        }
      }
      if (!isInsideChat(el) && el.title) {
        for (var eng3 in allTranslations) {
          if (el.title.indexOf(eng3) !== -1) {
            el.title = el.title.replace(eng3, allTranslations[eng3]);
          }
        }
      }
    }
    var buttons = document.querySelectorAll('button, [role="button"], [class*="permission"] span');
    for (var bi = 0; bi < buttons.length; bi++) localizeNode(buttons[bi]);
  }

  function start() {
    localizeDOM();
    new MutationObserver(function(mutations){
      localizeDOM();
      for (var mi = 0; mi < mutations.length; mi++) {
        var mut = mutations[mi];
        var added = mut.addedNodes;
        for (var ai = 0; ai < added.length; ai++) localizeNode(added[ai]);
        if (mut.type === 'characterData' && mut.target) localizeNode(mut.target);
      }
    }).observe(document.body, { childList: true, subtree: true, characterData: true });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
/* /claude-localizer */
"""


def _read_locale() -> str:
    if not os.path.isfile(CONFIG_PATH):
        return "en"
    try:
        with open(CONFIG_PATH, "rb") as f:
            cfg = tomllib.load(f)
        return cfg.get("locale", "en")
    except (OSError, tomllib.TOMLDecodeError):
        return "en"


def _strip_meta(obj):
    """Рекурсивно удаляет ключи, начинающиеся с `_` (документация).
    Применяется к любой вложенности — `_meta`, `_description` могут
    лежать на верхнем уровне, внутри `static`/`runtime`, и внутри
    конкретных подразделов вроде `static.settings`.
    """
    if isinstance(obj, dict):
        return {k: _strip_meta(v) for k, v in obj.items() if not k.startswith("_")}
    if isinstance(obj, list):
        return [_strip_meta(x) for x in obj]
    return obj


def _load_translations(locale: str) -> dict:
    """Загружает словарь переводов для locale.

    Возвращает структуру вида:
        {"static": {"settings": {...}, "ui": {...}},
         "runtime": {"permissions": {...}, "ui": {...}}}

    Все meta-ключи (`_meta`, `_description`) рекурсивно удаляются —
    они существуют только для документации внутри JSON.

    При locale="en" или отсутствии файла — пустой dict.
    """
    if locale == "en":
        return {}
    path = os.path.join(PATCHES_DIR, "locales", f"{locale}.json")
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return _strip_meta(data)


def _static_keys(translations: dict) -> dict:
    """Объединённый словарь {английский → перевод} для static-замен
    (settings + ui). Используется для патча package.json.
    """
    result: dict = {}
    static = translations.get("static", {})
    if not isinstance(static, dict):
        return result
    for section in ["settings", "ui"]:
        items = static.get(section)
        if isinstance(items, dict):
            result.update(items)
    return result


def _flatten_strings(obj: dict) -> dict:
    """Рекурсивно сплющивает вложенный словарь до пар {str: str}.
    Подразделы (вложенные dict) обходятся вглубь и сливаются в одну плоскость;
    `_`-префиксные ключи (мета/документация) пропускаются.

    Используется для runtime-блоков, где JSON-вложенность нужна только для
    группировки/читаемости (например, runtime.ui.menu.Add), а JS-локализатор
    ожидает плоский словарь английских→локализованных строк.
    """
    result: dict = {}
    for key, value in obj.items():
        if key.startswith("_"):
            continue
        if isinstance(value, dict):
            result.update(_flatten_strings(value))
        elif isinstance(value, str):
            result[key] = value
    return result


def _runtime_blocks(translations: dict) -> dict:
    """Возвращает словарь {permissions: {...}, ui: {...}} для подстановки
    в JS-локализатор. Каждый блок сплющивается до плоского {eng: ru},
    потому что JS объединяет блоки сам и сортирует ключи по длине.
    """
    out: dict = {}
    runtime = translations.get("runtime", {})
    if not isinstance(runtime, dict):
        return out
    for section in ["permissions", "ui"]:
        items = runtime.get(section)
        if isinstance(items, dict):
            out[section] = _flatten_strings(items)
    return out


def _read_version_from_string(s: str) -> str | None:
    """Парсит JSON и возвращает поле `version` (или None)."""
    try:
        data = json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return None
    return data.get("version") if isinstance(data, dict) else None


def _quoted(s: str) -> str:
    """Обрамляет строку JSON-кавычками для безопасного поиска
    внутри package.json. Это защита от ложных совпадений с
    подстроками внутри идентификаторов (например, "Permissions"
    в составе "allowDangerouslySkipPermissions" — без кавычек
    замена сломала бы конфигурацию).
    """
    return '"' + s + '"'


def _is_original(pkg_content: str, translations: dict, min_hits: int = 3) -> bool:
    """Эвристика: считаем package.json английским оригиналом, если в нём
    найдено хотя бы `min_hits` английских ключей из static-словаря
    в виде обрамлённых кавычками строковых литералов.
    """
    keys = list(_static_keys(translations).keys())
    if not keys:
        return True
    found = 0
    for k in keys:
        if _quoted(k) in pkg_content:
            found += 1
            if found >= min_hits:
                return True
    return False


def _restore_to_english(content: str, translations: dict) -> str:
    """Обратная замена локализованных строк на английские.
    Используется один раз — при первом запуске на уже пропатченном
    package.json, чтобы воссоздать оригинал для бэкапа.
    """
    for eng, loc in _static_keys(translations).items():
        if loc:
            loc_q = _quoted(loc)
            if loc_q in content:
                content = content.replace(loc_q, _quoted(eng))
    return content


def _ensure_original_backup(pkg_path: str, translations: dict) -> str | None:
    """Гарантирует наличие `package.json.original` рядом с package.json.

    - Если бэкапа нет и текущий файл — английский оригинал → сохраняет.
    - Если бэкапа нет и текущий уже пропатчен → пробует обратную замену
      по словарю текущей локали, сохраняет восстановленный английский.
    - Если бэкап есть и `version` в нём отличается от текущей → расширение
      обновилось: пересоздаёт бэкап из текущего, если тот английский.

    Возвращает путь к бэкапу или None, если бэкап создать не удалось.
    """
    original_path = pkg_path + ".original"

    try:
        with open(pkg_path, "r", encoding="utf-8") as f:
            cur_content = f.read()
    except OSError:
        return None

    if os.path.isfile(original_path):
        try:
            with open(original_path, "r", encoding="utf-8") as f:
                orig_content = f.read()
        except OSError:
            return original_path
        cur_ver = _read_version_from_string(cur_content)
        orig_ver = _read_version_from_string(orig_content)
        if cur_ver and orig_ver and cur_ver != orig_ver:
            if _is_original(cur_content, translations):
                try:
                    ext_patch.atomic_write(original_path, cur_content)
                except OSError:
                    pass
            else:
                sys.stderr.write(
                    f"localize.py: новая версия package.json {cur_ver} уже "
                    "локализована — старый бэкап оставлен; чтобы переключение "
                    "языков заработало, удалите package.json.original и "
                    "переустановите расширение.\n"
                )
        return original_path

    if _is_original(cur_content, translations):
        try:
            ext_patch.atomic_write(original_path, cur_content)
        except OSError:
            return None
        return original_path

    restored = _restore_to_english(cur_content, translations)
    if _is_original(restored, translations):
        try:
            ext_patch.atomic_write(original_path, restored)
        except OSError:
            return None
        return original_path

    sys.stderr.write(
        "localize.py: package.json уже локализован, и обратная замена по "
        "текущему словарю не дала результата — переустановите расширение "
        "Claude Code, чтобы получить английский оригинал, после чего бэкап "
        "создастся автоматически.\n"
    )
    return None


def _restore_from_backup(pkg_path: str, original_path: str) -> bool:
    """Перезаписывает package.json содержимым бэкапа."""
    try:
        with open(original_path, "r", encoding="utf-8") as src:
            content = src.read()
        ext_patch.atomic_write(pkg_path, content)
    except OSError:
        return False
    return True


def _apply_package_json(ext_dir: str, translations: dict) -> bool:
    """Применяет переводы к package.json через бэкап.

    1. Гарантирует наличие package.json.original (создаёт при первом
       запуске).
    2. Восстанавливает package.json из бэкапа (отбрасывает прошлую
       локализацию — это даёт корректное переключение языков).
    3. Применяет блоки settings + ui новой локали.
    """
    pkg_path = os.path.join(ext_dir, "package.json")
    if not os.path.isfile(pkg_path):
        return False

    original_path = _ensure_original_backup(pkg_path, translations)
    if original_path is None:
        return False
    if not _restore_from_backup(pkg_path, original_path):
        return False

    try:
        with open(pkg_path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return False

    all_maps = _static_keys(translations)

    changed = False
    for eng, loc in all_maps.items():
        eng_q = _quoted(eng)
        loc_q = _quoted(loc)
        if eng_q in content and loc_q not in content:
            content = content.replace(eng_q, loc_q)
            changed = True

    if changed:
        try:
            ext_patch.atomic_write(pkg_path, content)
        except OSError:
            return False
    return changed


def _restore_package_json_to_original(ext_dir: str) -> bool:
    """Возвращает package.json к английскому оригиналу из бэкапа.
    Используется при locale=en.
    """
    pkg_path = os.path.join(ext_dir, "package.json")
    if not os.path.isfile(pkg_path):
        return False
    original_path = pkg_path + ".original"
    if not os.path.isfile(original_path):
        return False
    return _restore_from_backup(pkg_path, original_path)


def _build_localizer_bootstrap(locale: str, translations: dict) -> str:
    """Подставляет locale + runtime.permissions/ui в JS-шаблон локализатора."""
    webview_translations = _runtime_blocks(translations)
    body = LOCALIZER_TEMPLATE.replace(
        "__LOCALE_JSON__", json.dumps(locale, ensure_ascii=False)
    )
    body = body.replace(
        "__TRANSLATIONS_JSON__",
        json.dumps(webview_translations, ensure_ascii=False),
    )
    return body


def _inject_webview_localizer(webview_dir: str, bootstrap: str) -> bool:
    """Вставляет/обновляет блок локализатора в webview/index.js."""
    index_js = os.path.join(webview_dir, "index.js")
    if not os.path.isfile(index_js):
        return False
    try:
        with open(index_js, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return False

    desired = bootstrap.strip()
    if LOC_MARKER_BEGIN in content:
        existing = _LOC_BLOCK_RE.search(content)
        if existing and existing.group(0).strip() == desired:
            return False
        new_content = _LOC_BLOCK_RE.sub(lambda _m: desired, content, count=1)
    else:
        new_content = content.rstrip() + "\n\n" + bootstrap

    if new_content == content:
        return False
    try:
        ext_patch.atomic_write(index_js, new_content)
    except OSError:
        return False
    return True


def _remove_webview_localizer(webview_dir: str) -> bool:
    """Удаляет блок локализатора (для locale=en или отсутствия переводов)."""
    index_js = os.path.join(webview_dir, "index.js")
    if not os.path.isfile(index_js):
        return False
    try:
        with open(index_js, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return False
    if LOC_MARKER_BEGIN not in content:
        return False
    stripped = _LOC_BLOCK_RE.sub("", content, count=1)
    new_content = stripped.rstrip() + "\n"
    if new_content == content:
        return False
    try:
        ext_patch.atomic_write(index_js, new_content)
    except OSError:
        return False
    return True


# ============================================================================
# Drift-анализатор: сравнивает snapshot DOM меню `/` (его шлёт webview
# JS-collector в claude-custom.js на каждое открытие) со словарём,
# и эмитит warning через additionalContext, если нашёл расхождения.
# ============================================================================

LOCALE_DRIFT_FILE = os.path.join(
    PROJECT_DIR, ".claude", "hooks-runtime", "locales-drift-pending.json"
)
# Файл с классифицированным отчётом (после _analyze_drift). В отличие от
# pending-файла, который содержит сырой snapshot DOM от webview JS,
# report-файл содержит уже разобранные несовпадения с указанием типа
# (new_command / command_drift / item_drift) и текущим ru-переводом
# для команд с дрейфом. Перезаписывается на каждом SessionStart, всегда
# (даже если total=0) — это даёт видимый в файле timestamp последней
# проверки и пустые списки как «всё ок».
LOCALE_DRIFT_REPORT_FILE = os.path.join(
    PROJECT_DIR, ".claude", "hooks-runtime", "locales-drift-report.json"
)


def _atomic_write_json(path: str, payload: dict) -> None:
    """Атомарная запись JSON через tmp-файл + os.replace.
    Защищает от частично записанного отчёта, если процесс упадёт во время write.
    """
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _read_hook_event_name() -> str:
    """Читает hook_event_name из stdin-JSON, который Claude Code передаёт хуку.
    При запуске из терминала / без stdin-JSON — fallback 'SessionStart'
    (хук зарегистрирован только на это событие).
    """
    fallback = "SessionStart"
    if sys.stdin.isatty():
        return fallback
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return fallback
        data = json.loads(raw)
        if isinstance(data, dict):
            return data.get("hook_event_name") or fallback
    except (OSError, json.JSONDecodeError):
        pass
    return fallback


def _load_pending_drift() -> tuple[list[dict] | None, list[str], str | None]:
    """Загружает items + texts из locales-drift-pending.json (snapshot
    меню `/`, отправленный webview JS-collector'ом).

    Возвращает кортеж (items, texts, collected_at):
      - items — структурированные пункты меню {section, label, title};
      - texts — плоский список ВСЕХ текстов в области меню (text-nodes
        + атрибуты title/aria-label/placeholder), для широкого
        обнаружения непереведённых строк;
      - collected_at используется при сохранении отчёта.

    (None, [], None) если файла нет/невалид.
    """
    if not os.path.isfile(LOCALE_DRIFT_FILE):
        return None, [], None
    try:
        with open(LOCALE_DRIFT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None, [], None
    if not isinstance(data, dict):
        return None, [], None
    items = data.get("items")
    texts = data.get("texts") or []
    if not isinstance(texts, list):
        texts = []
    collected_at = data.get("collected_at")
    if not isinstance(items, list):
        return None, [], None
    return (
        items,
        [t for t in texts if isinstance(t, str) and t.strip()],
        collected_at if isinstance(collected_at, str) else None,
    )


def _slash_command_translations(translations: dict) -> dict:
    """Возвращает {имя_команды: set(русских_переводов)} из подраздела
    runtime.ui.menu.command.Слеш-команды. Используется для классификации
    дрейфа: title в DOM должен совпадать с одним из русских переводов
    команды; иначе — дрейф ключа или новая команда.
    """
    runtime_ui = translations.get("runtime", {}).get("ui", {})
    if not isinstance(runtime_ui, dict):
        return {}
    menu = runtime_ui.get("menu", {})
    if not isinstance(menu, dict):
        return {}
    cmd = menu.get("command", {})
    if not isinstance(cmd, dict):
        return {}
    slash_section = cmd.get("Слеш-команды", {})
    if not isinstance(slash_section, dict):
        return {}

    out: dict = {}
    for cmd_name, entry in slash_section.items():
        if cmd_name.startswith("_"):
            continue
        if not isinstance(entry, dict):
            continue
        # entry: {eng_tooltip: ru_tooltip}
        ru_set = set()
        for k, v in entry.items():
            if k.startswith("_"):
                continue
            if isinstance(v, str):
                ru_set.add(v)
        out[cmd_name] = ru_set
    return out


# Эвристика «текст выглядит английским»: содержит латиницу, не содержит
# кириллицы. Длина > 1, иначе односимвольные элементы вроде «1» или
# знаков попадали бы как кандидаты на перевод.
_LATIN_RE = re.compile(r"[a-zA-Z]")
_CYRILLIC_RE = re.compile(r"[Ѐ-ӿ]")


def _looks_english(text: str) -> bool:
    if not text:
        return False
    text = text.strip()
    if len(text) < 2:
        return False
    if _CYRILLIC_RE.search(text):
        return False
    return bool(_LATIN_RE.search(text))


# Эвристические фильтры для широкого scan'а text-nodes меню — какие
# строки НЕ имеет смысла предлагать на перевод (имена slash-команд,
# числа, проценты, технические токены и т.д.).
_SKIP_TEXT_RE = re.compile(
    r"^("
    r"\s*$"                                 # пустая
    r"|/[a-zA-Z][a-zA-Z0-9_-]*"             # /init, /loop, /команды
    r"|@[\w-]+"                             # @mentions
    r"|#[\w-]+"                             # #tags
    r"|[\d.,:%+\-/() ]+"                    # числа/проценты/время
    r"|\d+%[^a-zA-Z]*"                      # «48%» с хвостом
    r"|[A-Z][a-zA-Z]*\+[A-Za-z0-9+]+"       # «Ctrl+D», «Cmd+Enter»
    r")$"
)


def _is_translatable_candidate(text: str, ru_values: set, flat_keys: set) -> bool:
    """Текст — кандидат на «английский без перевода» если:
      - выглядит английским (содержит латиницу, нет кириллицы);
      - длина ≥ 4 символов (короче — обычно мусор: «OK», «1»);
      - не похож на технический токен (см. _SKIP_TEXT_RE);
      - не находится среди русских значений словаря (значит уже
        переведён где-то ещё);
      - не находится среди английских ключей словаря (значит он
        уже зарегистрирован — отсутствие перевода тут было бы
        отдельной категорией «not_applied», но это редко).
    """
    if len(text) < 4:
        return False
    if not _looks_english(text):
        return False
    if _SKIP_TEXT_RE.match(text):
        return False
    if text in ru_values:
        return False
    if text in flat_keys:
        return False
    return True


def _analyze_drift(
    translations: dict,
    pending_items: list[dict],
    pending_texts: list[str],
) -> dict:
    """Классифицирует пары из DOM-snapshot'а меню `/` по 6 типам:

      - new_command          — label начинается с `/`, имя команды
                                отсутствует в подразделе
                                runtime.ui.menu.command.Слеш-команды.
      - command_drift        — label начинается с `/`, команда в словаре
                                есть, но title не совпадает ни с одним
                                русским переводом её ключей (устаревший
                                eng-ключ или Anthropic переписал текст).
      - item_drift           — label без `/` (пункт меню), title не
                                находится среди русских значений
                                плоского словаря — наш перевод не сработал.
      - untranslated_section — заголовок секции выглядит английским и
                                нет среди русских значений словаря.
                                Значит мы либо не переводили этот заголовок,
                                либо JS-локализатор его не нашёл в DOM.
      - untranslated_label   — label пункта (без `/`-prefix) выглядит
                                английским и нет среди русских значений
                                словаря. То же — пропавший перевод
                                или непокрытый текст.

    Дубликаты по label игнорируются: snapshot может содержать
    «фильтрованную» версию меню (пункты повторяются между snapshot'ами).
    """
    runtime_ui = translations.get("runtime", {}).get("ui", {})
    flat = _flatten_strings(runtime_ui) if isinstance(runtime_ui, dict) else {}
    ru_values = set(flat.values())
    flat_keys = set(flat.keys())
    slash_dict = _slash_command_translations(translations)

    report = {
        "new_command": [],
        "command_drift": [],
        "item_drift": [],
        "untranslated_section": [],
        "untranslated_label": [],
        "untranslated_text": [],
    }
    seen_labels = set()
    seen_sections = set()  # дедуп заголовков секций (повторяются на каждый item)

    for item in pending_items:
        if not isinstance(item, dict):
            continue
        label = (item.get("label") or "").strip()
        title = (item.get("title") or "").strip()
        section = (item.get("section") or "").strip() or None

        # Untranslated секции: дедупим, чтобы 20 commandItem'ов из одной
        # секции не плодили 20 одинаковых записей в отчёте.
        if section and section not in seen_sections:
            seen_sections.add(section)
            if _looks_english(section) and section not in ru_values:
                report["untranslated_section"].append({"section": section})

        if not label or not title:
            continue
        if label in seen_labels:
            continue
        seen_labels.add(label)

        # Untranslated label (для пунктов меню, не команд): label сам
        # английский. Категория параллельна item_drift (которая смотрит
        # на title); фиксирует случаи, когда label не локализован.
        if (
            not label.startswith("/")
            and _looks_english(label)
            and label not in ru_values
        ):
            report["untranslated_label"].append({
                "section": section, "label": label, "title": title,
            })

        if label.startswith("/"):
            if label not in slash_dict:
                report["new_command"].append({
                    "section": section, "label": label, "title": title,
                })
            elif title not in slash_dict[label]:
                report["command_drift"].append({
                    "section": section, "label": label, "title": title,
                    "expected_translations": sorted(slash_dict[label]),
                })
        else:
            if title not in ru_values:
                report["item_drift"].append({
                    "section": section, "label": label, "title": title,
                })

    # Широкий scan: пробегаем по всем text-node'ам и атрибутам, что
    # webview JS-collector прислал в pending_texts. Каждая строка
    # проверяется эвристикой _is_translatable_candidate; всё, что
    # выглядит английским, не покрыто словарём и не похоже на
    # тех. токен — попадает в untranslated_text.
    #
    # Дедуп между категориями: если строка уже захвачена как title в
    # new_command/command_drift/item_drift — не дублируем её в
    # untranslated_text. Иначе один и тот же тултип слеш-команды
    # появлялся бы И как command_drift (по title-атрибуту), И как
    # untranslated_text (по text-node того же тултипа).
    already_captured = set()
    for cat in ("new_command", "command_drift", "item_drift"):
        for it in report[cat]:
            t = (it.get("title") or "").strip()
            if t:
                already_captured.add(t)

    def _is_dup_of_captured(text: str) -> bool:
        """Точное совпадение ИЛИ префикс/суффикс при длине ≥ 100.
        Длинные descriptions Anthropic-команд могут быть обрезаны при
        передаче по сети (collector ограничивает text-node'ы), а
        title-атрибут пункта меню — нет. Поэтому при длине ≥100
        достаточно сравнить начала строк, чтобы поймать дубль.
        """
        if text in already_captured:
            return True
        if len(text) < 100:
            return False
        for cap in already_captured:
            if cap.startswith(text) or text.startswith(cap):
                return True
        return False

    seen_texts = set()
    for raw in pending_texts:
        text = (raw or "").strip()
        if not text or text in seen_texts:
            continue
        seen_texts.add(text)
        if _is_dup_of_captured(text):
            continue
        if _is_translatable_candidate(text, ru_values, flat_keys):
            report["untranslated_text"].append({"text": text})

    return report


def _save_drift_report(report: dict, pending_collected_at: str | None) -> None:
    """Сохраняет классифицированный отчёт в LOCALE_DRIFT_REPORT_FILE.
    Записывается ВСЕГДА (даже при total=0) — даёт видимый timestamp
    последней проверки и пустые списки как «всё ок».

    Поле `pending_collected_at` берётся из исходного pending-файла —
    помогает понять, насколько свежий был snapshot DOM (если меню
    давно не открывали, pending устарел и report тоже).
    """
    nc = report.get("new_command", [])
    cd = report.get("command_drift", [])
    idrft = report.get("item_drift", [])
    us = report.get("untranslated_section", [])
    ul = report.get("untranslated_label", [])
    ut = report.get("untranslated_text", [])
    payload = {
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z") or
                      time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pending_collected_at": pending_collected_at,
        "summary": {
            "new_command": len(nc),
            "command_drift": len(cd),
            "item_drift": len(idrft),
            "untranslated_section": len(us),
            "untranslated_label": len(ul),
            "untranslated_text": len(ut),
            "total": len(nc) + len(cd) + len(idrft) + len(us) + len(ul) + len(ut),
        },
        "new_command": nc,
        "command_drift": cd,
        "item_drift": idrft,
        "untranslated_section": us,
        "untranslated_label": ul,
        "untranslated_text": ut,
    }
    try:
        _atomic_write_json(LOCALE_DRIFT_REPORT_FILE, payload)
    except OSError:
        pass


def _emit_drift_warning(report: dict, event_name: str) -> None:
    """Эмитит блок через hookSpecificOutput.additionalContext с маркером
    [locale-drift WARNING], который модель обязана показать пользователю
    (по аналогии с [claude-custom-config WARNING]).
    """
    nc = report.get("new_command", [])
    cd = report.get("command_drift", [])
    idrft = report.get("item_drift", [])
    us = report.get("untranslated_section", [])
    ul = report.get("untranslated_label", [])
    ut = report.get("untranslated_text", [])
    total = len(nc) + len(cd) + len(idrft) + len(us) + len(ul) + len(ut)
    if total == 0:
        return

    lines = [
        f"[locale-drift WARNING] Обнаружено несовпадений локализации меню `/`: {total}. "
        "Snapshot DOM меню (от webview JS-collector'а) лежит в "
        ".claude/hooks-runtime/locales-drift-pending.json, классифицированный отчёт — в "
        ".claude/hooks-runtime/locales-drift-report.json. Сообщи пользователю:",
        "",
    ]
    if nc:
        lines.append(f"## Новые команды без перевода ({len(nc)})")
        for it in nc:
            lines.append(
                f"- `{it['label']}` (секция «{it.get('section') or '-'}») → "
                f"english: «{it['title']}»"
            )
        lines.append("")
    if cd:
        lines.append(f"## Дрейф ключей слеш-команд ({len(cd)})")
        for it in cd:
            lines.append(f"- `{it['label']}`: english в DOM «{it['title']}»")
            for ru in it.get("expected_translations", []):
                lines.append(f"    словарь содержит ru-перевод «{ru}»")
        lines.append("")
    if idrft:
        lines.append(f"## Дрейф пунктов меню ({len(idrft)})")
        for it in idrft:
            lines.append(
                f"- секция «{it.get('section') or '-'}», label «{it['label']}», "
                f"english title «{it['title']}»"
            )
        lines.append("")
    if us:
        lines.append(f"## Английские заголовки секций без перевода ({len(us)})")
        for it in us:
            lines.append(f"- «{it['section']}»")
        lines.append("")
    if ul:
        lines.append(f"## Английские лейблы пунктов без перевода ({len(ul)})")
        for it in ul:
            lines.append(
                f"- секция «{it.get('section') or '-'}», label «{it['label']}»"
            )
        lines.append("")
    if ut:
        lines.append(
            f"## Прочие английские тексты в области меню ({len(ut)})"
        )
        for it in ut:
            lines.append(f"- «{it['text']}»")
        lines.append("")

    output = {
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "additionalContext": "\n".join(lines),
        }
    }
    try:
        print(json.dumps(output, ensure_ascii=False))
    except OSError:
        pass


def main() -> int:
    locale = _read_locale()
    translations = _load_translations(locale)
    bootstrap = (
        _build_localizer_bootstrap(locale, translations) if translations else ""
    )

    for ext_dir in glob.glob(EXT_GLOB):
        if translations:
            _apply_package_json(ext_dir, translations)
        else:
            _restore_package_json_to_original(ext_dir)

        webview_dir = os.path.join(ext_dir, "webview")
        if not os.path.isdir(webview_dir):
            continue
        if bootstrap:
            _inject_webview_localizer(webview_dir, bootstrap)
        else:
            _remove_webview_localizer(webview_dir)

    # Drift-анализатор: читает pending-snapshot меню /, классифицирует
    # пары, СОХРАНЯЕТ ОТЧЁТ в locales-drift-report.json (атомарно), затем
    # эмитит warning через additionalContext, если нашёл несовпадения.
    # Отчёт сохраняется всегда (даже при total=0) — видимая отметка
    # последней проверки + пустые списки как «всё ок».
    if translations:
        pending_items, pending_texts, pending_collected_at = _load_pending_drift()
        if pending_items is not None:
            report = _analyze_drift(translations, pending_items, pending_texts)
            _save_drift_report(report, pending_collected_at)
            event_name = _read_hook_event_name()
            _emit_drift_warning(report, event_name)

    return 0


if __name__ == "__main__":
    sys.exit(main())
