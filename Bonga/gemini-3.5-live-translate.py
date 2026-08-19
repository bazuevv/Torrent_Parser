#!/usr/bin/env python3
"""Живой перевод речи эфиров через Gemini Live API (live-translate).

Модель gemini-3.5-live-translate-preview принимает непрерывный поток речи
и отдаёт перевод в реальном времени. Сейчас файл работает демо-режимом:
любое видео/аудио ffmpeg превращает в PCM16 mono 16 кГц и стримит в
модель, а в консоль падают оригинал (input_transcription) и перевод
(output_transcription) с меткой офсета отправленного звука.

    .venv/bin/python gemini-3.5-live-translate.py запись.mp4 --start 60 --duration 90
    .venv/bin/python gemini-3.5-live-translate.py запись.mp4 --speed 4

Ключ в репо не хранится: берётся из окружения GEMINI_API_KEY (или
GOOGLE_API_KEY). Get API key: aistudio.google.com → Get API key.

Ограничения Live API, учтённые в коде:
- сессия живёт ~10 минут. Сервер заранее присылает go_away — в этот
  момент переподключаемся с session_resumption (handle приходит в
  session_resumption_update и постоянно обновляется), звук не теряем:
  насос помнит, сколько байт подтверждено отправкой;
- вход строго PCM16 LE mono 16 кГц (audio/pcm;rate=16000). Переведённое
  аудио модель синтезирует в 24 кГц — оно нам не нужно, субтитры берём
  из текстовой транскрипции выхода;
- response_modalities у native-audio модели только AUDIO, текста без
  звука не бывает. echo_target_language=True: если модель уже говорит
  по-русски, её речь эхом возвращается транскрипцией — субтитры будут
  и для русскоязычных эфиров.
"""

import argparse
import asyncio
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


class PcmPump:
    """Насос PCM из ffmpeg с памятью позиции — переживает реконнекты.

    Чтение из pipe блокирующее, поэтому выполняем его в отдельном потоке
    (asyncio.to_thread). Чанк, прочитанный перед разрывом связи, но не
    подтверждённый отправкой, будет отдан повторно: дубль лучше дыры —
    модель стерпит повторные 100 мс, а пропущенный кусок фразы — нет.
    """

    def __init__(self, cmd):
        self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE,
                                      stdin=subprocess.DEVNULL)
        self._held = None                 # прочитан, но ещё не отправлен
        self._eof = False
        self.eof_at = None                # момент конца звука (для дренажа)
        self.sent = 0                     # байт, подтверждённых отправкой

    async def next_chunk(self):
        """Очередной чанк для отправки либо None, если звук кончился."""
        if self._held is None and not self._eof:
            data = await asyncio.to_thread(self._proc.stdout.read, CHUNK_BYTES)
            if data:
                self._held = data
            else:
                self._eof = True
                self.eof_at = time.monotonic()
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

    @property
    def eof(self):
        return self._eof and self._held is None

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


def mmss(seconds):
    return f'{seconds // 60:02.0f}:{seconds % 60:02.0f}'


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


async def send_loop(session, pump, flow, speed, started):
    """Льёт звук в сессию с пейсингом реального времени.

    flow сбрасывается на время реконнекта — отправка замирает, пока не
    появится живая сессия. speed > 1 отправляет быстрее реального времени:
    для демо-прогона записи это сокращает ожидание, модель переваривает.
    """
    while True:
        chunk = await pump.next_chunk()
        if chunk is None:
            return
        await flow.wait()
        await session.send_realtime_input(
            audio=types.Blob(data=chunk, mime_type=f'audio/pcm;rate={RATE}'))
        pump.confirm()
        # Не вырываемся вперёд звука: живой эфир приходит в темпе 1x,
        # а при обгоне буферы модели и латентность растут.
        target = pump.sent / BYTES_PER_SEC / speed
        pause = target - (time.monotonic() - started)
        if pause > 0:
            await asyncio.sleep(pause)


