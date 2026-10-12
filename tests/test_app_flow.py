"""Сквозные сценарии: запрос → задача → политика → инструмент → журнал."""

import tempfile
import unittest

from helpers import ALL_CAPS, NOW, PHONE, T0, T2, T3, approval, envelope, JURA
from jura.app import Jura
from jura.contracts.types import AuthLevel, TaskState as S
from jura.kernel.policy import PolicyConfig


class AppFlowTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.app = Jura(self.dir.name, policy_config=PolicyConfig(enabled_capabilities=ALL_CAPS),
                        clock=lambda: NOW)
        self.sent = []
        self.app.registry.register(T0, lambda a: "читаю")
        self.app.registry.register(T2, lambda a: self.sent.append(a) or "отправлено")
        self.app.registry.register(T3, lambda a: self.sent.append(a) or "переведено")

    def tearDown(self):
        self.app.close()
        self.dir.cleanup()

    def test_fast_path_done(self):
        r = self.app.execute_fast(envelope(), "t0")
        self.assertEqual((r.state, r.output), (S.DONE, "читаю"))
        self.assertTrue(self.app.log.verify().ok)

    def test_l2_waits_for_approval_then_runs_once(self):
        r = self.app.execute_fast(envelope(), "t2", {"to": "a@b.c"})
        self.assertEqual(r.state, S.AWAITING_APPROVAL)
        self.assertEqual(self.sent, [])
        r2 = self.app.approve(r.task_id, approval(r.call_digest))
        self.assertEqual((r2.state, r2.output), (S.DONE, "отправлено"))
        self.assertEqual(len(self.sent), 1)

    def test_weak_approval_keeps_task_waiting(self):
        r = self.app.execute_fast(envelope(), "t3", {"amount": 5000})
        self.assertEqual(r.required_auth, AuthLevel.BIOMETRIC)
        weak = self.app.approve(r.task_id, approval(r.call_digest, auth=AuthLevel.DEVICE, device=PHONE))
        self.assertEqual(weak.state, S.AWAITING_APPROVAL)
        self.assertEqual(self.sent, [])
        strong = self.app.approve(r.task_id, approval(r.call_digest, auth=AuthLevel.BIOMETRIC, device=PHONE))
        self.assertEqual(strong.state, S.DONE)
        self.assertEqual(len(self.sent), 1)

    def test_reject_cancels(self):
        r = self.app.execute_fast(envelope(), "t2", {"to": "x"})
        self.assertEqual(self.app.reject(r.task_id).state, S.CANCELLED)
        self.assertEqual(self.sent, [])

    def test_autonomous_l3_fails(self):
        r = self.app.execute_fast(envelope(principal=JURA, device=None, auth=AuthLevel.NONE), "t3", {"amount": 1})
        self.assertEqual(r.state, S.FAILED)
        self.assertEqual(self.sent, [])

    def test_unknown_tool_fails_cleanly(self):
        self.assertEqual(self.app.execute_fast(envelope(), "нет-такого").state, S.FAILED)

    def test_default_deny_when_policy_is_empty(self):
        self.dir2 = tempfile.TemporaryDirectory()
        app = Jura(self.dir2.name, clock=lambda: NOW)
        app.registry.register(T0, lambda a: "x")
        self.assertEqual(app.execute_fast(envelope(), "t0").state, S.FAILED)
        app.close()
        self.dir2.cleanup()

    def test_policy_change_is_audited(self):
        self.app.policy.replace_config(PolicyConfig(enabled_capabilities=frozenset({"demo.read"})),
                                       principal=envelope().principal, auth_level=AuthLevel.HARDWARE_KEY)
        ev = [e for e in self.app.log.events() if e.kind == "policy.changed"]
        self.assertEqual(len(ev), 1)
        self.assertIn("mail.send", ev[0].payload["disabled"])

    def test_state_and_log_agree(self):
        r = self.app.execute_fast(envelope(), "t0")
        kinds = [e.kind for e in self.app.log.events(task_id=r.task_id)]
        self.assertEqual(kinds[0], "task.created")
        self.assertEqual(kinds[-1], "task.transition")
        self.assertIn("tool.executed", kinds)


if __name__ == "__main__":
    unittest.main()
