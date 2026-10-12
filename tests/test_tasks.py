import unittest

from helpers import envelope
from jura.contracts.errors import IllegalTransition, TaskNotFound
from jura.contracts.types import TaskState as S
from jura.kernel.db import Database
from jura.kernel.events import EventLog
from jura.kernel.tasks import TaskManager


class TaskTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.addCleanup(self.db.close)
        self.log = EventLog(self.db)
        self.tm = TaskManager(self.db, self.log)

    def test_fast_path(self):
        t = self.tm.create(envelope(), "x")
        self.tm.transition(t.id, S.RUNNING)
        self.assertEqual(self.tm.transition(t.id, S.DONE).state, S.DONE)

    def test_every_transition_is_logged_atomically(self):
        t = self.tm.create(envelope(), "x")
        self.tm.transition(t.id, S.RUNNING, "go")
        self.tm.transition(t.id, S.DONE)
        kinds = [e.kind for e in self.log.events(task_id=t.id)]
        self.assertEqual(kinds, ["task.created", "task.transition", "task.transition"])
        self.assertTrue(self.log.verify().ok)

    def test_terminal_states_are_final(self):
        t = self.tm.create(envelope(), "x")
        self.tm.transition(t.id, S.RUNNING)
        self.tm.transition(t.id, S.DONE)
        with self.assertRaises(IllegalTransition):
            self.tm.transition(t.id, S.RUNNING)

    def test_illegal_jump(self):
        t = self.tm.create(envelope(), "x")
        with self.assertRaises(IllegalTransition):
            self.tm.transition(t.id, S.VERIFYING)

    def test_full_path_cannot_skip_plan_or_verify(self):
        t = self.tm.create(envelope(), "x", requires_verification=True)
        with self.assertRaises(IllegalTransition):
            self.tm.transition(t.id, S.RUNNING)
        self.tm.transition(t.id, S.PLANNED)
        self.tm.transition(t.id, S.RUNNING)
        with self.assertRaises(IllegalTransition):
            self.tm.transition(t.id, S.DONE)
        self.tm.transition(t.id, S.VERIFYING)
        self.tm.transition(t.id, S.RUNNING)   # самокоррекция
        self.tm.transition(t.id, S.VERIFYING)
        self.assertEqual(self.tm.transition(t.id, S.DONE).state, S.DONE)

    def test_failed_transition_writes_nothing(self):
        t = self.tm.create(envelope(), "x")
        before = self.log.head()[0]
        with self.assertRaises(IllegalTransition):
            self.tm.transition(t.id, S.VERIFYING)
        self.assertEqual(self.log.head()[0], before)

    def test_not_found(self):
        with self.assertRaises(TaskNotFound):
            self.tm.get("нет такой")

    def test_request_text_is_not_logged(self):
        self.tm.create(envelope(text="мой секретный пароль hunter2"), "x")
        self.assertNotIn("hunter2", str([e.payload for e in self.log.events()]))


if __name__ == "__main__":
    unittest.main()
