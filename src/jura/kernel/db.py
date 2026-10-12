"""Тонкая обёртка над SQLite: одно соединение, блокировка и вложенные транзакции.

Главное свойство: всё, что сделано внутри `with db.transaction():`, записывается
целиком или не записывается вообще. Это основа надёжности (состояние задачи и событие
в журнале никогда не расходятся).
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class Database:
    def __init__(self, path: str | Path):
        # isolation_level=None: транзакциями управляем сами, явно
        self._conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.RLock()
        self._depth = 0

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Внешний вызов открывает транзакцию, вложенные — SAVEPOINT (откатываются отдельно)."""
        with self._lock:
            outer = self._depth == 0
            savepoint = f"sp_{self._depth}"
            self._conn.execute("BEGIN IMMEDIATE" if outer else f"SAVEPOINT {savepoint}")
            self._depth += 1
            try:
                yield self._conn
            except BaseException:
                self._depth -= 1
                if outer:
                    self._conn.execute("ROLLBACK")
                else:
                    self._conn.execute(f"ROLLBACK TO {savepoint}")
                    self._conn.execute(f"RELEASE {savepoint}")
                raise
            else:
                self._depth -= 1
                self._conn.execute("COMMIT" if outer else f"RELEASE {savepoint}")

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def executescript(self, script: str) -> None:
        with self._lock:
            self._conn.executescript(script)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
