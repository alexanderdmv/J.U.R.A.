"""Model Router: выбирает модель под задачу и НЕ ПУСКАЕТ секретные данные туда, куда нельзя.

Жёсткие правила (не зависят от настроек и качества моделей):
  * зона S2 — ни одна модель не получает данные никогда;
  * зона S1 и зона P — ТОЛЬКО локальные модели. Облако — только для зоны O (открытое). Облако для P появится
    лишь вместе с Redaction Gateway, который превратит очищенный контекст в НОВЫЙ контекст зоны O (ADR-005);
  * модель получает данные не секретнее, чем заявлено в её паспорте (max_zone).

Остальное — предпочтения: качество, цена или локальность. Если лучшая модель упала,
пробуем следующую. Все вызовы пишутся в аудит (модель, зона, объём), но НЕ текст запроса.
"""

from __future__ import annotations

from typing import Any, Callable, Sequence

from jura.contracts.errors import ModelError, NoModelAvailable, ZoneViolation
from jura.contracts.model import ModelPort, ModelRequest, ModelResponse
from jura.contracts.types import Zone

Audit = Callable[[str, dict[str, Any]], None]


class ModelRouter:
    def __init__(self, models: Sequence[ModelPort], audit: Audit | None = None):
        self._models = list(models)
        self._audit = audit or (lambda kind, payload: None)

    @property
    def models(self) -> list[ModelPort]:
        return list(self._models)

    def candidates(self, req: ModelRequest) -> list[ModelPort]:
        if req.zone >= Zone.S2:
            raise ZoneViolation("данные зоны S2 не передаются ни одной модели")
        ok = []
        for m in self._models:
            c = m.capabilities
            if c.max_zone < req.zone:
                continue
            if req.zone >= Zone.P and not c.local:  # облако — только для открытых данных (до Redaction Gateway)
                continue
            if req.only and c.name != req.only:
                continue
            if req.min_context > c.context_tokens:
                continue
            if req.needs_tools and not c.tool_calling:
                continue
            if req.needs_structured and not c.structured_output:
                continue
            ok.append(m)
        if not ok:
            hint = " (облачные модели допускаются только для зоны O, пока нет Redaction Gateway)" if req.zone >= Zone.P else ""
            which = f" (запрошена модель '{req.only}')" if req.only else ""
            raise NoModelAvailable(f"нет подходящей модели для зоны {req.zone.name}{which}{hint}")
        keys = {
            "quality": lambda m: (-m.capabilities.quality, m.capabilities.cost_per_1k_tokens),
            "cost": lambda m: (m.capabilities.cost_per_1k_tokens, -m.capabilities.quality),
            "local": lambda m: (not m.capabilities.local, -m.capabilities.quality),
        }
        return sorted(ok, key=keys.get(req.prefer, keys["quality"]))

    def complete(self, req: ModelRequest) -> ModelResponse:
        last_error: Exception | None = None
        for model in self.candidates(req):
            c = model.capabilities
            try:
                resp = model.complete(req)
            except ModelError as exc:
                self._audit("model.failed", {"model": c.name, "zone": req.zone.name, "local": c.local})
                last_error = exc
                continue
            self._audit("model.call", {
                "model": c.name, "zone": req.zone.name, "local": c.local,
                "prompt_chars": len(req.prompt), "tokens_in": resp.tokens_in, "tokens_out": resp.tokens_out})
            return resp
        raise NoModelAvailable(f"все подходящие модели не ответили: {last_error}")
