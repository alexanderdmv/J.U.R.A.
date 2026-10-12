"""Policy Engine (PDP): единственное место, где решается «можно ли это сделать».

Принципы (ADR-003, ADR-005):
  * default deny — capability должна быть явно включена в конфиге;
  * права = принципал + устройство + уровень аутентификации + запрошенное действие,
    а НЕ «пользователь может всё»;
  * подтверждение привязано к хэшу конкретного вызова, имеет срок жизни и требует
    достаточной силы аутентификации на достаточно доверенном устройстве;
  * модель не решает ничего из этого — она может только предложить вызов.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from jura.contracts.errors import PolicyDenied
from jura.contracts.types import (
    Approval, AuthLevel, Decision, DecisionKind, Device, DeviceTier, Principal,
    PrincipalKind, Risk, ToolSpec, Zone,
)

# Какая аутентификация нужна для каждого уровня риска
REQUIRED_AUTH = {
    Risk.L0: AuthLevel.NONE,
    Risk.L1: AuthLevel.DEVICE,
    Risk.L2: AuthLevel.DEVICE,
    Risk.L3: AuthLevel.BIOMETRIC,   # Face ID на телефоне: всегда свежий и для конкретного действия
}
# Насколько доверенным должно быть устройство, на котором дано подтверждение
REQUIRED_TIER = {
    Risk.L0: DeviceTier.LOW,
    Risk.L1: DeviceTier.LOW,
    Risk.L2: DeviceTier.NORMAL,
    Risk.L3: DeviceTier.HIGH,
}
AUTONOMOUS = (PrincipalKind.JURA, PrincipalKind.TRIGGER)


@dataclass(frozen=True)
class PolicyConfig:
    enabled_capabilities: frozenset[str] = frozenset()  # пусто = всё запрещено
    approval_ttl_seconds: float = 120.0
    # Чем подтверждается смена политики. По умолчанию — физический ключ. Пока ключа нет, консоль использует
    # PASSPHRASE (повторный ввод пароля) — это ВРЕМЕННО, см. ADR-003; ужесточить, когда появится YubiKey.
    change_min_auth: AuthLevel = AuthLevel.HARDWARE_KEY


@dataclass(frozen=True)
class PolicyContext:
    principal: Principal
    device: Device | None
    auth_level: AuthLevel
    tool: ToolSpec
    data_zone: Zone
    tainted: bool               # в контексте есть недоверенный контент (письмо, веб)
    call_digest: str
    approval: Approval | None = None
    now: float = 0.0


class PolicyEngine:
    def __init__(self, config: PolicyConfig | None = None,
                 on_change: Callable[[PolicyConfig, PolicyConfig, Principal], None] | None = None):
        self._config = config or PolicyConfig()
        self._on_change = on_change

    @property
    def config(self) -> PolicyConfig:
        return self._config

    def replace_config(self, new: PolicyConfig, *, principal: Principal, auth_level: AuthLevel) -> None:
        """Изменение политики — привилегированная операция: только пользователь и только с нужным уровнем (change_min_auth)."""
        need = self._config.change_min_auth
        if principal.kind is not PrincipalKind.USER or auth_level < need:
            raise PolicyDenied(f"изменение политики требует пользователя и аутентификации не ниже {need.name}")
        old, self._config = self._config, new
        if self._on_change:
            self._on_change(old, new, principal)

    def decide(self, ctx: PolicyContext) -> Decision:
        cfg, tool = self._config, ctx.tool
        deny = lambda why: Decision(DecisionKind.DENY, why)  # noqa: E731

        # 1. default deny
        if tool.capability not in cfg.enabled_capabilities:
            return deny(f"capability '{tool.capability}' не включена политикой (default deny)")
        # 2. внешний источник никогда не исполнитель
        if ctx.principal.kind is PrincipalKind.EXTERNAL:
            return deny("внешний источник не может вызывать инструменты")
        # 3. зона данных: инструмент не должен получать более секретное, чем ему положено
        if ctx.data_zone > tool.max_zone:
            return deny(f"данные зоны {ctx.data_zone.name} нельзя передавать инструменту "
                        f"(его максимум — {tool.max_zone.name})")
        # 4. автономные принципалы не делают необратимого
        if ctx.principal.kind in AUTONOMOUS and tool.risk is Risk.L3:
            return deny("автономные действия уровня L3 запрещены")

        session_auth = REQUIRED_AUTH[tool.risk]   # что нужно от самой сессии (L0/L1)
        needed_auth = session_auth                # что нужно от подтверждения конкретного действия (L2/L3 и пароли)
        if tool.uses_credentials:  # настоящие пароли: каждый раз — повторный ввод пароля хранилища
            needed_auth = max(needed_auth, AuthLevel.PASSPHRASE)

        # 5. сессионная аутентификация пользователя для L0/L1
        if tool.risk <= Risk.L1 and ctx.principal.kind is PrincipalKind.USER:
            if ctx.auth_level < session_auth:
                return Decision(DecisionKind.ASK, f"нужна аутентификация не ниже {session_auth.name}", session_auth)

        # 6. подтверждение конкретного действия для L2/L3.
        #    Сейчас «нет подтверждения — нельзя». Позже сюда добавятся scoped-гранты,
        #    но taint-правило останется: недоверенный контент всегда требует человека.
        needs_approval = tool.risk >= Risk.L2 or tool.uses_credentials
        taint_forces_approval = ctx.tainted and tool.risk >= Risk.L2
        if needs_approval or taint_forces_approval:
            why = self._check_approval(ctx, needed_auth)
            if why:
                if taint_forces_approval:
                    why = f"в контексте недоверенный контент; {why}"
                return Decision(DecisionKind.ASK, why, needed_auth)

        return Decision(DecisionKind.ALLOW, "разрешено политикой")

    def _check_approval(self, ctx: PolicyContext, needed_auth: AuthLevel) -> str | None:
        """None — подтверждение годится. Иначе текст, чего не хватает."""
        a, tool = ctx.approval, ctx.tool
        if a is None:
            return "нужно подтверждение пользователя"
        if a.call_digest != ctx.call_digest:
            return "подтверждение относится к другому действию"
        if not (0 <= ctx.now - a.granted_at <= self._config.approval_ttl_seconds):
            return "подтверждение просрочено"
        if a.auth_level < needed_auth:
            return f"подтверждение слабее требуемого ({a.auth_level.name} < {needed_auth.name})"
        if a.device.tier < REQUIRED_TIER[tool.risk]:
            return f"устройство подтверждения недостаточно доверенное (нужен tier {REQUIRED_TIER[tool.risk].name})"
        return None
