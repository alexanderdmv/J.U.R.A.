import sqlite3
import unittest

from jura.kernel.db import Database
from jura.kernel.events import EventLog


class EventLogTests(unittest.TestCase):
    def fresh(self):
        db = Database(":memory:")
        self.addCleanup(db.close)  # соединения не должны «утекать» из тестов
        return db, EventLog(db)

    def test_append_and_verify(self):
        _, log = self.fresh()
        for i in range(5):
            log.append("test", "user:owner", {"i": i})
        r = log.verify()
        self.assertTrue(r.ok)
        self.assertEqual(r.checked, 5)

    def test_events_are_chained(self):
        _, log = self.fresh()
        a = log.append("a", "x")
        b = log.append("b", "x")
        self.assertEqual(b.prev_hash, a.hash)
        self.assertEqual(log.head(), (2, b.hash))

    def test_update_and_delete_are_blocked(self):
        db, log = self.fresh()
        log.append("a", "x", {"v": 1})
        with self.assertRaises(sqlite3.DatabaseError):
            db.execute("UPDATE events SET payload = '{}' WHERE seq = 1")
        with self.assertRaises(sqlite3.DatabaseError):
            db.execute("DELETE FROM events WHERE seq = 1")

    def test_tampering_is_detected(self):
        db, log = self.fresh()
        for i in range(3):
            log.append("a", "x", {"i": i})
        db.execute("DROP TRIGGER events_no_update")  # атакующий с доступом к файлу
        db.execute("UPDATE events SET payload = '{\"i\":99}' WHERE seq = 2")
        r = log.verify()
        self.assertFalse(r.ok)
        self.assertEqual(r.broken_at, 2)

    def test_truncation_needs_external_anchor(self):
        db, log = self.fresh()
        for i in range(3):
            log.append("a", "x", {"i": i})
        anchor = log.anchor()
        db.execute("DROP TRIGGER events_no_delete")
        db.execute("DELETE FROM events WHERE seq = 3")
        self.assertTrue(log.verify().ok, "без якоря обрезание хвоста не видно — известное ограничение")
        self.assertFalse(log.verify(anchor).ok, "с внешним якорем обрезание обнаруживается")

    def test_bad_anchor_format(self):
        _, log = self.fresh()
        log.append("a", "x")
        self.assertFalse(log.verify("мусор").ok)

    def test_rolled_back_transaction_leaves_no_event(self):
        db, log = self.fresh()
        with self.assertRaises(RuntimeError):
            with db.transaction():
                log.append("a", "x")
                raise RuntimeError("сбой посреди операции")
        self.assertEqual(log.head()[0], 0)

    def test_nested_savepoint_rolls_back_only_inner(self):
        db, log = self.fresh()
        with db.transaction():
            log.append("outer", "x")
            try:
                with db.transaction():
                    log.append("inner", "x")
                    raise RuntimeError
            except RuntimeError:
                pass
        kinds = [e.kind for e in log.events()]
        self.assertEqual(kinds, ["outer"])
        self.assertTrue(log.verify().ok)


if __name__ == "__main__":
    unittest.main()
