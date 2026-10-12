import unittest

from helpers import ALL_CAPS, NOW, PHONE, T0, T2, T3, approval, envelope
from jura.contracts.errors import (
    AlreadyExecuted, AmbiguousEffect, ApprovalRequired, PolicyDenied, ToolError, ToolNotFound, ToolUserError,
)
from jura.contracts.types import AuthLevel, Zone
from jura.kernel.db import Database
from jura.kernel.events import EventLog
from jura.kernel.outbox import Outbox
from jura.kernel.policy import PolicyConfig, PolicyEngine
from jura.kernel.tools import ToolPort, ToolRegistry, call_digest


class ToolPortTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.addCleanup(self.db.close)
        self.log = EventLog(self.db)
        self.outbox = Outbox(self.db)
        self.reg = ToolRegistry()
        self.calls = []
        self.policy = PolicyEngine(PolicyConfig(enabled_capabilities=ALL_CAPS))
        self.port = ToolPort(self.reg, self.policy, self.log, self.outbox, clock=lambda: NOW)
        for spec in (T0, T2, T3):
            self.reg.register(spec, lambda a, s=spec: self.calls.append((s.name, a)) or f"{s.name}-ok")

    def all_log_text(self):
        return str([e.payload for e in self.log.events()])

    def test_allowed_call_runs_and_is_logged(self):
        self.assertEqual(self.port.call(envelope(), "t0", {"q": 1}).data, "t0-ok")
        self.assertEqual([e.kind for e in self.log.events()], ["tool.decision", "tool.executed"])

    def test_values_and_results_never_reach_the_log(self):
        self.port.call(envelope(), "t0", {"password": "hunter2-секрет"})
        self.assertNotIn("hunter2", self.all_log_text())
        self.assertIn("password", self.all_log_text())  # имена аргументов — можно, значения — нет

    def test_denied_call_does_not_run_handler(self):
        self.policy.replace_config(PolicyConfig(), principal=envelope().principal,
                                   auth_level=AuthLevel.HARDWARE_KEY)
        with self.assertRaises(PolicyDenied):
            self.port.call(envelope(), "t0")
        self.assertEqual(self.calls, [])

    def test_ask_then_approved(self):
        with self.assertRaises(ApprovalRequired) as cm:
            self.port.call(envelope(), "t2", {"to": "a@b.c"})
        self.assertEqual(self.calls, [])
        ok = approval(cm.exception.call_digest)
        self.assertEqual(self.port.call(envelope(), "t2", {"to": "a@b.c"}, approval=ok).data, "t2-ok")

    def test_approval_for_one_call_cannot_run_another(self):
        ok = approval(call_digest("t2", {"to": "a@b.c"}))
        with self.assertRaises(ApprovalRequired):
            self.port.call(envelope(), "t2", {"to": "злоумышленник@x.y"}, approval=ok)
        self.assertEqual(self.calls, [])

    def test_unknown_tool(self):
        with self.assertRaises(ToolNotFound):
            self.port.call(envelope(), "нет-такого")
        self.assertEqual(self.log.events()[-1].kind, "tool.unknown")

    def test_handler_error_hides_details(self):
        def boom(args):
            raise RuntimeError("ключ=SECRET123")
        from jura.contracts.types import Risk, ToolSpec
        self.reg.register(ToolSpec("boom", "", Risk.L0, "demo.read"), boom)
        with self.assertRaises(ToolError) as cm:
            self.port.call(envelope(), "boom")
        self.assertNotIn("SECRET123", str(cm.exception))
        self.assertNotIn("SECRET123", self.all_log_text())

    def test_tool_user_error_shows_its_own_safe_message(self):
        def refuse(args):
            raise ToolUserError("разрешён только https")
        from jura.contracts.types import Risk, ToolSpec
        self.reg.register(ToolSpec("careful", "", Risk.L0, "demo.read"), refuse)
        with self.assertRaises(ToolError) as cm:
            self.port.call(envelope(), "careful")
        self.assertEqual(str(cm.exception), "разрешён только https")
        self.assertIn("разрешён только https", self.all_log_text())

    def test_irreversible_action_runs_at_most_once(self):
        env, args = envelope(), {"to": "a@b.c"}
        ok = approval(call_digest("t2", args))
        self.port.call(env, "t2", args, approval=ok, task_id="task-1")
        with self.assertRaises(AlreadyExecuted):
            self.port.call(env, "t2", args, approval=ok, task_id="task-1")
        self.assertEqual(len(self.calls), 1)

    def test_interrupted_action_is_flagged_not_repeated(self):
        env, args = envelope(), {"to": "a@b.c"}
        digest = call_digest("t2", args)
        self.outbox.enqueue(f"task-2:{digest}", "t2", {})   # «упали посреди действия»
        with self.assertRaises(AmbiguousEffect):
            self.port.call(env, "t2", args, approval=approval(digest), task_id="task-2")
        self.assertEqual(self.calls, [])

    def test_l3_end_to_end(self):
        args = {"amount": 5000}
        with self.assertRaises(ApprovalRequired) as cm:
            self.port.call(envelope(), "t3", args)
        self.assertEqual(cm.exception.required_auth, AuthLevel.BIOMETRIC)
        face_id = approval(cm.exception.call_digest, auth=AuthLevel.BIOMETRIC, device=PHONE)
        self.assertEqual(self.port.call(envelope(), "t3", args, approval=face_id).data, "t3-ok")

    def test_zone_violation_is_denied(self):
        with self.assertRaises(PolicyDenied):
            self.port.call(envelope(), "t0", data_zone=Zone.S1)

    def test_duplicate_registration(self):
        with self.assertRaises(ValueError):
            self.reg.register(T0, lambda a: None)

    def test_log_stays_valid(self):
        self.port.call(envelope(), "t0")
        self.assertTrue(self.log.verify().ok)


if __name__ == "__main__":
    unittest.main()
