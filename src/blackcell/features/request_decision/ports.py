from __future__ import annotations

from datetime import datetime
from typing import Protocol

from blackcell.features.request_decision.command import RequestDecision
from blackcell.features.request_decision.models import (
    DecisionAdapterResult,
    DecisionAttemptClaim,
    DecisionFailure,
    DecisionFailureRecord,
    DecisionPreparation,
    DecisionRequestRecord,
    DecisionResponse,
    DecisionRoute,
    DecisionSuccessRecord,
    DecisionTerminalRecord,
    DecisionUsage,
)


class DecisionGatewayPort(Protocol):
    """Policy-controlled model gateway with no tool or action authority."""

    def route(self, request: RequestDecision) -> DecisionRoute: ...

    def invoke(
        self,
        request: RequestDecision,
        route: DecisionRoute,
    ) -> DecisionAdapterResult: ...


class DecisionAttemptJournal(Protocol):
    """Durable request and attempt coordination owned by an edge adapter."""

    def register(
        self,
        request: RequestDecision,
        *,
        registered_at: datetime,
    ) -> DecisionRequestRecord: ...

    def record_route(
        self,
        request: DecisionRequestRecord,
        route: DecisionRoute,
        *,
        recorded_at: datetime,
    ) -> DecisionPreparation | DecisionTerminalRecord: ...

    def reject(
        self,
        request: DecisionRequestRecord,
        failure: DecisionFailure,
        *,
        recorded_at: datetime,
    ) -> DecisionFailureRecord: ...

    def acquire(
        self,
        preparation: DecisionPreparation,
        *,
        acquired_at: datetime,
    ) -> DecisionAttemptClaim | DecisionTerminalRecord: ...

    def begin_invoke(
        self,
        preparation: DecisionPreparation,
        claim: DecisionAttemptClaim,
        *,
        invoked_at: datetime,
    ) -> DecisionAttemptClaim | DecisionTerminalRecord:
        """Atomically admit one exact fenced claim across the live-call boundary."""
        ...

    def succeed(
        self,
        preparation: DecisionPreparation,
        claim: DecisionAttemptClaim,
        response: DecisionResponse,
        usage: DecisionUsage,
        *,
        recorded_at: datetime,
    ) -> DecisionSuccessRecord: ...

    def fail(
        self,
        request: DecisionRequestRecord,
        failure: DecisionFailure,
        *,
        preparation: DecisionPreparation | None,
        claim: DecisionAttemptClaim | None,
        usage: DecisionUsage | None,
        recorded_at: datetime,
    ) -> DecisionFailureRecord: ...


class Clock(Protocol):
    def __call__(self) -> datetime: ...


__all__ = ["Clock", "DecisionAttemptJournal", "DecisionGatewayPort"]
