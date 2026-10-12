"""«Работа под паролем»: хранилище → политика → подтверждение → инструмент → результат, без утечек."""

import json
import tempfile
import unittest
from pathlib import Path

from localservers import make_auth_server, stop, url
from test_connectors import FakeDriver
from jura.adapters.connectors.web_login import LoginOutcome
from jura.contracts.errors import JuraError, WrongCredentials
from jura.contracts.types import Risk, TaskState as S, ToolSpec, Zone
from jura.session import Session, leak_scan

PASS = "правильная-лошадь-батарея-скоба"
SITE_PASSWORD = "Zq7-uniq-pass-9931"


class OperateTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.home = Path(self.dir.name) / "home"
        self.server, self.seen = make_auth_server("alice", SITE_PASSWORD)
        self.addCleanup(stop, self.server)
        self.s, _ = Session.create(self.home, PASS, kdf_n=2**10, allow_insecure_http=True)
        self.addCleanup(self.dir.cleanup)
        self.addCleanup(self.close)
        self.s.secret_set("demo.username", "alice")
        self.s.secret_set("demo.password", SITE_PASSWORD)
        self.args = {"url": url(self.server), "secret": "demo"}

    def close(self):
        try:
            self.s.close()
        except Exception:
            pass

    def reopen(self, **kw):
        self.s.close()
        self.s = Session.open(self.home, passphrase=PASS, allow_insecure_http=True, **kw)
        return self.s

    yes = staticmethod(lambda text: True)
    no = staticmethod(lambda text: False)
    pw = staticmethod(lambda text: PASS)          # повторный ввод пароля хранилища при подтверждении
    cancel = staticmethod(lambda text: "")        # пустой ввод = отмена

    # --- политика ---
    def test_everything_is_denied_by_default_and_no_request_is_sent(self):
        res = self.s.run_tool("http.basic_check", self.args, self.yes, self.pw)
        self.assertIs(res.state, S.FAILED)
        self.assertIn("не включена", res.reason)
        self.assertEqual(self.seen, [])

    def test_enable_requires_the_passphrase_again(self):
        with self.assertRaises(WrongCredentials):
            self.s.policy_enable("http.basic", "не-тот-пароль-совсем")
        self.assertEqual(self.s.policy_enabled(), [])

    def test_unknown_capability(self):
        with self.assertRaises(JuraError):
            self.s.policy_enable("nuclear.launch", PASS)

    def test_policy_survives_restart_and_can_be_revoked(self):
        self.s.policy_enable("http.basic", PASS)
        self.assertEqual(self.reopen().policy_enabled(), ["http.basic"])
        self.s.policy_disable("http.basic", PASS)
        self.assertEqual(self.reopen().policy_enabled(), [])

    def test_policy_changes_are_audited(self):
        self.s.policy_enable("http.basic", PASS)
        kinds = [e.kind for e in self.s.app.log.events()]
        self.assertIn("policy.reauth", kinds)
        self.assertIn("policy.changed", kinds)

    def test_internal_entries_are_hidden(self):
        self.s.policy_enable("http.basic", PASS)
        self.assertNotIn("__policy__", self.s.secret_list())

    # --- работа ---
    def test_password_check_against_a_real_server(self):
        self.s.policy_enable("http.basic", PASS)
        res = self.s.run_tool("http.basic_check", self.args, self.yes, self.pw)
        self.assertIs(res.state, S.DONE)
        self.assertIn("HTTP 200", res.output)

    def test_wrong_password_is_reported_not_hidden(self):
        self.s.policy_enable("http.basic", PASS)
        self.s.secret_set("demo.password", "неверный-пароль")
        self.assertIn("HTTP 401", self.s.run_tool("http.basic_check", self.args, self.yes, self.pw).output)

    def test_refusing_the_confirmation_sends_nothing(self):
        self.s.policy_enable("http.basic", PASS)
        res = self.s.run_tool("http.basic_check", self.args, self.yes, self.cancel)
        self.assertIs(res.state, S.CANCELLED)
        self.assertEqual(self.seen, [])

    def test_confirmation_text_shows_what_will_happen(self):
        self.s.policy_enable("http.basic", PASS)
        shown = []
        self.s.run_tool("http.basic_check", self.args, self.yes, lambda t: shown.append(t) or "")
        self.assertIn("http.basic_check", shown[0])
        self.assertIn("secret=demo", shown[0])
        self.assertIn("сохранённые пароли", shown[0])
        self.assertNotIn(SITE_PASSWORD, shown[0])

    def test_wrong_passphrase_at_confirmation_cancels_and_sends_nothing(self):
        self.s.policy_enable("http.basic", PASS)
        with self.assertRaises(WrongCredentials):
            self.s.run_tool("http.basic_check", self.args, self.yes, lambda t: "не-тот-пароль-совсем")
        self.assertEqual(self.seen, [])
        self.assertEqual(self.s.app.tasks.list_by_state(S.CANCELLED)[-1].title, "tool:http.basic_check")

    def test_yes_alone_is_not_enough_for_tools_that_use_passwords(self):
        """Простого «да» с разблокированной консоли мало: пароли используются только после повторного ввода пароля."""
        self.s.policy_enable("http.basic", PASS)
        with self.assertRaises(JuraError):
            self.s.run_tool("http.basic_check", self.args, self.yes)  # способа ввести пароль нет
        self.assertEqual(self.seen, [])

    def test_plain_l2_tool_still_needs_only_yes_no(self):
        calls = []
        self.s.app.registry.register(ToolSpec("notes.send", "", Risk.L2, "notes.send"), lambda a: calls.append(a) or "ок")
        self.s.policy_enable("notes.send", PASS)
        self.assertIs(self.s.run_tool("notes.send", {}, self.no).state, S.CANCELLED)
        self.assertEqual(calls, [])
        self.assertIs(self.s.run_tool("notes.send", {}, self.yes).state, S.DONE)
        self.assertEqual(len(calls), 1)

    def test_results_carry_labels(self):
        self.s.policy_enable("http.basic", PASS)
        res = self.s.run_tool("http.basic_check", self.args, self.yes, self.pw)
        self.assertEqual((res.result.zone, res.result.trust.value, res.result.source), (Zone.P, "untrusted", "http.basic_check"))

    def test_insecure_url_is_refused_with_a_clear_reason(self):
        self.s.close()
        self.s = Session.open(self.home, passphrase=PASS)  # боевой режим: http запрещён
        self.s.policy_enable("http.basic", PASS)
        res = self.s.run_tool("http.basic_check", self.args, self.yes, self.pw)
        self.assertIs(res.state, S.FAILED)
        self.assertIn("https", res.reason)
        self.assertEqual(self.seen, [])

    def test_l3_cannot_be_approved_from_the_console(self):
        """Деньги: консоль не может дать Face ID — действие отменяется, обработчик не вызывается."""
        calls = []
        self.s.app.registry.register(ToolSpec("bank.transfer", "перевод", Risk.L3, "bank.transfer"),
                                     lambda a: calls.append(a) or "переведено")
        self.s.policy_enable("bank.transfer", PASS)
        with self.assertRaises(JuraError) as cm:
            self.s.run_tool("bank.transfer", {"amount": "5000"}, self.yes)
        self.assertIn("BIOMETRIC", str(cm.exception))
        self.assertEqual(calls, [])
        self.assertEqual(self.s.app.tasks.list_by_state(S.CANCELLED)[-1].title, "tool:bank.transfer")

    def test_unknown_tool_fails_cleanly(self):
        self.assertIs(self.s.run_tool("нет-такого", {}, self.yes).state, S.FAILED)

    # --- утечки ---
    def test_password_is_nowhere_on_disk_or_in_the_log(self):
        self.s.policy_enable("http.basic", PASS)
        self.s.run_tool("http.basic_check", self.args, self.yes, self.pw)
        events_text = json.dumps([e.payload for e in self.s.app.log.events()], ensure_ascii=False)
        self.assertNotIn(SITE_PASSWORD, events_text)
        self.s.close()
        self.assertEqual(leak_scan(SITE_PASSWORD, [self.home])[0], [])

    # --- вход через браузер (драйвер подменён) ---
    def test_web_login_flow_with_profile_in_vault(self):
        driver = FakeDriver(LoginOutcome(True, "страница: open.example.org"))
        self.s.close()
        self.s = Session.open(self.home, passphrase=PASS, allow_insecure_http=True, browser_driver=driver)
        self.s.site_set("example", {"url": "https://accounts.example.org/login", "user_selector": "#u",
                                    "password_selector": "#p", "submit_selector": "#go"})
        self.s.secret_set("example.username", "alice")
        self.s.secret_set("example.password", "Pw-веб-5521")
        self.s.policy_enable("web.login", PASS)
        res = self.s.run_tool("web.login", {"site": "example"}, self.yes, self.pw)
        self.assertIs(res.state, S.DONE)
        self.assertIn("✓ вход выполнен", res.output)
        self.assertEqual(driver.calls[0][1:], ("alice", "Pw-веб-5521"))
        self.assertEqual(self.s.sites(), ["example"])
        self.s.close()
        self.assertEqual(leak_scan("accounts.example.org", [self.home])[0], [], "профиль сайта должен быть зашифрован")
        self.assertEqual(leak_scan("Pw-веб-5521", [self.home])[0], [])

    def test_web_login_site_must_exist(self):
        self.s.policy_enable("web.login", PASS)
        res = self.s.run_tool("web.login", {"site": "нет"}, self.yes, self.pw)
        self.assertIs(res.state, S.FAILED)
        self.assertIn("site add", res.reason)


if __name__ == "__main__":
    unittest.main()
