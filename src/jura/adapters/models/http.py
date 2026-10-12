"""Общий HTTP-код адаптеров моделей: безопасные запросы и понятные ошибки.

Правила:
  * только https; обычный http — лишь для сервера на ЭТОМ ЖЕ компьютере (локальная модель: 127.0.0.1 / localhost / ::1);
  * редиректы не выполняются (иначе ключ API из заголовка мог бы уйти на другой сервер);
  * ключ никогда не попадает в сообщения об ошибках; текст запроса — тоже;
  * любой сбой превращается в ModelError (роутер тогда пробует следующую модель).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from jura.contracts.errors import ModelError

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
MAX_ERROR_TEXT = 200


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def is_loopback(url: str) -> bool:
    try:
        return urllib.parse.urlparse(url).hostname in LOOPBACK_HOSTS
    except ValueError:
        return False


def check_url(url: str, *, allow_http_loopback: bool = False, allow_insecure: bool = False) -> None:
    scheme = urllib.parse.urlparse(url).scheme
    if scheme == "https":
        return
    if scheme == "http" and ((allow_http_loopback and is_loopback(url)) or allow_insecure):
        return
    raise ModelError("разрешён только https (http допустим лишь для локальной модели на этом же компьютере)")


def _scrub(text: str, secrets: tuple[str, ...]) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return text


def _http_message(exc: urllib.error.HTTPError, secrets: tuple[str, ...]) -> str:
    code = exc.code
    if code in (401, 403):
        return f"ключ API отклонён ({code}): проверь ключ (secret set ПРОВАЙДЕР.api_key)"
    if code == 404:
        return "адрес или модель не найдены (404): проверь имя модели (model discover …)"
    if code == 429:
        return "превышен лимит запросов или баланс (429)"
    if 300 <= code < 400:
        return f"сервер ответил перенаправлением ({code}); за ним не следуем — это защита ключа"
    if code >= 500:
        return f"сервис временно недоступен ({code})"
    detail = ""
    try:
        body = json.loads(exc.read() or b"{}")
        err = body.get("error", body) if isinstance(body, dict) else {}
        detail = err.get("message", "") if isinstance(err, dict) else str(err)
    except (ValueError, OSError, AttributeError):
        pass
    detail = _scrub(str(detail), secrets).replace("\n", " ")[:MAX_ERROR_TEXT]
    return f"запрос отклонён ({code})" + (f": {detail}" if detail else "")


def request_json(method: str, url: str, *, headers: dict[str, str], body: dict[str, Any] | None = None,
                 timeout: float = 120, secrets: tuple[str, ...] = ()) -> dict[str, Any]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method,
                                     headers={**headers, "Content-Type": "application/json", "User-Agent": "jura/0.6"})
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        try:
            message = _http_message(exc, secrets)
        finally:
            exc.close()  # HTTPError держит открытое соединение
        raise ModelError(message) from None
    except urllib.error.URLError as exc:
        raise ModelError(f"сервер недоступен: {type(exc.reason).__name__}") from None
    except TimeoutError:
        raise ModelError("сервер не ответил вовремя") from None
    except OSError as exc:
        raise ModelError(f"сбой соединения ({type(exc).__name__})") from None
    try:
        parsed = json.loads(raw)
    except ValueError:
        raise ModelError("сервер вернул не JSON") from None
    if not isinstance(parsed, dict):
        raise ModelError("неожиданный формат ответа сервера")
    return parsed


def get_key(getter, hint: str) -> str:
    """Достаёт ключ API через брокер; без ключа — понятная ошибка с подсказкой."""
    from jura.contracts.errors import VaultError  # noqa: PLC0415
    try:
        key = getter()
    except VaultError:
        raise ModelError(f"нет ключа API. Сохрани: {hint}") from None
    if not key.strip():
        raise ModelError(f"ключ API пуст. Сохрани заново: {hint}")
    return key.strip()
