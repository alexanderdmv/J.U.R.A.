"""Журнал событий (Event Log): авторитетная ИСТОРИЯ всего, что делала система.

Свойства:
  * append-only — триггеры в БД запрещают UPDATE и DELETE;
  * hash-chain — каждое событие хранит хэш предыдущего, поэтому любая правка в середине
    журнала обнаруживается (tamper-evident);
  * якорь (anchor) — «seq:hash» последнего события. Если хранить его ВНЕ этой машины,
    обнаруживается и обрезание хвоста журнала. Без внешнего якоря хвост можно срезать
    незаметно — это известное ограничение, а не баг.

Шифрование: если задан шифр, содержимое события (payload) хранится зашифрованным. Хэш цепочки
считается по ШИФРОТЕКСТУ, поэтому целостность журнала (и резервной копии) проверяется БЕЗ ключа.
Открытыми остаются номер, время, вид события и исполнитель; закрыто всё, что внутри.

Честно: это «обнаружимо», а не «невозможно подменить».
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Callable

from jura.contracts.crypto import Cipher, pack_text, unpack_text
from jura.kernel.db import Database

GENESIS = "0" * 64
SCHEMA_VERSION = 1  # версия формата события; нужна для будущего upcasting (ADR-009)
LOG_PURPOSE = "log"


@dataclass(frozen=True)
class Event:
    seq: int
    ts: float
    kind: str
    actor: str
    task_id: str | None
    payload: dict[str, Any]
    schema_version: int
    prev_hash: str
    hash: str


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    checked: int
    broken_at: int | None = None
    reason: str = ""


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def _hash(seq: int, ts: float, kind: str, actor: str, task_id: str | None,
          payload_json: str, schema_version: int, prev_hash: str) -> str:
    data = json.dumps([seq, ts, kind, actor, task_id, payload_json, schema_version, prev_hash],
                      ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


class EventLog:
    def __init__(self, db: Database, clock: Callable[[], float] = time.time, cipher: Cipher | None = None):
        self._db = db
        self._clock = clock
        self._cipher = cipher
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS events(
                seq INTEGER PRIMARY KEY,
                ts REAL NOT NULL,
                kind TEXT NOT NULL,
                actor TEXT NOT NULL,
                task_id TEXT,
                payload TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                prev_hash TEXT NOT NULL,
                hash TEXT NOT NULL
            );
            CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
                BEGIN SELECT RAISE(ABORT, 'event log is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
                BEGIN SELECT RAISE(ABORT, 'event log is append-only'); END;
            """
        )

    def append(self, kind: str, actor: str, payload: dict[str, Any] | None = None,
               task_id: str | None = None) -> Event:
        payload = payload or {}
        payload_json = _canonical(payload)
        with self._db.transaction():
            rows = self._db.query("SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1")
            seq, prev = (rows[0]["seq"] + 1, rows[0]["hash"]) if rows else (1, GENESIS)
            ts = self._clock()
            stored = pack_text(self._cipher, payload_json, purpose=LOG_PURPOSE, aad=f"event:{seq}:payload")
            h = _hash(seq, ts, kind, actor, task_id, stored, SCHEMA_VERSION, prev)  # хэш — по тому, что лежит на диске
            self._db.execute(
                "INSERT INTO events(seq, ts, kind, actor, task_id, payload, schema_version, prev_hash, hash)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (seq, ts, kind, actor, task_id, stored, SCHEMA_VERSION, prev, h),
            )
        return Event(seq, ts, kind, actor, task_id, json.loads(payload_json), SCHEMA_VERSION, prev, h)

    def _row_to_event(self, r) -> Event:
        # allow_plaintext: старые события (до включения шифрования) защищены цепочкой хэшей
        text = unpack_text(self._cipher, r["payload"], purpose=LOG_PURPOSE,
                           aad=f"event:{r['seq']}:payload", allow_plaintext=True)
        return Event(r["seq"], r["ts"], r["kind"], r["actor"], r["task_id"], json.loads(text),
                     r["schema_version"], r["prev_hash"], r["hash"])

    def events(self, after_seq: int = 0, task_id: str | None = None) -> list[Event]:
        if task_id is None:
            rows = self._db.query("SELECT * FROM events WHERE seq > ? ORDER BY seq", (after_seq,))
        else:
            rows = self._db.query(
                "SELECT * FROM events WHERE seq > ? AND task_id = ? ORDER BY seq", (after_seq, task_id))
        return [self._row_to_event(r) for r in rows]

    def head(self) -> tuple[int, str]:
        rows = self._db.query("SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1")
        return (rows[0]["seq"], rows[0]["hash"]) if rows else (0, GENESIS)

    def anchor(self) -> str:
        """Строка для хранения вне Core (другое устройство, бумага, подпись ключом)."""
        seq, h = self.head()
        return f"{seq}:{h}"

    def verify(self, anchor: str | None = None) -> VerifyResult:
        prev, expected_seq, checked = GENESIS, 1, 0
        for r in self._db.query("SELECT * FROM events ORDER BY seq"):
            if r["seq"] != expected_seq:
                return VerifyResult(False, checked, r["seq"], "нарушена нумерация (пропуск события)")
            if r["prev_hash"] != prev:
                return VerifyResult(False, checked, r["seq"], "разрыв цепочки хэшей")
            calc = _hash(r["seq"], r["ts"], r["kind"], r["actor"], r["task_id"], r["payload"],
                         r["schema_version"], r["prev_hash"])
            if calc != r["hash"]:
                return VerifyResult(False, checked, r["seq"], "содержимое события изменено")
            prev, expected_seq, checked = r["hash"], expected_seq + 1, checked + 1
        if anchor:
            try:
                a_seq_s, a_hash = anchor.split(":", 1)
                a_seq = int(a_seq_s)
            except ValueError:
                return VerifyResult(False, checked, None, "якорь имеет неверный формат")
            row = self._db.query("SELECT hash FROM events WHERE seq = ?", (a_seq,))
            if not row or row[0]["hash"] != a_hash:
                return VerifyResult(False, checked, a_seq, "якорь не совпадает: журнал обрезан или переписан")
        return VerifyResult(True, checked)
