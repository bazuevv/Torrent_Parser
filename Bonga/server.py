#!/usr/bin/env python3
"""HTTP-сервер плеера: статика + общая база аккаунтов.

`python -m http.server` умел только отдавать файлы, поэтому база ников жила
в localStorage — своя у каждого браузера, да ещё и отдельная для 127.0.0.1
и 192.168.1.100 (это разные origin). Здесь она общая: accounts.json лежит
рядом с плеером, читается и пополняется всеми клиентами сразу.

  GET  /api/accounts  -> {"accounts": [["Ник","67",3301], …]}
  POST /api/accounts  <- [["Ник","67",3301], …]  (сливается с существующей)
                      -> {"taken": N, "added": M, "total": K}
"""

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from html import unescape as html_unescape
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import (parse_qs, quote as urlquote, unquote, urlencode,
                          urljoin, urlparse)

ROOT = os.path.dirname(os.path.abspath(__file__))
STORE = os.path.join(ROOT, 'accounts.json')
ONLINE_STORE = os.path.join(ROOT, 'online.json')
LADDER_STORE = os.path.join(ROOT, 'ladder.json')
NOTES_STORE = os.path.join(ROOT, 'notes.json')
PORT = 8777

# Записи кладём на большой диск: час 720p ≈ 1,5 ГБ, час 1080p ≈ 3,8 ГБ.
REC_DIR = os.environ.get('BONGA_REC_DIR', '/mnt/DATA/Bonga_rec')
MAX_RECORDINGS = 3                           # больше трёх ffmpeg разом не держим
DEFAULT_SEGMENT = int(os.environ.get('BONGA_HLS_TIME', '60'))


def pick_encoder():
    """NVENC пережимает на видеокарте почти без нагрузки; libx264 — запасной."""
    try:
        out = subprocess.run(['ffmpeg', '-hide_banner', '-encoders'],
                             capture_output=True, text=True, timeout=20).stdout
        if 'h264_nvenc' in out:
            return 'h264_nvenc'
    except (OSError, subprocess.SubprocessError):
        pass
    return 'libx264'


ENCODER = pick_encoder()

EDGE_RE = re.compile(r'^(\d+|us\d+)?$')      # номер сервера, либо пусто
USER_RE = re.compile(r'^[A-Za-z0-9_.-]{1,30}$')
ID_RE = re.compile(r'^[A-Za-z0-9_.-]{1,60}$')
LOCK = threading.Lock()                      # сервер многопоточный, запись сериализуем
REC_LOCK = threading.Lock()
MAX_BODY = 32 * 1024 * 1024                  # 78 тыс. ников укладываются в ~3 МБ

RECORDINGS = {}                              # id -> dict(proc, user, dir, started, …)


def load():
    try:
        with open(STORE, encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def merge(rows):
    """Сливает строки [ник, edge, зрители] в базу. -> (принято, новых, всего)."""
    with LOCK:
        base = load()
        taken = added = 0

        for row in rows:
            if not isinstance(row, list) or not row:
                continue
            user = row[0]
            edge = '' if len(row) < 2 or row[1] is None else str(row[1])
            if not isinstance(user, str) or not user or not EDGE_RE.match(edge):
                continue

            viewers = 0
            if len(row) > 2:
                try:
                    viewers = int(row[2])
                except (TypeError, ValueError):
                    viewers = 0

            key = user.lower()
            prev = base.get(key)
            if prev is None:
                added += 1
            else:
                # Запись с известным сервером не затираем пустой.
                edge = edge or prev[1]
                viewers = viewers or prev[2]

            base[key] = [user, edge, viewers]
            taken += 1

        tmp = STORE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(base, f, ensure_ascii=False, separators=(',', ':'))
        os.replace(tmp, STORE)               # подмена атомарная: читатели не увидят обрывок

        return taken, added, len(base)


# --------------------------------------------------------------------------
# Настройки плеера
# --------------------------------------------------------------------------
# Настройки общие для всех браузеров и устройств, поэтому живут на сервере,
# а не в localStorage. Все значения числовые — так не нужно разбираться с
# типами, а границы отсекают и опечатки, и злой умысел.

SETTINGS_STORE = os.path.join(ROOT, 'settings.json')
SETTINGS_LOCK = threading.Lock()

SETTINGS_RANGE = {
    'defQuality': (0, 9999),     # 0 — авто, 9999 — максимум
    'segment': (5, 900),         # длина куска записи, секунды
    'thumbs': (0, 3600),         # период обновления превью, 0 — никогда
    'hist': (0, 86400),          # период проверки истории
    'pull': (10, 86400),         # период перечитывания базы ников
    'ttl': (1, 10080),           # годность результата проверки эфира, минуты
    'gap': (0, 1440),            # пауза между самостоятельными обходами, минуты
    'pool': (1, 256),            # параллельных запросов при обходе
    'buffer': (10, 7200),        # буфер памяти, секунды
    'auto': (0, 86400),          # автопереключение комнат, секунды
    'hideLeft': (0, 1),
    'hideRight': (0, 1),
    'rec': (0, 1),               # писать ли эфир на диск автоматически
    'keepMin': (0, 86400),       # ниже этой длины запись удаляем без вопросов
    'deep': (0, 1440),           # период глубокой проверки истории, минуты
    'deepBatch': (0, 500),       # сколько ников за заход, 0 — все
    'recMode': (0, 1),           # 0 — запись вручную, 1 — автоматически
    'maxRate': (0, 100000),      # потолок битрейта просмотра, кбит/с; 0 — без ограничения
    'recRate': (0, 100000),      # потолок битрейта записи; выше — пережимаем
    'catalog': (0, 3600),        # период опроса каталога сайта, секунды; 0 — не опрашивать
    'act': (0, 600),             # период замера активности комнат истории; 0 — не мерить
    'ladder': (0, 604800),       # как часто перемерять битрейты дорожек; 0 — не мерить
    'warm': (0, 120),            # окно разгона канала, секунды; 0 — верить оценке сразу
    'wallQ': (144, 2160),        # потолок высоты дорожки в ячейках стены
    'wallCols': (0, 8),          # столбцов в стене; 0 — подбирать по числу ячеек
    'msgs': (0, 1),              # показывать ли сообщения о событиях в верхней панели
    'subsOrig': (0, 1),          # субтитры перевода: показывать оригинал над переводом
}

# Путь к папке записей — единственная строковая настройка. Разрешаем только
# внутри понятных корней: сервис работает под обычным пользователем, а systemd
# всё равно пускает на запись лишь перечисленные в юните каталоги.
ALLOWED_ROOTS = ('/mnt/DATA', '/mnt/Projects', '/home/vladimir')


def valid_rec_dir(path):
    if not isinstance(path, str) or not path.startswith('/'):
        return None
    norm = os.path.normpath(path)
    if not any(norm == root or norm.startswith(root + '/') for root in ALLOWED_ROOTS):
        return None
    try:
        os.makedirs(norm, exist_ok=True)
        probe = os.path.join(norm, '.write-test')
        with open(probe, 'w', encoding='utf-8') as f:
            f.write('ok')
        os.remove(probe)
        return norm
    except OSError:
        return None


def browse_dirs(raw):
    """Список подпапок для обозревателя в настройках.

    Наружу отдаём только то, что лежит внутри разрешённых корней: сервер виден
    всей локальной сети, и гулять по файловой системе ему незачем.
    """
    roots = {'path': '', 'parent': None,
             'dirs': [{'name': r, 'path': r} for r in ALLOWED_ROOTS]}
    if not raw:
        return roots

    norm = os.path.normpath(raw)
    if not any(norm == root or norm.startswith(root + '/') for root in ALLOWED_ROOTS):
        return dict(roots, error='путь вне разрешённых корней')

    parent = '' if norm in ALLOWED_ROOTS else os.path.dirname(norm)
    try:
        with os.scandir(norm) as it:
            dirs = [{'name': e.name, 'path': os.path.join(norm, e.name)}
                    for e in it if e.is_dir() and not e.name.startswith('.')]
    except OSError as err:
        return {'path': norm, 'parent': parent, 'dirs': [], 'error': err.strerror or str(err)}

    dirs.sort(key=lambda d: d['name'].lower())
    return {'path': norm, 'parent': parent, 'dirs': dirs[:500],
            'writable': os.access(norm, os.W_OK)}


def rec_dir():
    """Куда писать записи прямо сейчас: настройка или значение по умолчанию."""
    path = load_settings().get('recDir')
    return path if isinstance(path, str) and path else REC_DIR


VIDEO_EXT = ('.mp4', '.m4v', '.mov', '.webm', '.mkv', '.ts')
LIB_LOCK = threading.Lock()
LIB_META = {}                 # (путь, размер, mtime) → длительность в секундах


def valid_lib_dir(path):
    """Папка для просмотра. В отличие от папки записей право на запись не
    требуется: сюда мы только читаем."""
    if not isinstance(path, str) or not path.startswith('/'):
        return None
    norm = os.path.normpath(path)
    if not any(norm == root or norm.startswith(root + '/') for root in ALLOWED_ROOTS):
        return None
    return norm if os.path.isdir(norm) else None


def lib_dir():
    path = load_settings().get('libDir')
    return path if isinstance(path, str) and path else ''


def probe_duration(full, size, mtime):
    """Длительность файла. ffprobe стоит десятки миллисекунд, а список
    перечитывается на каждое открытие панели — поэтому помним ответ,
    пока файл не изменился."""
    key = (full, size, int(mtime))
    with LIB_LOCK:
        if key in LIB_META:
            return LIB_META[key]
    seconds = 0
    try:
        out = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', full],
            capture_output=True, text=True, timeout=10).stdout.strip()
        seconds = int(float(out)) if out else 0
    except (OSError, ValueError, subprocess.SubprocessError):
        seconds = 0
    with LIB_LOCK:
        if len(LIB_META) > 4000:
            LIB_META.clear()
        LIB_META[key] = seconds
    return seconds


