"""Реестр моделей: какие модели настроены. Хранится в зашифрованном хранилище (`__models__`); ключи API — там же,
под именами ПРОВАЙДЕР.api_key, и читаются только через брокер учётных данных.

Правила зон здесь НЕ настраиваются — их жёстко держит роутер: S2 никому, S1 и P только локальным, облако только для O.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from jura.adapters.models import anthropic, gemini, openai_compat
from jura.contracts.errors import JuraError, ModelError, VaultError
from jura.contracts.model import ModelPort
from jura.kernel.credentials import CredentialBroker
from jura.kernel.vault import Vault

MODELS_KEY = "__models__"
PROVIDERS = ("anthropic", "openai", "gemini", "local")
KEY_NAMES = {"anthropic": "anthropic.api_key", "openai": "openai.api_key", "gemini": "gemini.api_key"}
NAME_RE = re.compile(r"^[\w.-]{1,40}$")


@dataclass(frozen=True)
class ModelEntry:
    name: str
    provider: str
    model: str
    base_url: str | None = None
    context: int | None = None
    max_output: int | None = None
    quality: int | None = None

    @property
    def is_local(self) -> bool:
        return self.provider == "local"


class ModelRegistry:
    def __init__(self, vault: Vault, broker: CredentialBroker, allow_insecure_http: bool = False):
        self._vault, self._broker, self._insecure = vault, broker, allow_insecure_http

    # --- хранение ---
    def entries(self) -> list[ModelEntry]:
        try:
            raw = json.loads(self._vault.get(MODELS_KEY))
        except VaultError:
            return []
        return [ModelEntry(**e) for e in raw]

    def _save(self, entries: list[ModelEntry]) -> None:
        self._vault.set(MODELS_KEY, json.dumps([e.__dict__ for e in entries], ensure_ascii=False))

    def add(self, name: str, provider: str, model: str, *, base_url: str | None = None, context: int | None = None,
            max_output: int | None = None, quality: int | None = None) -> ModelEntry:
        if not NAME_RE.match(name):
            raise JuraError("имя модели: буквы, цифры, точка, дефис, подчёркивание (до 40 символов)")
        if provider not in PROVIDERS:
            raise JuraError(f"провайдер должен быть одним из: {', '.join(PROVIDERS)}")
        if not model.strip():
            raise JuraError("не указано имя модели у провайдера (список: model discover ПРОВАЙДЕР)")
        for label, value in (("context", context), ("max-output", max_output)):
            if value is not None and value <= 0:
                raise JuraError(f"--{label} должно быть положительным числом")
        entries = self.entries()
        if any(e.name == name for e in entries):
            raise JuraError(f"модель '{name}' уже есть (удали: model remove {name})")
        entry = ModelEntry(name, provider, model, base_url, context, max_output, quality)
        self._adapter(entry)  # проверка настроек (например, «локальная» модель обязана быть на этом компьютере)
        self._save([*entries, entry])
        return entry

    def remove(self, name: str) -> None:
        entries = self.entries()
        if not any(e.name == name for e in entries):
            raise JuraError(f"модели '{name}' нет")
        self._save([e for e in entries if e.name != name])

    def key_present(self, provider: str) -> bool:
        return provider not in KEY_NAMES or KEY_NAMES[provider] in self._vault.names()

    # --- сборка адаптеров ---
    def _key_getter(self, provider: str):
        get = self._broker.getter_for(f"model.{provider}", re.escape(KEY_NAMES[provider]))
        return lambda: get(KEY_NAMES[provider])

    def _adapter(self, e: ModelEntry) -> ModelPort:
        common: dict[str, Any] = {"name": e.name, "max_output": e.max_output, "context_tokens": e.context,
                                  "quality": e.quality}
        insecure = self._insecure
        if e.provider == "local":
            return openai_compat.OpenAICompatModel(
                e.model, base_url=e.base_url or openai_compat.OLLAMA_API, local=True, allow_insecure_http=insecure, **common)
        getter = self._key_getter(e.provider)
        if e.provider == "openai":
            return openai_compat.OpenAICompatModel(
                e.model, base_url=e.base_url or openai_compat.OPENAI_API, get_api_key=getter, allow_insecure_http=insecure, **common)
        kwargs = {k: v for k, v in {"max_output": e.max_output, "context_tokens": e.context, "quality": e.quality}.items()
                  if v is not None}
        cls = anthropic.AnthropicModel if e.provider == "anthropic" else gemini.GeminiModel
        return cls(e.model, getter, name=e.name, base_url=e.base_url or (anthropic.API if e.provider == "anthropic" else gemini.API),
                   allow_insecure_http=insecure, **kwargs)

    def build(self) -> tuple[list[ModelPort], dict[str, str]]:
        """(рабочие модели, {имя: причина}) — одна испорченная запись не мешает остальным."""
        models, errors = [], {}
        for e in self.entries():
            try:
                models.append(self._adapter(e))
            except (ModelError, JuraError, TypeError) as exc:
                errors[e.name] = str(exc)
        return models, errors

    # --- поиск доступных моделей у провайдера ---
    def discover(self, provider: str, base_url: str | None = None) -> list[str]:
        if provider == "local":
            return openai_compat.list_models(base_url=base_url or openai_compat.OLLAMA_API, local=True,
                                             allow_insecure_http=self._insecure)
        if provider not in KEY_NAMES:
            raise JuraError(f"провайдер должен быть одним из: {', '.join(PROVIDERS)}")
        getter = self._key_getter(provider)
        if provider == "anthropic":
            return anthropic.list_models(getter, base_url=base_url or anthropic.API, allow_insecure_http=self._insecure)
        if provider == "gemini":
            return gemini.list_models(getter, base_url=base_url or gemini.API, allow_insecure_http=self._insecure)
        return openai_compat.list_models(base_url=base_url or openai_compat.OPENAI_API, get_api_key=getter,
                                         allow_insecure_http=self._insecure)
