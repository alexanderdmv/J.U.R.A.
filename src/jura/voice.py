"""Голосовой контур: сборка стека, «послушать», «эхо» и диагностика `voice-check`.

Распознавание — Parakeet v3 (ru/uk/cs/en, язык определяет сама), синтез — один голос Piper на всех четырёх языках (ADR-015).
Разговор с ответами по смыслу — `talk` в оболочке (`runtime/dialogue.py`). Голос НЕ подтверждает рискованные действия.
Модели лежат в JURA_MODELS_DIR (по умолчанию ~/.jura-models): там piper/ (голоса) и hf/ (модели распознавания).
"""

from __future__ import annotations

import importlib.metadata
import os
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from jura import machine
from jura.adapters.voice.mic_sounddevice import SoundDeviceRecorder
from jura.adapters.voice.onnx_asr_stt import DEFAULT_MODEL, DEFAULT_QUANT, OnnxAsrStt
from jura.adapters.voice.piper_tts import DEFAULT_VOICE, PiperTts
from jura.adapters.voice.windows_tts import WindowsSapiTts
from jura.contracts.errors import JuraError, VoiceError
from jura.contracts.voice import (
    SAMPLE_RATE, AudioRecorder, RecordedAudio, SpeechToText, TextToSpeech, Transcript, peak_level,
)

MIN_SECONDS = 0.3
SILENCE_PEAK = 0.01
STOP_WORDS = ("стоп", "хватит", "закончить", "досить", "вистачить", "stop", "enough", "konec", "dost",
              "exit", "quit", "выход")  # последние — чтобы выйти, набрав слово вместо Enter


@dataclass
class VoiceStack:
    recorder: AudioRecorder
    stt: SpeechToText
    tts: TextToSpeech


def models_dir(env=None) -> Path:
    """Папка скачанных моделей (голоса, распознавание): настройка компьютера (machine.py), например D:/jura-models."""
    return machine.models_dir(env)


