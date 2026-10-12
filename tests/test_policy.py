import unittest

from helpers import (
    ALL_CAPS, CAR, DESKTOP, EXTERNAL, JURA, NOW, PHONE, T0, T1, T2, T3, TS2, TRIGGER, USER, approval,
)
from jura.contracts.errors import PolicyDenied
from jura.contracts.types import AuthLevel, DecisionKind as K, Zone
from jura.kernel.policy import PolicyConfig, PolicyContext, PolicyEngine


def engine(caps=ALL_CAPS):
    return PolicyEngine(PolicyConfig(enabled_capabilities=frozenset(caps)))


def ctx(tool, *, principal=USER, auth=AuthLevel.DEVICE, zone=Zone.P, tainted=False, appr=None, digest="d1"):
    return PolicyContext(principal=principal, device=DESKTOP if principal is USER else None, auth_level=auth,
                         tool=tool, data_zone=zone, tainted=tainted, call_digest=digest, approval=appr, now=NOW)


class PolicyTests(unittest.TestCase):
    def kind(self, c, caps=ALL_CAPS):
        return engine(caps).decide(c).kind

    # --- default deny, принципалы, зоны ---
    def test_default_deny(self):
        self.assertIs(self.kind(ctx(T0), caps=()), K.DENY)

    def test_external_principal_is_denied(self):
        self.assertIs(self.kind(ctx(T0, principal=EXTERNAL)), K.DENY)

    def test_zone_above_tool_limit_is_denied(self):
        self.assertIs(self.kind(ctx(T1, zone=Zone.S1)), K.DENY)

    def test_s2_only_to_tool_that_declares_s2(self):
        self.assertIs(self.kind(ctx(TS2, zone=Zone.S2)), K.ALLOW)
        self.assertIs(self.kind(ctx(T1, zone=Zone.S2)), K.DENY)

    # --- уровни риска и аутентификация ---
    def test_l0_needs_nothing(self):
        self.assertIs(self.kind(ctx(T0, auth=AuthLevel.NONE)), K.ALLOW)

    def test_l1_needs_device_auth(self):
        d = engine().decide(ctx(T1, auth=AuthLevel.NONE))
        self.assertIs(d.kind, K.ASK)
        self.assertEqual(d.required_auth, AuthLevel.DEVICE)
        self.assertIs(self.kind(ctx(T1, auth=AuthLevel.DEVICE)), K.ALLOW)

    def test_l2_needs_approval(self):
        self.assertIs(self.kind(ctx(T2)), K.ASK)
        self.assertIs(self.kind(ctx(T2, appr=approval("d1"))), K.ALLOW)

    def test_approval_is_bound_to_exact_call(self):
        self.assertIs(self.kind(ctx(T2, appr=approval("другой-хэш"))), K.ASK)

    def test_approval_expires(self):
        self.assertIs(self.kind(ctx(T2, appr=approval("d1", at=NOW - 500))), K.ASK)

    def test_future_dated_approval_is_rejected(self):
        self.assertIs(self.kind(ctx(T2, appr=approval("d1", at=NOW + 50))), K.ASK)

    # --- L3: «переведи €5000» всегда требует сильную аутентификацию ---
    def test_l3_needs_biometric_even_if_user_gave_the_command(self):
        session_is_strong = ctx(T3, auth=AuthLevel.HARDWARE_KEY)  # сессия сильная, но без подтверждения
        self.assertIs(self.kind(session_is_strong), K.ASK)

    def test_l3_weak_approval_is_not_enough(self):
        d = engine().decide(ctx(T3, appr=approval("d1", auth=AuthLevel.DEVICE, device=PHONE)))
        self.assertIs(d.kind, K.ASK)
        self.assertEqual(d.required_auth, AuthLevel.BIOMETRIC)

    def test_l3_approval_from_low_tier_device_is_not_enough(self):
        self.assertIs(self.kind(ctx(T3, appr=approval("d1", auth=AuthLevel.BIOMETRIC, device=CAR))), K.ASK)

    def test_l3_biometric_on_phone_is_enough(self):
        self.assertIs(self.kind(ctx(T3, appr=approval("d1", auth=AuthLevel.BIOMETRIC, device=PHONE))), K.ALLOW)

    # --- автономные принципалы ---
    def test_autonomous_l3_is_always_denied(self):
        strong = approval("d1", auth=AuthLevel.HARDWARE_KEY, device=PHONE)
        self.assertIs(self.kind(ctx(T3, principal=JURA, appr=strong)), K.DENY)
        self.assertIs(self.kind(ctx(T3, principal=TRIGGER, appr=strong)), K.DENY)

    def test_autonomous_l2_becomes_a_proposal(self):
        self.assertIs(self.kind(ctx(T2, principal=JURA)), K.ASK)

    def test_autonomous_l1_is_allowed_without_device(self):
        self.assertIs(self.kind(ctx(T1, principal=JURA, auth=AuthLevel.NONE)), K.ALLOW)

    # --- taint ---
    def test_untrusted_context_mentions_taint(self):
        d = engine().decide(ctx(T2, tainted=True))
        self.assertIs(d.kind, K.ASK)
        self.assertIn("недоверенный", d.reason)

    # --- изменение самой политики ---
    def test_policy_change_needs_user_and_hardware_key(self):
        e, new = engine(), PolicyConfig(enabled_capabilities=frozenset({"x"}))
        for p, a in [(USER, AuthLevel.BIOMETRIC), (JURA, AuthLevel.HARDWARE_KEY), (USER, AuthLevel.NONE)]:
            with self.assertRaises(PolicyDenied):
                e.replace_config(new, principal=p, auth_level=a)
        self.assertEqual(e.config.enabled_capabilities, ALL_CAPS)

    def test_tools_using_passwords_always_need_a_passphrase_level_approval(self):
        from jura.contracts.types import Risk, ToolSpec
        tool = ToolSpec("t-cred", "", Risk.L2, "mail.send", uses_credentials=True)
        d = engine().decide(ctx(tool))
        self.assertIs(d.kind, K.ASK)
        self.assertEqual(d.required_auth, AuthLevel.PASSPHRASE)
        self.assertIs(self.kind(ctx(tool, appr=approval("d1", auth=AuthLevel.DEVICE))), K.ASK)      # «да» не годится
        self.assertIs(self.kind(ctx(tool, appr=approval("d1", auth=AuthLevel.PASSPHRASE))), K.ALLOW)

    def test_even_a_reversible_tool_needs_approval_if_it_uses_passwords(self):
        from jura.contracts.types import Risk, ToolSpec
        tool = ToolSpec("t-cred1", "", Risk.L1, "demo.write", uses_credentials=True)
        self.assertIs(self.kind(ctx(tool)), K.ASK)
        self.assertIs(self.kind(ctx(tool, appr=approval("d1", auth=AuthLevel.PASSPHRASE))), K.ALLOW)

    def test_auth_levels_are_ordered(self):
        L = AuthLevel
        self.assertTrue(L.NONE < L.DEVICE < L.PASSPHRASE < L.BIOMETRIC < L.HARDWARE_KEY)

    def test_passphrase_alone_cannot_change_policy_by_default(self):
        e = engine()
        with self.assertRaises(PolicyDenied):
            e.replace_config(PolicyConfig(), principal=USER, auth_level=AuthLevel.PASSPHRASE)

    def test_interim_console_mode_accepts_passphrase_but_nothing_weaker(self):
        e = PolicyEngine(PolicyConfig(change_min_auth=AuthLevel.PASSPHRASE))
        with self.assertRaises(PolicyDenied):
            e.replace_config(PolicyConfig(), principal=USER, auth_level=AuthLevel.DEVICE)
        with self.assertRaises(PolicyDenied):
            e.replace_config(PolicyConfig(), principal=JURA, auth_level=AuthLevel.HARDWARE_KEY)
        e.replace_config(PolicyConfig(), principal=USER, auth_level=AuthLevel.PASSPHRASE)

    def test_passphrase_does_not_satisfy_l3(self):
        weak = approval("d1", auth=AuthLevel.PASSPHRASE, device=PHONE)
        self.assertIs(self.kind(ctx(T3, appr=weak)), K.ASK)

    def test_policy_change_is_reported(self):
        seen = []
        e = PolicyEngine(PolicyConfig(), on_change=lambda old, new, p: seen.append((old, new, p)))
        e.replace_config(PolicyConfig(enabled_capabilities=frozenset({"x"})),
                         principal=USER, auth_level=AuthLevel.HARDWARE_KEY)
        self.assertEqual(len(seen), 1)


if __name__ == "__main__":
    unittest.main()
