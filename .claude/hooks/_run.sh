#!/usr/bin/env bash
# Cross-platform Python hook dispatcher.
#
# Usage: bash .claude/hooks/_run.sh <hook-script.py> [args...]
#
# Зачем нужен:
#   - На Linux канонический Python — `python3`.
#   - На Windows `python3.exe` обычно битый Microsoft Store-стаб
#     (exit 49 без сообщения), а реальный исполняемый файл называется
#     `python.exe` или вызывается через лаунчер `py -3`.
#   - Один settings.json должен работать на обеих платформах.
#
# Скрипт перебирает имена python3 → python → py в указанном порядке
# и выбирает ПЕРВЫЙ, который действительно запускает Python 3 (а не
# Windows-стаб). Проверка стаба: пробный `-c 'import sys; sys.exit(0)'`
# должен завершиться кодом 0.
#
# Пути:
#   - Скрипт вызывается с cwd = project root (Claude Code гарантирует
#     это для хуков), поэтому относительный путь к хук-скрипту работает.
#   - Если по какой-то причине cwd сбросился, поднимаемся к каталогу
#     скрипта через $0 и оттуда находим хук.

# НЕ ставим `set -e`: ниже используем return-коды для перебора кандидатов,
# и нам нужно дойти до конца списка даже если первые два упали.

SCRIPT_NAME="${1:-}"
if [ -z "$SCRIPT_NAME" ]; then
    echo "Usage: $0 <hook-script.py> [args...]" >&2
    exit 1
fi
shift

HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
HOOK_PATH="$HOOK_DIR/$SCRIPT_NAME"

# Принудительно ставим UTF-8 для всех Python I/O. На Windows Python по
# умолчанию использует системную ANSI codepage (cp1251 для русской
# локали), из-за чего json.load(sys.stdin) падает на кириллице, а
# print() с UTF-8 портит вывод.
# PYTHONUTF8=1 (Python 3.7+) — глобальный UTF-8 mode, покрывает stdin,
# stdout, stderr, fs paths.
# PYTHONIOENCODING — fallback на случай если UTF-8 mode выключен.
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

if [ ! -f "$HOOK_PATH" ]; then
    echo "Hook script not found: $HOOK_PATH" >&2
    exit 1
fi

# Пробуем интерпретатор по имени; если он есть и не битый — exec.
# `exec` заменяет процесс bash на python, stdin/stdout/stderr и код
# возврата пробрасываются прозрачно.
try_python() {
    local py="$1"
    shift  # убрать имя интерпретатора, остаются хук-аргументы
    if ! command -v "$py" >/dev/null 2>&1; then
        return 1
    fi
    # Битый Windows Store-стаб выходит с кодом 49 без вывода версии.
    if ! "$py" -c 'import sys; sys.exit(0)' >/dev/null 2>&1; then
        return 1
    fi
    exec "$py" "$HOOK_PATH" "$@"
}

try_python python3 "$@"
try_python python "$@"

# `py` — Python launcher на Windows; флаг `-3` выбирает Python 3.x.
if command -v py >/dev/null 2>&1; then
    if py -3 -c 'import sys; sys.exit(0)' >/dev/null 2>&1; then
        exec py -3 "$HOOK_PATH" "$@"
    fi
fi

echo "No working Python 3 interpreter found (tried: python3, python, py -3)" >&2
exit 1
