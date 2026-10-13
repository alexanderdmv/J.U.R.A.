"""Синтез речи Piper: один голос (один тембр) на русском, украинском, чешском и английском — в том числе вперемешку (ADR-015).

Как это работает: фраза делится на куски по языкам (`langmix`). Кусок на «родном» языке голоса Piper читает как обычно,
а кусок на другом языке мы сами переводим в фонемы по правилам ЕГО языка (espeak-ng внутри Piper) и вставляем блоком
`[[ фонемы ]]`. Тембр остаётся тем же, меняется только произношение. Звуки, которых голос никогда не слышал
(чешские долгие гласные, ř), заменяем ближайшими знакомыми — получается лёгкий акцент вместо каши.

Полностью локально, на процессоре. Голоса — файлы .onnx + .onnx.json в папке голосов (скачивает `python -m jura setup`).
Проверено на ноутбуке владельца: голос ru_RU-irina-medium, фраза из 5 с синтезируется за ~0,3 с.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

from jura.adapters.voice.langmix import segments
from jura.contracts.errors import VoiceError

DEFAULT_VOICE = "ru_RU-irina-medium"
ESPEAK = {"ru": "ru", "uk": "uk", "cs": "cs", "en": "en-us"}
# Замены для чужих голосу звуков (подобраны прослушиванием и обратным распознаванием, см. ADR-015).
# Ключ — язык куска; применяются, только если голос сам не на этом языке.
PHONEME_FIXES: dict[str, tuple[tuple[str, str], ...]] = {
    "cs": (("ː", ""), ("r̝", "rʐ"), ("̝", ""), ("̩", ""), ("ɲ", "nʲ"), ("ɟ", "dʲ"), ("c", "tʲ")),
}
_TAIL = re.compile(r"[\s,.!?;:…]+$")


def _lang_of_voice(espeak_voice: str) -> str:
    code = espeak_voice.split("-")[0].lower()
    return code if code in ESPEAK else "ru"


class PiperTts:
    def __init__(self, voices_dir: str | Path, voice: str = DEFAULT_VOICE, *, speed: float = 1.0,
                 piper_module: Any = None, phonemizer: Any = None, play: Callable[[Any, int], None] | None = None):
        self.voices_dir = Path(voices_dir)
        self.voice_name = voice
        self._speed = speed
        self._piper, self._phonemizer, self._play = piper_module, phonemizer, play
        self._voice: Any = None
        self.base_lang = "ru"

    # --- загрузка ---
    def _modules(self):
        if self._piper is None:
            try:
                import piper  # noqa: PLC0415 — тяжёлая часть, грузим только когда нужен голос
                from piper.phonemize_espeak import EspeakPhonemizer  # noqa: PLC0415
            except ImportError:
                raise VoiceError("не установлен piper-tts. Выполни: pip install -e .") from None
            self._piper = piper
            self._phonemizer = self._phonemizer or EspeakPhonemizer()
        return self._piper

    def load(self) -> None:
        if self._voice is not None:
            return
        path = self.voices_dir / f"{self.voice_name}.onnx"
        if not path.exists() or not path.with_suffix(".onnx.json").exists():
            raise VoiceError(f"нет голоса {self.voice_name} в {self.voices_dir}. Скачать: python -m jura setup")
        try:
            self._voice = self._modules().PiperVoice.load(str(path))
        except Exception as exc:
            raise VoiceError(f"не удалось загрузить голос {self.voice_name} ({type(exc).__name__})") from None
        self.base_lang = _lang_of_voice(self._voice.config.espeak_voice)

    # --- текст → то, что читает Piper ---
    def prepare(self, text: str) -> str:
        """Текст, где куски на чужих голосу языках заменены блоками [[ фонемы ]]."""
        self.load()
        parts = []
        for lang, chunk in segments(text):
            if lang is None or lang == self.base_lang:
                parts.append(chunk)
                continue
            tail = _TAIL.search(chunk)
            core = chunk[:tail.start()] if tail else chunk
            sentences = self._phonemizer.phonemize(ESPEAK[lang], core)
            phonemes = " ".join("".join(s) for s in sentences).strip(" .,!?;:")
            for old, new in PHONEME_FIXES.get(lang, ()):
                phonemes = phonemes.replace(old, new)
            parts.append(f"[[ {phonemes} ]]" + (tail.group() if tail else " "))
        return "".join(parts)

    def synthesize(self, text: str):
        """(аудио float32, частота). Пустой текст — пустое аудио."""
        import numpy as np  # noqa: PLC0415
        self.load()
        if not text.strip():
            return np.zeros(0, dtype=np.float32), 22050
        config = self._piper.SynthesisConfig(length_scale=1.0 / self._speed) if self._speed != 1.0 else None
        try:
            chunks = list(self._voice.synthesize(self.prepare(text), syn_config=config))
        except Exception as exc:  # текст не включаем в сообщение
            raise VoiceError(f"сбой синтеза речи ({type(exc).__name__})") from None
        if not chunks:
            return np.zeros(0, dtype=np.float32), 22050
        return np.concatenate([c.audio_float_array for c in chunks]), chunks[0].sample_rate

    # --- интерфейс TextToSpeech ---
    def speak(self, text: str) -> None:
        audio, rate = self.synthesize(text)
        if len(audio):
            (self._play or _play_sounddevice)(audio, rate)

    def voices(self) -> list[str]:
        return sorted(p.name[:-len(".onnx")] for p in self.voices_dir.glob("*.onnx")) if self.voices_dir.exists() else []


def _play_sounddevice(audio, rate: int) -> None:
    try:
        import sounddevice as sd  # noqa: PLC0415
        sd.play(audio, rate)
        sd.wait()
    except Exception as exc:
        raise VoiceError(f"не удалось воспроизвести звук ({type(exc).__name__}). Проверь динамики в Windows") from None
