"""Тестовая модель без сети. Нужна, чтобы проверять ядро без ключей и денег."""

from __future__ import annotations

from typing import Callable

from jura.contracts.errors import ModelError
from jura.contracts.model import ModelCapabilities, ModelRequest, ModelResponse
from jura.contracts.types import Zone


class FakeModel:
    def __init__(self, name: str, *, local: bool = False, max_zone: Zone = Zone.P, quality: int = 1,
                 cost: float = 0.0, context_tokens: int = 8000, tool_calling: bool = True,
                 structured_output: bool = True, reply: str | Callable[[ModelRequest], str] | None = None,
                 fail: bool = False):
        self.capabilities = ModelCapabilities(
            name=name, local=local, max_zone=max_zone, context_tokens=context_tokens,
            tool_calling=tool_calling, structured_output=structured_output,
            cost_per_1k_tokens=cost, quality=quality)
        self._reply, self._fail = reply, fail
        self.calls: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls.append(request)
        if self._fail:
            raise ModelError(f"{self.capabilities.name}: симулированный сбой")
        if callable(self._reply):
            text = self._reply(request)
        else:
            text = self._reply or f"[{self.capabilities.name}] {request.prompt[:40]}"
        return ModelResponse(text, self.capabilities.name,
                             tokens_in=len(request.prompt.split()), tokens_out=len(text.split()))
