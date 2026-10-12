"""Credential Broker: выдаёт коннектору только те секреты, которые ему положено читать, и записывает каждое чтение.

Раньше коннектор получал `vault.get` целиком и теоретически мог прочитать ЛЮБОЙ секрет по имени. Теперь у каждого
коннектора свой шаблон допустимых имён: например web.login читает `ИМЯ.username`, `ИМЯ.password` и `site.ИМЯ`,
но не `binance.api_secret` и не служебные записи (`__policy__`). Каждое успешное чтение пишется в журнал
(какой коннектор и какое имя — без значения).

Честно: это защита от ошибок и от чужих плагинов, не от злонамеренного кода внутри самого ядра.
"""

from __future__ import annotations

import re
from typing import Any, Callable

from jura.contracts.errors import CredentialDenied
from jura.kernel.vault import Vault

Audit = Callable[[str, dict[str, Any]], None]


class CredentialBroker:
    def __init__(self, vault: Vault, audit: Audit | None = None):
        self._vault = vault
        self._audit = audit or (lambda kind, payload: None)

    def getter_for(self, connector: str, name_pattern: str) -> Callable[[str], str]:
        """Функция get_secret для коннектора: разрешены только имена, целиком подходящие под шаблон."""
        allowed = re.compile(name_pattern)

        def get(name: str) -> str:
            if name.startswith("__") or not allowed.fullmatch(name):
                self._audit("credential.denied", {"connector": connector, "name": name})
                raise CredentialDenied(f"коннектору '{connector}' не разрешено читать секрет '{name}'")
            value = self._vault.get(name)  # VaultError, если секрета нет
            self._audit("credential.used", {"connector": connector, "name": name})
            return value

        return get
