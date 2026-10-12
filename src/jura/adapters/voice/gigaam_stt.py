"""Распознавание русской речи: GigaAM v3 (Сбер) через onnx-asr. Полностью локально, на процессоре, без PyTorch.

Ставится отдельно: pip install -e ".[voice]". При ПЕРВОМ запуске модель скачивается из интернета (несколько сотен МБ);
дальше работает без сети. Звук никуда не отправляется.

ВАЖНО: вызовы onnx_asr написаны по его документации, но с настоящей библиотекой этот файл впервые проверяется у владельца
(в тестах она подменена). Если что-то не так — `voice-check` покажет, где.
"""

from __future__ import annotations

import time
from typing import Any

from jura.contracts.errors import VoiceError
from jura.contracts.voice import SAMPLE_RATE, Transcript

DEFAULT_MODEL = "gigaam-v3-rnnt"  # без знаков препинания, самая низкая ошибка; "gigaam-v3-e2e-rnnt" — с пунктуацией


class GigaAMStt:
    def __init__(self, model_name: str = DEFAULT_MODEL, quantization: str | None = None, onnx_asr_module: Any = None):
        self.name = model_name
        self._quant, self._mod, self._model = quantization, onnx_asr_module, None

    def load(self) -> float:
        """Загружает модель (при первом запуске — скачивает). Возвращает секунды."""
        if self._model is not None:
            return 0.0
        mod = self._mod
        if mod is None:
            try:
                import onnx_asr as mod  # noqa: PLC0415 — необязательная зависимость
            except ImportError:
                raise VoiceError('не установлен onnx-asr. Выполни: pip install -e ".[voice]"') from None
        t0 = time.perf_counter()
        try:
            kwargs = {"quantization": self._quant} if self._quant else {}
            self._model = mod.load_model(self.name, **kwargs)
        except Exception as exc:
            raise VoiceError(f"не удалось загрузить модель {self.name} ({type(exc).__name__}: {str(exc)[:200]}). "
                             "При первом запуске нужен интернет для скачивания модели") from None
        return time.perf_counter() - t0

    def transcribe(self, pcm16: bytes, sample_rate: int = SAMPLE_RATE) -> Transcript:
        if sample_rate != SAMPLE_RATE:
            raise VoiceError(f"нужна частота {SAMPLE_RATE} Гц, получено {sample_rate}")
        if not pcm16:
            raise VoiceError("пустая запись")
        self.load()
        import numpy as np  # noqa: PLC0415 — приезжает вместе с onnx-asr
        wave = np.frombuffer(pcm16[: len(pcm16) // 2 * 2], dtype=np.int16).astype(np.float32) / 32768.0
        t0 = time.perf_counter()
        try:
            try:
                text = self._model.recognize(wave, sample_rate=sample_rate)
            except TypeError:  # старые версии библиотеки без параметра sample_rate
                text = self._model.recognize(wave)
        except Exception as exc:
            raise VoiceError(f"сбой распознавания ({type(exc).__name__})") from None
        elapsed = time.perf_counter() - t0
        return Transcript(str(text).strip(), len(wave) / sample_rate, elapsed, self.name)
