"""Память на SQLite. Реализует контракт MemoryPort; ядро о SQLite ничего не знает.

Правила, которые соблюдает этот адаптер:
  * S2 в память не попадает никогда (секреты живут только в Vault);
  * фильтр по зоне выполняется внутри запроса;
  * факты из недоверенных источников «непроверенные» и не возвращаются, пока человек их не подтвердил;
  * supersede не стирает старое, а помечает замещённым;
  * если задан шифр: содержимое, источник и теги хранятся ЗАШИФРОВАННЫМИ (свой ключ на каждую зону),
    а открытый текст в зашифрованной базе считается подменой и отвергается.

Открытыми остаются: идентификатор, зона, время, флаги, ссылки между версиями. Закрыто всё остальное.
Поиск пока работает так: подходящие по зоне записи расшифровываются и сравниваются в памяти процесса
(для личного объёма достаточно; индекс — позже).
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from jura.contracts.crypto import Cipher, pack_text, unpack_text
from jura.contracts.errors import ZoneViolation
from jura.contracts.memory import MemoryItem
from jura.contracts.types import Trust, Zone


class SqliteMemory:
    def __init__(self, path: str | Path = ":memory:", cipher: Cipher | None = None):
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._cipher = cipher
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS memory(
                    id TEXT PRIMARY KEY, content TEXT NOT NULL, zone INTEGER NOT NULL,
                    source TEXT NOT NULL, trust TEXT NOT NULL, confidence REAL NOT NULL,
                    created_at REAL NOT NULL, verified INTEGER NOT NULL, tags TEXT NOT NULL,
                    supersedes TEXT, superseded_by TEXT)""")
            self._conn.commit()

    # --- шифрование полей ---
    def _pack(self, text: str, zone: Zone, item_id: str, field: str) -> str:
        return pack_text(self._cipher, text, purpose=f"memory.{zone.name}", aad=f"memory:{item_id}:{field}")

    def _unpack(self, stored: str, zone: Zone, item_id: str, field: str) -> str:
        return unpack_text(self._cipher, stored, purpose=f"memory.{zone.name}", aad=f"memory:{item_id}:{field}")

    def _to_item(self, r: sqlite3.Row) -> MemoryItem:
        zone = Zone(r["zone"])
        tags = self._unpack(r["tags"], zone, r["id"], "tags")
        return MemoryItem(r["id"], self._unpack(r["content"], zone, r["id"], "content"), zone,
                          self._unpack(r["source"], zone, r["id"], "source"), Trust(r["trust"]),
                          r["confidence"], r["created_at"], bool(r["verified"]),
                          tuple(t for t in tags.split(",") if t), r["supersedes"])

    # --- запись ---
    def write(self, content: str, *, zone: Zone, source: str, trust: Trust,
              confidence: float = 1.0, tags: tuple[str, ...] = ()) -> MemoryItem:
        return self._insert(content, zone, source, trust, confidence, tags, None)

    def _insert(self, content, zone, source, trust, confidence, tags, supersedes, commit: bool = True) -> MemoryItem:
        if zone >= Zone.S2:
            raise ZoneViolation("зона S2 не хранится в памяти — только в Vault")
        item_id, now = uuid.uuid4().hex, time.time()
        verified = trust is not Trust.UNTRUSTED
        with self._lock:
            self._conn.execute(
                "INSERT INTO memory(id, content, zone, source, trust, confidence, created_at, verified, tags, supersedes)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (item_id, self._pack(content, zone, item_id, "content"), int(zone),
                 self._pack(source, zone, item_id, "source"), trust.value, confidence, now, int(verified),
                 self._pack(",".join(tags), zone, item_id, "tags"), supersedes))
            if commit:
                self._conn.commit()
        return MemoryItem(item_id, content, zone, source, trust, confidence, now, verified, tags, supersedes)

    # --- чтение ---
    def query(self, text: str, *, max_zone: Zone, limit: int = 10) -> list[MemoryItem]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM memory WHERE zone <= ? AND verified = 1 AND superseded_by IS NULL"
                " ORDER BY created_at DESC", (int(max_zone),)).fetchall()
        words = [w for w in re.findall(r"\w+", text.lower()) if w]
        scored = []
        for r in rows:
            item = self._to_item(r)  # расшифровка здесь; ошибка = неверный ключ или подмена
            hay = (item.content + " " + " ".join(item.tags)).lower()
            score = sum(1 for w in words if w in hay)
            if score or not words:
                scored.append((score, item))
        scored.sort(key=lambda p: (-p[0], -p[1].created_at))  # при равенстве свежие выше
        return [item for _, item in scored[:limit]]

    def count_by_zone(self) -> dict[str, int]:
        """Сколько записей в каждой зоне — без расшифровки."""
        with self._lock:
            rows = self._conn.execute("SELECT zone, COUNT(*) AS n FROM memory GROUP BY zone").fetchall()
        return {Zone(r["zone"]).name: r["n"] for r in rows}

    # --- изменения ---
    def supersede(self, old_id: str, content: str, *, zone: Zone, source: str, trust: Trust,
                  confidence: float = 1.0) -> MemoryItem:
        """Новая версия и пометка старой записываются ОДНОЙ транзакцией: при сбое посередине не появится две «живые» версии."""
        with self._lock:
            try:
                new = self._insert(content, zone, source, trust, confidence, (), old_id, commit=False)
                self._mark_superseded(old_id, new.id)
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
        return new

    def _mark_superseded(self, old_id: str, new_id: str) -> None:
        self._conn.execute("UPDATE memory SET superseded_by = ? WHERE id = ?", (new_id, old_id))

    def confirm(self, item_id: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE memory SET verified = 1 WHERE id = ?", (item_id,))
            self._conn.commit()

    def forget(self, item_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM memory WHERE id = ?", (item_id,))
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
