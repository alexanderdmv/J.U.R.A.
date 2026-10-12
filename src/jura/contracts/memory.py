"""Контракт памяти. Сегодня SQLite, завтра что угодно — ядро не заметит."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from jura.contracts.types import Trust, Zone


@dataclass(frozen=True)
class MemoryItem:
    id: str
    content: str
    zone: Zone
    source: str                 # откуда факт (provenance)
    trust: Trust
    confidence: float
    created_at: float
    verified: bool              # UNTRUSTED не становится фактом, пока человек не подтвердил
    tags: tuple[str, ...] = ()
    supersedes: str | None = None


class MemoryPort(Protocol):
    def write(self, content: str, *, zone: Zone, source: str, trust: Trust,
              confidence: float = 1.0, tags: tuple[str, ...] = ()) -> MemoryItem: ...

    def query(self, text: str, *, max_zone: Zone, limit: int = 10) -> list[MemoryItem]:
        """Фильтр по зоне выполняется ВНУТРИ памяти, до того как данные уйдут наружу."""
        ...

    def supersede(self, old_id: str, content: str, *, zone: Zone, source: str, trust: Trust,
                  confidence: float = 1.0) -> MemoryItem:
        """Новое не стирает старое, а заменяет его с историей."""
        ...

    def confirm(self, item_id: str) -> None: ...

    def forget(self, item_id: str) -> None: ...
