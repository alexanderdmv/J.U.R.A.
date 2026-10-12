"""Tool Port (PEP): единственный путь вызова инструментов.

Любой вызов проходит: поиск → политика → (подтверждение) → защита от повтора → исполнение → журнал.
Обработчики хранятся внутри реестра; остальной код получает только ToolPort.call().

В журнал пишутся имя инструмента, риск, зона, хэш вызова и решение — но НЕ значения аргументов
и НЕ результат: там могут быть данные зон S1/S2.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Callable

from jura.contracts.errors import (
    AlreadyExecuted, AmbiguousEffect, ApprovalRequired, PolicyDenied, ToolError, ToolNotFound, ToolUserError,
)
from jura.contracts.types import (
    Approval, DecisionKind, Envelope, Risk, ToolResult, ToolSpec, Trust, Zone, worst_trust,
)
from jura.kernel.events import EventLog
from jura.kernel.outbox import Outbox
from jura.kernel.policy import PolicyContext, PolicyEngine

Handler = Callable[[dict[str, Any]], Any]


def call_digest(tool_name: str, args: dict[str, Any]) -> str:
    """Хэш конкретного вызова. Подтверждение действительно только для него."""
    data = json.dumps([tool_name, args], sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), default=str)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def as_result(spec: ToolSpec, raw: Any) -> ToolResult:
    """Приводит любой возврат обработчика к ToolResult. Метки нельзя сделать мягче, чем заявлено в ToolSpec."""
    if isinstance(raw, ToolResult):
        return ToolResult(raw.data, max(raw.zone, spec.result_zone), worst_trust(raw.trust, spec.result_trust),
                          raw.source or spec.name)
    return ToolResult(raw, spec.result_zone, spec.result_trust, spec.name)


class ToolRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._handlers: dict[str, Handler] = {}

    def register(self, spec: ToolSpec, handler: Handler) -> None:
        if spec.name in self._specs:
            raise ValueError(f"инструмент '{spec.name}' уже зарегистрирован")
        self._specs[spec.name] = spec
        self._handlers[spec.name] = handler

    def spec(self, name: str) -> ToolSpec:
        try:
            return self._specs[name]
        except KeyError:
            raise ToolNotFound(name) from None

    def specs(self) -> list[ToolSpec]:
        return sorted(self._specs.values(), key=lambda s: s.name)

    def _handler(self, name: str) -> Handler:  # использовать только внутри ToolPort
        return self._handlers[name]


class ToolPort:
    def __init__(self, registry: ToolRegistry, policy: PolicyEngine, log: EventLog,
                 outbox: Outbox | None = None, clock: Callable[[], float] = time.time):
        self._registry, self._policy, self._log = registry, policy, log
        self._outbox, self._clock = outbox, clock

    def call(self, envelope: Envelope, tool_name: str, args: dict[str, Any] | None = None, *,
             data_zone: Zone | None = None, tainted: bool | None = None,
             approval: Approval | None = None, task_id: str | None = None) -> ToolResult:
        args = args or {}
        actor = str(envelope.principal)
        try:
            spec = self._registry.spec(tool_name)
        except ToolNotFound:
            self._log.append("tool.unknown", actor, {"tool": tool_name}, task_id)
            raise

        zone = data_zone if data_zone is not None else envelope.zone
        is_tainted = tainted if tainted is not None else envelope.trust is Trust.UNTRUSTED
        digest = call_digest(tool_name, args)
        decision = self._policy.decide(PolicyContext(
            principal=envelope.principal, device=envelope.device, auth_level=envelope.auth_level,
            tool=spec, data_zone=zone, tainted=is_tainted, call_digest=digest,
            approval=approval, now=self._clock()))

        self._log.append("tool.decision", actor, {
            "tool": tool_name, "risk": spec.risk.name, "capability": spec.capability,
            "zone": zone.name, "tainted": is_tainted, "digest": digest,
            "decision": decision.kind.value, "reason": decision.reason,
            "arg_names": sorted(args)}, task_id)

        if decision.kind is DecisionKind.DENY:
            raise PolicyDenied(decision.reason)
        if decision.kind is DecisionKind.ASK:
            raise ApprovalRequired(decision.reason, decision.required_auth, digest)

        key = self._guard_irreversible(spec, digest, task_id)
        try:
            result = self._registry._handler(tool_name)(args)
        except ToolUserError as exc:  # инструмент сам гарантирует, что текст безопасен
            self._log.append("tool.failed", actor, {"tool": tool_name, "digest": digest,
                                                     "error_type": "ToolUserError", "message": str(exc)}, task_id)
            raise ToolError(str(exc)) from None
        except Exception as exc:  # текст ошибки не сохраняем: там могут быть секреты
            self._log.append("tool.failed", actor, {"tool": tool_name, "digest": digest,
                                                     "error_type": type(exc).__name__}, task_id)
            raise ToolError(f"инструмент '{tool_name}' завершился ошибкой ({type(exc).__name__})") from None
        if key and self._outbox:
            self._outbox.mark_done(key)
        result = as_result(spec, result)
        self._log.append("tool.executed", actor, {"tool": tool_name, "digest": digest, "ok": True,
                                                   "result_zone": result.zone.name,
                                                   "result_trust": result.trust.value}, task_id)
        return result

    def _guard_irreversible(self, spec: ToolSpec, digest: str, task_id: str | None) -> str | None:
        """Необратимые действия (L2+, не idempotent) выполняются не более одного раза на (задача, вызов)."""
        if not (self._outbox and task_id and spec.risk >= Risk.L2 and not spec.idempotent):
            return None
        key = f"{task_id}:{digest}"
        if self._outbox.enqueue(key, spec.name, {"digest": digest}, task_id):
            return key
        if self._outbox.status(key) == "done":
            raise AlreadyExecuted(f"действие '{spec.name}' уже выполнено")
        raise AmbiguousEffect(f"действие '{spec.name}' было начато, но результат неизвестен — проверь вручную")
