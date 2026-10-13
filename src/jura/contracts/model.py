"""Контракт модели. Ядро знает только этот интерфейс, а не OpenAI/Claude/Gemini."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from jura.contracts.types import Zone


@dataclass(frozen=True)
class ModelCapabilities:
    """Паспорт модели: что умеет и какие данные ей можно отдавать."""

    name: str
    local: bool                 # работает на нашем железе
    max_zone: Zone              # самая секретная зона, которую можно ей отдать
    context_tokens: int
    tool_calling: bool = False
    structured_output: bool = False
    modalities: tuple[str, ...] = ("text",)
    cost_per_1k_tokens: float = 0.0
    quality: int = 0            # относительный ранг; позже заполняется результатами evals


@dataclass(frozen=True)
class ModelRequest:
    prompt: str
    zone: Zone                  # самая секретная зона данных внутри prompt
    needs_tools: bool = False
    needs_structured: bool = False
    min_context: int = 0
    prefer: str = "quality"     # "quality" | "cost" | "local"
    only: str | None = None     # выбрать модель с этим именем; правила зон и возможностей при этом всё равно действуют
    # Диалог (ADR-015): инструкция модели и прошлые реплики. Их зона входит в `zone` — роутер смотрит только на неё.
    system: str = ""
    history: tuple[Turn, ...] = ()

    def messages(self) -> list[dict[str, str]]:
        """История + текущий запрос в общем виде {role: user|assistant, content}; адаптеры переводят в формат вендора."""
        return [{"role": t.role, "content": t.text} for t in self.history] + [{"role": "user", "content": self.prompt}]


@dataclass(frozen=True)
class Turn:
    role: str                   # "user" | "assistant"
    text: str


@dataclass(frozen=True)
class ModelResponse:
    text: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0


class ModelPort(Protocol):
    capabilities: ModelCapabilities

    def complete(self, request: ModelRequest) -> ModelResponse:
        """Любой сбой вендора адаптер обязан превратить в jura.contracts.errors.ModelError."""
        ...
