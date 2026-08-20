#!/usr/bin/env python3
"""Отладка классификатора речи/музыки на записях.

Гоняет YAMNet по видео/аудио файлу ровно так, как хаб гоняет его по живому
эфиру (окно 0.96 с, шаг 0.48 с), и показывает таймлайн вердиктов с суммами
групп — чтобы подбирать параметры решающего правила не на непредсказуемых
трансляциях, а на записях с известным содержанием:

    .venv/bin/python kind-debug.py запись.mp4                  # сводка сегментов
    .venv/bin/python kind-debug.py запись.mp4 --timeline       # каждый шаг
    .venv/bin/python kind-debug.py запись.mp4 --margin 1.5 --smooth 5

Крутилки:
  --speech-min  абсолютный порог суммы речевых классов (как в хабе, 0.25):
                ниже — музыка/пение, выше — речь. Не «у кого больше», потому
                что фоновая музыка под речью делит скор с речью
  --quiet-rms   порог тишины (пауза между фразами — не музыка), как в хабе
  --smooth      усреднять скоры по N кадрам перед решением (1 = как в хабе)
  --hold        сколько кадров подряд должны соглашаться для смены вердикта
                (3 = как в хабе)
"""

import argparse
import csv
import os
import subprocess
import sys

import numpy as np
import onnxruntime as ort

RATE = 16000
KIND_WIN = RATE * 96 // 100
KIND_HOP = RATE * 48 // 100

SPEECH = {0, 1, 2, 3}               # Speech, Child speech, Conversation, Narration
SING = {24, 25, 26, 27, 28, 29, 30, 31, 32, 35, 250}
MUSIC = {132, 133}                  # Music, Musical instrument
QUIET_RMS = 0.012
SPEECH_MIN = 0.25

HERE = os.path.dirname(os.path.abspath(__file__))


def load():
    sess = ort.InferenceSession(os.path.join(HERE, 'models', 'yamnet.onnx'),
                                providers=['CPUExecutionProvider'])
    names = [r['display_name'] for r in
             csv.DictReader(open(os.path.join(HERE, 'models', 'yamnet_class_map.csv'),
                                 encoding='utf-8'))]
    return sess, names


def verdicts(path, sess, args):
    """Генератор (сек, rms, суммы_групп) по каждому шагу окна."""
    proc = subprocess.Popen(
        ['ffmpeg', '-v', 'error', '-ss', str(args.start), '-i', path]
        + (['-t', str(args.duration)] if args.duration else [])
        + ['-map', '0:a?', '-ac', '1', '-ar', str(RATE), '-f', 's16le', '-'],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    buf = bytearray()
    tail = []                          # последние N кадров скоров для сглаживания
    while True:
        chunk = proc.stdout.read(KIND_HOP * 2)
        if not chunk:
            break
        buf += chunk
        while len(buf) >= KIND_WIN * 2:
            window = np.frombuffer(bytes(buf[:KIND_WIN * 2]), np.int16)
            del buf[:KIND_HOP * 2]
            x = window.astype(np.float32) / 32768
            rms = float(np.sqrt(np.mean(x * x)))
            scores = sess.run(None, {'waveform': x})[0].mean(axis=0)
            tail.append(scores)
            tail = tail[-args.smooth:]
            s = np.mean(tail, axis=0)
            yield ((len(verdicts.seen) * KIND_HOP / RATE, rms,
                    float(sum(s[i] for i in SPEECH)),
                    float(sum(s[i] for i in SING)),
                    float(sum(s[i] for i in MUSIC))))
            verdicts.seen.append(1)
    proc.stdout.close()
    proc.terminate()


verdicts.seen = []


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('file')
    ap.add_argument('--start', type=float, default=0, help='с какой секунды (по умолчанию 0)')
    ap.add_argument('--duration', type=float, default=0, help='сколько секунд (0 = до конца)')
    ap.add_argument('--timeline', action='store_true', help='каждый шаг, не только смены')
    ap.add_argument('--speech-min', type=float, default=SPEECH_MIN)
    ap.add_argument('--quiet-rms', type=float, default=QUIET_RMS)
    ap.add_argument('--smooth', type=int, default=1)
    ap.add_argument('--hold', type=int, default=3)
    ap.add_argument('--top', action='store_true', help='показывать топ-классы кадра')
    args = ap.parse_args()

    sess, names = load()
    kind, run_start = 'speech', 0.0
    votes = []
    totals = {}
    for sec, rms, sp, si, mu in verdicts(args.file, sess, args):
        if rms < args.quiet_rms:
            frame = 'quiet'
        else:
            # Как в хабе: абсолютный порог речи, дальше пение против музыки.
            if sp >= args.speech_min:
                frame = 'speech'
            elif si >= mu and si >= 0.1:
                frame = 'singing'
            else:
                frame = 'music'
        votes.append(frame)
        totals[frame] = totals.get(frame, 0) + 1

        # Смена вердикта — только после --hold одинаковых кадров подряд.
        stable = len(votes) >= args.hold and len(set(votes[-args.hold:])) == 1
        if stable and frame != kind:
            if args.timeline:
                print(f'{run_start:7.1f}–{sec:7.1f}  {kind}')
            kind, run_start = frame, sec
        if args.timeline and args.top:
            top = np.argsort([sp, si, mu])[::-1]
            print(f'  {sec:7.1f}с rms={rms:.3f} речь={sp:.2f} пение={si:.2f} '
                  f'музыка={mu:.2f} → {frame}')
    print(f'{run_start:7.1f}–   конец  {kind}')
    total = max(1, sum(totals.values()))
    print('доля времени:', {k: f'{v * 100 / total:.0f}%' for k, v in totals.items()})


if __name__ == '__main__':
    main()
