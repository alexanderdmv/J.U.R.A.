"""Голосовой контур: сборка стека, «послушать», «эхо» и диагностика `voice-check`.

Сейчас это вход и выход без «мозга»: услышал → показал текст → (по желанию) запомнил или проговорил обратно.
Когда появится диалоговое ядро, текст пойдёт в него по тому же пути, что и набранный с клавиатуры
(политики, подтверждения, журнал). Голос НЕ подтверждает рискованные действия.
"""

from __future__ import annotations

import importlib.metadata
import os
import wave
from dataclasses import dataclass
from typing import Callable

from jura.adapters.voice.gigaam_stt import DEFAULT_MODEL, GigaAMStt
from jura.adapters.voice.mic_sounddevice import SoundDeviceRecorder
from jura.adapters.voice.windows_tts import WindowsSapiTts
from jura.contracts.errors import JuraError, VoiceError
from jura.contracts.voice import (
    SAMPLE_RATE, AudioRecorder, RecordedAudio, SpeechToText, TextToSpeech, Transcript, peak_level,
)

MIN_SECONDS = 0.3
SILENCE_PEAK = 0.01
STOP_WORDS = ("стоп", "хватит", "закончить")


@dataclass
class VoiceStack:
    recorder: AudioRecorder
    stt: SpeechToText
    tts: TextToSpeech


def build_voice_stack(env=None) -> VoiceStack:
    """Рабочий стек. Настройки — переменными окружения (все необязательные):
    JURA_STT_MODEL, JURA_STT_QUANT (например int8), JURA_MIC_DEVICE (номер из voice-check), JURA_TTS_VOICE, JURA_TTS_RATE (-10..10)."""
    env = os.environ if env is None else env
    device = env.get("JURA_MIC_DEVICE")
    return VoiceStack(
        recorder=SoundDeviceRecorder(device=int(device) if device and device.isdigit() else device or None),
        stt=GigaAMStt(env.get("JURA_STT_MODEL", DEFAULT_MODEL), env.get("JURA_STT_QUANT") or None),
        tts=WindowsSapiTts(env.get("JURA_TTS_VOICE") or None, int(env.get("JURA_TTS_RATE", "0") or 0)),
    )


def read_wav(path: str) -> RecordedAudio:
    """Читает WAV 16 кГц, моно, 16 бит (для проверки распознавания без микрофона)."""
    try:
        with wave.open(path, "rb") as w:
            if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (SAMPLE_RATE, 1, 2):
                raise VoiceError(f"нужен WAV 16000 Гц, моно, 16 бит; у файла: {w.getframerate()} Гц, "
                                 f"{w.getnchannels()} кан., {w.getsampwidth() * 8} бит")
            pcm = w.readframes(w.getnframes())
    except (wave.Error, EOFError, OSError) as exc:
        raise VoiceError(f"не удалось прочитать WAV-файл ({type(exc).__name__})") from None
    return RecordedAudio(pcm, SAMPLE_RATE, peak_level(pcm))


def listen_once(stack: VoiceStack, *, input_fn: Callable[[str], str] | None = None,
                out: Callable[[str], None] = print, wav_path: str | None = None,
                max_seconds: float = 30.0) -> Transcript:
    ask = input_fn or input  # input ищется в момент вызова (а не при импорте) — так его можно подменять в тестах
    if wav_path:
        audio = read_wav(wav_path)
    else:
        audio = stack.recorder.record(
            max_seconds=max_seconds,
            start=lambda: ask("Нажми Enter и говори… "),
            stop=lambda: ask("Идёт запись. Enter — закончить… "))
    if audio.seconds < MIN_SECONDS:
        raise VoiceError("слишком короткая запись — ничего не записалось")
    if audio.peak < SILENCE_PEAK:
        out("⚠ Запись почти без звука: проверь микрофон (voice-check) и громкость ввода в Windows.")
    out(f"(записано {audio.seconds:.1f} с, уровень пика {audio.peak:.0%}; распознаю…)")
    return stack.stt.transcribe(audio.pcm16, audio.sample_rate)


def echo_loop(stack: VoiceStack, *, input_fn: Callable[[str], str] | None = None,
              out: Callable[[str], None] = print) -> int:
    """Диагностический цикл: слушает и повторяет услышанное голосом. Скажи «стоп» — выход. Возвращает число фраз."""
    spoken = 0
    out("Режим эха: скажи фразу — я её повторю. Для выхода скажи «стоп».")
    while True:
        try:
            result = listen_once(stack, input_fn=input_fn, out=out)
        except VoiceError as exc:
            out(f"✗ {exc}")
            return spoken
        text = result.text.strip()
        out(f"Услышал: «{text}»  (распознавание {result.compute_seconds:.1f} с, RTF {result.rtf:.2f})")
        if not text:
            stack.tts.speak("Не расслышал.")
            continue
        if text.lower().strip(" .!?,") in STOP_WORDS:
            stack.tts.speak("Хорошо, заканчиваю.")
            return spoken
        stack.tts.speak(text)
        spoken += 1


# ----------------------------------------------------------------- диагностика ---
def _version(pkg: str) -> str | None:
    try:
        return importlib.metadata.version(pkg)
    except importlib.metadata.PackageNotFoundError:
        return None


def diagnose(stack: VoiceStack, out: Callable[[str], None] = print) -> bool:
    """Проверяет по пунктам, что нужно для голоса, и говорит, ЧТО именно сломано. Возвращает True, если всё в порядке."""
    ok = True

    def check(title: str, fn: Callable[[], str], fatal: bool = True) -> None:
        nonlocal ok
        try:
            out(f"✓ {title}: {fn()}")
        except (JuraError, Exception) as exc:  # диагностика обязана дойти до конца
            out(f"{'✗' if fatal else '⚠'} {title}: {exc if isinstance(exc, JuraError) else type(exc).__name__ + ': ' + str(exc)[:200]}")
            ok = ok and not fatal

    out("Проверка голосового контура (всё работает локально)\n")
    for pkg, hint in (("numpy", ""), ("onnxruntime", ""), ("onnx-asr", ' → pip install -e ".[voice]"'),
                      ("sounddevice", ' → pip install -e ".[voice]"')):
        v = _version(pkg)
        if v is None:
            out(f"✗ пакет {pkg}: не установлен{hint}")
            ok = False
        elif pkg == "onnxruntime" and v == "1.24.1":
            out(f"⚠ пакет {pkg} {v}: у этой версии известны проблемы с onnx-asr — поставь другую (pip install \"onnxruntime!=1.24.1\")")
        else:
            out(f"✓ пакет {pkg}: {v}")

    check("микрофоны", lambda: "; ".join(stack.recorder.devices()) or "не найдено ни одного устройства ввода")
    check("голоса синтеза речи (Windows)", lambda: (
        "; ".join(stack.tts.voices()) or "нет") + ("" if any("ru-RU" in v for v in stack.tts.voices())
                                                   else "  ⚠ русского голоса нет — речь будет с акцентом (позже: Piper)"), fatal=False)

    def load_and_try() -> str:
        loader = getattr(stack.stt, "load", None)
        took = loader() if loader else 0.0
        silence = bytes(SAMPLE_RATE * 2)  # 1 секунда тишины
        res = stack.stt.transcribe(silence)
        return f"модель {stack.stt.name} загружена за {took:.1f} с; контрольный прогон {res.compute_seconds:.2f} с"

    check("модель распознавания", load_and_try)
    out("\nДальше: «listen» — проверить распознавание голосом, «speak текст» — проверить озвучивание, «echo» — оба сразу.")
    return ok
