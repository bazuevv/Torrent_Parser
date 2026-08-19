#!/usr/bin/env python3
"""Живой перевод речи эфиров через Gemini Live API (live-translate).

Модель gemini-3.5-live-translate-preview принимает непрерывный поток речи
и отдаёт перевод в реальном времени. Два режима работы.

Демо — файл в консоль (оригинал и перевод построчно):

    .venv/bin/python gemini-3.5-live-translate.py запись.mp4 --start 60 --duration 90
    .venv/bin/python gemini-3.5-live-translate.py запись.mp4 --speed 4

Хаб — WebSocket-сервер для плеера:

    .venv/bin/python gemini-3.5-live-translate.py --serve

Браузер подключается к ws://<host>:8778 и шлёт текстовые JSON-команды
и бинарные чанки PCM16 LE mono 16 кГц, в ответ получает события:

    → {"type":"start","target":"ru"}   начать/сбросить перевод
    → {"type":"stop"}                  остановить перевод
    → бинарный кадр                    очередной чанк звука
    ← {"type":"orig","text":"..."}     дельта транскрипции оригинала
    ← {"type":"ru","text":"..."}       дельта перевода
    ← {"type":"phrase_end"}            конец реплики (turn_complete)
    ← {"type":"status","text":"..."}   служебное: ready/обрыв/переподключение

Перевод один на всех: сессию заводит «start», звук кормит любой клиент
(последний приславший), субтитры летят всем подключённым. Уход клиента,
который кормил звук, останавливает перевод — без источника он бессмыслен.

Ключ в репо не хранится, порядок поиска: --key, переменные окружения
GEMINI_API_KEY/GOOGLE_API_KEY, Bonga/config_secrets.py (GEMINI_API_KEY =
'...'). Get API key: aistudio.google.com.

Ограничения Live API, учтённые в коде (замеры 2026-08-19):
- сессия живёт ~10 минут. Сервер заранее присылает go_away — перепод-
  ключаемся с session_resumption (handle приходит в
  session_resumption_update и постоянно обновляется). На время разрыва
  звук копится в очереди; переполнение — выкидываем старейшие чанки:
  перевод отсталого куска уже никому не нужен;
- вход строго PCM16 LE mono 16 кГц (audio/pcm;rate=16000). Переведённое
  аудио модель синтезирует в 24 кГц — оно нам не нужно, субтитры берём
  из текстовой транскрипции выхода;
- response_modalities у native-audio модели только AUDIO.
  echo_target_language=True: если модель уже говорит по-русски, её речь
  эхом возвращается транскрипцией — субтитры будут и для русскоязычных
  эфиров;
- после конца речи сервер бесконечно шлёт пустые server_content (~3/с):
  «ждать тишины» для выхода нельзя. И отменять приём по таймауту нельзя —
  wait_for рвёт конвейер приёма SDK, и сессия выглядит «закрытой серве-
  ром» (прецедент: демо реконнектилось в цикле, пока не убрали таймаут).
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time

from google import genai
from google.genai import types

try:
    # Локальные ключи из Bonga/config_secrets.py — файл в .gitignore.
    from config_secrets import GEMINI_API_KEY as FILE_KEY
except ImportError:
    FILE_KEY = None

MODEL = 'gemini-3.5-live-translate-preview'
RATE = 16000
BYTES_PER_SEC = RATE * 2                 # 16 бит * 1 канал
CHUNK_BYTES = BYTES_PER_SEC // 10        # 100 мс — как в официальных примерах
GRACE_AFTER_EOF = 25                     # сколько секунд ждать хвост перевода
RECONNECT_BACKOFF = 2                    # пауза перед повторным подключением, с
QUEUE_CHUNKS = 300                       # буфер звука на разрыв: 300×100 мс = 30 с


# --------------------------------------------------------------------------
# Общее
# --------------------------------------------------------------------------

def resolve_key(args):
    key = (args.key
           or os.environ.get('GEMINI_API_KEY')
           or os.environ.get('GOOGLE_API_KEY')
           or FILE_KEY)
    if not key:
        sys.exit('Нет ключа. Порядок поиска: --key, переменные GEMINI_API_KEY/'
                 'GOOGLE_API_KEY, Bonga/config_secrets.py (GEMINI_API_KEY = \'...\'). '
                 'Ключ: aistudio.google.com → Get API key')
    return key


# --------------------------------------------------------------------------
# Живой перевод: одна сессия Gemini с реконнектами
# --------------------------------------------------------------------------

class Translator:
    """Сессия Gemini-перевода: кормим звуком, забираем транскрипции.

    Один экземпляр — один перевод. start() поднимает фоновый цикл подклю-
    чения, feed() кладёт PCM-чанки в очередь, колбэки получают дельты
    текста. go_away и тихие закрытия сервера лечатся session_resumption;
    на время разрыва звук копится в очереди. stop() гасит всё; start()
    после stop() заводит сессию заново, с чистого листа.
    """

    def __init__(self, client, target='ru',
                 on_orig=None, on_ru=None, on_phrase=None, on_status=None):
        self._client = client
        self._target = target
        self._on_orig = on_orig or (lambda text: None)
        self._on_ru = on_ru or (lambda text: None)
        self._on_phrase = on_phrase or (lambda: None)
        self._on_status = on_status or (lambda text: None)
        self._queue = asyncio.Queue(maxsize=QUEUE_CHUNKS)
        self._handle = None             # resumption-хэндл живой сессии
        self._task = None
        self._stopping = False

    def feed(self, pcm):
        """Чанк PCM16 LE mono 16 кГц; неблокирующий."""
        try:
            self._queue.put_nowait(pcm)
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self._queue.put_nowait(pcm)
            except asyncio.QueueFull:
                pass

    async def start(self):
        if self._task is not None:
            return
        self._stopping = False
        self._task = asyncio.create_task(self._loop())

    async def stop(self):
        self._stopping = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._handle = None
        while not self._queue.empty():
            self._queue.get_nowait()

    def _config(self):
        extra = {}
        if self._handle:
            # Без handle поле не заполняем: первый вход — новая сессия.
            extra['session_resumption'] = types.SessionResumptionConfig(
                handle=self._handle)
        return types.LiveConnectConfig(
            response_modalities=[types.Modality.AUDIO],
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            translation_config=types.TranslationConfig(
                target_language_code=self._target,
                echo_target_language=True),
            **extra)

    async def _send_loop(self, session):
        while True:
            chunk = await self._queue.get()
            # Ошибка отправки = сессия умерла. Чанк (100 мс) теряем —
            # переподключением займётся внешний цикл; возвращать его в
            # очередь нет способа без перекоса порядка.
            await session.send_realtime_input(
                audio=types.Blob(data=chunk, mime_type=f'audio/pcm;rate={RATE}'))

    async def _loop(self):
        while not self._stopping:
            sender = None
            try:
                async with self._client.aio.live.connect(
                        model=MODEL, config=self._config()) as session:
                    sender = asyncio.create_task(self._send_loop(session))
                    self._on_status('ready')
                    async for msg in session.receive():
                        if self._stopping:
                            break

                        upd = msg.session_resumption_update
                        if upd and upd.new_handle:
                            self._handle = upd.new_handle

                        if msg.go_away:
                            # Сервер закроет сессию: уходим сами, но по-хорошему —
                            # с сохранённым handle следующий виток продолжит.
                            self._on_status('сессия закрывается, переподключаюсь')
                            break

                        sc = msg.server_content
                        if sc is None:
                            continue
                        if sc.input_transcription and sc.input_transcription.text:
                            self._on_orig(sc.input_transcription.text)
                        if sc.output_transcription and sc.output_transcription.text:
                            self._on_ru(sc.output_transcription.text)
                        if sc.turn_complete:
                            self._on_phrase()
                    else:
                        # Приём кончился сам, без go_away — сервер закрыл
                        # сессию молча. Переподключаемся с resumption.
                        if not self._stopping:
                            self._on_status('сессия закрыта сервером, '
                                            'переподключаюсь')
            except asyncio.CancelledError:
                raise                       # это stop() — выходим тихо
            except Exception as err:        # сеть, шлюз, квота
                if not self._stopping:
                    self._on_status(f'обрыв: {type(err).__name__}: {err}')
            finally:
                if sender is not None:
                    sender.cancel()
                    if not self._stopping:
                        try:
                            await sender
                        except asyncio.CancelledError:
                            pass
                        except Exception:
                            pass
            if not self._stopping:
                await asyncio.sleep(RECONNECT_BACKOFF)


# --------------------------------------------------------------------------
# Демо-режим: файл → консоль
# --------------------------------------------------------------------------

class PcmPump:
    """Насос PCM из ffmpeg с памятью позиции.

    Чтение из pipe блокирующее, поэтому выполняем его в отдельном потоке
    (asyncio.to_thread). Позиция учитывается только после подтверждённой
    отправки — чанк, прочитанный до сбоя, будет отдан повторно: дубль
    лучше дыры, а пропущенный кусок фразы — нет.
    """

    def __init__(self, cmd):
        self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE,
                                      stdin=subprocess.DEVNULL)
        self._held = None                 # прочитан, но ещё не отправлен
        self._eof = False
        self.sent = 0                     # байт, подтверждённых отправкой

    async def next_chunk(self):
        """Очередной чанк для отправки либо None, если звук кончился."""
        if self._held is None and not self._eof:
            data = await asyncio.to_thread(self._proc.stdout.read, CHUNK_BYTES)
            if data:
                self._held = data
            else:
                self._eof = True
                err = self._proc.stderr.read().decode('utf-8', 'replace')[-400:]
                if err:
                    print(f'ffmpeg: {err}', file=sys.stderr)
                if self.sent == 0:
                    raise RuntimeError('звуковой дорожки нет или она пуста')
        return self._held

    def confirm(self):
        """Вызывать только после успешной отправки чанка."""
        self.sent += len(self._held)
        self._held = None

    def close(self):
        for stream in (self._proc.stdout, self._proc.stderr):
            try:
                stream.close()
            except OSError:
                pass
        self._proc.terminate()


def ffmpeg_cmd(path, start, duration):
    cmd = ['ffmpeg', '-v', 'error', '-nostdin']
    if start:
        cmd += ['-ss', str(start)]
    cmd += ['-i', path]
    if duration:
        cmd += ['-t', str(duration)]
    return cmd + ['-map', '0:a:0?', '-ac', '1', '-ar', str(RATE),
                  '-sample_fmt', 's16', '-f', 's16le', '-']


class Console:
    """Печать потока транскрипций: дельта — строка, реплика — блок.

    Дельты приходят кусками внутри реплики, конец реплики — turn_complete.
    Печатаем каждую дельту отдельной строкой: видно, как перевод подрастает
    и с какой задержкой. На turn_complete — пустая строка-разделитель.
    """

    def delta(self, kind, text):
        mark = 'ОР≫' if kind == 'orig' else 'RU≫'
        print(f'{mark} {text}', flush=True)

    def turn_done(self):
        print(flush=True)


async def run(args):
    client = genai.Client(api_key=resolve_key(args))
    console = Console()
    tr = Translator(client, target=args.target,
                    on_orig=lambda t: console.delta('orig', t),
                    on_ru=lambda t: console.delta('ru', t),
                    on_phrase=console.turn_done,
                    on_status=lambda s: print(f'[{s}]', file=sys.stderr))
    pump = PcmPump(ffmpeg_cmd(args.file, args.start, args.duration))
    await tr.start()
    started = time.monotonic()
    sent = 0
    try:
        while True:
            chunk = await pump.next_chunk()
            if chunk is None:
                break
            # Темп реального времени: живой эфир приходит 1x, обгонять
            # незачем — очередь внутри Translator раздуется.
            pause = ((sent + len(chunk)) / BYTES_PER_SEC / args.speed
                     - (time.monotonic() - started))
            if pause > 0:
                await asyncio.sleep(pause)
            tr.feed(chunk)
            pump.confirm()
            sent += len(chunk)
        # Хвост: модель дописывает перевод после конца звука.
        await asyncio.sleep(GRACE_AFTER_EOF)
    except RuntimeError as err:
        sys.exit(str(err))
    finally:
        await tr.stop()
        pump.close()


# --------------------------------------------------------------------------
# Режим хаба: WebSocket-сервер для плеера
# --------------------------------------------------------------------------

async def serve_hub(args):
    import websockets
    try:
        from websockets.asyncio.server import serve as ws_serve   # websockets ≥ 13
    except ImportError:
        from websockets import serve as ws_serve                  # старые версии

    client = genai.Client(api_key=resolve_key(args))
    clients = set()                      # все подключённые браузеры
    box = {'tr': None, 'owner': None}    # текущий перевод и кто кормит звук

    async def _send_safe(ws, message):
        try:
            await ws.send(message)
        except Exception:
            pass                         # мёртвый клиент выпадет сам в handler'е

    def broadcast(kind, **fields):
        message = json.dumps({'type': kind, **fields}, ensure_ascii=False)
        for ws in list(clients):
            asyncio.create_task(_send_safe(ws, message))

    async def stop_translator():
        tr, box['tr'] = box['tr'], None
        box['owner'] = None
        if tr is not None:
            await tr.stop()

    async def handler(ws):
        clients.add(ws)
        try:
            async for message in ws:
                if isinstance(message, (bytes, bytearray)):
                    if box['tr'] is not None:
                        box['owner'] = ws
                        box['tr'].feed(bytes(message))
                    continue
                try:
                    msg = json.loads(message)
                except ValueError:
                    continue
                kind = msg.get('type')
                if kind == 'start':
                    # start = и «начать», и «сбросить» (смена комнаты в плеере
                    # должна начинать перевод с чистого листа).
                    await stop_translator()
                    box['tr'] = Translator(
                        client, target=msg.get('target') or 'ru',
                        on_orig=lambda t: broadcast('orig', text=t),
                        on_ru=lambda t: broadcast('ru', text=t),
                        on_phrase=lambda: broadcast('phrase_end'),
                        on_status=lambda s: broadcast('status', text=s))
                    await box['tr'].start()
                elif kind == 'stop':
                    await stop_translator()
        except websockets.ConnectionClosed:
            pass
        finally:
            clients.discard(ws)
            # Ушёл кормивший звук или вообще все — переводить нечего.
            if ws is box['owner'] or not clients:
                await stop_translator()

    async with ws_serve(handler, args.host, args.port, max_size=1 << 20):
        print(f'хаб перевода слушает ws://{args.host}:{args.port}', file=sys.stderr)
        await asyncio.Future()           # пока не остановят Ctrl-C


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description='Живой перевод звуковой дорожки через Gemini Live API.')
    ap.add_argument('file', nargs='?',
                    help='видео/аудио файл для демо-режима (любой формат ffmpeg)')
    ap.add_argument('--serve', action='store_true',
                    help='режим хаба: WebSocket-сервер для плеера')
    ap.add_argument('--host', default='0.0.0.0',
                    help='адрес хаба (по умолчанию вся LAN, как у server.py)')
    ap.add_argument('--port', type=int, default=8778,
                    help='порт хаба (по умолчанию 8778, соседний с плеером)')
    ap.add_argument('--start', type=float, default=0, help='с какой секунды (по умолчанию 0)')
    ap.add_argument('--duration', type=float, default=0, help='сколько секунд (0 = до конца)')
    ap.add_argument('--speed', type=float, default=1.0, help='темп отправки, 1 = реальное время')
    ap.add_argument('--target', default='ru', help='целевой язык перевода (по умолчанию ru)')
    ap.add_argument('--key', help='API-ключ; иначе переменные GEMINI_API_KEY/GOOGLE_API_KEY')
    args = ap.parse_args()

    if args.serve:
        asyncio.run(serve_hub(args))
        return
    if not args.file:
        ap.error('укажите файл или --serve')
    if not os.path.isfile(args.file):
        sys.exit(f'нет файла: {args.file}')
    asyncio.run(run(args))


if __name__ == '__main__':
    main()
