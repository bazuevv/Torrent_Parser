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
import subprocess
import tempfile
import time

# Маркеры всех блоков, которые наши хуки вставляют в файлы расширения.
# Таблица общая, а не «у каждого хука свой», ровно по одной причине:
# эталонный снимок бандла обязан вычищать ВСЕ блоки, а не только свой.
# Заводите новый блок — впишите его сюда, иначе он попадёт в эталон и
# после каждого восстановления будет накладываться поверх себя.
BOOTSTRAP_MARKERS = ("/* claude-green-timestamp */", "/* /claude-green-timestamp */")
LOCALIZER_MARKERS = ("/* claude-localizer */", "/* /claude-localizer */")
BLOCK_MARKERS = (BOOTSTRAP_MARKERS, LOCALIZER_MARKERS)

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


# --- целостность бандла и эталонный снимок --------------------------------
#
# Вчерашняя поломка (2026-08-31) показала слабое место: когда файл
# расширения оказался повреждён, чинить его было НЕЧЕМ. Патчеры
# переписывают только участок между маркерами, а мусор за его пределами
# переживает любой запуск; эталонного снимка бандла никто не хранил
# (для package.json он есть — package.json.original, а для index.js
# не было). Здесь этот пробел и закрывается.


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def check_blocks(content: str) -> str | None:
    """Целы ли наши блоки в файле. None — всё в порядке.

    Проверка нарочно дешёвая (счёт подстрок): она идёт на каждом
    сообщении пользователя, и разбирать ради неё пятимегабайтный файл
    нельзя. Ловит ровно ту порчу, которую даёт гонка писателей:
    задвоенные и потерянные маркеры, перевёрнутую пару.
    """
    for begin, end in BLOCK_MARKERS:
        starts, ends = content.count(begin), content.count(end)
        if starts > 1 or ends > 1 or starts != ends:
            return (f"маркеры блока {begin} разъехались: "
                    f"открывающих {starts}, закрывающих {ends}")
        if starts == 1 and content.index(end) < content.index(begin):
            return f"блок {begin} перевёрнут: закрывающий маркер раньше открывающего"
    return None


def strip_blocks(content: str) -> str | None:
    """Бандл без наших блоков. None — файл повреждён, снимать нечего."""
    if check_blocks(content) is not None:
        return None
    for begin, end in BLOCK_MARKERS:
        if begin in content:
            start = content.index(begin)
            finish = content.index(end) + len(end)
            content = content[:start] + content[finish:]
    return content.rstrip() + "\n"


def node_check(source: str) -> str | None:
    """Разбирается ли JS. None — да либо проверить нечем (нет node).

    Отсутствие node трактуем как «проверить нечем», а не как ошибку:
    патч не должен отваливаться из-за того, что в системе нет ноды.
    """
    node = shutil.which("node")
    if not node:
        return None
    fd, tmp = tempfile.mkstemp(suffix=".js", prefix=".claude-check-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(source)
        done = subprocess.run([node, "--check", tmp],
                              capture_output=True, text=True, timeout=30)
        if done.returncode == 0:
            return None
        first = (done.stderr or "").strip().splitlines()
        return next((ln.strip() for ln in first if "Error" in ln), "не разбирается")
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def reference_path(path: str) -> str:
    return path + ".original"


def sync_reference(path: str, content: str) -> None:
    """Обновляет эталонный снимок бандла (тот же файл без наших блоков).

    Эталон лежит рядом, ВНУТРИ папки версии расширения. Это не мелочь:
    обновление расширения создаёт новую папку, и снимок там заводится
    заново. Общий эталон на все версии рано или поздно наложили бы на
    чужой бандл.

    Снимок обновляется молча, когда бандл изменился (вышла новая версия
    расширения), и не пишется вовсе, если файл повреждён или снимок не
    разбирается: закрепить поломку в эталоне — значит остаться без
    средства лечения ровно тогда, когда оно понадобится.
    """
    pristine = strip_blocks(content)
    if pristine is None:
        return
    ref = reference_path(path)
    try:
        if os.path.isfile(ref) and _read(ref) == pristine:
            return
    except OSError:
        pass
    if node_check(pristine) is not None:
        return
    try:
        atomic_write(ref, pristine)
    except OSError:
        pass


def repair_from_reference(path: str) -> bool:
    """Возвращает файл к эталону. False — эталона нет или он негоден."""
    ref = reference_path(path)
    if not os.path.isfile(ref):
        return False
    try:
        pristine = _read(ref)
    except OSError:
        return False
    if not pristine.strip() or check_blocks(pristine) is not None:
        return False
    try:
        atomic_write(path, pristine)
    except OSError:
        return False
    return True