def build_voice_stack(env=None) -> VoiceStack:
    """Рабочий стек. Настройки — переменными окружения (все необязательные):
    JURA_MODELS_DIR, JURA_STT_MODEL, JURA_STT_QUANT (по умолчанию int8; «none» — полная), JURA_MIC_DEVICE (номер из voice-check),
    JURA_TTS (piper | windows), JURA_TTS_VOICE (имя голоса), JURA_TTS_SPEED (1.0 — обычная скорость)."""
    is_real_env = env is None
    env = os.environ if env is None else env
    base = models_dir(env)
    if is_real_env:  # модели распознавания качает huggingface_hub — всегда в папку моделей J.U.R.A., а не на диск C:
        os.environ["HF_HOME"] = str(base / "hf")
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    device = env.get("JURA_MIC_DEVICE")
    quant = env.get("JURA_STT_QUANT", DEFAULT_QUANT)
    if env.get("JURA_TTS", "piper") == "windows":
        tts = WindowsSapiTts(env.get("JURA_TTS_VOICE") or None, int(env.get("JURA_TTS_RATE", "0") or 0))
    else:
        tts = PiperTts(base / "piper", env.get("JURA_TTS_VOICE") or DEFAULT_VOICE,
                       speed=float(env.get("JURA_TTS_SPEED", "1") or 1))
    return VoiceStack(
        recorder=SoundDeviceRecorder(device=int(device) if device and device.isdigit() else device or None),
        stt=OnnxAsrStt(env.get("JURA_STT_MODEL", DEFAULT_MODEL), None if quant.lower() == "none" else quant),
        tts=tts,
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
                max_seconds: float = 30.0, started: bool = False) -> Transcript:
    """started=True — пользователь уже нажал Enter (talk спрашивает сам, чтобы можно было набрать «exit»)."""
    ask = input_fn or input  # input ищется в момент вызова (а не при импорте) — так его можно подменять в тестах
    if wav_path:
        audio = read_wav(wav_path)
    else:
        audio = stack.recorder.record(
            max_seconds=max_seconds,
            start=(lambda: None) if started else (lambda: ask("Нажми Enter и говори… ")),
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


def talk_loop(reply_fn: Callable[[str], object], stack: VoiceStack, *, input_fn: Callable[[str], str] | None = None,
              out: Callable[[str], None] = print, typed: bool = False, mute: bool = False,
              warm_up: Callable[[], None] | None = None) -> int:
    """Разговор: сказал → распознал → ответ модели → ответ голосом. «Стоп» (на любом из языков) — выход.
    typed=True — реплики набираются с клавиатуры (ответ всё равно голосом); mute=True — без озвучки.
    reply_fn возвращает объект с полями text, spoken, model, seconds (runtime.dialogue.Reply). Возвращает число ответов."""
    ask = input_fn or input
    say = (lambda text: None) if mute else stack.tts.speak
    out("Загружаю распознавание и голос…")
    if not typed:
        loader = getattr(stack.stt, "load", None)
        if loader:
            loader()
    getattr(stack.tts, "load", lambda: None)()
    if warm_up:
        warm_up()
    out("Готово. Говори, J.U.R.A. ответит. Выход: скажи «стоп» или набери exit и нажми Enter.\n")
    answered = 0
    while True:
        try:
            if typed:
                text = ask("ты> ").strip()
            else:
                typed_word = ask("Enter — говорить, «exit» — выход… ").strip()
                if typed_word.lower() in STOP_WORDS:
                    say("Окей, до связи.")
                    return answered
                heard = listen_once(stack, input_fn=input_fn, out=out, started=True)
                text = heard.text.strip()
                out(f"ты: {text}" if text else "(ничего не распознано)")
        except VoiceError as exc:
            out(f"✗ {exc}")
            return answered
        except (EOFError, KeyboardInterrupt):
            return answered
        if not text:
            continue
        if text.lower().strip(" .!?,") in STOP_WORDS:
            say("Окей, до связи.")
            return answered
        try:
            reply = reply_fn(text)
        except JuraError as exc:
            out(f"✗ {exc}")
            continue
        out(f"J.U.R.A.: {reply.text}\n   ({reply.model}, {reply.seconds:.1f} с)")
        try:
            say(reply.spoken)
        except VoiceError as exc:
            out(f"⚠ не смог озвучить: {exc}")
        answered += 1


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
    for pkg, hint in (("numpy", ""), ("onnxruntime", ""), ("onnx-asr", " → pip install -e ."),
                      ("sounddevice", " → pip install -e ."), ("piper-tts", " → pip install -e .")):
        v = _version(pkg)
        if v is None:
            out(f"✗ пакет {pkg}: не установлен{hint}")
            ok = False
        elif pkg == "onnxruntime" and v == "1.24.1":
            out(f"⚠ пакет {pkg} {v}: у этой версии известны проблемы с onnx-asr — поставь другую (pip install \"onnxruntime!=1.24.1\")")
        else:
            out(f"✓ пакет {pkg}: {v}")

    check("микрофоны", lambda: "; ".join(stack.recorder.devices()) or "не найдено ни одного устройства ввода")
    if isinstance(stack.tts, PiperTts):
        def piper_voice() -> str:
            stack.tts.load()
            return (f"{stack.tts.voice_name} (родной язык {stack.tts.base_lang}; остальные — тем же голосом); "
                    f"скачано: {', '.join(stack.tts.voices())}")
        check("голос синтеза речи (Piper)", piper_voice)
    else:
        check("голоса синтеза речи (Windows)", lambda: (
            "; ".join(stack.tts.voices()) or "нет") + ("" if any("ru-RU" in v for v in stack.tts.voices())
                                                       else "  ⚠ русского голоса нет — речь будет с акцентом"), fatal=False)

    def load_and_try() -> str:
        loader = getattr(stack.stt, "load", None)
        took = loader() if loader else 0.0
        silence = bytes(SAMPLE_RATE * 2)  # 1 секунда тишины
        res = stack.stt.transcribe(silence)
        return f"модель {stack.stt.name} загружена за {took:.1f} с; контрольный прогон {res.compute_seconds:.2f} с"

    check("модель распознавания", load_and_try)
    out("\nДальше: «listen» — проверить распознавание голосом, «speak текст» — проверить озвучивание, «echo» — оба сразу.")
    return ok
