"""Task State Machine: у задачи всегда понятное состояние, а каждый переход — событие в журнале.

Состояние задачи и запись в журнале делаются в ОДНОЙ транзакции: они не могут разойтись.

Два пути (см. диаграмму 03):
  fast path : CREATED → RUNNING → DONE
  full path : CREATED → PLANNED → RUNNING → VERIFYING → DONE   (requires_verification=True)
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Callable

from jura.contracts.crypto import Cipher, pack_text, unpack_text
from jura.contracts.errors import IllegalTransition, TaskNotFound
from jura.contracts.types import Envelope, TaskState
from jura.kernel.db import Database
from jura.kernel.events import EventLog

S = TaskState

ALLOWED: dict[TaskState, frozenset[TaskState]] = {
    S.CREATED: frozenset({S.PLANNED, S.RUNNING, S.CANCELLED, S.FAILED}),
    S.PLANNED: frozenset({S.AWAITING_APPROVAL, S.RUNNING, S.SUSPENDED, S.CANCELLED, S.FAILED}),
    S.AWAITING_APPROVAL: frozenset({S.RUNNING, S.CANCELLED, S.FAILED}),
    S.RUNNING: frozenset({S.AWAITING_APPROVAL, S.VERIFYING, S.DONE, S.SUSPENDED, S.CANCELLED, S.FAILED}),
    S.VERIFYING: frozenset({S.DONE, S.RUNNING, S.FAILED, S.CANCELLED}),  # RUNNING = самокоррекция
    S.SUSPENDED: frozenset({S.RUNNING, S.CANCELLED}),
    S.DONE: frozenset(),
    S.FAILED: frozenset(),
    S.CANCELLED: frozenset(),
}


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    state: TaskState
    principal: str
    requires_verification: bool
    created_at: float
    updated_at: float


class TaskManager:
    def __init__(self, db: Database, log: EventLog, clock: Callable[[], float] = time.time,
                 cipher: Cipher | None = None):
        self._db, self._log, self._clock, self._cipher = db, log, clock, cipher
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks(
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                state TEXT NOT NULL,
                principal TEXT NOT NULL,
                requires_verification INTEGER NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            """
        )

    def _title(self, task_id: str, stored: str) -> str:
        return unpack_text(self._cipher, stored, purpose="tasks", aad=f"task:{task_id}:title")

    def _to_task(self, r) -> Task:
        return Task(r["id"], self._title(r["id"], r["title"]), TaskState(r["state"]), r["principal"],
                    bool(r["requires_verification"]), r["created_at"], r["updated_at"])

    def create(self, envelope: Envelope, title: str, requires_verification: bool = False) -> Task:
        task_id, now = uuid.uuid4().hex, self._clock()
        with self._db.transaction():
            self._db.execute(
                "INSERT INTO tasks(id, title, state, principal, requires_verification, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (task_id, pack_text(self._cipher, title, purpose="tasks", aad=f"task:{task_id}:title"),
                 S.CREATED.value, str(envelope.principal), int(requires_verification), now, now),
            )
            # Текст запроса в журнал НЕ пишем: он может быть из зоны S1/S2.
            self._log.append(
                "task.created", str(envelope.principal),
                {"title": title, "envelope_id": envelope.id, "zone": envelope.zone.name,
                 "trust": envelope.trust.value, "auth": envelope.auth_level.name,
                 "device": envelope.device.id if envelope.device else None,
                 "requires_verification": requires_verification},
                task_id,
            )
        return self.get(task_id)

    def get(self, task_id: str) -> Task:
        rows = self._db.query("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if not rows:
            raise TaskNotFound(task_id)
        return self._to_task(rows[0])

    def list_by_state(self, state: TaskState) -> list[Task]:
        return [self._to_task(r) for r in
                self._db.query("SELECT * FROM tasks WHERE state = ? ORDER BY created_at", (state.value,))]

    def transition(self, task_id: str, to: TaskState, reason: str = "") -> Task:
        with self._db.transaction():
            task = self.get(task_id)
            if to not in ALLOWED[task.state]:
                raise IllegalTransition(f"{task.state.value} → {to.value} запрещён")
            if task.requires_verification:
                if task.state is S.CREATED and to is S.RUNNING:
                    raise IllegalTransition("full path: сначала PLANNED")
                if task.state is S.RUNNING and to is S.DONE:
                    raise IllegalTransition("full path: сначала VERIFYING")
            self._db.execute("UPDATE tasks SET state = ?, updated_at = ? WHERE id = ?",
                             (to.value, self._clock(), task_id))
            self._log.append("task.transition", task.principal,
                             {"from": task.state.value, "to": to.value, "reason": reason}, task_id)
        return self.get(task_id)
