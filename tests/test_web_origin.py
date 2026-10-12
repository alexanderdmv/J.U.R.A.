"""Защита от кражи пароля через перенаправление: пароль вводится только на разрешённом origin (схема + хост + порт).

Playwright здесь поддельный (подставлен в sys.modules): проверяется НАША логика, а не браузер.
"""

import sys
import types
import unittest
from unittest.mock import patch

from jura.adapters.connectors import web_login
from jura.adapters.connectors.web_login import PlaywrightDriver, allowed_origins, origin_of
from jura.contracts.errors import ToolUserError

PROFILE = {"url": "https://good.example/login", "user_selector": "#u", "password_selector": "#p",
           "submit_selector": "#go", "hold_seconds": 0}


class FakePage:
    def __init__(self, redirect_on_goto=None, redirect_after_fill=None):
        self.url = "about:blank"
        self.calls, self._goto_to, self._fill_to = [], redirect_on_goto, redirect_after_fill

    def goto(self, url, **kw):
        self.calls.append(("goto", url))
        self.url = self._goto_to or url

    def fill(self, selector, value, **kw):
        self.calls.append(("fill", selector, value))
        if self._fill_to and selector == "#u":  # страница «перескочила» на другой адрес, пока мы вводили логин
            self.url = self._fill_to

    def click(self, selector, **kw):
        self.calls.append(("click", selector))

    def wait_for_selector(self, *a, **kw): self.calls.append(("wait_selector",))
    def wait_for_url(self, *a, **kw): self.calls.append(("wait_url",))
    def wait_for_timeout(self, ms): pass


def fake_playwright(page):
    sync = types.ModuleType("playwright.sync_api")
    sync.TimeoutError = type("TimeoutError", (Exception,), {})

    class Browser:
        def new_context(self): return types.SimpleNamespace(new_page=lambda: page)
        def close(self): page.closed = True

    class PW:
        chromium = types.SimpleNamespace(launch=lambda headless: Browser())
        def __enter__(self): return self
        def __exit__(self, *a): return False

    sync.sync_playwright = lambda: PW()
    return patch.dict(sys.modules, {"playwright": types.ModuleType("playwright"), "playwright.sync_api": sync})


def login(page, profile=None):
    with fake_playwright(page):
        return PlaywrightDriver().login(profile or PROFILE, "alice", "Pw-секрет-4417")


def filled(page):
    return [c for c in page.calls if c[0] == "fill"]


class OriginTests(unittest.TestCase):
    def test_origin_is_normalized(self):
        self.assertEqual(origin_of("https://EXAMPLE.org:443/x?y=1"), origin_of("https://example.org/other"))
        self.assertEqual(origin_of("https://a.example")[2], 443)
        self.assertEqual(origin_of("не адрес"), ("", "", 0))

    def test_different_scheme_host_or_port_are_different_origins(self):
        base = origin_of("https://good.example/login")
        for other in ("http://good.example/login", "https://evil.example/login", "https://good.example:8443/login",
                      "https://good.example.evil.example/", "https://sub.good.example/"):
            self.assertNotEqual(origin_of(other), base, other)

    def test_only_https_origins_are_ever_allowed(self):
        prof = {**PROFILE, "allowed_origins": ["http://plain.example", "https://auth.example"]}
        self.assertEqual({o[1] for o in allowed_origins(prof)}, {"good.example", "auth.example"})


class PlaywrightDriverOriginTests(unittest.TestCase):
    def test_normal_login_on_the_approved_origin(self):
        page = FakePage()
        out = login(page)
        self.assertTrue(out.success)
        self.assertEqual([c[1] for c in filled(page)], ["#u", "#p"])
        self.assertIn(("click", "#go"), page.calls)
        self.assertTrue(page.closed)

    def test_redirect_to_another_domain_means_nothing_is_typed(self):
        """Сценарий из ревью: good.example → 302 → evil.example. Раньше пароль ввёлся бы на чужом сайте."""
        page = FakePage(redirect_on_goto="https://evil.example/login")
        out = login(page)
        self.assertFalse(out.success)
        self.assertEqual(filled(page), [], "на чужом адресе нельзя вводить ни логин, ни пароль")
        self.assertNotIn(("click", "#go"), page.calls)
        self.assertIn("evil.example", out.detail)
        self.assertIn("НЕ вводился", out.detail)
        self.assertNotIn("Pw-секрет-4417", out.detail)
        self.assertTrue(page.closed)

    def test_downgrade_to_http_and_other_port_are_refused(self):
        for target in ("http://good.example/login", "https://good.example:8443/login"):
            page = FakePage(redirect_on_goto=target)
            self.assertFalse(login(page).success, target)
            self.assertEqual(filled(page), [], target)

    def test_page_that_jumps_away_while_typing_the_login_never_gets_the_password(self):
        page = FakePage(redirect_after_fill="https://evil.example/steal")
        out = login(page)
        self.assertFalse(out.success)
        self.assertEqual([c[1] for c in filled(page)], ["#u"])  # логин успел уйти, пароль — нет
        self.assertNotIn(("click", "#go"), page.calls)

    def test_explicitly_allowed_login_domain_is_accepted(self):
        page = FakePage(redirect_on_goto="https://auth.example/login")
        prof = {**PROFILE, "allowed_origins": ["https://auth.example"]}
        out = login(page, prof)
        self.assertTrue(out.success)
        self.assertEqual(len(filled(page)), 2)

    def test_allowed_origin_must_be_exact(self):
        page = FakePage(redirect_on_goto="https://auth.example.evil.example/login")
        prof = {**PROFILE, "allowed_origins": ["https://auth.example"]}
        self.assertFalse(login(page, prof).success)
        self.assertEqual(filled(page), [])

    def test_missing_playwright_gives_install_hint(self):
        with patch.dict(sys.modules, {"playwright": None, "playwright.sync_api": None}):
            with self.assertRaises(ToolUserError) as cm:
                PlaywrightDriver().login(PROFILE, "a", "b")
        self.assertIn("python -m jura setup", str(cm.exception))


class HandlerProfileValidationTests(unittest.TestCase):
    def test_non_https_allowed_origin_in_profile_is_rejected(self):
        import json
        data = {"site.x": json.dumps({**PROFILE, "allowed_origins": ["http://plain.example"]}),
                "x.username": "a", "x.password": "b"}
        (_, handler), = web_login.make_tools(data.__getitem__, driver=types.SimpleNamespace(login=lambda *a: None))
        with self.assertRaises(ToolUserError) as cm:
            handler({"site": "x"})
        self.assertIn("allowed_origins", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
