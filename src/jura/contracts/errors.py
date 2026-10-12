"""Все ошибки J.U.R.A. в одном месте. Базовый класс — JuraError."""

from __future__ import annotations


class JuraError(Exception):
    """Базовая ошибка проекта."""


# --- политики и инструменты ---------------------------------------------------
class PolicyDenied(JuraError):
    """Политика запретила действие. Повторять бессмысленно."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class ApprovalRequired(JuraError):
    """Нужно подтверждение человека (и, возможно, более сильная аутентификация)."""

    def __init__(self, reason: str, required_auth, call_digest: str):
        super().__init__(reason)
        self.reason = reason
        self.required_auth = required_auth
        self.call_digest = call_digest


class ToolNotFound(JuraError):
    pass


class ToolError(JuraError):
    """Инструмент упал. Текст исходной ошибки намеренно не сохраняется (там могут быть секреты)."""


class ToolUserError(JuraError):
    """Ошибка, текст которой инструмент ГАРАНТИРУЕТ безопасным (без секретов): его можно показать и записать в журнал.
    Любое другое исключение инструмента прячется, чтобы случайно не раскрыть секрет."""


class AlreadyExecuted(JuraError):
    """Необратимое действие с этим ключом уже выполнено — повторно не выполняем."""


class AmbiguousEffect(JuraError):
    """Действие было начато, но неизвестно, завершилось ли оно. Нужна проверка человеком."""


# --- задачи -------------------------------------------------------------------
class TaskNotFound(JuraError):
    pass


class IllegalTransition(JuraError):
    pass


# --- данные и зоны ------------------------------------------------------------
class ZoneViolation(JuraError):
    """Попытка отдать данные туда, куда их отдавать нельзя."""


# --- хранилище секретов -------------------------------------------------------
class VaultError(JuraError):
    pass


class VaultLocked(VaultError):
    pass


class WrongCredentials(VaultError):
    pass


class RecoveryCodeInvalid(VaultError):
    pass


class CredentialDenied(VaultError):
    """Коннектору не положено читать этот секрет (брокер учётных данных отказал)."""


# --- модели -------------------------------------------------------------------
class ModelError(JuraError):
    """Адаптер модели обязан превращать любые сбои вендора в эту ошибку."""


class NoModelAvailable(JuraError):
    pass


# --- шифрование и резервные копии ---------------------------------------------
class DecryptionError(JuraError):
    """Не расшифровалось: неверный ключ, повреждённые или подменённые данные."""


class BackupError(JuraError):
    """Резервную копию нельзя создать, прочитать или она не прошла проверку."""


# --- голос ---------------------------------------------------------------------
class VoiceError(JuraError):
    """Микрофон, распознавание или синтез речи не сработали. Текст сообщения не содержит произнесённого."""
