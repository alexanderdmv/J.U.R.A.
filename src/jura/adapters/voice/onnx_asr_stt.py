"""Распознавание речи через onnx-asr. Полностью локально, на процессоре, без PyTorch (ADR-015).

По умолчанию — Parakeet TDT 0.6B v3 (NVIDIA): 25 европейских языков, в том числе русский, украинский, чешский и английский;
язык определяет сама, ставит знаки препинания. Запасной вариант только для русского — GigaAM v3 (`JURA_STT_MODEL=gigaam-v3-rnnt`).
При ПЕРВОМ запуске модель скачивается из интернета (сотни МБ) в HF_HOME; дальше работает без сети. Звук никуда не отправляется.
Проверено на ноутбуке владельца (i5-11300H): Parakeet int8 распознаёт быстрее реального времени в 5–10 раз.
"""

from __future__ import annotations

import time
from typing import Any

from jura.contracts.errors import VoiceError
from jura.contracts.voice import SAMPLE_RATE, Transcript

DEFAULT_MODEL = "nemo-parakeet-tdt-0.6b-v3"
DEFAULT_QUANT = "int8"  # в 4 раза меньше и быстрее на процессоре; на слух владельца разницы с полной нет
VC_REDIST = "https://aka.ms/vs/17/release/vc_redist.x64.exe"


def import_hint(package: str, exc: ImportError) -> str:
    """Понятная причина, почему библиотека не загрузилась. На чистой Windows пакет стоит, но onnxruntime не находит
    Visual C++ Runtime — и это совсем не «не установлен» (так было в Windows Sandbox)."""
    if "DLL load failed" in str(exc):
        return (f"{package} установлен, но не загружается: нет Microsoft Visual C++ Runtime. "
                f"Поставь его (install.cmd сделает сам) или вручную: {VC_REDIST}")
    return f"не установлен {package}. Выполни: pip install -e ."


class OnnxAsrStt:
    def __init__(self, model_name: str = DEFAULT_MODEL, quantization: str | None = DEFAULT_QUANT,
                 onnx_asr_module: Any = None):
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
            except ImportError as exc:
                raise VoiceError(import_hint("onnx-asr", exc)) from None
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
