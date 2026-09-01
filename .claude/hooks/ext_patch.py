#!/usr/bin/env python3
"""Общие примитивы для хуков, которые правят файлы расширения.

Файлы расширения Claude Code (`webview/index.js`, `extension.js`,
`package.json`) правят сразу несколько наших хуков, и делают это
одновременно. Хуки одного события harness запускает параллельно:
на SessionStart стартуют `patch-claude-webview.py`, `localize.py`,
`patch-extension-csp.py` и `patch-extension-settings.py` разом, в
каждом открытом окне VSCode свой комплект, а `http-server.py` дёргает
патчер ещё и на каждую смену TOML-конфига. В журнале за один рабочий
день 91 случай, когда два и более патчера стартовали в пределах 0.3 с;
рекорд — восемь одновременно.

Отсюда два правила, ради которых существует этот модуль:

1. **Писать только атомарно** (`atomic_write`). `open(path, "w")`
   усекает файл и наполняет его несколькими системными вызовами —
   два писателя оставляют на диске смесь двух версий.
2. Один писатель на файл в каждый момент времени (появится здесь же
   отдельной функцией-локом).

Прецедент, ради которого это написано (2026-08-31). Смесь двух версий
`index.js` особенно коварна: у всех писателей совпадает
четырёхмегабайтный префикс с кодом приложения, а расходятся только
хвосты с блоками-маркерами. Файл сохраняет правдоподобный размер, код
приложения цел, но маркеры разъезжаются (наблюдали «один BEGIN, два
END»). Синтаксическая ошибка в любом месте убивает бандл целиком, и
ВСЕ webview расширения — центральные вкладки, правая панель, список
сессий — открываются пустыми, хотя extension host жив и отвечает.

Само по себе это не лечится: `_upsert_marker_block` переписывает лишь
участок между первым BEGIN и первым END, поэтому мусор за его
пределами переживает любой следующий запуск хука. Ни откат
репозитория, ни отключение хуков не помогают — порча живёт в файле
расширения. Тогда помогла только полная перезапись `index.js` хуками
другого проекта.
"""

import contextlib
import errno
import fcntl
import os
import shutil
import tempfile
import time

# Лок общий для ВСЕЙ машины, а не для проекта. Файлы расширения одни на
# все окна и все проекты: вчерашняя починка пришла ровно оттуда — хуки
# соседнего проекта переписали index.js. Проектный лок такого писателя
# не остановил бы, поэтому файл лежит в пользовательском кэше, а не
# в .claude/hooks-runtime.
LOCK_PATH = os.path.join(
    os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"),
    "claude-code-ext-patch.lock",
)

# Сколько ждём чужую правку. Одна правка — это чтение и запись
# пятимегабайтного файла, то есть доли секунды; двадцати секунд хватает
# и на восемь одновременных писателей с запасом.
LOCK_TIMEOUT_SEC = 20.0


def atomic_write(path: str, content: str) -> None:
    """Пишет файл через временный рядом + `os.replace`.

    `os.replace` подменяет файл одной операцией ядра: читатель видит
    либо старое содержимое целиком, либо новое, и никогда — половину.
    Временный файл обязан лежать в той же директории: переименование
    атомарно только внутри одной файловой системы.
    """
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".claude-tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        # mkstemp создаёт файл с правами 0600. Без переноса прав
        # исходного файла расширение осталось бы нечитаемым для всех,
        # кроме владельца, — VSCode запускают и от другого пользователя.
        if os.path.exists(path):
            shutil.copymode(path, tmp)
        os.replace(tmp, path)
    except BaseException:
        # Не оставляем мусор в каталоге расширения, если запись
        # оборвалась (в том числе по KeyboardInterrupt/SIGTERM —
        # extension host убивает хуки при перезапуске).
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@contextlib.contextmanager
def patch_lock(timeout: float = LOCK_TIMEOUT_SEC):
    """Сериализует правку файлов расширения между процессами.

    `atomic_write` защищает от смеси двух версий в одном файле, но не
    от потери правки: два процесса читают файл ДО чужой записи и каждый
    строит новое содержимое из устаревшего снимка — чей-то блок молча
    исчезает, а вернётся он только следующим запуском хука. Поэтому под
    локом должен идти весь цикл «прочитал → изменил → записал», а не
    одна запись.

    Отдаёт True, если лок взят. Не дождавшись за `timeout`, работаем
    БЕЗ лока и говорим об этом вызывающему: заблокировать сообщение
    пользователя из-за застрявшего соседа хуже, чем разово потерять
    чужой блок, — целостность файла в этом случае всё равно защищена
    атомарной записью.
    """
    handle = None
    try:
        os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
        handle = open(LOCK_PATH, "a+")
    except OSError:
        # Нет кэш-каталога или прав — патчим без сериализации.
        yield False
        return

    acquired = False
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
            break
        except OSError as exc:
            if exc.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)

    try:
        yield acquired
    finally:
        try:
            if acquired:
                fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()
        except OSError:
            pass
