import unittest

from jura.adapters.memory.sqlite_memory import SqliteMemory
from jura.contracts.errors import ZoneViolation
from jura.contracts.types import Trust, Zone


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.m = SqliteMemory(":memory:")
        self.addCleanup(self.m.close)

    def w(self, text, zone=Zone.P, trust=Trust.USER, **kw):
        return self.m.write(text, zone=zone, source="test", trust=trust, **kw)

    def test_write_and_query(self):
        self.w("Любимый язык программирования — Python")
        self.assertEqual(len(self.m.query("python", max_zone=Zone.P)), 1)
        self.assertEqual(self.m.query("rust", max_zone=Zone.P), [])

    def test_zone_filter_is_applied_inside(self):
        self.w("публичное", Zone.O)
        self.w("приватное", Zone.P)
        self.w("чувствительное", Zone.S1)
        texts = {i.content for i in self.m.query("", max_zone=Zone.P)}
        self.assertEqual(texts, {"публичное", "приватное"})

    def test_s2_is_never_stored(self):
        with self.assertRaises(ZoneViolation):
            self.w("пароль от банка", Zone.S2)

    def test_untrusted_is_invisible_until_confirmed(self):
        item = self.w("письмо утверждает, что счёт сменился", trust=Trust.UNTRUSTED)
        self.assertFalse(item.verified)
        self.assertEqual(self.m.query("счёт", max_zone=Zone.P), [])
        self.m.confirm(item.id)
        self.assertEqual(len(self.m.query("счёт", max_zone=Zone.P)), 1)

    def test_supersede_keeps_history_but_hides_old(self):
        old = self.w("Живу в городе А")
        new = self.m.supersede(old.id, "Живу в городе Б", zone=Zone.P, source="test", trust=Trust.USER)
        self.assertEqual(new.supersedes, old.id)
        found = [i.content for i in self.m.query("живу", max_zone=Zone.P)]
        self.assertEqual(found, ["Живу в городе Б"])

    def test_supersede_is_atomic(self):
        """Сбой между «записать новую» и «пометить старую» не оставляет две живые версии."""
        from unittest.mock import patch
        old = self.w("версия один")
        with patch.object(SqliteMemory, "_mark_superseded", side_effect=RuntimeError("сбой")):
            with self.assertRaises(RuntimeError):
                self.m.supersede(old.id, "версия два", zone=Zone.P, source="test", trust=Trust.USER)
        self.assertEqual([i.content for i in self.m.query("версия", max_zone=Zone.P)], ["версия один"])
        self.assertEqual(self.m._conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0], 1)
        self.w("после сбоя запись работает")  # соединение не «зависло» в полуоткрытой транзакции

    def test_forget(self):
        item = self.w("удали меня")
        self.m.forget(item.id)
        self.assertEqual(self.m.query("удали", max_zone=Zone.P), [])


if __name__ == "__main__":
    unittest.main()
