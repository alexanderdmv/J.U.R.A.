"""Claude (Anthropic Messages API). Облачная модель: ей можно отдавать только открытые данные (зона O).

Формат по официальной документации: POST /v1/messages, заголовки x-api-key и anthropic-version, max_tokens обязателен,
ответ — список блоков content, расход — usage.input_tokens / usage.output_tokens.
С настоящим сервисом этот адаптер впервые проверяется у владельца (в тестах — локальный сервер того же формата).
"""

from __future__ import annotations

from typing import Callable

from jura.adapters.models.http import check_url, get_key, request_json
from jura.contracts.errors import ModelError
from jura.contracts.model import ModelCapabilities, ModelRequest, ModelResponse
from jura.contracts.types import Zone

API = "https://api.anthropic.com"
VERSION = "2023-06-01"
KEY_HINT = "secret set anthropic.api_key"


def _headers(key: str) -> dict[str, str]:
    return {"x-api-key": key, "anthropic-version": VERSION}


class AnthropicModel:
    def __init__(self, model: str, get_api_key: Callable[[], str], *, name: str | None = None, base_url: str = API,
                 max_output: int = 2048, context_tokens: int = 100_000, quality: int = 5, timeout: float = 120,
                 allow_insecure_http: bool = False):
        check_url(base_url, allow_insecure=allow_insecure_http)
        self._model, self._get_key, self._base = model, get_api_key, base_url.rstrip("/")
        self._max_output, self._timeout = max_output, timeout
        self.capabilities = ModelCapabilities(
            name=name or f"anthropic:{model}", local=False, max_zone=Zone.O, context_tokens=context_tokens,
            quality=quality)

    def complete(self, request: ModelRequest) -> ModelResponse:
        key = get_key(self._get_key, KEY_HINT)
        data = request_json("POST", f"{self._base}/v1/messages", headers=_headers(key), timeout=self._timeout,
                            secrets=(key,), body={"model": self._model, "max_tokens": self._max_output,
                                                  "messages": [{"role": "user", "content": request.prompt}]})
        blocks = data.get("content")
        if not isinstance(blocks, list):
            raise ModelError("неожиданный ответ сервиса (нет списка content)")
        text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")
        usage = data.get("usage") or {}
        if not text and data.get("stop_reason") == "max_tokens":
            raise ModelError("ответ пуст: лимит токенов исчерпан (увеличь --max-output)")
        return ModelResponse(text, self.capabilities.name, int(usage.get("input_tokens", 0)),
                             int(usage.get("output_tokens", 0)))


def list_models(get_api_key: Callable[[], str], *, base_url: str = API, allow_insecure_http: bool = False) -> list[str]:
    check_url(base_url, allow_insecure=allow_insecure_http)
    key = get_key(get_api_key, KEY_HINT)
    data = request_json("GET", f"{base_url.rstrip('/')}/v1/models?limit=100", headers=_headers(key), secrets=(key,), timeout=30)
    return [m["id"] for m in data.get("data", []) if isinstance(m, dict) and "id" in m]