def lib_list():
    """Видеофайлы в папке просмотра, новые сверху. Подпапки не обходим:
    записи лежат плоско, а рекурсия по чужой папке может уйти надолго."""
    root = lib_dir()
    if not root:
        return {'dir': '', 'files': [], 'error': 'папка не задана'}
    if not valid_lib_dir(root):
        return {'dir': root, 'files': [], 'error': 'папка недоступна'}

    files = []
    try:
        with os.scandir(root) as it:
            for entry in it:
                if not entry.is_file() or not entry.name.lower().endswith(VIDEO_EXT):
                    continue
                stat = entry.stat()
                files.append({'name': entry.name, 'bytes': stat.st_size,
                              'at': int(stat.st_mtime),
                              'seconds': probe_duration(entry.path, stat.st_size,
                                                        stat.st_mtime)})
    except OSError as err:
        return {'dir': root, 'files': [], 'error': err.strerror or str(err)}

    files.sort(key=lambda f: -f['at'])
    return {'dir': root, 'files': files[:1000]}


def load_settings():
    try:
        with open(SETTINGS_STORE, encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return {k: v for k, v in data.items()
                if k in SETTINGS_RANGE or k in ('recDir', 'libDir', 'lang')}
    except (OSError, ValueError):
        return {}


LANG_DIR = os.path.join(ROOT, 'lang')
LANG_RE = re.compile(r'^[a-z]{2}(-[a-z]{2})?$')


def known_langs():
    """Коды словарей из папки lang/ — по именам файлов вида ru.json.

    Список именно с диска: добавить язык должно быть достаточно одним файлом,
    без правки ни сервера, ни страницы."""
    try:
        return sorted(name[:-5] for name in os.listdir(LANG_DIR)
                      if name.endswith('.json') and LANG_RE.match(name[:-5]))
    except OSError:
        return []


def save_settings(patch):
    with SETTINGS_LOCK:
        merged = load_settings()
        for key, value in patch.items():
            # Язык интерфейса: 'auto' либо код словаря из папки lang/. Проверяем
            # по списку файлов, а не по перечислению в коде: новый язык должен
            # добавляться одним файлом, без правки сервера.
            if key == 'lang':
                code = str(value or '').strip().lower()
                if code == 'auto' or code in known_langs():
                    merged[key] = code or 'auto'
                continue
            if key in ('recDir', 'libDir'):
                if isinstance(value, str) and not value.strip():
                    merged.pop(key, None)           # пусто — вернуться к умолчанию
                    continue
                checked = valid_rec_dir(value) if key == 'recDir' else valid_lib_dir(value)
                if checked:
                    merged[key] = checked
                continue
            if key not in SETTINGS_RANGE:
                continue
            try:
                number = int(value)
            except (TypeError, ValueError):
                continue
            low, high = SETTINGS_RANGE[key]
            merged[key] = min(max(number, low), high)

        tmp = SETTINGS_STORE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(merged, f, ensure_ascii=False, separators=(',', ':'))
        os.replace(tmp, SETTINGS_STORE)
        return merged


# --------------------------------------------------------------------------
# Кто был в эфире на момент последней проверки
# --------------------------------------------------------------------------
# Полный обход — это тысячи запросов и несколько минут, поэтому результат живёт
# на сервере, а не в localStorage: проверил один браузер — видят все.

ONLINE_LOCK = threading.Lock()
MAX_ONLINE_ROWS = 50000
# Сколько каталог сайта считается свежим. Закладка присылает его раз в минуту,
# так что пяти минут хватает пережить пропущенную попытку; после закрытия
# вкладки со сборщиком список молча возвращается к нашему обходу.
CATALOG_TRUST_S = 300


def load_online():
    try:
        with open(ONLINE_STORE, encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get('live'), list):
            return data
    except (OSError, ValueError):
        pass
    return {'at': 0, 'live': []}


def save_online(rows, src='sweep'):
    """Список эфира приходит из двух источников. Наш обход (`sweep`) стучится
    плейлистом по последним известным серверам и находит лишь тех, кто с них
    не переехал, — вчера это дало 23 комнаты из полутора тысяч. Каталог сайта
    (`catalog`), который приносит закладка, содержит всех и с верными номерами
    серверов, поэтому он главнее: обход его не затирает, а лишь дополняет
    теми, кого в каталоге нет (другие вкладки сайта, ручной поиск)."""
    live = []
    for row in rows[:MAX_ONLINE_ROWS]:
        if not isinstance(row, list) or len(row) < 2:
            continue
        user, edge = row[0], str(row[1])
        if not isinstance(user, str) or not user or not EDGE_RE.match(edge):
            continue
        viewers = 0
        if len(row) > 2:
            try:
                viewers = int(row[2])
            except (TypeError, ValueError):
                viewers = 0

        seen = 0                       # когда CDN в последний раз обновил превью
        if len(row) > 3:
            try:
                seen = int(row[3])
            except (TypeError, ValueError):
                seen = 0

        # Пятым идёт адрес превью с сайта. Пустой он у строк из обхода и от
        # закладки — там его просто неоткуда взять, плеер откатится на догадку
        # по edge. Формат от этого не ломается: старые записи короче на поле.
        shot = str(row[4]).strip() if len(row) > 4 and isinstance(row[4], str) else ''
        if not shot.startswith('https://'):
            shot = ''

        live.append([user, edge, viewers, seen, shot])

    at = int(time.time())
    prev = load_online()
    fresh_catalog = (prev.get('src') == 'catalog'
                     and at - int(prev.get('at') or 0) < CATALOG_TRUST_S)
    if src == 'sweep' and fresh_catalog:
        # Метку времени оставляем каталожную: иначе обход, идущий каждую
        # минуту, бесконечно продлевал бы доверие к устаревшему каталогу,
        # и после закрытия вкладки со сборщиком список бы не обновился.
        known = {row[0].lower() for row in prev['live']}
        live = prev['live'] + [row for row in live if row[0].lower() not in known]
        at, src = int(prev['at']), 'catalog'

    payload = {'at': at, 'src': src, 'live': live}
    with ONLINE_LOCK:
        tmp = ONLINE_STORE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, separators=(',', ':'))
        os.replace(tmp, ONLINE_STORE)
    return payload


# --------------------------------------------------------------------------
# Запись эфира на диск
# --------------------------------------------------------------------------

def host_of(edge):
    return f'mobile-edge{edge}' if edge.isdigit() else f'mobile-edge-{edge}'


def pick_variant(master_url, max_rate=0):
    """Из мастер-плейлиста выбирает самую качественную дорожку в пределах
    потолка битрейта. Отдавать ffmpeg мастер целиком нельзя: он молча возьмёт
    первую дорожку, а это 240p. Возвращает (адрес, битрейт).
    """
    req = urllib.request.Request(master_url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=15) as resp:
        lines = resp.read(64 * 1024).decode('utf-8', 'replace').splitlines()

    variants = []
    for i, line in enumerate(lines):
        if not line.startswith('#EXT-X-STREAM-INF'):
            continue
        uri = lines[i + 1].strip() if i + 1 < len(lines) else ''
        if not uri or uri.startswith('#'):
            continue
        bw = re.search(r'BANDWIDTH=(\d+)', line)
        variants.append((int(bw.group(1)) if bw else 0, uri))

    if not variants:
        return None, 0

    limit = max_rate * 1000 if max_rate else 0
    fits = [v for v in variants if not limit or v[0] <= limit]
    best = max(fits, key=lambda v: v[0]) if fits else min(variants, key=lambda v: v[0])
    return urljoin(master_url, best[1]), best[0]


