"""Durable scheduler for the executor-independent execution review stage."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from blackcell.kernel import (
    ConcurrencyError,
    EventConflictError,
    EventEnvelope,
    EventStore,
    IdempotencyConflict,
    JsonInput,
    utc_now,
)
from blackcell.orchestration.review_lifecycle import (
    REVIEW_CLAIMED,
    REVIEW_DISPATCH_AMBIGUOUS,
    REVIEW_FAILED,
    REVIEW_LEASE_RENEWED,
    REVIEW_PROVIDER_DISPATCH_STARTED,
    REVIEW_RECONCILIATION_REQUIRED,
    REVIEW_REQUEUED,
    REVIEW_SUCCEEDED,
    ReviewCandidate,
    ReviewLease,
    ReviewLifecycleError,
    ReviewLifecycleState,
    ReviewLifecycleStatus,
    fold_review_lifecycle,
    review_lease_payload,
    review_provider_request_id,
    review_stream,
)
from blackcell.orchestration.run_lifecycle import RUN_SUCCEEDED, RUNTIME_EVENT_SOURCE

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_FAILURE_CODE = re.compile(r"[a-z0-9][a-z0-9-]{0,127}\Z")
_MAX_FINDINGS = 64


class ReviewRuntimeFailureCode(StrEnum):
    INVALID_REQUEST = "invalid-review-runtime-request"
    NOT_FOUND = "review-runtime-not-found"
    CONFLICT = "review-runtime-conflict"


class ReviewRuntimeError(RuntimeError):
    """Content-free scheduler failure for durable review work."""

    def __init__(self, code: ReviewRuntimeFailureCode) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True, slots=True)
class ClaimedReview:
    lease: ReviewLease
    claim_event_id: str


@dataclass(frozen=True, slots=True)
class ReviewReconciliationReport:
    requeued_run_ids: tuple[str, ...]
    ambiguous_run_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReviewRuntimeService:
    events: EventStore

    def inspect(self, run_id: str) -> ReviewLifecycleState | None:
        try:
            stream = self.events.read_stream(review_stream(run_id))
            return None if not stream else fold_review_lifecycle(run_id, stream)
        except ReviewLifecycleError as error:
            raise ReviewRuntimeError(ReviewRuntimeFailureCode.CONFLICT) from error

    def claim(
        self,
        candidate: ReviewCandidate,
        *,
        worker_id: str,
        lease_expires_at: datetime,
        claimed_at: datetime | None = None,
    ) -> ClaimedReview:
        if not isinstance(candidate, ReviewCandidate):
            raise ReviewRuntimeError(ReviewRuntimeFailureCode.INVALID_REQUEST)
        try:
            worker = _identifier(worker_id)
            at = _aware(claimed_at or utc_now())
            expires_at = _aware(lease_expires_at)
        except ValueError as error:
            raise ReviewRuntimeError(ReviewRuntimeFailureCode.INVALID_REQUEST) from error
        if expires_at <= at:
            raise ReviewRuntimeError(ReviewRuntimeFailureCode.INVALID_REQUEST)
        execution_event = self._require_execution_event(candidate)
        if execution_event.actor == worker:
            raise ReviewRuntimeError(ReviewRuntimeFailureCode.CONFLICT)

        stream_id = review_stream(candidate.run_id)
        existing = self.events.read_stream(stream_id)
        previous: ReviewLifecycleState | None = None
        if existing:
            try:
                previous = fold_review_lifecycle(candidate.run_id, existing)
            except ReviewLifecycleError as error:
                raise ReviewRuntimeError(ReviewRuntimeFailureCode.CONFLICT) from error
            if previous.status is not ReviewLifecycleStatus.REQUEUED:
                raise ReviewRuntimeError(ReviewRuntimeFailureCode.CONFLICT)
            if _candidate_identity(candidate) != _lease_identity(previous.lease):
                raise ReviewRuntimeError(ReviewRuntimeFailureCode.CONFLICT)

        lease = ReviewLease(
            run_id=candidate.run_id,
            review_id=candidate.review_id,
            attempt=1 if previous is None else previous.lease.attempt + 1,
            fencing_token=1 if previous is None else previous.lease.fencing_token + 1,
            worker_id=worker,
            run_event_id=candidate.run_event_id,
            run_event_digest=candidate.run_event_digest,
            state_digest=candidate.state_digest,
            artifact_evidence_digest=candidate.artifact_evidence_digest,
            expires_at=expires_at,
        )
        event = EventEnvelope.create(
            stream_id=stream_id,
            stream_sequence=len(existing) + 1,
            event_type=REVIEW_CLAIMED,
            actor=worker,
            source=RUNTIME_EVENT_SOURCE,
            payload={
                "principal_id": worker,
                "lease_digest": lease.digest,
                "lease": review_lease_payload(lease),
                "status": "claimed",
            },
            recorded_at=at,
            effective_at=at,
            correlation_id=candidate.correlation_id,
            causation_id=(
                candidate.run_event_id if previous is None else previous.latest_event.event_id
            ),
            idempotency_key=f"review-claimed:{lease.digest}",
        )
        self._append(candidate.run_id, existing, event)
        return ClaimedReview(lease=lease, claim_event_id=event.event_id)

    def renew_lease(
        self,
        lease: ReviewLease,
        *,
        lease_expires_at: datetime,
        principal_id: str,
        renewed_at: datetime | None = None,
    ) -> ReviewLease:
        state, existing = self._require_active(
            lease,
            principal_id,
            ReviewLifecycleStatus.CLAIMED,
        )
        try:
            at = _aware(renewed_at or utc_now())
            expires_at = _aware(lease_expires_at)
        except ValueError as error:
            raise ReviewRuntimeError(ReviewRuntimeFailureCode.INVALID_REQUEST) from error
        if expires_at <= at or expires_at <= lease.expires_at:
            raise ReviewRuntimeError(ReviewRuntimeFailureCode.INVALID_REQUEST)
        renewed = replace(lease, expires_at=expires_at)
        event = self._transition(
            state,
            REVIEW_LEASE_RENEWED,
            {
                "previous_lease_digest": lease.digest,
                "lease_digest": renewed.digest,
                "lease": review_lease_payload(renewed),
                "status": "claimed",
            },
            principal_id=principal_id,
            idempotency_key=f"review-lease-renewed:{lease.digest}:{renewed.digest}",
            recorded_at=at,
        )
        self._append(lease.run_id, existing, event)
        return renewed

    def record_provider_dispatch(
        self,
        lease: ReviewLease,
        *,
        acceptance_digest: str,
        context_digest: str,
        context_artifact_digest: str,
        principal_id: str,
        dispatched_at: datetime | None = None,
    ) -> str:
        state, existing = self._require_active(
            lease,
            principal_id,
            ReviewLifecycleStatus.CLAIMED,
        )
        try:
            acceptance = _digest(acceptance_digest)
            context = _digest(context_digest)
            context_artifact = _digest(context_artifact_digest)
            at = _aware(dispatched_at or utc_now())
        except ValueError as error:
            raise ReviewRuntimeError(ReviewRuntimeFailureCode.INVALID_REQUEST) from error
        if context != context_artifact or at > lease.expires_at:
            raise ReviewRuntimeError(ReviewRuntimeFailureCode.CONFLICT)
        request_id = review_provider_request_id(lease.digest)
        event = self._transition(
            state,
            REVIEW_PROVIDER_DISPATCH_STARTED,
            {
                "run_id": lease.run_id,
                "review_id": lease.review_id,
                "lease_digest": lease.digest,
                "provider_request_id": request_id,
                "acceptance_digest": acceptance,
                "context_digest": context,
                "context_artifact_digest": context_artifact,
                "status": "provider-dispatch-started",
            },
            principal_id=principal_id,
            idempotency_key=f"review-provider-dispatch:{lease.digest}",
            recorded_at=at,
        )
        self._append(lease.run_id, existing, event)
        return event.event_id

    def record_success(
        self,
        lease: ReviewLease,
        *,
        context_digest: str,
        proposal_artifact_digest: str,
        provider_result_artifact_digest: str,
        admitted_artifact_digest: str,
        finding_count: int,
        principal_id: str,
        completed_at: datetime | None = None,
    ) -> ReviewLifecycleState:
        state, existing = self._require_active(
            lease,
            principal_id,
            ReviewLifecycleStatus.PROVIDER_DISPATCHED,
        )
        try:
            context = _digest(context_digest)
            proposal = _digest(proposal_artifact_digest)
            provider = _digest(provider_result_artifact_digest)
            admitted = _digest(admitted_artifact_digest)
            findings = _bounded_integer(finding_count, maximum=_MAX_FINDINGS)
            at = _aware(completed_at or utc_now())
        except ValueError as error:
            raise ReviewRuntimeError(ReviewRuntimeFailureCode.INVALID_REQUEST) from error
        if context != state.context_digest or at > lease.expires_at:
            raise ReviewRuntimeError(ReviewRuntimeFailureCode.CONFLICT)
        event = self._transition(
            state,
            REVIEW_SUCCEEDED,
            {
                "run_id": lease.run_id,
                "review_id": lease.review_id,
                "lease_digest": lease.digest,
                "context_digest": context,
                "proposal_artifact_digest": proposal,
                "provider_result_artifact_digest": provider,
                "admitted_artifact_digest": admitted,
                "finding_count": findings,
                "status": "succeeded",
            },
            principal_id=principal_id,
            idempotency_key=f"review-succeeded:{lease.digest}",
            recorded_at=at,
        )
        self._append(lease.run_id, existing, event)
        return self._require_state(lease.run_id)

    def record_failure(
        self,
        lease: ReviewLease,
        *,
        failure_code: str,
        result_artifact_digest: str | None,
        principal_id: str,
        failed_at: datetime | None = None,
    ) -> ReviewLifecycleState:
        state, existing = self._require_active(lease, principal_id)
        try:
            failure = _failure(failure_code)
            result = None if result_artifact_digest is None else _digest(result_artifact_digest)
            at = _aware(failed_at or utc_now())
        except ValueError as error:
            raise ReviewRuntimeError(ReviewRuntimeFailureCode.INVALID_REQUEST) from error
        event = self._transition(
            state,
            REVIEW_FAILED,
            {
                "run_id": lease.run_id,
                "review_id": lease.review_id,
                "lease_digest": lease.digest,
                "failure_code": failure,
                "result_artifact_digest": result,
                "status": "reviewer-error",
            },
            principal_id=principal_id,
            idempotency_key=f"review-failed:{lease.digest}",
            recorded_at=at,
        )
        self._append(lease.run_id, existing, event)
        return self._require_state(lease.run_id)

    def reconcile(self, *, principal_id: str) -> ReviewReconciliationReport:
        try:
            principal = _identifier(principal_id)
        except ValueError as error:
            raise ReviewRuntimeError(ReviewRuntimeFailureCode.INVALID_REQUEST) from error
        requeued: list[str] = []
        ambiguous: list[str] = []
        for run_id in self._review_run_ids():
            state = self._require_state(run_id)
            existing = self.events.read_stream(review_stream(run_id))
            if state.active and principal == state.lease.worker_id:
                raise ReviewRuntimeError(ReviewRuntimeFailureCode.CONFLICT)
            if state.status is ReviewLifecycleStatus.CLAIMED:
                event_type = REVIEW_REQUEUED
                payload: Mapping[str, JsonInput] = {
                    "run_id": run_id,
                    "review_id": state.review_id,
                    "lease_digest": state.lease.digest,
                    "disposition": "pre-dispatch",
                    "status": "requeued",
                }
                key = f"review-requeued:{state.lease.digest}"
                requeued.append(run_id)
            elif state.status is ReviewLifecycleStatus.PROVIDER_DISPATCHED:
                event_type = REVIEW_RECONCILIATION_REQUIRED
                payload = {
                    "run_id": run_id,
                    "review_id": state.review_id,
                    "lease_digest": state.lease.digest,
                    "failure_code": REVIEW_DISPATCH_AMBIGUOUS,
                    "status": "reconciliation-required",
                }
                key = f"review-reconciliation-required:{state.lease.digest}"
                ambiguous.append(run_id)
            else:
                continue
            event = self._transition(
                state,
                event_type,
                payload,
                principal_id=principal,
                idempotency_key=key,
                recorded_at=utc_now(),
            )
            self._append(run_id, existing, event)
        return ReviewReconciliationReport(tuple(requeued), tuple(ambiguous))

    def _require_active(
        self,
        lease: ReviewLease,
        principal_id: str,
        expected_status: ReviewLifecycleStatus | None = None,
    ) -> tuple[ReviewLifecycleState, tuple[EventEnvelope, ...]]:
        if not isinstance(lease, ReviewLease):
            raise ReviewRuntimeError(ReviewRuntimeFailureCode.INVALID_REQUEST)
        try:
            principal = _identifier(principal_id)
        except ValueError as error:
            raise ReviewRuntimeError(ReviewRuntimeFailureCode.INVALID_REQUEST) from error
        existing = self.events.read_stream(review_stream(lease.run_id))
        if not existing:
            raise ReviewRuntimeError(ReviewRuntimeFailureCode.NOT_FOUND)
        try:
            state = fold_review_lifecycle(lease.run_id, existing)
        except ReviewLifecycleError as error:
            raise ReviewRuntimeError(ReviewRuntimeFailureCode.CONFLICT) from error
        allowed = (
            {ReviewLifecycleStatus.CLAIMED, ReviewLifecycleStatus.PROVIDER_DISPATCHED}
            if expected_status is None
            else {expected_status}
        )
        if (
            state.status not in allowed
            or state.lease.digest != lease.digest
            or state.lease.worker_id != principal
        ):
            raise ReviewRuntimeError(ReviewRuntimeFailureCode.CONFLICT)
        return state, existing

    def _require_state(self, run_id: str) -> ReviewLifecycleState:
        state = self.inspect(run_id)
        if state is None:
            raise ReviewRuntimeError(ReviewRuntimeFailureCode.NOT_FOUND)
        return state

    def _require_execution_event(self, candidate: ReviewCandidate) -> EventEnvelope:
        event = self.events.get(candidate.run_event_id)
        if (
            event is None
            or event.stream_id != f"run:{candidate.run_id}"
            or event.event_type != RUN_SUCCEEDED
            or event.source != RUNTIME_EVENT_SOURCE
            or event.correlation_id != candidate.correlation_id
            or event.payload_hash != candidate.run_event_digest
            or event.payload.get("run_id") != candidate.run_id
            or event.payload.get("status") != "succeeded"
        ):
            raise ReviewRuntimeError(ReviewRuntimeFailureCode.CONFLICT)
        return event

    @staticmethod
    def _transition(
        state: ReviewLifecycleState,
        event_type: str,
        payload: Mapping[str, JsonInput],
        *,
        principal_id: str,
        idempotency_key: str,
        recorded_at: datetime,
    ) -> EventEnvelope:
        return EventEnvelope.create(
            stream_id=review_stream(state.run_id),
            stream_sequence=state.stream_sequence + 1,
            event_type=event_type,
            actor=principal_id,
            source=RUNTIME_EVENT_SOURCE,
            payload={"principal_id": principal_id, **dict(payload)},
            recorded_at=recorded_at,
            effective_at=recorded_at,
            correlation_id=state.latest_event.correlation_id,
            causation_id=state.latest_event.event_id,
            idempotency_key=idempotency_key,
        )

    def _append(
        self,
        run_id: str,
        existing: tuple[EventEnvelope, ...],
        event: EventEnvelope,
    ) -> None:
        try:
            fold_review_lifecycle(run_id, (*existing, event))
            stored = self.events.append(event, expected_sequence=len(existing))
            fold_review_lifecycle(run_id, (*existing, stored))
        except ReviewLifecycleError as error:
            raise ReviewRuntimeError(ReviewRuntimeFailureCode.CONFLICT) from error
        except (ConcurrencyError, EventConflictError, IdempotencyConflict) as error:
            raise ReviewRuntimeError(ReviewRuntimeFailureCode.CONFLICT) from error

    def _review_run_ids(self) -> tuple[str, ...]:
        run_ids: list[str] = []
        seen: set[str] = set()
        cursor = 0
        while True:
            events = self.events.read_all(after_position=cursor, limit=200)
            if not events:
                break
            for event in events:
                if event.event_type == REVIEW_CLAIMED and event.stream_id.startswith("review:"):
                    run_id = event.stream_id.removeprefix("review:")
                    if run_id not in seen:
                        seen.add(run_id)
                        run_ids.append(run_id)
            position = events[-1].global_position
            if position is None:
                raise ReviewRuntimeError(ReviewRuntimeFailureCode.CONFLICT)
            cursor = position
        return tuple(run_ids)


def _candidate_identity(value: ReviewCandidate) -> tuple[str, ...]:
    return (
        value.run_id,
        value.review_id,
        value.run_event_id,
        value.run_event_digest,
        value.state_digest,
        value.artifact_evidence_digest,
    )


def _lease_identity(value: ReviewLease) -> tuple[str, ...]:
    return (
        value.run_id,
        value.review_id,
        value.run_event_id,
        value.run_event_digest,
        value.state_digest,
        value.artifact_evidence_digest,
    )


def _identifier(value: object) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError
    return value


def _digest(value: object) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError
    return value


def _failure(value: object) -> str:
    if not isinstance(value, str) or _FAILURE_CODE.fullmatch(value) is None:
        raise ValueError
    return value


def _bounded_integer(value: object, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise ValueError
    return value


def _aware(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError
    return value


__all__ = [
    "ClaimedReview",
    "ReviewCandidate",
    "ReviewReconciliationReport",
    "ReviewRuntimeError",
    "ReviewRuntimeFailureCode",
    "ReviewRuntimeService",
]
