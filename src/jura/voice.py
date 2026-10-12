"""Контракты голоса: запись, распознавание речи (STT), синтез речи (TTS).

Аудио живёт только в оперативной памяти и на диск не пишется. Всё распознавание и синтез — локальные:
звук и текст не покидают компьютер.
"""

from __future__ import annotations

from array import array
from dataclasses import dataclass
from typing import Callable, Protocol

SAMPLE_RATE = 16_000  # все STT-модели здесь работают с 16 кГц, моно, 16 бит


@dataclass(frozen=True)
class RecordedAudio:
    pcm16: bytes        # сырые отсчёты int16, моно
    sample_rate: int
    peak: float         # громкость пика 0..1 — помогает понять, что микрофон «молчит»

    @property
    def seconds(self) -> float:
        return len(self.pcm16) / 2 / self.sample_rate


@dataclass(frozen=True)
class Transcript:
    text: str
    audio_seconds: float
    compute_seconds: float
    model: str

    @property
    def rtf(self) -> float:
        """Real-time factor: время распознавания / длительность звука. Меньше 1 — быстрее реального времени."""
        return self.compute_seconds / self.audio_seconds if self.audio_seconds else 0.0


def peak_level(pcm16: bytes) -> float:
    samples = array("h")
    samples.frombytes(pcm16[: len(pcm16) // 2 * 2])
    return max((abs(s) for s in samples), default=0) / 32768.0


class AudioRecorder(Protocol):
    def record(self, *, max_seconds: float, start: Callable[[], None], stop: Callable[[], None]) -> RecordedAudio:
        """start() блокирует, пока пользователь не готов начать; запись идёт, пока выполняется stop()."""
        ...

    def devices(self) -> list[str]: ...


class SpeechToText(Protocol):
    name: str

    def transcribe(self, pcm16: bytes, sample_rate: int = SAMPLE_RATE) -> Transcript: ...


class TextToSpeech(Protocol):
    def speak(self, text: str) -> None:
        """Произносит текст и возвращается, когда речь закончилась."""
        ...

    def voices(self) -> list[str]: ...
