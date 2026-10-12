"""Базовые типы: зоны данных, уровни риска, принципалы, устройства, конверт, задачи.

Это «словарь» проекта. Читать сверху вниз — достаточно, чтобы понять язык J.U.R.A.
"""

from __future__ import annotations

import enum
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


class Zone(enum.IntEnum):
    """Зона данных. Число растёт с секретностью, поэтому max() = «унаследовать самую строгую»."""

    O = 0   # Open — публичное, облако свободно
    P = 1   # Private — код, почта, календарь; облако только через Redaction Gateway
    S1 = 2  # Sensitive — только локальные модели
    S2 = 3  # Credentials — модель не видит вообще, только детерминированный код


class Risk(enum.IntEnum):
    L0 = 0  # чтение
    L1 = 1  # обратимое локальное
    L2 = 2  # внешняя запись
    L3 = 3  # деньги, замки, необратимое


class Trust(enum.Enum):
    TRUSTED = "trusted"      # код и конфиг самой системы
    USER = "user"            # слова пользователя
    UNTRUSTED = "untrusted"  # письма, веб, файлы — могут содержать чужие инструкции


class AuthLevel(enum.IntEnum):
    NONE = 0
    DEVICE = 1        # известное устройство с действующей сессией
    PASSPHRASE = 2    # пароль хранилища введён повторно, прямо сейчас
    BIOMETRIC = 3     # свежий Face ID / passkey на доверенном телефоне
    HARDWARE_KEY = 4  # физический ключ (YubiKey)


class DeviceTier(enum.IntEnum):
    LOW = 0     # машина, часы, общая комната
    NORMAL = 1  # ноутбук
    HIGH = 2    # личный телефон, основной ПК


class PrincipalKind(enum.Enum):
    USER = "user"
    JURA = "jura"          # сам J.U.R.A. действует по своей инициативе
    TRIGGER = "trigger"    # cron / webhook
    EXTERNAL = "external"  # внешний источник — никогда не исполнитель


class TaskState(enum.Enum):
    CREATED = "created"
    PLANNED = "planned"
    AWAITING_APPROVAL = "awaiting_approval"
    RUNNING = "running"
    VERIFYING = "verifying"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUSPENDED = "suspended"


def max_zone(*zones: Zone, default: Zone = Zone.O) -> Zone:
    """Производные данные наследуют самую строгую зону из источников."""
    return max(zones, default=default)


_TRUST_RANK = {Trust.TRUSTED: 0, Trust.USER: 1, Trust.UNTRUSTED: 2}


def worst_trust(*levels: Trust) -> Trust:
    """Производные данные наследуют наименьшее доверие: смесь доверенного и недоверенного — недоверенная."""
    return max(levels, key=_TRUST_RANK.__getitem__, default=Trust.TRUSTED)


@dataclass(frozen=True)
class Principal:
    kind: PrincipalKind
    id: str = ""

    def __str__(self) -> str:
        return f"{self.kind.value}:{self.id}" if self.id else self.kind.value


@dataclass(frozen=True)
class Device:
    id: str
    kind: str  # "desktop", "phone", "car", ...
    tier: DeviceTier = DeviceTier.NORMAL


@dataclass(frozen=True)
class Envelope:
    """Всё, что входит в систему, упаковано в конверт: кто, откуда, насколько ему доверять."""

    principal: Principal
    text: str
    device: Device | None = None
    auth_level: AuthLevel = AuthLevel.NONE
    trust: Trust = Trust.USER
    zone: Zone = Zone.P  # по умолчанию данные считаются приватными (default policy)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)
    schema_version: int = 1


@dataclass(frozen=True)
class ToolSpec:
    """Описание инструмента. Риск и зона заданы здесь, а не решаются моделью."""

    name: str
    description: str
    risk: Risk
    capability: str                 # например "mail.send"; политика включает capability явно
    max_zone: Zone = Zone.P         # самая секретная зона данных, которую инструмент вправе получить
    idempotent: bool = False        # повтор безопасен? если нет — защита через outbox
    result_zone: Zone = Zone.P      # минимальная зона результата (результат инструмента не может быть «менее секретным»)
    result_trust: Trust = Trust.UNTRUSTED  # по умолчанию результат недоверенный: письмо, страница, ответ сервера
    uses_credentials: bool = False  # использует сохранённые пароли → каждое выполнение требует повторного ввода пароля


@dataclass(frozen=True)
class ToolResult:
    """Результат инструмента вместе с метками. Без меток данные «теряют» происхождение: письмо (зона P, недоверенное)
    превратилось бы в обычный текст, который можно отправить в облачную модель. Любой коннектор возвращает данные
    через ToolPort, и метки не могут стать мягче, чем объявлено в ToolSpec."""

    data: Any
    zone: Zone = Zone.P
    trust: Trust = Trust.UNTRUSTED
    source: str = ""


@dataclass(frozen=True)
class Approval:
    """Подтверждение конкретного действия человеком. Привязано к хэшу вызова, а не «вообще»."""

    call_digest: str
    auth_level: AuthLevel
    device: Device
    granted_at: float


class DecisionKind(enum.Enum):
    ALLOW = "allow"
    ASK = "ask"    # нужно подтверждение человека
    DENY = "deny"


@dataclass(frozen=True)
class Decision:
    kind: DecisionKind
    reason: str
    required_auth: AuthLevel = AuthLevel.NONE
