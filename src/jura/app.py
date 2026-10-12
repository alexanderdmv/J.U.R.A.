"""Сборка системы (composition root): здесь, и только здесь, компоненты соединяются.

`execute_fast` — тонкий сквозной срез (walking skeleton): запрос → задача → политика →
инструмент → результат, и всё это в журнале. Умных агентов здесь ещё нет, и это намеренно:
сначала проверяем границы, потом наращиваем интеллект.

Известное ограничение Phase 0: ожидающий подтверждения вызов хранится в памяти процесса,
поэтому после перезапуска он теряется. Сделать его durable (в БД) — первая задача Phase 1.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from jura.adapters.memory.sqlite_memory import SqliteMemory
from jura.contracts.errors import (
    AlreadyExecuted, AmbiguousEffect, ApprovalRequired, JuraError, PolicyDenied, ToolError, ToolNotFound,
)
from jura.contracts.crypto import Cipher
from jura.contracts.model import ModelPort
from jura.contracts.types import Approval, AuthLevel, Envelope, Principal, TaskState, ToolResult
from jura.kernel.db import Database
from jura.kernel.events import EventLog
from jura.kernel.outbox import Outbox
from jura.kernel.policy import PolicyConfig, PolicyEngine
from jura.kernel.tasks import TaskManager
from jura.kernel.tools import ToolPort, ToolRegistry
from jura.runtime.router import ModelRouter


@dataclass(frozen=True)
class TaskResult:
    task_id: str
    state: TaskState
    output: Any = None
    result: ToolResult | None = None   # те же данные вместе с метками: зона, доверие, источник
    reason: str = ""
    required_auth: AuthLevel | None = None
    call_digest: str = ""


class Jura:
    def __init__(self, data_dir: str | Path, *, policy_config: PolicyConfig | None = None,
                 models: Sequence[ModelPort] = (), clock: Callable[[], float] = time.time,
                 cipher: Cipher | None = None):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = Database(self.data_dir / "jura.db")
        self.log = EventLog(self.db, clock, cipher)
        self.tasks = TaskManager(self.db, self.log, clock, cipher)
        self.outbox = Outbox(self.db, clock)
        self.policy = PolicyEngine(policy_config, on_change=self._on_policy_change)
        self.registry = ToolRegistry()
        self.tools = ToolPort(self.registry, self.policy, self.log, self.outbox, clock)
        self.models = ModelRouter(models, audit=lambda kind, p: self.log.append(kind, "router", p))
        self.memory = SqliteMemory(self.data_dir / "memory.db", cipher)
        self._pending: dict[str, tuple[Envelope, str, dict[str, Any]]] = {}

    def set_models(self, models: Sequence[ModelPort]) -> None:
        """Заменяет набор моделей (правила зон в роутере остаются теми же)."""
        self.models = ModelRouter(models, audit=lambda kind, p: self.log.append(kind, "router", p))

    def _on_policy_change(self, old: PolicyConfig, new: PolicyConfig, principal: Principal) -> None:
        self.log.append("policy.changed", str(principal), {
            "enabled": sorted(new.enabled_capabilities - old.enabled_capabilities),
            "disabled": sorted(old.enabled_capabilities - new.enabled_capabilities)})

    # --- сквозной срез ---
    def execute_fast(self, envelope: Envelope, tool_name: str, args: dict[str, Any] | None = None, *,
                     approval: Approval | None = None) -> TaskResult:
        task = self.tasks.create(envelope, f"tool:{tool_name}")
        self.tasks.transition(task.id, TaskState.RUNNING, "fast path")
        return self._run(task.id, envelope, tool_name, args or {}, approval)

    def approve(self, task_id: str, approval: Approval) -> TaskResult:
        """Продолжить задачу, ожидающую подтверждения."""
        if task_id not in self._pending:
            raise JuraError("нет ожидающего подтверждения вызова для этой задачи")
        envelope, tool_name, args = self._pending.pop(task_id)
        self.tasks.transition(task_id, TaskState.RUNNING, "получено подтверждение")
        return self._run(task_id, envelope, tool_name, args, approval)

    def reject(self, task_id: str) -> TaskResult:
        self._pending.pop(task_id, None)
        self.tasks.transition(task_id, TaskState.CANCELLED, "отклонено пользователем")
        return TaskResult(task_id, TaskState.CANCELLED, reason="отклонено пользователем")

    def _run(self, task_id: str, envelope: Envelope, tool_name: str, args: dict[str, Any],
             approval: Approval | None) -> TaskResult:
        try:
            out = self.tools.call(envelope, tool_name, args, approval=approval, task_id=task_id)
        except ApprovalRequired as exc:
            self._pending[task_id] = (envelope, tool_name, args)
            self.tasks.transition(task_id, TaskState.AWAITING_APPROVAL, exc.reason)
            return TaskResult(task_id, TaskState.AWAITING_APPROVAL, reason=exc.reason,
                              required_auth=exc.required_auth, call_digest=exc.call_digest)
        except (PolicyDenied, ToolNotFound, ToolError, AlreadyExecuted, AmbiguousEffect) as exc:
            reason = getattr(exc, "reason", None) or str(exc)
            self.tasks.transition(task_id, TaskState.FAILED, reason)
            return TaskResult(task_id, TaskState.FAILED, reason=reason)
        self.tasks.transition(task_id, TaskState.DONE, "ok")
        return TaskResult(task_id, TaskState.DONE, output=out.data, result=out)

    def close(self) -> None:
        self.memory.close()
        self.db.close()