def rec_size(path):
    total = 0
    try:
        with os.scandir(path) as it:
            for entry in it:
                if entry.is_file():
                    total += entry.stat().st_size
    except OSError:
        pass
    return total


def rec_state(rid, rec):
    """phase: recording -> merging -> ready (или failed, если склейка не вышла)."""
    alive = rec['proc'].poll() is None
    phase = 'recording' if alive else rec.get('phase', 'merging')
    mp4 = os.path.join(rec_dir(), rid + '.mp4')

    if phase == 'ready':
        size = os.path.getsize(mp4) if os.path.exists(mp4) else 0
        url = f'rec/{rid}.mp4'
    else:
        size = rec_size(rec['dir'])
        url = f'rec/{rid}/index.m3u8'

    return {
        'id': rid,
        'user': rec['user'],
        'running': alive,
        'phase': phase,
        'started': int(rec['started']),      # нужен плееру, чтобы совместить шкалы
        'seconds': rec.get('duration') or int(time.time() - rec['started']),
        'bytes': size,
        'url': url,
    }


def rec_merge(rid):
    """Склеивает сегменты в один mp4 и убирает временный каталог.

    Пока идёт запись, куски нужны — иначе браузер не смог бы перематывать
    незаконченный файл. Как только ffmpeg отпустил поток, склеиваем всё в
    один mp4 без перекодирования и удаляем каталог с сегментами.
    """
    rec = RECORDINGS.get(rid)
    if not rec:
        return

    playlist = os.path.join(rec['dir'], 'index.m3u8')
    target = os.path.join(rec_dir(), rid + '.mp4')

    ok = False
    if os.path.exists(playlist):
        cmd = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'warning', '-y',
               '-allowed_extensions', 'ALL', '-i', playlist,
               '-c', 'copy', '-bsf:a', 'aac_adtstoasc',
               '-movflags', '+faststart', target]
        try:
            with open(os.path.join(rec['dir'], 'ffmpeg.log'), 'ab') as log:
                ok = subprocess.run(cmd, stdout=log, stderr=log,
                                    stdin=subprocess.DEVNULL, timeout=3600).returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False

    if ok and os.path.getsize(target) > 0:
        shutil.rmtree(rec['dir'], ignore_errors=True)
        rec['phase'] = 'ready'
        write_log(f'record merged: {rid}.mp4, {os.path.getsize(target)} байт')
        # Пока склеивали, запись могли пометить на выброс — убираем результат.
        if rec.get('discard'):
            rec_purge(rid)
    else:
        rec['phase'] = 'failed'          # сегменты не трогаем, данные целы
        write_log(f'record merge FAILED: {rid}, сегменты оставлены в {rec["dir"]}')


def rec_start(user, edge, view_rate, segment, rec_rate=0):
    """Запускает ffmpeg, который льёт эфир в HLS на диск. -> состояние сессии."""
    source, bitrate = pick_variant(
        f'https://{host_of(edge)}.bcvcdn.com/hls/stream_{user}/playlist.m3u8', view_rate)
    if not source:
        raise RuntimeError('в плейлисте нет дорожек — эфира сейчас нет')

    # Два каталога в одну секунду дали бы одинаковый id, общий каталог и двух
    # ffmpeg разом: сервер запомнил бы только последнего, а первый писал бы
    # в тот же каталог до конца эфира. Поэтому имя делаем уникальным.
    base_id = f'{user}_{time.strftime("%Y%m%d-%H%M%S")}'
    rid, suffix = base_id, 1
    while rid in RECORDINGS or os.path.exists(os.path.join(rec_dir(), rid)):
        suffix += 1
        rid = f'{base_id}-{suffix}'

    # Одну и ту же комнату дважды не пишем.
    for other, rec in list(RECORDINGS.items()):
        if rec['user'].lower() == user.lower() and rec['proc'].poll() is None:
            write_log(f'record restart: гашу прежнюю запись {other}')
            rec_stop(other)

    out = os.path.join(rec_dir(), rid)
    os.makedirs(out, exist_ok=True)

    # Дорожка тяжелее потолка записи — пережимаем. На видеокарте это почти
    # бесплатно; без неё пришлось бы грузить процессор кодированием в реальном
    # времени. Звук не трогаем, он и так десятки килобит.
    limit = rec_rate * 1000 if rec_rate else 0
    squeeze = bool(limit and bitrate > limit)
    if squeeze:
        # Заданный битрейт — средний, а не потолок: статичной сцене хватит
        # меньшего, движению даём запас в полтора раза. Если приравнять
        # maxrate к цели, кодировщик лишается свободы и режет качество там,
        # где достаточно было занять запас.
        peak = int(rec_rate * 1.5)
        video = ['-c:v', ENCODER, '-b:v', f'{rec_rate}k',
                 '-maxrate', f'{peak}k', '-bufsize', f'{peak * 2}k']
        if ENCODER == 'h264_nvenc':
            # Замер на GTX 1060 (см. ниже): пресеты p4…p7 на Pascal дают
            # побайтово одинаковый результат, поэтому p5 взят как нейтральный —
            # на более новой карте он начнёт что-то значить. Ниже p4 опускаться
            # нельзя: p1 промахивается мимо цели втрое.
            #
            # Просмотр вперёд и spatial-AQ на полной силе перебирали заданный
            # битрейт (+8% и +12% к цели), а настройка существует ровно ради
            # предсказуемого размера файла. AQ оставлен вполсилы: он защищает
            # от полос на тёмных стенах, чего в этих эфирах хватает, но теперь
            # стоит +5%, а не +12%. temporal-AQ бесплатен — укладывается в цель.
            video += ['-preset', 'p5', '-rc', 'vbr', '-bf', '3',
                      '-spatial-aq', '1', '-aq-strength', '4', '-temporal-aq', '1']
        else:
            video += ['-preset', 'veryfast']
        codecs = video + ['-c:a', 'copy']
    else:
        codecs = ['-c', 'copy']            # без перекодирования: процессор не греем

    cmd = [
        'ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'warning',
        '-user_agent', 'Mozilla/5.0',
        '-reconnect', '1', '-reconnect_streamed', '1', '-reconnect_delay_max', '10',
        '-i', source,
        *codecs,
        '-f', 'hls',
        # Длина сегмента приходит из настроек плеера. Короткие дают дешёвую
        # перемотку по записи, длинные — меньше файлов. Резать ffmpeg всё равно
        # будет по ближайшему ключевому кадру.
        '-hls_time', str(segment),
        '-hls_list_size', '0',               # плейлист не обрезается
        '-hls_playlist_type', 'event',       # можно перематывать к самому началу
        '-hls_flags', 'append_list+independent_segments',
        '-hls_segment_filename', os.path.join(out, 'seg_%05d.ts'),
        os.path.join(out, 'index.m3u8'),
    ]

    write_log(f'record {rid}: источник {bitrate / 1e6:.1f} Мбит/с, ' +
              (f'пережимаю до {rec_rate / 1000:.1f} Мбит/с в среднем, пик {rec_rate * 1.5 / 1000:.1f} ({ENCODER})'
               if squeeze else 'копирую как есть'))

    log = open(os.path.join(out, 'ffmpeg.log'), 'ab')
    proc = subprocess.Popen(cmd, stdout=log, stderr=log, stdin=subprocess.DEVNULL)

    with REC_LOCK:
        # Больше MAX_RECORDINGS одновременно не держим: старейшую гасим.
        alive = [(k, v) for k, v in RECORDINGS.items() if v['proc'].poll() is None]
        if len(alive) >= MAX_RECORDINGS:
            oldest = min(alive, key=lambda kv: kv[1]['started'])[0]
            rec_stop(oldest, _locked=True)
        RECORDINGS[rid] = {'proc': proc, 'user': user, 'dir': out,
                           'started': time.time(), 'log': log, 'source': source}
        return rec_state(rid, RECORDINGS[rid])


def rec_purge(rid):
    """Убирает и каталог сегментов, и склеенный mp4."""
    folder = os.path.join(rec_dir(), rid)
    target = os.path.join(rec_dir(), rid + '.mp4')
    if os.path.isdir(folder):
        shutil.rmtree(folder, ignore_errors=True)
    try:
        os.remove(target)
    except OSError:
        pass
    write_log(f'record purged: {rid}')


