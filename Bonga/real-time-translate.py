# pip install camb-sdk

import asyncio
import os
import sys
import wave

from camb.client import CambAI
from camb.live_transcription import FileAudioSource
from camb.realtime import ServerEventType

# Input WAV must be PCM16 mono 24 kHz.
# ffmpeg -i in.wav -ar 24000 -ac 1 -sample_fmt s16 input_24k_mono.wav

def resolve_key() -> str:
    key = os.environ.get('CAMB_API_KEY')
    if not key:
        try:
            from config_secrets import CAMB_API_KEY as FILE_KEY
            key = FILE_KEY
        except ImportError:
            pass
    if not key:
        sys.exit("Нет ключа. Порядок поиска: переменная CAMB_API_KEY, "
                 "Bonga/config_secrets.py (CAMB_API_KEY = '...')")
    return key

async def main() -> None:
    client = CambAI(api_key=resolve_key())
    session = await client.realtime.connect(
        source_language="en-us",
        target_language="de-de",
        model="iris",
    )

    out_audio = bytearray()
    audio_done = asyncio.Event()

    @session.on(ServerEventType.TEXT_DONE)
    def _(event):
        print("translation:", event.text)

    @session.on(ServerEventType.AUDIO_DELTA)
    def _(event):
        out_audio.extend(event.data)  # raw PCM16 mono 24 kHz

    @session.on(ServerEventType.AUDIO_DONE)
    def _(event):
        audio_done.set()

    async with session:
        await session.wait_until_ready()
        await session.stream_audio(
            FileAudioSource("input_24k_mono.wav", real_time=True),
        )
        try:
            await asyncio.wait_for(audio_done.wait(), timeout=30)
        except asyncio.TimeoutError:
            pass

    with wave.open("translated.wav", "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(24000)
        out.writeframes(bytes(out_audio))
    print("Audio saved to translated.wav")

asyncio.run(main())