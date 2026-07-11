from __future__ import annotations

from blackcell.features.request_decision.models import DecisionFailureKind


class DecisionRequestError(RuntimeError):
    """Base error for the request-decision application boundary."""


class DecisionOutputError(DecisionRequestError):
    """A model response cannot be admitted as a typed decision proposal."""


class DecisionJournalError(DecisionRequestError):
    """The durable decision-attempt record is unavailable or invalid."""


class DecisionIdentityConflict(DecisionJournalError):
    """A durable decision identity was reused with different semantic content."""


class DecisionAttemptInProgress(DecisionJournalError):
    """A decision attempt is already active and must not be invoked again."""


class DecisionGatewayError(DecisionRequestError):
    """Stable, content-free failure reported by a gateway edge adapter."""

    def __init__(
        self,
        kind: DecisionFailureKind,
        code: str,
        *,
        retryable: bool = False,
        exception_type: str | None = None,
    ) -> None:
        if not isinstance(kind, DecisionFailureKind):
            raise TypeError("gateway failure kind must be a DecisionFailureKind")
        if not code.strip():
            raise ValueError("gateway failure code must not be empty")
        if not isinstance(retryable, bool):
            raise TypeError("gateway failure retryable marker must be a boolean")
        if exception_type is not None and not exception_type.strip():
            raise ValueError("gateway failure exception_type must not be blank")
        super().__init__(code)
        self.kind = kind
        self.code = code
        self.retryable = retryable
        self.exception_type = exception_type


__all__ = [
    "DecisionAttemptInProgress",
    "DecisionGatewayError",
    "DecisionIdentityConflict",
    "DecisionJournalError",
    "DecisionOutputError",
    "DecisionRequestError",
]