def rec_stop(rid, _locked=False):
    """Гасит ffmpeg мягко, чтобы он дописал плейлист до конца."""
    def do():
        rec = RECORDINGS.get(rid)
        if not rec:
            return None
        if rec['proc'].poll() is None:
            rec['proc'].send_signal(signal.SIGINT)
            try:
                rec['proc'].wait(timeout=10)
            except subprocess.TimeoutExpired:
                rec['proc'].kill()
                try:
                    rec['proc'].wait(timeout=5)   # дожидаемся смерти: иначе ffmpeg
                except subprocess.TimeoutExpired:  # успеет восстановить каталог
                    write_log(f'record: ffmpeg {rid} не умер даже после kill')
        try:
            rec['log'].close()
        except OSError:
            pass

        # Длительность фиксируем до склейки, дальше время идти не должно.
        rec.setdefault('duration', int(time.time() - rec['started']))
        if rec.get('phase') not in ('merging', 'ready', 'failed'):
            rec['phase'] = 'merging'
            threading.Thread(target=rec_merge, args=(rid,), daemon=True).start()
        return rec_state(rid, rec)

    if _locked:
        return do()
    with REC_LOCK:
        return do()


LOG_PATH = os.path.join(REC_DIR, 'server.log')
PLAY_LOG_PATH = os.path.join(REC_DIR, 'playback.log')
LOG_LOCK = threading.Lock()
PLAY_LOG_LOCK = threading.Lock()


def write_log(line):
    """Свой файл лога: вывод юнита в journald по какой-то причине не оседает."""
    stamp = time.strftime('%Y-%m-%d %H:%M:%S')
    try:
        with LOG_LOCK:
            if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 5 * 1024 * 1024:
                os.replace(LOG_PATH, LOG_PATH + '.1')
            with open(LOG_PATH, 'a', encoding='utf-8') as f:
                f.write(f'{stamp} {line}\n')
    except OSError:
        pass


def write_play_log(lines):
    """Журнал воспроизведения от плеера: нужен, чтобы разбирать обрывы потом,
    когда страница уже перезагружена и её память потеряна."""
    try:
        with PLAY_LOG_LOCK:
            if os.path.exists(PLAY_LOG_PATH) and os.path.getsize(PLAY_LOG_PATH) > 5 * 1024 * 1024:
                os.replace(PLAY_LOG_PATH, PLAY_LOG_PATH + '.1')
            with open(PLAY_LOG_PATH, 'a', encoding='utf-8') as f:
                for line in lines[:5000]:
                    if isinstance(line, str):
                        f.write(line.replace('\n', ' ')[:2000] + '\n')
    except OSError:
        pass


# --------------------------------------------------------------------------
# Заметки о комнатах
#
# Единственной памятью о комнате было время последнего просмотра. Заметки живут
# на сервере, а не в localStorage: их пишут для себя надолго, и терять их при
# смене браузера или чистке данных обидно вдвойне.
NOTES_LOCK = threading.Lock()
NOTES = {}                    # ник в нижнем регистре → {'user','text','at'}
NOTE_MAX = 500                # длина одной заметки
NOTES_MAX = 5000              # сколько всего храним


def load_notes():
    try:
        with open(NOTES_STORE, encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_notes():
    with NOTES_LOCK:
        data = dict(sorted(NOTES.items(), key=lambda kv: -(kv[1].get('at') or 0))[:NOTES_MAX])
        NOTES.clear()
        NOTES.update(data)
    try:
        tmp = NOTES_STORE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, NOTES_STORE)
    except OSError:
        pass


def set_note(user, text):
    """Пустой текст стирает заметку — отдельной кнопки удаления не нужно."""
    key = user.lower()
    clean = ' '.join(str(text).split())[:NOTE_MAX] if text else ''
    with NOTES_LOCK:
        if clean:
            NOTES[key] = {'user': user, 'text': clean, 'at': int(time.time())}
        else:
            NOTES.pop(key, None)
        left = len(NOTES)
    save_notes()
    return clean, left


# --------------------------------------------------------------------------
# Раздел «Обо мне» со страницы профиля
#
# Модель описывает себя сама, и это самое осмысленное, что можно положить в
# пустую заметку по умолчанию. Страница профиля серверу доступна: Cloudflare
# её не закрывает, как и листинг.
ABOUT_LOCK = threading.Lock()
ABOUT = {}                    # ник → {'text','at'}
ABOUT_TTL = 604800            # неделя: описание меняют редко
PROFILE_BASE = os.environ.get('BONGA_PROFILE_BASE', 'https://ru17.bongacams.com')


def fetch_about(user):
    """Текст раздела «Обо мне». Разметка: заголовок, затем блок с классом
    txtsm_text — из него и берём содержимое."""
    url = f'{PROFILE_BASE}/profile/{urlquote(user)}'
    req = urllib.request.Request(url, headers={'User-Agent': CATALOG_UA,
                                               'Accept': 'text/html'})
    with urllib.request.urlopen(req, timeout=20) as res:
        html = res.read().decode('utf-8', 'replace')

    head = html.find('Обо мне')
    if head < 0:
        return ''
    block = re.search(r'class="txtsm_text[^"]*"[^>]*>(.*?)</div>',
                      html[head:head + 20000], re.S)
    if not block:
        return ''
    text = re.sub(r'<[^>]+>', ' ', block.group(1))
    text = html_unescape(text)
    return ' '.join(text.split())[:NOTE_MAX]


def about_of(user):
    """С кэшем: страница профиля весит триста килобайт, а описание меняют
    раз в сезон."""
    key = user.lower()
    now = int(time.time())
    with ABOUT_LOCK:
        item = ABOUT.get(key)
        if item and now - item['at'] < ABOUT_TTL:
            return item['text']
    try:
        text = fetch_about(user)
    except Exception:
        return ''
    with ABOUT_LOCK:
        if len(ABOUT) > 3000:
            ABOUT.clear()
        ABOUT[key] = {'text': text, 'at': now}
    return text


def default_gateway():
    """Адрес роутера из таблицы маршрутизации: нужен, чтобы отделить свою
    локальную сеть от участка до провайдера."""
    try:
        with open('/proc/net/route', encoding='ascii') as f:
            for line in f.readlines()[1:]:
                cols = line.split()
                if len(cols) > 3 and cols[1] == '00000000' and int(cols[3], 16) & 2:
                    num = int(cols[2], 16)
                    return '.'.join(str((num >> (8 * i)) & 255) for i in range(4))
    except (OSError, ValueError):
        pass
    return ''


def tcp_ms(host, port, timeout=4.0):
    """Время установки TCP-соединения. Отказ в соединении засчитываем как
    успех: пакет дошёл и вернулся, а открыт порт или нет — для замера
    задержки безразлично."""
    started = time.time()
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.close()
    except ConnectionRefusedError:
        pass
    except OSError:
        return None
    return (time.time() - started) * 1000


