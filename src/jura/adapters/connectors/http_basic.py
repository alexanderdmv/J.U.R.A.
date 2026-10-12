"""http.basic_check — проверить, что сохранённые логин и пароль принимаются сервером (HTTP Basic, только HTTPS).

Это самый простой настоящий тест цепочки «хранилище → брокер → подстановка секрета → сеть → результат».
Защита: редиректы НЕ выполняются (иначе заголовок с паролем мог бы уйти на другой сервер), только https,
пароль не попадает в результат и сообщения об ошибках, использование пароля требует повторного ввода пароля хранилища.

Известное ограничение: адрес здесь — аргумент вызова, поэтому перед выполнением ты видишь его в вопросе подтверждения.
Для постоянной работы с сервисами используется web.login, где адрес закреплён в профиле.
"""

from __future__ import annotations

import base64
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from jura.contracts.errors import CredentialDenied, ToolUserError, VaultError
from jura.contracts.types import Risk, ToolSpec

GetSecret = Callable[[str], str]
TIMEOUT = 15
NAME_PATTERN = r"[\w.-]+\.(username|password)"  # какие секреты брокер разрешает этому коннектору


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # 3xx превратится в HTTPError, и мы просто сообщим код


def make_tools(get_secret: GetSecret, *, allow_insecure_http: bool = False):
    """allow_insecure_http — только для автотестов с локальным сервером."""
    spec = ToolSpec("http.basic_check", "Проверить логин/пароль из хранилища на сервере (HTTP Basic, https)",
                    Risk.L2, "http.basic", uses_credentials=True)

    def handler(args: dict[str, Any]) -> str:
        url, name = args.get("url", ""), args.get("secret", "")
        if not url or not name:
            raise ToolUserError("нужны аргументы: url=https://… secret=ИМЯ (ищет ИМЯ.username и ИМЯ.password)")
        scheme = urllib.parse.urlparse(url).scheme
        if scheme != "https" and not (allow_insecure_http and scheme == "http"):
            raise ToolUserError("разрешён только https: по http пароль ушёл бы открытым текстом")
        try:
            user, password = get_secret(f"{name}.username"), get_secret(f"{name}.password")
        except CredentialDenied as exc:
            raise ToolUserError(str(exc)) from None
        except VaultError as exc:
            raise ToolUserError(f"{exc} — сохрани: secret set {name}.username / secret set {name}.password") from None
        token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
        request = urllib.request.Request(url, headers={"Authorization": f"Basic {token}", "User-Agent": "jura/0.6"})
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(request, timeout=TIMEOUT) as resp:
                status = resp.status
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()  # HTTPError держит открытое соединение
        except urllib.error.URLError as exc:
            raise ToolUserError(f"сервер недоступен: {type(exc.reason).__name__}") from None
        except TimeoutError:
            raise ToolUserError("сервер не ответил вовремя") from None
        meaning = ("логин и пароль приняты" if 200 <= status < 300
                   else "логин/пароль отклонены" if status in (401, 403)
                   else "ответ-перенаправление (за ним не следуем — это защита пароля от утечки)" if 300 <= status < 400
                   else "сервер ответил иначе")
        return f"HTTP {status}: {meaning}"

    return [(spec, handler)]
