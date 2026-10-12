"""Outbox: защита необратимых действий от двойного выполнения (ADR-004).

Перед тем как сделать что-то необратимое (письмо, перевод), мы записываем намерение
с уникальным ключом. Возможные исходы:
  * ключа нет         → записали, можно выполнять;
  * ключ есть, done   → уже выполнено, второй раз НЕ делаем;
  * ключ есть, pending → сбой посреди действия: неизвестно, произошло ли оно.
                         Молча повторять нельзя — нужна проверка человеком.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable

from jura.kernel.db import Database


@dataclass(frozen=True)
class OutboxItem:
    key: str
    kind: str
    status: str
    payload: dict[str, Any]
    task_id: str | None


class Outbox:
    def __init__(self, db: Database, clock: Callable[[], float] = time.time):
        self._db = db
        self._clock = clock
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS outbox(
                key TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                payload TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                task_id TEXT,
                created_at REAL NOT NULL,
                done_at REAL
            );
            """
        )

    def enqueue(self, key: str, kind: str, payload: dict[str, Any], task_id: str | None = None) -> bool:
        """True — ключ новый. False — такой ключ уже есть (смотри status())."""
        with self._db.transaction():
            if self._db.query("SELECT 1 FROM outbox WHERE key = ?", (key,)):
                return False
            self._db.execute(
                "INSERT INTO outbox(key, kind, payload, task_id, created_at) VALUES(?,?,?,?,?)",
                (key, kind, json.dumps(payload, sort_keys=True, default=str), task_id, self._clock()),
            )
            return True

    def status(self, key: str) -> str | None:
        rows = self._db.query("SELECT status FROM outbox WHERE key = ?", (key,))
        return rows[0]["status"] if rows else None

    def mark_done(self, key: str) -> None:
        self._db.execute("UPDATE outbox SET status='done', done_at=? WHERE key=?", (self._clock(), key))

    def pending(self) -> list[OutboxItem]:
        rows = self._db.query("SELECT * FROM outbox WHERE status='pending' ORDER BY created_at")
        return [OutboxItem(r["key"], r["kind"], r["status"], json.loads(r["payload"]), r["task_id"]) for r in rows]