def probe_target(target, tries=6):
    """Серия замеров до одного адреса. Потери считаем по превышению над
    лучшим результатом: переспрос потерянного SYN добавляет около секунды,
    поэтому всё, что медленнее лучшего на 600 мс, — почти наверняка потеря."""
    name, host, port = target
    times = [t for t in (tcp_ms(host, port) for _ in range(tries)) if t is not None]
    row = {'name': name, 'host': host, 'tries': tries, 'ok': len(times)}
    if not times:
        return row | {'verdict': 'недоступен'}

    best = min(times)
    lost = sum(1 for t in times if t > best + 600)
    row |= {'best': round(best), 'median': round(sorted(times)[len(times) // 2]),
            'worst': round(max(times)), 'lost': lost,
            'loss': round((lost + tries - len(times)) / tries * 100)}
    if row['loss'] >= 30:
        row['verdict'] = 'плохо'
    elif row['loss'] > 0 or len(times) < tries:
        row['verdict'] = 'с потерями'
    else:
        row['verdict'] = 'чисто'
    return row


def net_check():
    """Диагностика канала: одни и те же замеры до роутера, до нейтральных
    узлов и до CDN трансляций. Расхождение между ними и показывает, где
    рвётся — в своей сети, у провайдера или на маршруте к вещателю."""
    started = time.time()

    dns_ms, dns_err = None, ''
    try:
        at = time.time()
        socket.getaddrinfo('mobile-edge9.bcvcdn.com', 443, socket.AF_INET)
        dns_ms = round((time.time() - at) * 1000)
    except OSError as exc:
        dns_err = str(exc)

    targets = []
    gateway = default_gateway()
    if gateway:
        targets.append(('Роутер', gateway, 80))
    targets += [('Cloudflare', '1.1.1.1', 443),
                ('Google', '8.8.8.8', 443),
                ('Яндекс', 'ya.ru', 443)]

    # Серверы вещания берём те, что сейчас реально раздают эфир: проверять
    # наугад бессмысленно, часть номеров вообще не существует.
    edges, seen = [], set()
    for row in load_online().get('live', []):
        edge = str(row[1]) if len(row) > 1 else ''
        if edge and edge not in seen:
            seen.add(edge)
            edges.append(edge)
        if len(edges) == 3:
            break
    targets += [(f'CDN эфира ({e})', f'{host_of(e)}.bcvcdn.com', 443) for e in edges]

    with ThreadPoolExecutor(len(targets)) as pool:
        rows = list(pool.map(probe_target, targets))

    ref = [r for r in rows if r['name'] in ('Cloudflare', 'Google', 'Яндекс') and 'loss' in r]
    cdn = [r for r in rows if r['name'].startswith('CDN') and 'loss' in r]
    ref_loss = sum(r['loss'] for r in ref) / len(ref) if ref else 0
    cdn_loss = sum(r['loss'] for r in cdn) / len(cdn) if cdn else 0

    if not ref and not cdn:
        verdict = 'Сеть не отвечает вовсе — проверьте кабель и роутер.'
    elif cdn_loss < 10 and ref_loss < 10:
        verdict = 'Канал чист. Превью сейчас должны грузиться без задержек.'
    elif ref_loss >= 10 and cdn_loss >= 10:
        verdict = ('Теряются пакеты до всех адресов, не только до вещателя — '
                   'проблема в своём канале или у провайдера.')
    elif cdn_loss >= 10:
        verdict = ('До нейтральных узлов чисто, а до серверов вещания пакеты '
                   'теряются — рвётся маршрут к CDN, у нас не лечится.')
    else:
        verdict = 'Пакеты теряются до нейтральных узлов, а до CDN проходят.'

    return {'at': int(time.time()), 'took': round(time.time() - started, 1),
            'dns': dns_ms, 'dnsError': dns_err, 'gateway': gateway,
            'targets': rows, 'verdict': verdict}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def log_message(self, fmt, *args):
        write_log(f'{self.address_string()} {fmt % args}')

    def cors_origin(self):
        """Разрешаем читать ответ только страницам самого сайта.

        Нужно, чтобы закладка на bongacams.com могла отправить собранные ники
        прямо сюда и показать результат. Защитой это не считается: CORS не
        мешает чужому сайту прислать запрос, он лишь мешает прочитать ответ.
        """
        origin = self.headers.get('Origin') or ''
        return origin if re.match(r'^https://([a-z0-9-]+\.)*bongacams\.com$', origin) else None

    def _json(self, payload, code=200):
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        allow = self.cors_origin()
        if allow:
            self.send_header('Access-Control-Allow-Origin', allow)
            self.send_header('Vary', 'Origin')
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        allow = self.cors_origin()
        self.send_response(204 if allow else 403)
        if allow:
            self.send_header('Access-Control-Allow-Origin', allow)
            self.send_header('Access-Control-Allow-Methods', 'POST, GET, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type')
            self.send_header('Access-Control-Max-Age', '86400')
            self.send_header('Vary', 'Origin')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def _serve_recording(self, path):
        """Отдаёт файлы записи из папки записей — она лежит вне корня сервера.

        Два вида путей: rec/<id>/<файл> — сегменты и плейлист во время записи,
        rec/<id>.mp4 — готовая склейка. Для mp4 обязателен Range: без него
        браузер не сможет перематывать файл.
        """
        parts = [p for p in path[len('/rec/'):].split('/') if p not in ('', '.', '..')]
        if not all(ID_RE.match(p) for p in parts):
            return self.send_error(404)

        if len(parts) == 1 and parts[0].endswith('.mp4'):
            full, ctype, cacheable = os.path.join(rec_dir(), parts[0]), 'video/mp4', True
        elif len(parts) == 2:
            full = os.path.join(rec_dir(), parts[0], parts[1])
            playlist = parts[1].endswith('.m3u8')
            ctype = 'application/vnd.apple.mpegurl' if playlist else 'video/mp2t'
            cacheable = not playlist          # плейлист растёт, сегменты неизменны
        else:
            return self.send_error(404)

        if not os.path.isfile(full):
            return self.send_error(404)
        self._serve_file(full, ctype, cacheable)

    def _serve_library(self, path):
        """Файл из папки просмотра. Имя берём только последним звеном пути и
        сверяем со списком папки: так наружу не выйдет ни «..», ни ссылка на
        соседний каталог — сервер виден всей локальной сети."""
        name = unquote(path[len('/lib/'):]).strip('/')
        root = lib_dir()
        if not root or '/' in name or name in ('', '.', '..'):
            return self.send_error(404)
        if not name.lower().endswith(VIDEO_EXT):
            return self.send_error(404)

        full = os.path.join(root, name)
        if os.path.realpath(os.path.dirname(full)) != os.path.realpath(root):
            return self.send_error(404)
        if not os.path.isfile(full):
            return self.send_error(404)

        kind = 'video/mp4'
        if name.lower().endswith('.webm'):
            kind = 'video/webm'
        elif name.lower().endswith('.mkv'):
            kind = 'video/x-matroska'
        elif name.lower().endswith('.ts'):
            kind = 'video/mp2t'
        self._serve_file(full, kind, True)

    def _serve_file(self, full, ctype, cacheable):
        try:
            size = os.path.getsize(full)
        except OSError:
            return self.send_error(404)

        start, end, status = 0, size - 1, 200
        rng = self.headers.get('Range')
        if rng:
            m = re.match(r'bytes=(\d*)-(\d*)\s*$', rng.strip())
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    end = int(m.group(2)) if m.group(2) else size - 1
                else:
                    start = max(0, size - int(m.group(2)))
                if start >= size:
                    self.send_response(416)
                    self.send_header('Content-Range', f'bytes */{size}')
                    self.end_headers()
                    return
                end = min(end, size - 1)
                status = 206

        length = end - start + 1
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Length', str(length))
        if status == 206:
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        self.send_header('Cache-Control', 'max-age=86400' if cacheable else 'no-store')
        self.end_headers()

        try:
            with open(full, 'rb') as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (OSError, BrokenPipeError, ConnectionResetError):
            pass                              # браузер закрыл соединение — обычное дело

    def do_GET(self):
        path = self.path.split('?')[0]

        if path == '/api/accounts':
            return self._json({'accounts': list(load().values())})

        if path == '/api/online':
            return self._json(load_online())

        if path == '/api/settings':
            return self._json(load_settings())

        # Словари интерфейса: список кодов и сам словарь по коду. Русский лежит
        # в самой странице — она обязана открыться и без сети; остальные языки
        # подгружаются отсюда.
        if path == '/api/langs':
            return self._json({'langs': known_langs()})

        if path.startswith('/api/lang/'):
            code = path[len('/api/lang/'):].lower()
            if not LANG_RE.match(code) or code not in known_langs():
                return self._json({'error': 'нет такого словаря'}, 404)
            try:
                with open(os.path.join(LANG_DIR, f'{code}.json'), encoding='utf-8') as f:
                    return self._json({'lang': code, 'words': json.load(f)})
            except (OSError, ValueError) as exc:
                return self._json({'error': f'словарь не читается: {exc}'}, 500)

        if path == '/api/orphans':
            return self._json({'orphans': orphan_dirs()})

        if path == '/api/netcheck':
            return self._json(net_check())

        if path == '/api/activity':
            with ACTIVITY_LOCK:
                out = {}
                for key, item in ACTIVITY.items():
                    score = activity_score(item['samples'])
                    if score:
                        out[key] = score | {'user': item['user']}
                return self._json({'rooms': out, 'watched': len(ACTIVITY_WATCH)})

        if path == '/api/browse':
            query = parse_qs(urlparse(self.path).query)
            return self._json(browse_dirs((query.get('path') or [''])[0]))

        if path == '/api/record':
            with REC_LOCK:
                return self._json({'recordings': [rec_state(k, v)
                                                  for k, v in RECORDINGS.items()]})

        if path == '/api/library':
            return self._json(lib_list())

        if path == '/api/notes':
            with NOTES_LOCK:
                return self._json({'notes': dict(NOTES), 'count': len(NOTES)})

        if path == '/api/about':
            query = parse_qs(urlparse(self.path).query)
            name = (query.get('user') or [''])[0].strip()
            if not name:
                return self.send_error(400, 'expected user')
            return self._json({'user': name, 'text': about_of(name)})

        if path == '/api/ladder':
            query = parse_qs(urlparse(self.path).query)
            name = (query.get('user') or [''])[0].lower()
            with LADDER_LOCK:
                if name:
                    return self._json({'room': LADDER.get(name)})
                return self._json({'rooms': LADDER, 'count': len(LADDER)})

        if path.startswith('/rec/'):
            return self._serve_recording(path)

        if path.startswith('/lib/'):
            return self._serve_library(path)

        return super().do_GET()

    def do_POST(self):
        path = self.path.split('?')[0]

        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            return self.send_error(400, 'bad length')
        if length < 0 or length > MAX_BODY:
            return self.send_error(413, 'bad body size')

        try:
            body = json.loads(self.rfile.read(length) or b'null')
        except ValueError:
            return self.send_error(400, 'bad json')

        if path == '/api/accounts':
            if not isinstance(body, list):
                return self.send_error(400, 'expected array')
            taken, added, total = merge(body)
            self.log_message('accounts: принято %d, новых %d, всего %d', taken, added, total)
            return self._json({'taken': taken, 'added': added, 'total': total})

        if path == '/api/settings':
            if not isinstance(body, dict):
                return self.send_error(400, 'expected object')
            merged = save_settings(body)
            self.log_message('settings: %s', ', '.join(f'{k}={v}' for k, v in body.items()))
            return self._json(merged)

        if path == '/api/log':
            lines = body.get('lines') if isinstance(body, dict) else body
            if not isinstance(lines, list):
                return self.send_error(400, 'expected array')
            write_play_log(lines)
            return self._json({'written': len(lines)})

        if path == '/api/online':
            rows = body.get('live') if isinstance(body, dict) else body
            if not isinstance(rows, list):
                return self.send_error(400, 'expected array')
            src = body.get('src') if isinstance(body, dict) else None
            payload = save_online(rows, 'catalog' if src == 'catalog' else 'sweep')
            self.log_message('online: сохранено %d записей (%s)',
                             len(payload['live']), payload['src'])
            return self._json({'at': payload['at'], 'src': payload['src'],
                               'count': len(payload['live'])})

        if path == '/api/notes':
            if not isinstance(body, dict):
                return self.send_error(400, 'expected object')
            user = body.get('user')
            if not isinstance(user, str) or not user.strip():
                return self.send_error(400, 'expected user')
            text, left = set_note(user.strip(), body.get('text') or '')
            write_log(f'note {"сохранена" if text else "удалена"}: {user.strip()}')
            return self._json({'user': user.strip(), 'text': text, 'count': left})

        if path == '/api/ladder':
            # Комнату, которой сервер ещё не мерил, взвешивает сам плеер —
            # и присылает результат сюда, чтобы следующий зритель получил
            # готовые числа сразу, ещё до первого куска.
            if not isinstance(body, dict):
                return self.send_error(400, 'expected object')
            user = body.get('user')
            edge = str(body.get('edge') or '')
            rates = body.get('rates')
            if not isinstance(user, str) or not user or not isinstance(rates, dict):
                return self.send_error(400, 'expected user and rates')
            if not EDGE_RE.match(edge):
                return self.send_error(400, 'bad edge')

            clean = {}
            for key, value in list(rates.items())[:16]:
                if not re.fullmatch(r'\d{2,5}x\d{2,5}', str(key)):
                    continue
                try:
                    bits = int(value)
                except (TypeError, ValueError):
                    continue
                if 1000 < bits < 200_000_000:      # мусор и переполнение отсекаем
                    clean[str(key)] = bits
            if not clean:
                return self._json({'stored': 0})

            with LADDER_LOCK:
                LADDER[user.lower()] = {'user': user, 'edge': edge,
                                        'at': int(time.time()), 'rates': clean}
            save_ladder()
            write_log(f'ladder: от плеера {user} ({edge}), дорожек {len(clean)}')
            return self._json({'stored': len(clean)})

        if path == '/api/activity':
            # История живёт в localStorage браузера, сервер о ней не знает —
            # поэтому список наблюдаемых ников присылает клиент. Ники разных
            # браузеров складываются, каждый со своим сроком годности.
            names = body.get('users') if isinstance(body, dict) else body
            if not isinstance(names, list):
                return self.send_error(400, 'expected array')
            now = time.time()
            with ACTIVITY_LOCK:
                for name in names[:200]:
                    if isinstance(name, str) and name:
                        ACTIVITY_WATCH[name.lower()] = now
                watched = len(ACTIVITY_WATCH)
            return self._json({'watched': watched})

        if path == '/api/record':
            if not isinstance(body, dict):
                return self.send_error(400, 'expected object')
            user = str(body.get('user') or '')
            edge = str(body.get('edge') or '')
            try:
                max_rate = int(body.get('maxRate') or 0)
            except (TypeError, ValueError):
                max_rate = 0
            try:
                rec_rate = int(body.get('recRate') or 0)
            except (TypeError, ValueError):
                rec_rate = 0
            try:
                segment = int(body.get('segment') or DEFAULT_SEGMENT)
            except (TypeError, ValueError):
                segment = DEFAULT_SEGMENT
            segment = min(max(segment, 5), 900)          # от 5 секунд до 15 минут
            if not USER_RE.match(user) or not edge or not EDGE_RE.match(edge):
                return self.send_error(400, 'bad user or edge')
            try:
                state = rec_start(user, edge, max_rate, segment, rec_rate)
            except Exception as err:                       # сеть, ffmpeg, пустой плейлист
                return self._json({'error': str(err)}, 502)
            self.log_message('record start: %s -> %s', state['id'], state['url'])
            return self._json(state)

        if path in ('/api/orphans/keep', '/api/orphans/drop'):
            rid = str(body.get('id') or '') if isinstance(body, dict) else ''
            if not ID_RE.match(rid) or not os.path.isdir(os.path.join(rec_dir(), rid)):
                return self.send_error(400, 'bad id')

            if path.endswith('/keep'):
                name = merge_folder(rid)
                return self._json({'merged': name} if name else {'error': 'склейка не удалась'},
                                  200 if name else 500)

            shutil.rmtree(os.path.join(rec_dir(), rid), ignore_errors=True)
            self.log_message('orphan dropped: %s', rid)
            return self._json({'dropped': rid})

        if path in ('/api/record/stop', '/api/record/delete'):
            rid = str(body.get('id') or '') if isinstance(body, dict) else ''
            if not ID_RE.match(rid):
                return self.send_error(400, 'bad id')

            if path == '/api/record/delete':
                rec = RECORDINGS.get(rid)
                if rec is not None:
                    rec['discard'] = True          # склейка, если идёт, уберёт за собой
                rec_stop(rid)
                if not rec or rec.get('phase') in (None, 'ready', 'failed'):
                    rec_purge(rid)
                    with REC_LOCK:
                        RECORDINGS.pop(rid, None)
                self.log_message('record delete: %s', rid)
                return self._json({'deleted': rid})

            state = rec_stop(rid)

            self.log_message('record stop: %s', rid)
            return self._json(state or {'id': rid, 'running': False})

        return self.send_error(404)


def orphan_dirs():
    """Каталоги с сегментами, за которыми уже никто не следит.

    Появляются, когда сервер перезапустили посреди записи: ffmpeg погиб вместе
    с ним, а куски остались. Никто их не склеит, пока не попросят.
    """
    found = []
    try:
        entries = sorted(os.scandir(rec_dir()), key=lambda e: e.name)
    except OSError:
        return found

    for entry in entries:
        if not entry.is_dir() or not ID_RE.match(entry.name):
            continue
        rec = RECORDINGS.get(entry.name)
        if rec and rec['proc'].poll() is None:
            continue                                  # пишется прямо сейчас
        try:
            segments = [f for f in os.listdir(entry.path) if f.endswith('.ts')]
        except OSError:
            continue
        if not segments:
            continue
        found.append({
            'id': entry.name,
            'user': entry.name.rsplit('_', 1)[0],
            'segments': len(segments),
            'bytes': rec_size(entry.path),
            'at': int(entry.stat().st_mtime),
        })
    return found


def merge_folder(rid):
    """Склеивает осиротевший каталог. Плейлист может врать (часть сегментов
    уже удалена), поэтому склеиваем прямо по списку файлов на диске."""
    folder = os.path.join(rec_dir(), rid)
    try:
        segments = sorted(f for f in os.listdir(folder) if f.endswith('.ts'))
    except OSError:
        return None
    if not segments:
        return None

    listing = os.path.join(folder, 'concat.txt')
    with open(listing, 'w', encoding='utf-8') as f:
        for name in segments:
            f.write("file '%s'\n" % os.path.join(folder, name))

    target = os.path.join(rec_dir(), rid + '.mp4')
    if os.path.exists(target):                        # первая часть уже склеена
        target = os.path.join(rec_dir(), rid + '_part2.mp4')

    cmd = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
           '-f', 'concat', '-safe', '0', '-i', listing,
           '-c', 'copy', '-bsf:a', 'aac_adtstoasc', '-movflags', '+faststart', target]
    try:
        code = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              stdin=subprocess.DEVNULL, timeout=3600).returncode
    except (OSError, subprocess.SubprocessError):
        code = 1

    if code == 0 and os.path.exists(target) and os.path.getsize(target) > 0:
        shutil.rmtree(folder, ignore_errors=True)
        write_log(f'orphan merged: {os.path.basename(target)}, {os.path.getsize(target)} байт')
        return os.path.basename(target)

    write_log(f'orphan merge FAILED: {rid}')
    return None


