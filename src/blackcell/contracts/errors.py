"""Stable errors and exit classes."""

from enum import IntEnum
from typing import Any


class ExitClass(IntEnum):
    OK = 0
    ERROR = 1
    VALIDATION_ERROR = 2
    AUTH_ERROR = 3
    PERMISSION_ERROR = 4
    CONFLICT = 5
    REMOTE_ERROR = 6
    PENDING = 7
    NOT_FOUND = 8
    POLICY_ERROR = 9


class BlackcellError(Exception):
    """Base exception safe for CLI serialization."""

    exit_class = ExitClass.ERROR
    code = "unexpected_error"

    def __init__(
        self,
        message: str,
        *,
        recovery: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.recovery = recovery
        self.details = details or {}


class ValidationFailure(BlackcellError):
    exit_class = ExitClass.VALIDATION_ERROR
    code = "validation_error"


class AuthenticationFailure(BlackcellError):
    exit_class = ExitClass.AUTH_ERROR
    code = "authentication_error"


class PermissionFailure(BlackcellError):
    exit_class = ExitClass.PERMISSION_ERROR
    code = "permission_error"


class ConflictFailure(BlackcellError):
    exit_class = ExitClass.CONFLICT
    code = "conflict"


class RemoteFailure(BlackcellError):
    exit_class = ExitClass.REMOTE_ERROR
    code = "remote_error"


class PendingFailure(BlackcellError):
    exit_class = ExitClass.PENDING
    code = "pending"


class NotFoundFailure(BlackcellError):
    exit_class = ExitClass.NOT_FOUND
    code = "not_found"


class PolicyFailure(BlackcellError):
    exit_class = ExitClass.POLICY_ERROR
    code = "policy_error"
