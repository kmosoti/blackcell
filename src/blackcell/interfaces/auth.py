from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

_MAX_AUTHORIZATION_CHARS = 4_096


class ServiceScope(StrEnum):
    READ = "read"
    RUN = "run"
    APPROVE = "approve"
    ADMIN = "admin"


ALL_SERVICE_SCOPES = tuple(ServiceScope)


class AuthenticationFailureCode(StrEnum):
    MISSING_CREDENTIAL = "missing-credential"
    MALFORMED_CREDENTIAL = "malformed-credential"
    INVALID_CREDENTIAL = "invalid-credential"


class AuthorizationFailureCode(StrEnum):
    INSUFFICIENT_SCOPE = "insufficient-scope"


class AuthenticationError(PermissionError):
    def __init__(self, code: AuthenticationFailureCode) -> None:
        self.code = code
        super().__init__(code.value)


class AuthorizationError(PermissionError):
    def __init__(self, code: AuthorizationFailureCode) -> None:
        self.code = code
        super().__init__(code.value)


@runtime_checkable
class CredentialVerifier(Protocol):
    def verify(self, candidate: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class ServicePrincipal:
    principal_id: str
    scopes: tuple[ServiceScope, ...]

    def __post_init__(self) -> None:
        if not self.principal_id.strip():
            raise ValueError("principal_id must not be empty")
        if any(not isinstance(scope, ServiceScope) for scope in self.scopes):
            raise TypeError("principal scopes must be recognized")
        normalized = tuple(sorted(set(self.scopes), key=lambda item: item.value))
        if not normalized:
            raise ValueError("a service principal requires at least one scope")
        object.__setattr__(self, "scopes", normalized)


class BearerAuthenticator:
    """Authenticate one strict Authorization header through an opaque verifier."""

    def __init__(self, verifier: CredentialVerifier, principal: ServicePrincipal) -> None:
        if not isinstance(verifier, CredentialVerifier):
            raise TypeError("verifier must implement CredentialVerifier")
        self._verifier = verifier
        self._principal = principal

    def authenticate(self, authorization_values: Sequence[str]) -> ServicePrincipal:
        if isinstance(authorization_values, str):
            raise TypeError("authorization_values must preserve header multiplicity")
        values = tuple(authorization_values)
        if not values:
            raise AuthenticationError(AuthenticationFailureCode.MISSING_CREDENTIAL)
        if len(values) != 1:
            raise AuthenticationError(AuthenticationFailureCode.MALFORMED_CREDENTIAL)
        header = values[0]
        if not isinstance(header, str) or not 1 <= len(header) <= _MAX_AUTHORIZATION_CHARS:
            raise AuthenticationError(AuthenticationFailureCode.MALFORMED_CREDENTIAL)
        pieces = header.split(" ")
        if (
            len(pieces) != 2
            or pieces[0].casefold() != "bearer"
            or not pieces[1]
            or "," in pieces[1]
            or any(character.isspace() for character in pieces[1])
            or any(not 0x21 <= ord(character) <= 0x7E for character in pieces[1])
        ):
            raise AuthenticationError(AuthenticationFailureCode.MALFORMED_CREDENTIAL)
        if not self._verifier.verify(pieces[1]):
            raise AuthenticationError(AuthenticationFailureCode.INVALID_CREDENTIAL)
        return self._principal


class ScopeAuthorizer:
    """Require explicitly assigned scopes; admin has no implicit scope expansion."""

    def require(
        self,
        principal: ServicePrincipal,
        required: ServiceScope | Sequence[ServiceScope],
    ) -> None:
        scopes = (required,) if isinstance(required, ServiceScope) else tuple(required)
        if not scopes or any(not isinstance(scope, ServiceScope) for scope in scopes):
            raise TypeError("required scopes must be recognized and non-empty")
        if not set(scopes).issubset(principal.scopes):
            raise AuthorizationError(AuthorizationFailureCode.INSUFFICIENT_SCOPE)


__all__ = [
    "ALL_SERVICE_SCOPES",
    "AuthenticationError",
    "AuthenticationFailureCode",
    "AuthorizationError",
    "AuthorizationFailureCode",
    "BearerAuthenticator",
    "CredentialVerifier",
    "ScopeAuthorizer",
    "ServicePrincipal",
    "ServiceScope",
]