CATALOG_URL = 'https://bongacams.com/tools/listing_v3.php'
CATALOG_TAB = os.environ.get('BONGA_CATALOG_TAB', 'female')
CATALOG_PAGE = 500                # за один запрос; проверено — отдаёт и тысячу
CATALOG_UA = ('Mozilla/5.0 (X11; Linux x86_64; rv:153.0) '
              'Gecko/20100101 Firefox/153.0')


def thumb_of(row):
    """Адрес превью из строки листинга.

    Плеер до сих пор угадывал его как mobile-edge<N>.bcvcdn.com/stream_<ник>.jpg
    и обычно попадал: edge публикует кадр рядом с потоком. Но не всегда —
    у venusx1 поток на edge 69, а кадра там нет вовсе, зато он есть на 35.
    Сайт же знает точный адрес и держит превью на отдельном CDN.

    Приходит вида «//i.bgicdn.com/…/c38918.{ext}»: без схемы и с заглушкой
    расширения. Берём webp — он вдвое легче jpg (7.8 КБ против 12), а на
    тысяче с лишним карточек это заметная разница по трафику."""
    raw = str(row.get('thumb_image') or '').strip()
    if not raw or '{ext}' not in raw and not raw.endswith(('.jpg', '.webp')):
        return ''
    url = raw.replace('{ext}', 'webp')
    if url.startswith('//'):
        url = 'https:' + url
    return url if url.startswith('https://') else ''


