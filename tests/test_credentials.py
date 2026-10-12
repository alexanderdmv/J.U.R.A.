import tempfile
import unittest
from pathlib import Path

from jura.adapters.connectors import http_basic, web_login
from jura.contracts.errors import CredentialDenied, VaultError
from jura.kernel.credentials import CredentialBroker
from jura.kernel.vault import Vault

PASS = "правильная-лошадь-батарея-скоба"


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.vault, _ = Vault.create(Path(self.dir.name) / "v", PASS, kdf_n=2**10)
        for name, value in {"demo.username": "alice", "demo.password": "Pw-4417", "мойсайт.password": "ру-пароль",
                            "site.demo": "{}", "binance.api_key": "KEY", "binance.api_secret": "SECRET-ЗНАЧЕНИЕ",
                            "anthropic.api_key": "sk-123"}.items():
            self.vault.set(name, value)
        self.audit = []
        self.broker = CredentialBroker(self.vault, audit=lambda k, p: self.audit.append((k, p)))

    def test_allowed_names_are_served_and_audited_without_values(self):
        get = self.broker.getter_for("web.login", web_login.NAME_PATTERN)
        self.assertEqual(get("demo.password"), "Pw-4417")
        self.assertEqual(get("site.demo"), "{}")
        self.assertEqual(get("мойсайт.password"), "ру-пароль")
        self.assertEqual([k for k, _ in self.audit], ["credential.used"] * 3)
        self.assertEqual(self.audit[0][1], {"connector": "web.login", "name": "demo.password"})
        self.assertNotIn("Pw-4417", str(self.audit))

    def test_connector_cannot_read_other_secrets(self):
        """Пример из ревью: web.login может читать example.username/password, но не binance.api_secret."""
        for connector, pattern in (("web.login", web_login.NAME_PATTERN), ("http.basic", http_basic.NAME_PATTERN)):
            get = self.broker.getter_for(connector, pattern)
            for forbidden in ("binance.api_secret", "binance.api_key", "anthropic.api_key", "__policy__"):
                with self.assertRaises(CredentialDenied):
                    get(forbidden)
        self.assertEqual(self.audit[0][0], "credential.denied")
        self.assertNotIn("SECRET-ЗНАЧЕНИЕ", str(self.audit))

    def test_http_basic_cannot_read_site_profiles(self):
        with self.assertRaises(CredentialDenied):
            self.broker.getter_for("http.basic", http_basic.NAME_PATTERN)("site.demo")

    def test_internal_entries_are_never_served_even_with_a_permissive_pattern(self):
        self.vault.set("__policy__", "{}")
        with self.assertRaises(CredentialDenied):
            self.broker.getter_for("evil", r".*")("__policy__")

    def test_pattern_must_match_the_whole_name(self):
        get = self.broker.getter_for("http.basic", http_basic.NAME_PATTERN)
        for sneaky in ("demo.password.extra", "x demo.password", "demo.password\n", "demo.passwordX"):
            with self.assertRaises(CredentialDenied):
                get(sneaky)

    def test_missing_secret_is_a_plain_vault_error_not_a_denial(self):
        get = self.broker.getter_for("http.basic", http_basic.NAME_PATTERN)
        with self.assertRaises(VaultError) as cm:
            get("нет.password")
        self.assertNotIsInstance(cm.exception, CredentialDenied)


if __name__ == "__main__":
    unittest.main()