async def run(args):
    key = (args.key
           or os.environ.get('GEMINI_API_KEY')
           or os.environ.get('GOOGLE_API_KEY')
           or FILE_KEY)
    if not key:
        sys.exit('Нет ключа. Порядок поиска: --key, переменные GEMINI_API_KEY/'
                 'GOOGLE_API_KEY, Bonga/config_secrets.py (GEMINI_API_KEY = \'...\'). '
                 'Ключ: aistudio.google.com → Get API key')

    client = genai.Client(api_key=key)
    pump = PcmPump(ffmpeg_cmd(args.file, args.start, args.duration))
    console = Console()
    flow = asyncio.Event()
    flow.set()
    handle = None                         # для session_resumption
    started = time.monotonic()

    while True:
        config = types.LiveConnectConfig(
            response_modalities=[types.Modality.AUDIO],
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            translation_config=types.TranslationConfig(
                target_language_code=args.target,
                echo_target_language=True),
            # Без handle поле не заполняем: первый вход — новая сессия.
            **({'session_resumption': types.SessionResumptionConfig(handle=handle)}
               if handle else {}),
        )
        reconnect = False
        sender = None
        try:
            async with client.aio.live.connect(model=MODEL, config=config) as session:
                print('[подключено]', file=sys.stderr)
                sender = asyncio.create_task(
                    send_loop(session, pump, flow, args.speed, started))
                # Приём обычным async for: даже в полной тишине сервер шлёт
                # пустые server_content ~3 раза в секунду (замер 2026-08-19),
                # так что цикл живёт и проверки внутри него срабатывают.
                # «Ждать тишины» для выхода нельзя — пустые сообщения не
                # кончаются; выходим по дренажу: EOF + GRACE секунд.
                # Отменять __anext__ по таймауту нельзя: wait_for рвёт
                # внутренний конвейер приёма, и сессия выглядит «закрытой
                # сервером» (прецедент: демо реконнектилось в цикле).
                async for msg in session.receive():
                    if pump.eof and time.monotonic() - pump.eof_at > GRACE_AFTER_EOF:
                        break

                    upd = msg.session_resumption_update
                    if upd and upd.new_handle:
                        handle = upd.new_handle

                    if msg.go_away:
                        # Сервер закроет сессию: уходим сами, но по-хорошему —
                        # с сохранённым handle следующий цикл продолжит разговор.
                        print('\n[сессия закрывается, переподключаюсь]',
                              file=sys.stderr)
                        reconnect = not pump.eof
                        break

                    sc = msg.server_content
                    if sc is None:
                        continue
                    if sc.input_transcription and sc.input_transcription.text:
                        console.delta('orig', sc.input_transcription.text)
                    if sc.output_transcription and sc.output_transcription.text:
                        console.delta('ru', sc.output_transcription.text)
                    if sc.turn_complete:
                        console.turn_done()
                else:
                    # Приём кончился сам, без go_away и без конца звука —
                    # сервер закрыл сессию молча. Продолжаем с того же места.
                    if not pump.eof:
                        print('\n[сессия закрыта сервером, переподключаюсь]',
                              file=sys.stderr)
                        reconnect = True
        except Exception as err:                    # сеть, шлюз, квота
            if pump.eof:
                print(f'\n[обрыв в конце потока: {type(err).__name__}: {err}]',
                      file=sys.stderr)
                break
            print(f'\n[обрыв: {type(err).__name__}: {err}, переподключаюсь]',
                  file=sys.stderr)
            reconnect = True

        if sender is not None:
            sender.cancel()
            try:
                await sender
            except (asyncio.CancelledError, Exception):
                pass                        # причина уже в логе выше
        if not reconnect:
            break
        # Порядок: сперва flow.clear() — отправка замрёт до новой сессии.
        flow.clear()
        await asyncio.sleep(RECONNECT_BACKOFF)
        flow.set()

    pump.close()


def main():
    ap = argparse.ArgumentParser(
        description='Живой перевод звуковой дорожки через Gemini Live API.')
    ap.add_argument('file', help='видео/аудио файл (любой формат ffmpeg)')
    ap.add_argument('--start', type=float, default=0, help='с какой секунды (по умолчанию 0)')
    ap.add_argument('--duration', type=float, default=0, help='сколько секунд (0 = до конца)')
    ap.add_argument('--speed', type=float, default=1.0, help='темп отправки, 1 = реальное время')
    ap.add_argument('--target', default='ru', help='целевой язык перевода (по умолчанию ru)')
    ap.add_argument('--key', help='API-ключ; иначе переменные GEMINI_API_KEY/GOOGLE_API_KEY')
    args = ap.parse_args()
    if not os.path.isfile(args.file):
        sys.exit(f'нет файла: {args.file}')
    asyncio.run(run(args))


if __name__ == '__main__':
    main()
