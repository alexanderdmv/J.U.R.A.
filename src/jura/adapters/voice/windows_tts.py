"""Синтез речи через встроенные голоса Windows (SAPI) — без установки чего-либо. Полностью локально.

Это стартовый вариант, чтобы J.U.R.A. заговорил сразу. Качество зависит от установленных голосов
(русский голос Microsoft Irina есть не на каждой системе — `voice-check` покажет, что доступно).
Позже заменяется на Piper или Silero за тем же интерфейсом.

Текст передаётся в PowerShell через переменную окружения, а не через командную строку: так в него невозможно «вставить команду».
"""

from __future__ import annotations

import base64
import os
import subprocess
import sys
from typing import Any, Callable

from jura.contracts.errors import VoiceError

SPEAK_SCRIPT = r"""
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$name = $env:JURA_TTS_VOICE
if ($name) { $s.SelectVoice($name) } else {
  $ru = $s.GetInstalledVoices() | Where-Object { $_.Enabled -and $_.VoiceInfo.Culture.Name -eq 'ru-RU' } | Select-Object -First 1
  if ($ru) { $s.SelectVoice($ru.VoiceInfo.Name) }
}
if ($env:JURA_TTS_RATE) { $s.Rate = [int]$env:JURA_TTS_RATE }
$s.Speak($env:JURA_TTS_TEXT)
"""
LIST_SCRIPT = (
    "Add-Type -AssemblyName System.Speech; "
    "(New-Object System.Speech.Synthesis.SpeechSynthesizer).GetInstalledVoices() | "
    "ForEach-Object { $_.VoiceInfo.Name + ' [' + $_.VoiceInfo.Culture.Name + ']' }"
)


def _command(script: str) -> list[str]:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")  # устойчиво к кавычкам и переводам строк
    return ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]


class WindowsSapiTts:
    def __init__(self, voice: str | None = None, rate: int = 0, runner: Callable[..., Any] = subprocess.run,
                 platform: str | None = None):
        self._voice, self._rate, self._run = voice, rate, runner
        self._platform = platform or sys.platform

    def _check(self) -> None:
        if self._platform != "win32":
            raise VoiceError("встроенный синтез речи Windows доступен только в Windows")

    def speak(self, text: str) -> None:
        self._check()
        if not text.strip():
            return
        env = dict(os.environ, JURA_TTS_TEXT=text, JURA_TTS_RATE=str(self._rate))
        if self._voice:
            env["JURA_TTS_VOICE"] = self._voice
        try:
            res = self._run(_command(SPEAK_SCRIPT), env=env, capture_output=True, text=True, timeout=180)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise VoiceError(f"не удалось запустить синтез речи ({type(exc).__name__})") from None
        if res.returncode != 0:  # текст не включаем в сообщение
            first = (res.stderr or "").strip().splitlines()[:1]
            raise VoiceError("синтез речи завершился ошибкой" + (f": {first[0][:150]}" if first else ""))

    def voices(self) -> list[str]:
        self._check()
        try:
            res = self._run(_command(LIST_SCRIPT), capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise VoiceError(f"не удалось получить список голосов ({type(exc).__name__})") from None
        return [line.strip() for line in (res.stdout or "").splitlines() if line.strip()]
