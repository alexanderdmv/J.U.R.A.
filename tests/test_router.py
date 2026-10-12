import unittest

from fakes import FakeModel
from jura.contracts.errors import NoModelAvailable, ZoneViolation
from jura.contracts.model import ModelRequest
from jura.contracts.types import ToolResult, Trust, Zone, max_zone
from jura.runtime.router import ModelRouter


def req(zone=Zone.O, prompt="привет", **kw):
    return ModelRequest(prompt=prompt, zone=zone, **kw)


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.cloud_best = FakeModel("cloud-best", quality=9, cost=0.5)
        self.cloud_cheap = FakeModel("cloud-cheap", quality=3, cost=0.01)
        self.local = FakeModel("local", local=True, max_zone=Zone.S1, quality=5)

    def router(self, *models, audit=None):
        return ModelRouter(models, audit=audit)

    # --- жёсткие правила зон ---
    def test_s2_never_reaches_any_model(self):
        r = self.router(self.cloud_best, self.local)
        with self.assertRaises(ZoneViolation):
            r.complete(req(Zone.S2))
        self.assertEqual(self.local.calls + self.cloud_best.calls, [])

    def test_s1_goes_to_local_only(self):
        r = self.router(self.cloud_best, self.local)
        self.assertEqual(r.complete(req(Zone.S1)).model, "local")
        self.assertEqual(self.cloud_best.calls, [])

    def test_private_data_goes_to_local_only_until_redaction_gateway_exists(self):
        """Блокер из ревью: P в облако нельзя, пока нет Redaction Gateway (ADR-005)."""
        r = self.router(self.cloud_best, self.local)
        self.assertEqual(r.complete(req(Zone.P)).model, "local")
        self.assertEqual(self.cloud_best.calls, [])

    def test_private_data_with_only_cloud_models_is_refused_with_explanation(self):
        with self.assertRaises(NoModelAvailable) as cm:
            self.router(self.cloud_best, self.cloud_cheap).complete(req(Zone.P))
        self.assertIn("Redaction Gateway", str(cm.exception))

    def test_open_data_may_use_the_cloud(self):
        self.assertEqual(self.router(self.cloud_best, self.local).complete(req(Zone.O)).model, "cloud-best")

    def test_cloud_model_that_misdeclares_zone_is_still_excluded(self):
        liar = FakeModel("liar", local=False, max_zone=Zone.S1, quality=99)
        for zone in (Zone.P, Zone.S1):
            with self.assertRaises(NoModelAvailable):
                self.router(liar).complete(req(zone))

    def test_tool_result_labels_decide_where_the_data_may_go(self):
        """Письмо из почты (зона P, недоверенное) нельзя «забыть» и отправить в облако: зона берётся из ToolResult."""
        email = ToolResult("текст письма", zone=Zone.P, trust=Trust.UNTRUSTED, source="gmail")
        r = self.router(self.cloud_best)
        with self.assertRaises(NoModelAvailable):
            r.complete(req(max_zone(Zone.O, email.zone), prompt=email.data))
        self.assertEqual(self.cloud_best.calls, [])
        self.assertEqual(self.router(self.cloud_best, self.local).complete(
            req(email.zone, prompt=email.data)).model, "local")

    # --- предпочтения и сбои (на открытых данных, где облако допустимо) ---
    def test_prefers_quality_by_default_and_cost_on_request(self):
        r = self.router(self.cloud_cheap, self.cloud_best)
        self.assertEqual(r.complete(req()).model, "cloud-best")
        self.assertEqual(r.complete(req(prefer="cost")).model, "cloud-cheap")

    def test_falls_back_when_best_model_fails(self):
        broken = FakeModel("broken", quality=10, fail=True)
        self.assertEqual(self.router(broken, self.cloud_best).complete(req()).model, "cloud-best")

    def test_all_failing_raises(self):
        r = self.router(FakeModel("a", fail=True), FakeModel("b", fail=True))
        with self.assertRaises(NoModelAvailable):
            r.complete(req())

    def test_capability_filters(self):
        no_tools = FakeModel("nt", quality=10, tool_calling=False)
        self.assertEqual(self.router(no_tools, self.cloud_cheap).complete(req(needs_tools=True)).model, "cloud-cheap")
        with self.assertRaises(NoModelAvailable):
            self.router(FakeModel("small", context_tokens=1000)).complete(req(min_context=5000))

    def test_audit_has_metadata_but_not_the_prompt(self):
        seen = []
        r = self.router(self.cloud_best, audit=lambda kind, p: seen.append((kind, p)))
        r.complete(req(prompt="мой план hunter2"))
        self.assertEqual(seen[0][0], "model.call")
        self.assertNotIn("hunter2", str(seen))
        self.assertEqual(seen[0][1]["zone"], "O")


if __name__ == "__main__":
    unittest.main()
