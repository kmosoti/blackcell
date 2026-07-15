from __future__ import annotations

import pytest

from blackcell.config import SecretValue
from blackcell.interfaces import (
    AuthenticationError,
    AuthenticationFailureCode,
    AuthorizationError,
    AuthorizationFailureCode,
    BearerAuthenticator,
    ScopeAuthorizer,
    ServicePrincipal,
    ServiceScope,
)

TOKEN = "Runtime-v1_opaque-token.0123456789-ABCDEFG"


def test_bearer_authentication_returns_one_typed_principal() -> None:
    principal = ServicePrincipal(
        "service:test",
        (ServiceScope.RUN, ServiceScope.READ, ServiceScope.READ),
    )
    authenticator = BearerAuthenticator(SecretValue(TOKEN), principal)

    authenticated = authenticator.authenticate((f"bEaReR {TOKEN}",))

    assert authenticated == principal
    assert authenticated.scopes == (ServiceScope.READ, ServiceScope.RUN)


@pytest.mark.parametrize(
    ("headers", "code"),
    (
        ((), AuthenticationFailureCode.MISSING_CREDENTIAL),
        (
            ("Bearer one", "Bearer two"),
            AuthenticationFailureCode.MALFORMED_CREDENTIAL,
        ),
        (("Basic abc",), AuthenticationFailureCode.MALFORMED_CREDENTIAL),
        (("Bearer",), AuthenticationFailureCode.MALFORMED_CREDENTIAL),
        (("Bearer  token",), AuthenticationFailureCode.MALFORMED_CREDENTIAL),
        (("Bearer token extra",), AuthenticationFailureCode.MALFORMED_CREDENTIAL),
        (("Bearer token,other",), AuthenticationFailureCode.MALFORMED_CREDENTIAL),
        ((" Bearer token",), AuthenticationFailureCode.MALFORMED_CREDENTIAL),
        ((f"Bearer {TOKEN}x",), AuthenticationFailureCode.INVALID_CREDENTIAL),
    ),
)
def test_bearer_authentication_rejects_missing_malformed_and_invalid_credentials(
    headers: tuple[str, ...],
    code: AuthenticationFailureCode,
) -> None:
    authenticator = BearerAuthenticator(
        SecretValue(TOKEN),
        ServicePrincipal("service:test", (ServiceScope.READ,)),
    )

    with pytest.raises(AuthenticationError) as caught:
        authenticator.authenticate(headers)

    assert caught.value.code is code
    assert str(caught.value) == code.value
    assert TOKEN not in str(caught.value)


def test_header_multiplicity_must_be_preserved_by_transport() -> None:
    authenticator = BearerAuthenticator(
        SecretValue(TOKEN),
        ServicePrincipal("service:test", (ServiceScope.READ,)),
    )

    with pytest.raises(TypeError, match="multiplicity"):
        authenticator.authenticate(f"Bearer {TOKEN}")


def test_scope_authorization_requires_every_explicit_scope() -> None:
    principal = ServicePrincipal(
        "service:test",
        (ServiceScope.READ, ServiceScope.RUN),
    )
    authorizer = ScopeAuthorizer()

    authorizer.require(principal, ServiceScope.READ)
    authorizer.require(principal, (ServiceScope.READ, ServiceScope.RUN))
    with pytest.raises(AuthorizationError) as caught:
        authorizer.require(principal, ServiceScope.APPROVE)

    assert caught.value.code is AuthorizationFailureCode.INSUFFICIENT_SCOPE
    assert str(caught.value) == "insufficient-scope"


def test_admin_scope_does_not_expand_to_undeclared_authority() -> None:
    principal = ServicePrincipal("service:admin-only", (ServiceScope.ADMIN,))

    with pytest.raises(AuthorizationError, match="insufficient-scope"):
        ScopeAuthorizer().require(principal, ServiceScope.RUN)


def test_principal_and_required_scope_contracts_reject_empty_or_unknown_values() -> None:
    with pytest.raises(ValueError, match="principal_id"):
        ServicePrincipal("", (ServiceScope.READ,))
    with pytest.raises(ValueError, match="at least one"):
        ServicePrincipal("service:test", ())
    with pytest.raises(TypeError, match="non-empty"):
        ScopeAuthorizer().require(
            ServicePrincipal("service:test", (ServiceScope.READ,)),
            (),
        )
