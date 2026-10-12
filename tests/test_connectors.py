import unittest

from localservers import make_auth_server, make_recording_server, stop, url
from jura.adapters.connectors import http_basic, web_login
from jura.adapters.connectors.web_login import LoginOutcome
from jura.contracts.errors import ToolUserError, VaultError


def getter(data):
    def get(name):
        if name not in data:
            raise VaultError(f"секрет '{name}' не найден")
        return data[name]
    return get


class HttpBasicTests(unittest.TestCase):
    PASSWORD = "Zq7-uniq-pass-9931"

    def setUp(self):
        self.evil, self.evil_seen = make_recording_server()
        self.server, self.seen = make_auth_server("alice", self.PASSWORD, redirect_to=url(self.evil, "/steal"))
        self.addCleanup(stop, self.server)
        self.addCleanup(stop, self.evil)
        self.secrets = {"demo.username": "alice", "demo.password": self.PASSWORD}
        (_, self.run), = http_basic.make_tools(getter(self.secrets), allow_insecure_http=True)

    def test_correct_credentials_are_accepted(self):
        self.assertIn("HTTP 200", self.run({"url": url(self.server), "secret": "demo"}))

    def test_wrong_password_is_rejected(self):
        self.secrets["demo.password"] = "неверный"
        self.assertIn("HTTP 401", self.run({"url": url(self.server), "secret": "demo"}))

    def test_redirect_is_not_followed_and_password_does_not_leak(self):
        result = self.run({"url": url(self.server, "/redir"), "secret": "demo"})
        self.assertIn("HTTP 302", result)
        self.assertEqual(self.evil_seen, [], "пароль ушёл на сервер, на который нас перенаправили!")

    def test_password_is_never_in_the_result(self):
        self.assertNotIn(self.PASSWORD, self.run({"url": url(self.server), "secret": "demo"}))

    def test_https_is_required_by_default(self):
        (_, strict), = http_basic.make_tools(getter(self.secrets))
        with self.assertRaises(ToolUserError) as cm:
            strict({"url": url(self.server), "secret": "demo"})
        self.assertIn("https", str(cm.exception))
        self.assertEqual(self.seen, [], "запрос не должен был уйти")

    def test_missing_secret_gives_helpful_message(self):
        with self.assertRaises(ToolUserError) as cm:
            self.run({"url": url(self.server), "secret": "нет-такого"})
        self.assertIn("secret set", str(cm.exception))

    def test_arguments_are_required(self):
        with self.assertRaises(ToolUserError):
            self.run({"url": url(self.server)})

    def test_unreachable_server(self):
        with self.assertRaises(ToolUserError) as cm:
            self.run({"url": "http://127.0.0.1:1/x", "secret": "demo"})
        self.assertIn("недоступен", str(cm.exception))
        self.assertNotIn(self.PASSWORD, str(cm.exception))


class FakeDriver:
    def __init__(self, outcome=None, error=None):
        self.outcome, self.error, self.calls = outcome or LoginOutcome(True, "страница: example.org"), error, []

    def login(self, profile, username, password):
        self.calls.append((profile, username, password))
        if self.error:
            raise self.error
        return self.outcome


class WebLoginTests(unittest.TestCase):
    PROFILE = ('{"url": "https://accounts.example.org/login", "user_selector": "#u", '
               '"password_selector": "#p", "submit_selector": "#go"}')

    def make(self, driver=None, **overrides):
        data = {"site.example": self.PROFILE, "example.username": "alice", "example.password": "Pw-секрет-4417"}
        data.update(overrides)
        self.driver = driver or FakeDriver()
        (_, handler), = web_login.make_tools(getter(data), self.driver)
        return handler

    def test_success_injects_credentials_from_vault(self):
        out = self.make()({"site": "example"})
        self.assertIn("✓ вход выполнен", out)
        profile, user, password = self.driver.calls[0]
        self.assertEqual((user, password), ("alice", "Pw-секрет-4417"))
        self.assertEqual(profile["url"], "https://accounts.example.org/login")

    def test_failure_is_reported(self):
        out = self.make(FakeDriver(LoginOutcome(False, "поле пароля осталось")))({"site": "example"})
        self.assertIn("✗ вход не подтвердился", out)

    def test_url_cannot_be_overridden_by_arguments(self):
        """Даже обманутая модель не может отправить пароль на чужой адрес: адрес берётся из профиля."""
        self.make()({"site": "example", "url": "https://evil.example", "user_selector": "x"})
        self.assertEqual(self.driver.calls[0][0]["url"], "https://accounts.example.org/login")

    def test_missing_profile_explains_how_to_create(self):
        (_, handler), = web_login.make_tools(getter({}), FakeDriver())  # хранилище пусто
        with self.assertRaises(ToolUserError) as cm:
            handler({"site": "x"})
        self.assertIn("site add", str(cm.exception))

    def test_broken_profile(self):
        with self.assertRaises(ToolUserError) as cm:
            self.make(**{"site.example": "{не json"})({"site": "example"})
        self.assertIn("повреждён", str(cm.exception))

    def test_incomplete_profile(self):
        with self.assertRaises(ToolUserError) as cm:
            self.make(**{"site.example": '{"url": "https://a.b"}'})({"site": "example"})
        self.assertIn("не хватает", str(cm.exception))

    def test_non_https_profile_is_refused(self):
        bad = self.PROFILE.replace("https://accounts", "http://accounts")
        with self.assertRaises(ToolUserError):
            self.make(**{"site.example": bad})({"site": "example"})
        self.assertEqual(self.driver.calls, [])

    def test_missing_credentials(self):
        data = {"site.example": self.PROFILE}
        (_, handler), = web_login.make_tools(getter(data), FakeDriver())
        with self.assertRaises(ToolUserError) as cm:
            handler({"site": "example"})
        self.assertIn("secret set example.username", str(cm.exception))

    def test_site_argument_required(self):
        with self.assertRaises(ToolUserError):
            self.make()({})

    def test_driver_crash_does_not_leak_secrets(self):
        handler = self.make(FakeDriver(error=RuntimeError("fill failed for value Pw-секрет-4417 user alice")))
        with self.assertRaises(ToolUserError) as cm:
            handler({"site": "example"})
        self.assertNotIn("Pw-секрет-4417", str(cm.exception))
        self.assertNotIn("alice", str(cm.exception))

    def test_secrets_in_driver_detail_are_scrubbed(self):
        out = self.make(FakeDriver(LoginOutcome(False, "страница показала Pw-секрет-4417")))({"site": "example"})
        self.assertNotIn("Pw-секрет-4417", out)


if __name__ == "__main__":
    unittest.main()
