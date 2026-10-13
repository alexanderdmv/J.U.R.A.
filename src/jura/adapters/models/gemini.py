"""Gemini (Google, нативный generateContent). Облачная модель: только открытые данные (зона O).

Формат по документации: POST /v1beta/models/{модель}:generateContent, ключ в заголовке x-goog-api-key (НЕ в адресе),
тело contents[].parts[].text и generationConfig.maxOutputTokens. У «думающих» моделей токены рассуждения тратятся из того же
лимита, поэтому лимит по умолчанию щедрый. С настоящим сервисом адаптер впервые проверяется у владельца.
"""

from __future__ import annotations

import urllib.parse
from typing import Callable

from jura.adapters.models.http import check_url, get_key, request_json
from jura.contracts.errors import ModelError
from jura.contracts.model import ModelCapabilities, ModelRequest, ModelResponse
from jura.contracts.types import Zone

API = "https://generativelanguage.googleapis.com/v1beta"
KEY_HINT = "secret set gemini.api_key"


def _bare(model: str) -> str:
    return model.removeprefix("models/")  # список моделей отдаёт имена вида «models/gemini-…»


class GeminiModel:
    def __init__(self, model: str, get_api_key: Callable[[], str], *, name: str | None = None, base_url: str = API,
                 max_output: int = 2048, context_tokens: int = 100_000, quality: int = 5, timeout: float = 120,
                 allow_insecure_http: bool = False):
        check_url(base_url, allow_insecure=allow_insecure_http)
        self._model, self._get_key, self._base = _bare(model), get_api_key, base_url.rstrip("/")
        self._max_output, self._timeout = max_output, timeout
        self.capabilities = ModelCapabilities(
            name=name or f"gemini:{self._model}", local=False, max_zone=Zone.O, context_tokens=context_tokens,
            quality=quality)

    def complete(self, request: ModelRequest) -> ModelResponse:
        key = get_key(self._get_key, KEY_HINT)
        url = f"{self._base}/models/{urllib.parse.quote(self._model, safe='')}:generateContent"
        contents = [{"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
                    for m in request.messages()]
        body = {"contents": contents, "generationConfig": {"maxOutputTokens": self._max_output}}
        if request.system:
            body["systemInstruction"] = {"parts": [{"text": request.system}]}
        data = request_json("POST", url, headers={"x-goog-api-key": key}, secrets=(key,), timeout=self._timeout,
                            body=body)
        candidates = data.get("candidates") or []
        if not candidates:
            reason = (data.get("promptFeedback") or {}).get("blockReason", "нет вариантов ответа")
            raise ModelError(f"сервис не вернул ответ: {reason}")
        parts = (candidates[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and not p.get("thought"))
        if not text and candidates[0].get("finishReason") == "MAX_TOKENS":
            raise ModelError("ответ пуст: лимит токенов ушёл на «рассуждение» (увеличь --max-output)")
        usage = data.get("usageMetadata") or {}
        return ModelResponse(text, self.capabilities.name, int(usage.get("promptTokenCount", 0)),
                             int(usage.get("candidatesTokenCount", 0)) + int(usage.get("thoughtsTokenCount", 0)))


def list_models(get_api_key: Callable[[], str], *, base_url: str = API, allow_insecure_http: bool = False) -> list[str]:
    check_url(base_url, allow_insecure=allow_insecure_http)
    key = get_key(get_api_key, KEY_HINT)
    data = request_json("GET", f"{base_url.rstrip('/')}/models?pageSize=100", headers={"x-goog-api-key": key},
                        secrets=(key,), timeout=30)
    return [_bare(m["name"]) for m in data.get("models", [])
            if isinstance(m, dict) and "generateContent" in m.get("supportedGenerationMethods", [])]
