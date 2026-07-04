#!/usr/bin/env python3
"""
Утилитарный скрипт (НЕ hook!). Читает .claude/hooks-runtime/warn.log целиком,
выводит содержимое в stdout и очищает файл.

Используется моделью, когда нужно проверить факт срабатывания
warn-destructive-outside.py (т.е. появлялось ли у пользователя окно
подтверждения). После прочтения модель получает всю накопленную
историю, а файл возвращается в пустое состояние — чтобы не расти
бесконечно и не захламлять вывод при следующих проверках.

Атомарность: файл сначала переименовывается (rename), потом читается
и удаляется. Это защищает от гонки с warn-destructive-outside.py,
который может писать в warn.log в любой момент. Параллельная запись
пройдёт уже в новый, свежесозданный warn.log (если будет) или
в переименованный файл (если handle был открыт до rename) — ни одна
запись не теряется при обычных сценариях.
"""
import os
import pathlib
import sys
import time


def main() -> int:
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "")
    if not project_dir:
        script_path = pathlib.Path(__file__).resolve()
        project_dir = str(script_path.parent.parent.parent)

    log = pathlib.Path(project_dir) / ".claude" / "hooks-runtime" / "warn.log"
    if not log.exists():
        return 0

    consumed = log.parent / f"{log.name}.consumed.{time.time_ns()}"
    try:
        log.rename(consumed)
    except FileNotFoundError:
        return 0
    except Exception as e:
        print(f"# consume-warn-log: rename failed: {e}", file=sys.stderr)
        return 0

    try:
        sys.stdout.write(consumed.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"# consume-warn-log: read failed: {e}", file=sys.stderr)
    finally:
        try:
            consumed.unlink()
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
