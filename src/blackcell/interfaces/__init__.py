"""Framework-neutral inbound interface contracts."""

from blackcell.interfaces.auth import (
    ALL_SERVICE_SCOPES,
    AuthenticationError,
    AuthenticationFailureCode,
    AuthorizationError,
    AuthorizationFailureCode,
    BearerAuthenticator,
    CredentialVerifier,
    ScopeAuthorizer,
    ServicePrincipal,
    ServiceScope,
)

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
