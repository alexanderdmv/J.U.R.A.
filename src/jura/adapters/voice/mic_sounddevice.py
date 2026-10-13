"""Запись с микрофона по принципу push-to-talk (Enter — начать, Enter — закончить). Библиотека: sounddevice.

Ставится отдельно: pip install -e .. Аудио накапливается в памяти и никуда не сохраняется.
"""

from __future__ import annotations

from typing import Any, Callable

from jura.contracts.errors import VoiceError
from jura.contracts.voice import SAMPLE_RATE, RecordedAudio, peak_level


class SoundDeviceRecorder:
    def __init__(self, device: int | str | None = None, sample_rate: int = SAMPLE_RATE, sd_module: Any = None):
        self._device, self._rate, self._sd_mod = device, sample_rate, sd_module

    def _sd(self):
        if self._sd_mod is not None:
            return self._sd_mod
        try:
            import sounddevice  # noqa: PLC0415 — необязательная зависимость
        except ImportError:
            raise VoiceError('микрофон недоступен: не установлен sounddevice. Выполни: pip install -e .') from None
        except OSError:  # пакет есть, но не найдена системная библиотека звука PortAudio
            raise VoiceError("sounddevice установлен, но не найдена системная библиотека PortAudio "
                             "(в Windows она идёт внутри пакета: попробуй pip install --force-reinstall sounddevice)") from None
        return sounddevice

    def devices(self) -> list[str]:
        sd = self._sd()
        try:
            default = sd.default.device[0]
            return [f"{i}{' (по умолчанию)' if i == default else ''}: {d['name']}"
                    for i, d in enumerate(sd.query_devices()) if d.get("max_input_channels", 0) > 0]
        except Exception as exc:
            raise VoiceError(f"не удалось получить список микрофонов ({type(exc).__name__})") from None

    def record(self, *, max_seconds: float, start: Callable[[], None], stop: Callable[[], None]) -> RecordedAudio:
        sd = self._sd()
        limit = int(max_seconds * self._rate) * 2  # байт
        chunks: list[bytes] = []
        taken = [0]

        def callback(indata, frames, time_info, status):  # вызывается звуковой подсистемой в отдельном потоке
            if taken[0] < limit:
                data = bytes(indata)
                chunks.append(data)
                taken[0] += len(data)

        start()
        try:
            with sd.InputStream(samplerate=self._rate, channels=1, dtype="int16", device=self._device,
                                callback=callback):
                stop()
        except Exception as exc:
            if isinstance(exc, VoiceError):
                raise
            raise VoiceError(f"не удалось открыть микрофон ({type(exc).__name__}). Проверь «voice-check» "
                             "и разрешение на доступ к микрофону в настройках Windows") from None
        pcm = b"".join(chunks)[:limit]
        return RecordedAudio(pcm, self._rate, peak_level(pcm))
