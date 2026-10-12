"""web.login — войти на сайт под сохранённой учётной записью, в видимом окне браузера.

Профиль сайта (адрес и селекторы полей) хранится в зашифрованном хранилище как `site.ИМЯ`, а логин и пароль —
как `ИМЯ.username` и `ИМЯ.password`. Адрес берётся ИЗ ПРОФИЛЯ, а не из аргументов вызова.

ЗАЩИТА ОТ ПЕРЕНАПРАВЛЕНИЯ. Браузер по умолчанию следует за редиректами, поэтому «адрес из профиля» сам по себе
не гарантирует, что мы окажемся на нём. Перед КАЖДЫМ вводом (логин, пароль) и перед нажатием кнопки код сверяет
фактический origin страницы (схема + хост + порт) с разрешённым: адресом профиля и, при необходимости, перечисленными
в профиле `allowed_origins` (например, отдельный домен страницы входа). Любое другое место — немедленный отказ, пароль не вводится.

Драйвер браузера заменяемый. Рабочий — Playwright (ставится вместе с проектом; браузер Chromium скачивает команда
`python -m jura setup`). Логика проверена тестами с поддельным драйвером и поддельным Playwright; с настоящим браузером
этот файл впервые проверяется у владельца.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Protocol
from urllib.parse import urlparse

from jura.contracts.errors import CredentialDenied, ToolUserError, VaultError
from jura.contracts.types import Risk, ToolSpec

GetSecret = Callable[[str], str]
REQUIRED = ("url", "user_selector", "password_selector", "submit_selector")
NAME_PATTERN = r"[\w.-]+\.(username|password)|site\.[\w.-]+"  # какие секреты брокер разрешает этому коннектору
_DEFAULT_PORTS = {"https": 443, "http": 80}


@dataclass(frozen=True)
class LoginOutcome:
    success: bool
    detail: str = ""


class BrowserDriver(Protocol):
    def login(self, profile: dict[str, Any], username: str, password: str) -> LoginOutcome: ...


def origin_of(url: str) -> tuple[str, str, int]:
    """(схема, хост, порт) в нормализованном виде. Для неразборчивого адреса — пустой origin, который ничему не равен."""
    try:
        p = urlparse(url)
        scheme = (p.scheme or "").lower()
        return scheme, (p.hostname or "").lower(), p.port or _DEFAULT_PORTS.get(scheme, 0)
    except ValueError:
        return "", "", 0


def allowed_origins(profile: dict[str, Any]) -> set[tuple[str, str, int]]:
    urls = [profile["url"], *profile.get("allowed_origins", [])]
    return {o for o in map(origin_of, urls) if o[0] == "https" and o[1]}


def _scrub(text: str, *secrets: str) -> str:
    """Вычищает секреты из любого текста, прежде чем он станет сообщением об ошибке."""
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return text


class PlaywrightDriver:
    """Рабочий драйвер. Видимое окно Chromium: ты видишь, что делает J.U.R.A."""

    def login(self, profile: dict[str, Any], username: str, password: str) -> LoginOutcome:
        try:
            from playwright.sync_api import TimeoutError as PWTimeout
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise ToolUserError("не установлен Playwright. Выполни: pip install -e .  и  python -m jura setup") from None

        timeout = int(profile.get("timeout_seconds", 20)) * 1000
        allowed = allowed_origins(profile)
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            try:
                page = browser.new_context().new_page()

                def off_origin() -> LoginOutcome | None:
                    """None — мы на разрешённом адресе. Иначе готовый отказ (только хост: в адресе могут быть токены)."""
                    if origin_of(page.url) in allowed:
                        return None
                    host = urlparse(page.url).hostname or "неизвестный адрес"
                    return LoginOutcome(False, f"страница оказалась на другом адресе ({host}) — пароль НЕ вводился")

                page.goto(profile["url"], wait_until="domcontentloaded", timeout=30_000)
                if (bad := off_origin()):
                    return bad
                page.fill(profile["user_selector"], username, timeout=timeout)
                if (bad := off_origin()):  # страница могла перейти на другой адрес, пока мы вводили логин
                    return bad
                page.fill(profile["password_selector"], password, timeout=timeout)
                if (bad := off_origin()):
                    return bad
                page.click(profile["submit_selector"], timeout=timeout)
                try:
                    if profile.get("success_selector"):
                        page.wait_for_selector(profile["success_selector"], timeout=timeout)
                    elif profile.get("success_url_contains"):
                        needle = profile["success_url_contains"]
                        page.wait_for_url(lambda url: needle in url, timeout=timeout)
                    else:  # без явного признака: поле пароля исчезло с экрана
                        page.wait_for_selector(profile["password_selector"], state="hidden", timeout=timeout)
                    ok = True
                except PWTimeout:
                    ok = False
                host = urlparse(page.url).netloc
                page.wait_for_timeout(int(profile.get("hold_seconds", 5)) * 1000)  # чтобы ты успел увидеть результат
                return LoginOutcome(ok, f"страница: {host}")
            except PWTimeout as exc:
                return LoginOutcome(False, _scrub("не найден элемент или страница не отвечает: " + str(exc).splitlines()[0],
                                                  username, password))
            finally:
                browser.close()


def make_tools(get_secret: GetSecret, driver: BrowserDriver | None = None):
    spec = ToolSpec("web.login", "Войти на сайт под сохранённой учётной записью (окно браузера)", Risk.L2, "web.login",
                    uses_credentials=True)
    drv = driver or PlaywrightDriver()

    def handler(args: dict[str, Any]) -> str:
        site = args.get("site", "")
        if not site:
            raise ToolUserError("нужен аргумент: site=ИМЯ (профиль создаётся командой site add)")
        try:
            profile = json.loads(get_secret(f"site.{site}"))
        except CredentialDenied as exc:
            raise ToolUserError(str(exc)) from None
        except VaultError:
            raise ToolUserError(f"профиль сайта '{site}' не найден. Создай: site add {site} URL ПОЛЕ_ЛОГИНА ПОЛЕ_ПАРОЛЯ КНОПКА") from None
        except ValueError:
            raise ToolUserError(f"профиль сайта '{site}' повреждён") from None
        missing = [k for k in REQUIRED if not profile.get(k)]
        if missing:
            raise ToolUserError(f"в профиле '{site}' не хватает: {', '.join(missing)}")
        if origin_of(profile["url"])[0] != "https":
            raise ToolUserError("адрес входа должен быть https")
        extra = [u for u in profile.get("allowed_origins", []) if origin_of(u)[0] != "https"]
        if extra:
            raise ToolUserError("все allowed_origins должны быть адресами https")
        try:
            user, password = get_secret(f"{site}.username"), get_secret(f"{site}.password")
        except CredentialDenied as exc:
            raise ToolUserError(str(exc)) from None
        except VaultError as exc:
            raise ToolUserError(f"{exc} — сохрани: secret set {site}.username / secret set {site}.password") from None

        try:
            outcome = drv.login(profile, user, password)
        except ToolUserError:
            raise
        except Exception as exc:  # детали драйвера могут содержать данные страницы — отдаём только тип и очищенный текст
            raise ToolUserError(_scrub(f"сбой браузера ({type(exc).__name__}): {str(exc).splitlines()[0] if str(exc) else ''}",
                                       user, password)) from None
        text = _scrub(outcome.detail, user, password)
        return f"✓ вход выполнен ({text})" if outcome.success else f"✗ вход не подтвердился ({text})"

    return [(spec, handler)]
