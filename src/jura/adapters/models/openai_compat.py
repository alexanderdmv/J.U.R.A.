"""Модели с интерфейсом «OpenAI chat completions»: облачный OpenAI И локальные серверы (Ollama, LM Studio, llama.cpp).

Один адаптер на оба случая, различие — в настройках:
  * облако (OpenAI): https, ключ в Authorization, параметр max_completion_tokens (новые модели отвергают max_tokens),
    данные — только зона O;
  * локально: сервер ОБЯЗАН быть на этом же компьютере (127.0.0.1/localhost/::1) — иначе «локальная» модель могла бы
    оказаться чужим сервером, и данные S1 ушли бы наружу. Параметр max_tokens, ключ не нужен, данные — до зоны S1.
Зона S2 не передаётся никому — это правило роутера.
"""

from __future__ import annotations

from typing import Callable

from jura.adapters.models.http import check_url, get_key, is_loopback, request_json
from jura.contracts.errors import ModelError
from jura.contracts.model import ModelCapabilities, ModelRequest, ModelResponse
from jura.contracts.types import Zone

OPENAI_API = "https://api.openai.com/v1"
OLLAMA_API = "http://127.0.0.1:11434/v1"
KEY_HINT = "secret set openai.api_key"


class OpenAICompatModel:
    def __init__(self, model: str, *, base_url: str, get_api_key: Callable[[], str] | None = None, local: bool = False,
                 name: str | None = None, max_output: int | None = None, context_tokens: int | None = None,
                 quality: int | None = None, timeout: float | None = None, allow_insecure_http: bool = False):
        if local and not is_loopback(base_url):
            raise ModelError("локальная модель должна работать на этом же компьютере (127.0.0.1 или localhost); "
                             "иначе данные ушли бы на другой сервер")
        check_url(base_url, allow_http_loopback=local, allow_insecure=allow_insecure_http)
        self._model, self._get_key, self._base, self._local = model, get_api_key, base_url.rstrip("/"), local
        self._max_output = max_output or (1024 if local else 2048)
        self._timeout = timeout or (300 if local else 120)   # локальная генерация бывает медленной
        self._token_param = "max_tokens" if local else "max_completion_tokens"
        self.capabilities = ModelCapabilities(
            name=name or f"{'local' if local else 'openai'}:{model}", local=local,
            max_zone=Zone.S1 if local else Zone.O,
            context_tokens=context_tokens or (4096 if local else 100_000),  # у Ollama по умолчанию окно всего 4K
            quality=quality if quality is not None else (3 if local else 5))

    def _auth(self) -> tuple[dict[str, str], tuple[str, ...]]:
        if self._get_key is None:
            return {}, ()
        key = get_key(self._get_key, KEY_HINT)
        return {"Authorization": f"Bearer {key}"}, (key,)

    def complete(self, request: ModelRequest) -> ModelResponse:
        headers, secrets = self._auth()
        data = request_json("POST", f"{self._base}/chat/completions", headers=headers, secrets=secrets,
                            timeout=self._timeout,
                            body={"model": self._model, self._token_param: self._max_output,
                                  "messages": ([{"role": "system", "content": request.system}] if request.system else [])
                                  + request.messages()})
        try:
            choice = data["choices"][0]
            text = choice["message"].get("content") or ""
        except (KeyError, IndexError, TypeError, AttributeError):
            raise ModelError("неожиданный ответ сервера (нет choices[0].message)") from None
        if not text and choice.get("finish_reason") == "length":
            raise ModelError("ответ пуст: лимит токенов ушёл на «рассуждение» модели (увеличь --max-output)")
        usage = data.get("usage") or {}
        return ModelResponse(text, self.capabilities.name, int(usage.get("prompt_tokens", 0)),
                             int(usage.get("completion_tokens", 0)))


def list_models(*, base_url: str, get_api_key: Callable[[], str] | None = None, local: bool = False,
                allow_insecure_http: bool = False) -> list[str]:
    if local and not is_loopback(base_url):
        raise ModelError("локальный сервер должен быть на этом же компьютере (127.0.0.1 или localhost)")
    check_url(base_url, allow_http_loopback=local, allow_insecure=allow_insecure_http)
    headers, secrets = {}, ()
    if get_api_key is not None:
        key = get_key(get_api_key, KEY_HINT)
        headers, secrets = {"Authorization": f"Bearer {key}"}, (key,)
    data = request_json("GET", f"{base_url.rstrip('/')}/models", headers=headers, secrets=secrets, timeout=30)
    return [m["id"] for m in data.get("data", []) if isinstance(m, dict) and "id" in m]
