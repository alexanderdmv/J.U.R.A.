"""Общие заготовки для тестов."""

from jura.contracts.types import (
    Approval, AuthLevel, Device, DeviceTier, Envelope, Principal, PrincipalKind, Risk, ToolSpec, Zone,
)

NOW = 1_000_000.0

USER = Principal(PrincipalKind.USER, "owner")
JURA = Principal(PrincipalKind.JURA, "self")
TRIGGER = Principal(PrincipalKind.TRIGGER, "cron")
EXTERNAL = Principal(PrincipalKind.EXTERNAL, "mail")

DESKTOP = Device("pc-1", "desktop", DeviceTier.NORMAL)
PHONE = Device("iphone-1", "phone", DeviceTier.HIGH)
CAR = Device("car-1", "car", DeviceTier.LOW)

T0 = ToolSpec("t0", "чтение", Risk.L0, "demo.read")
T1 = ToolSpec("t1", "обратимая запись", Risk.L1, "demo.write")
T2 = ToolSpec("t2", "письмо", Risk.L2, "mail.send")
T3 = ToolSpec("t3", "перевод", Risk.L3, "bank.transfer")
TS2 = ToolSpec("sign", "подпись секретом", Risk.L1, "crypto.sign", max_zone=Zone.S2)

ALL_CAPS = frozenset({"demo.read", "demo.write", "mail.send", "bank.transfer", "crypto.sign"})


def envelope(principal=USER, device=DESKTOP, auth=AuthLevel.DEVICE, **kw):
    return Envelope(principal=principal, text=kw.pop("text", "тест"), device=device, auth_level=auth, **kw)


def approval(digest, auth=AuthLevel.DEVICE, device=DESKTOP, at=NOW):
    return Approval(call_digest=digest, auth_level=auth, device=device, granted_at=at)