def fetch_catalog(pages=8):
    """Каталог эфира прямо с сайта, без браузера и без чужих кук.

    Единственное требование эндпоинта — заголовок X-Requested-With: он
    отвечает тем же JSON, что уходит странице при прокрутке списка. Логин
    не нужен, список публичный. Возвращает [[ник, сервер, зрители], …]
    только по тем, кто вещает прямо сейчас, — листинг других и не знает."""
    out, seen = [], set()
    total = 0
    for page in range(pages):
        query = urlencode({'livetab': CATALOG_TAB,
                           'offset': page * CATALOG_PAGE, 'limit': CATALOG_PAGE})
        req = urllib.request.Request(
            f'{CATALOG_URL}?{query}',
            headers={'User-Agent': CATALOG_UA, 'Accept': '*/*',
                     'X-Requested-With': 'XMLHttpRequest'})
        with urllib.request.urlopen(req, timeout=20) as res:
            data = json.loads(res.read())
        if not isinstance(data, dict):
            break
        total = int(data.get('total_count') or 0)
        models = data.get('models') or []
        if not models:
            break
        for row in models:
            user = row.get('username')
            # Американские серверы приходят как «-us10», с ведущим дефисом:
            # он часть имени хоста (mobile-edge-us10), а не номера. Без снятия
            # дефиса треть эфира отсеивалась проверкой EDGE_RE.
            edge = str(row.get('vsid') or '').lstrip('-')
            if not isinstance(user, str) or not user or user.lower() in seen:
                continue
            if not edge:
                # запасной путь: номер сервера прячется и в esid («live-edge74»)
                found = re.search(r'live-edge-?(us\d+|\d+)', str(row.get('esid') or ''))
                edge = found.group(1) if found else ''
            if not EDGE_RE.match(edge):
                continue
            seen.add(user.lower())
            out.append([user, edge, int(row.get('viewers') or 0), 0, thumb_of(row)])
        if len(models) < CATALOG_PAGE or (total and len(seen) >= total):
            break
        time.sleep(0.4)          # без паузы сайт иногда обрывает выдачу на середине
    return out, total


def catalog_worker():
    """Опрос каталога по расписанию. Отдельный поток, а не задача по таймеру
    внутри запроса: список нужен и когда ни одна страница плеера не открыта —
    иначе первый же зашедший увидит устаревший снимок."""
    while True:
        period = load_settings().get('catalog', 60)
        if not period:
            time.sleep(30)                 # опрос выключен — просто ждём включения
            continue
        try:
            rows, total = fetch_catalog()
            if rows:
                _, added, size = merge(rows)
                payload = save_online(rows, 'catalog')
                write_log(f'catalog: в эфире {len(payload["live"])} из {total} '
                          f'по данным сайта, новых ников {added}, в базе {size}')
            else:
                write_log('catalog: сайт вернул пустой список')
        except Exception as exc:           # сеть, разбор, смена формата — не роняем поток
            write_log(f'catalog: не получилось — {type(exc).__name__}: {exc}')
        time.sleep(max(15, period))


# --------------------------------------------------------------------------
# Активность комнаты по колебаниям битрейта
#
# Замеры на живом эфире (2026-08-14) показали: вес куска отражает подвижность
# сцены, и нижняя ступень отвечает на неё резче всех — при небольшой активности
# 240p гуляла в 2.3 раза, тогда как оригинал той же комнаты лишь на 42%.
# Мерить надо поштучно: усреднение по трём кускам стирало у оригинала разброс
# с 42% до 3%, то есть ровно тот сигнал, который мы ищем.
#
# Считает сервер, а не браузеры: наблюдение имеет смысл только непрерывное,
# а вкладку закрывают. Результат общий для всех клиентов.
ACTIVITY_LOCK = threading.Lock()
ACTIVITY = {}                 # ник → {'user','edge','url','seen','samples':[(t, Мбит/с)]}
ACTIVITY_WATCH = {}           # ник → когда клиент в последний раз им интересовался
ACTIVITY_TTL = 1800           # столько ник остаётся под наблюдением без спроса
ACTIVITY_KEEP = 60            # сколько последних кусков помним на комнату
ACTIVITY_POOL = 8


def lowest_variant(user, edge):
    """Адрес плейлиста самой мелкой пережатой дорожки. Она чувствительнее
    прочих, а стоит столько же: HEAD не качает тело."""
    base = f'https://{host_of(edge)}.bcvcdn.com/hls/stream_{urlquote(user)}/'
    req = urllib.request.Request(base + 'playlist.m3u8',
                                 headers={'User-Agent': CATALOG_UA})
    with urllib.request.urlopen(req, timeout=8) as res:
        text = res.read().decode('utf-8', 'replace')

    best = None
    for attrs, rel in re.findall(r'#EXT-X-STREAM-INF:([^\n]+)\n([^\n]+)', text):
        size = re.search(r'RESOLUTION=(\d+)x(\d+)', attrs)
        if not size:
            continue
        pixels = int(size.group(1)) * int(size.group(2))
        if best is None or pixels < best[0]:
            best = (pixels, urljoin(base, rel.strip()))
    return best[1] if best else ''


def sample_activity(entry):
    """Один заход по комнате: сколько весит каждый новый кусок. Куски,
    посчитанные в прошлый раз, пропускаем по имени — окно плейлиста всего
    8 секунд, и при частом опросе они повторяются."""
    try:
        if not entry.get('url'):
            entry['url'] = lowest_variant(entry['user'], entry['edge'])
        if not entry['url']:
            return
        req = urllib.request.Request(entry['url'], headers={'User-Agent': CATALOG_UA})
        with urllib.request.urlopen(req, timeout=8) as res:
            media = res.read().decode('utf-8', 'replace')

        fresh = []
        for secs, name in re.findall(r'#EXTINF:([\d.]+)[^\n]*\n([^#\n]+)', media):
            name = name.strip()
            if name in entry['seen']:
                continue
            head = urllib.request.Request(urljoin(entry['url'], name),
                                          headers={'User-Agent': CATALOG_UA},
                                          method='HEAD')
            with urllib.request.urlopen(head, timeout=8) as res:
                size = int(res.headers.get('Content-Length') or 0)
            if size and float(secs):
                fresh.append((int(time.time()), size * 8 / float(secs) / 1e6))
            entry['seen'].append(name)

        del entry['seen'][:-12]
        entry['samples'] += fresh
        del entry['samples'][:-ACTIVITY_KEEP]
    except Exception:
        entry['url'] = ''          # мог смениться сервер — пересоберём адрес


