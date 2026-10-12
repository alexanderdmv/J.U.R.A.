import unittest

from helpers import ALL_CAPS, NOW, envelope
from jura.contracts.types import Risk, ToolResult, ToolSpec, Trust, Zone, worst_trust
from jura.kernel.db import Database
from jura.kernel.events import EventLog
from jura.kernel.policy import PolicyConfig, PolicyEngine
from jura.kernel.tools import ToolPort, ToolRegistry, as_result


class ToolResultTests(unittest.TestCase):
    def test_plain_return_gets_failsafe_labels(self):
        spec = ToolSpec("t", "", Risk.L0, "demo.read")
        res = as_result(spec, "сырой текст")
        self.assertEqual((res.data, res.zone, res.trust, res.source), ("сырой текст", Zone.P, Trust.UNTRUSTED, "t"))

    def test_handler_cannot_soften_the_declared_labels(self):
        spec = ToolSpec("mail.read", "", Risk.L0, "demo.read", result_zone=Zone.S1, result_trust=Trust.UNTRUSTED)
        res = as_result(spec, ToolResult("письмо", zone=Zone.O, trust=Trust.TRUSTED, source="gmail"))
        self.assertEqual((res.zone, res.trust, res.source), (Zone.S1, Trust.UNTRUSTED, "gmail"))

    def test_handler_may_make_labels_stricter(self):
        spec = ToolSpec("t", "", Risk.L0, "demo.read", result_zone=Zone.P, result_trust=Trust.TRUSTED)
        res = as_result(spec, ToolResult("x", zone=Zone.S1, trust=Trust.UNTRUSTED))
        self.assertEqual((res.zone, res.trust), (Zone.S1, Trust.UNTRUSTED))

    def test_trusted_local_tool_can_declare_trusted_result(self):
        spec = ToolSpec("notes", "", Risk.L1, "demo.write", result_trust=Trust.TRUSTED)
        self.assertEqual(as_result(spec, "ок").trust, Trust.TRUSTED)

    def test_worst_trust_ordering(self):
        self.assertEqual(worst_trust(Trust.TRUSTED, Trust.USER), Trust.USER)
        self.assertEqual(worst_trust(Trust.USER, Trust.UNTRUSTED, Trust.TRUSTED), Trust.UNTRUSTED)
        self.assertEqual(worst_trust(), Trust.TRUSTED)

    def test_tool_port_returns_labeled_result_and_logs_labels_not_data(self):
        db = Database(":memory:")
        self.addCleanup(db.close)
        log = EventLog(db)
        reg = ToolRegistry()
        reg.register(ToolSpec("mail.read", "", Risk.L0, "demo.read", result_zone=Zone.P), lambda a: "СЕКРЕТНОЕ-ПИСЬМО")
        port = ToolPort(reg, PolicyEngine(PolicyConfig(enabled_capabilities=ALL_CAPS)), log, clock=lambda: NOW)
        res = port.call(envelope(), "mail.read")
        self.assertIsInstance(res, ToolResult)
        self.assertEqual((res.zone, res.trust, res.source), (Zone.P, Trust.UNTRUSTED, "mail.read"))
        executed = [e for e in log.events() if e.kind == "tool.executed"][0].payload
        self.assertEqual((executed["result_zone"], executed["result_trust"]), ("P", "untrusted"))
        self.assertNotIn("СЕКРЕТНОЕ-ПИСЬМО", str([e.payload for e in log.events()]))


if __name__ == "__main__":
    unittest.main()