def activity_score(samples):
    """Оценка по собственной норме комнаты: разрешение и энкодер у всех разные,
    поэтому абсолютные Мбит/с между комнатами несопоставимы.

    spread — размах между десятым и девяностым процентилем в долях медианы:
    насколько сцена вообще шевелится. level — последние куски против медианы:
    происходит ли что-то прямо сейчас."""
    values = sorted(x for _, x in samples)
    if len(values) < 6:
        return None
    mid = values[len(values) // 2]
    if mid <= 0:
        return None
    lo = values[int(len(values) * 0.1)]
    hi = values[int(len(values) * 0.9)]
    recent = [x for _, x in samples[-3:]]
    return {'spread': round((hi - lo) / mid, 2),
            'level': round(sum(recent) / len(recent) / mid, 2),
            'median': round(mid, 2), 'count': len(samples)}


# --------------------------------------------------------------------------
# Замеренные битрейты дорожек по комнатам
#
# Плеер меряет лесенку сам, но только после первого куска — а до тех пор
# решения принимаются по заявленному, где у 720p бывает 3.4 при настоящих
# 0.65. Особенно заметно на первой комнате после перезагрузки страницы:
# кэша нет, окно шире, и подбор успевает уронить качество.
#
# Поэтому сервер меряет то же самое заранее и хранит на диске: плеер получает
# готовые числа до того, как загрузит первый кусок.
LADDER_LOCK = threading.Lock()
LADDER = {}                   # ник → {'edge','at','rates':{'1920x1080': бит/с}}
LADDER_KEEP = 3000            # комнат в хранилище; дальше вытесняем старые


def load_ladder():
    try:
        with open(LADDER_STORE, encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_ladder():
    """Пишем через временный файл: сервер перезапускают на ходу, и оборванная
    запись оставила бы битый json."""
    with LADDER_LOCK:
        rows = sorted(LADDER.items(), key=lambda kv: -(kv[1].get('at') or 0))
        data = dict(rows[:LADDER_KEEP])
        LADDER.clear()
        LADDER.update(data)
    try:
        tmp = LADDER_STORE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, separators=(',', ':'))
        os.replace(tmp, LADDER_STORE)
    except OSError:
        pass


def measure_ladder(user, edge):
    """Настоящий вес каждой дорожки комнаты. Видео не качаем: длину куска
    отдаёт Content-Length, длительность лежит в плейлисте рядом с #EXTINF.

    Ключ — разрешение: по нему плеер и сопоставляет свои дорожки, а номер в
    списке для этого не годится (порядок задаётся заявленным битрейтом)."""
    base = f'https://{host_of(edge)}.bcvcdn.com/hls/stream_{urlquote(user)}/'
    req = urllib.request.Request(base + 'playlist.m3u8',
                                 headers={'User-Agent': CATALOG_UA})
    with urllib.request.urlopen(req, timeout=10) as res:
        top = res.read().decode('utf-8', 'replace')

    def weigh(pair):
        """Вес одной дорожки. Запросы идут вперемешку: до заокеанских серверов
        каждый обходится в полсекунды, и последовательный обход занимал
        восемнадцать секунд вместо двух."""
        resolution, rel = pair
        media_url = urljoin(base, rel)
        try:
            req = urllib.request.Request(media_url, headers={'User-Agent': CATALOG_UA})
            with urllib.request.urlopen(req, timeout=10) as res:
                media = res.read().decode('utf-8', 'replace')
            parts = re.findall(r'#EXTINF:([\d.]+)[^\n]*\n([^#\n]+)', media)[-3:]
            if not parts:
                return None

            def size_of(name):
                head = urllib.request.Request(urljoin(media_url, name.strip()),
                                              headers={'User-Agent': CATALOG_UA},
                                              method='HEAD')
                with urllib.request.urlopen(head, timeout=10) as res:
                    return int(res.headers.get('Content-Length') or 0)

            with ThreadPoolExecutor(len(parts)) as pool:
                total = sum(pool.map(size_of, [name for _, name in parts]))
            seconds = sum(float(secs) for secs, _ in parts)
            return (resolution, int(total * 8 / seconds)) if total and seconds else None
        except Exception:
            return None                   # одна дорожка не ответила — не беда

    variants = []
    for attrs, rel in re.findall(r'#EXT-X-STREAM-INF:([^\n]+)\n([^\n]+)', top):
        size = re.search(r'RESOLUTION=(\d+x\d+)', attrs)
        if size:
            variants.append((size.group(1), rel.strip()))
    if not variants:
        return {}

    with ThreadPoolExecutor(len(variants)) as pool:
        return dict(x for x in pool.map(weigh, variants) if x)


def ladder_worker():
    """Обновление замеров по расписанию. Берём те же комнаты, за которыми
    следит детектор активности, — это ники из истории, то есть ровно те, куда
    пользователь заходит."""
    while True:
        period = load_settings().get('ladder', 43200)
        if not period:
            time.sleep(60)
            continue

        with ACTIVITY_LOCK:
            wanted = set(ACTIVITY_WATCH)
        live = [(row[0], str(row[1])) for row in load_online().get('live', [])
                if row[0].lower() in wanted]

        now = int(time.time())
        stale = []
        with LADDER_LOCK:
            for user, edge in live:
                item = LADDER.get(user.lower())
                if not item or item.get('edge') != edge or now - (item.get('at') or 0) >= period:
                    stale.append((user, edge))

        if stale:
            def one(pair):
                user, edge = pair
                try:
                    rates = measure_ladder(user, edge)
                except Exception:
                    return
                if not rates:
                    return
                with LADDER_LOCK:
                    LADDER[user.lower()] = {'user': user, 'edge': edge,
                                            'at': int(time.time()), 'rates': rates}

            with ThreadPoolExecutor(min(4, len(stale))) as pool:
                list(pool.map(one, stale[:40]))
            save_ladder()
            write_log(f'ladder: обновлено комнат {min(len(stale), 40)}, '
                      f'в хранилище {len(LADDER)}')
        # Круг делаем частым независимо от периода: период задаёт срок годности
        # замера, а не паузу между проверками. Иначе при неделе новая комната
        # в истории ждала бы своего первого замера сорок часов.
        time.sleep(60)


def activity_worker():
    while True:
        period = load_settings().get('act', 15)
        if not period:
            time.sleep(30)
            continue

        now = time.time()
        with ACTIVITY_LOCK:
            for key, asked in list(ACTIVITY_WATCH.items()):
                if now - asked > ACTIVITY_TTL:
                    ACTIVITY_WATCH.pop(key, None)
                    ACTIVITY.pop(key, None)
            wanted = set(ACTIVITY_WATCH)

        # Опрашиваем только тех, кто сейчас вещает: у ушедшей комнаты
        # плейлиста нет, и каждый заход стоил бы таймаута.
        live = {}
        for row in load_online().get('live', []):
            if row[0].lower() in wanted:
                live[row[0].lower()] = (row[0], str(row[1]))

        with ACTIVITY_LOCK:
            for key in list(ACTIVITY):
                if key not in live:
                    ACTIVITY.pop(key)
            for key, (user, edge) in live.items():
                item = ACTIVITY.get(key)
                if not item or item['edge'] != edge:
                    ACTIVITY[key] = {'user': user, 'edge': edge, 'url': '',
                                     'seen': [], 'samples': []}
            batch = list(ACTIVITY.values())

        if batch:
            with ThreadPoolExecutor(min(ACTIVITY_POOL, len(batch))) as pool:
                list(pool.map(sample_activity, batch))
        time.sleep(max(5, period))


def reaper():
    """Эфир может кончиться сам — тогда ffmpeg выходит, и склейку надо завести."""
    while True:
        time.sleep(15)
        for rid, rec in list(RECORDINGS.items()):
            if rec['proc'].poll() is not None and 'phase' not in rec:
                write_log(f'record ended by source: {rid}')
                rec_stop(rid)


if __name__ == '__main__':
    os.makedirs(REC_DIR, exist_ok=True)
    threading.Thread(target=reaper, daemon=True).start()
    threading.Thread(target=catalog_worker, daemon=True).start()
    threading.Thread(target=activity_worker, daemon=True).start()
    LADDER.update(load_ladder())
    NOTES.update(load_notes())
    threading.Thread(target=ladder_worker, daemon=True).start()
    print(f'Плеер:  http://127.0.0.1:{PORT}/player.html')
    print(f'Записи: {rec_dir()} (логи всегда в {REC_DIR})')
    try:
        ThreadingHTTPServer(('0.0.0.0', PORT), Handler).serve_forever()
    finally:
        for rid in list(RECORDINGS):                       # не бросаем ffmpeg сиротами
            rec_stop(rid)
