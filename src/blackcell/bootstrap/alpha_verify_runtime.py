"""Durable scheduler for deterministic executor- and reviewer-independent verification."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
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
from blackcell.orchestration.alpha_lifecycle import ALPHA_EVENT_SOURCE, ALPHA_RUN_SUCCEEDED
from blackcell.orchestration.alpha_review_lifecycle import (
    ALPHA_REVIEW_SUCCEEDED,
    AlphaReviewLifecycleError,
    AlphaReviewLifecycleStatus,
    alpha_review_stream,
    fold_alpha_review_lifecycle,
)
from blackcell.orchestration.alpha_verify import AlphaVerificationStatus
from blackcell.orchestration.alpha_verify_lifecycle import (
    ALPHA_VERIFICATION_CLAIMED,
    ALPHA_VERIFICATION_COMPLETED,
    ALPHA_VERIFICATION_FAILED,
    ALPHA_VERIFICATION_REQUEUED,
    AlphaVerificationCandidate,
    AlphaVerificationLease,
    AlphaVerificationLifecycleError,
    AlphaVerificationLifecycleState,
    AlphaVerificationLifecycleStatus,
    alpha_verification_lease_payload,
    alpha_verification_stream,
    fold_alpha_verification_lifecycle,
)

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_FAILURE_CODE = re.compile(r"[a-z0-9][a-z0-9-]{0,127}\Z")


class AlphaVerificationRuntimeFailureCode(StrEnum):
    INVALID_REQUEST = "invalid-alpha-verification-runtime-request"
    NOT_FOUND = "alpha-verification-runtime-not-found"
    CONFLICT = "alpha-verification-runtime-conflict"


class AlphaVerificationRuntimeError(RuntimeError):
    """Content-free scheduler failure for deterministic verification work."""

    def __init__(self, code: AlphaVerificationRuntimeFailureCode) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True, slots=True)
class AlphaClaimedVerification:
    lease: AlphaVerificationLease
    claim_event_id: str


@dataclass(frozen=True, slots=True)
class AlphaVerificationReconciliationReport:
    requeued_run_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AlphaVerificationRuntimeService:
    events: EventStore

    def inspect(self, run_id: str) -> AlphaVerificationLifecycleState | None:
        try:
            stream = self.events.read_stream(alpha_verification_stream(run_id))
            return None if not stream else fold_alpha_verification_lifecycle(run_id, stream)
        except AlphaVerificationLifecycleError as error:
            raise AlphaVerificationRuntimeError(
                AlphaVerificationRuntimeFailureCode.CONFLICT
            ) from error

    def claim(
        self,
        candidate: AlphaVerificationCandidate,
        *,
        worker_id: str,
        lease_expires_at: datetime,
        claimed_at: datetime | None = None,
    ) -> AlphaClaimedVerification:
        if not isinstance(candidate, AlphaVerificationCandidate):
            raise AlphaVerificationRuntimeError(AlphaVerificationRuntimeFailureCode.INVALID_REQUEST)
        try:
            worker = _identifier(worker_id)
            at = _aware(claimed_at or utc_now())
            expires_at = _aware(lease_expires_at)
        except ValueError as error:
            raise AlphaVerificationRuntimeError(
                AlphaVerificationRuntimeFailureCode.INVALID_REQUEST
            ) from error
        if expires_at <= at:
            raise AlphaVerificationRuntimeError(AlphaVerificationRuntimeFailureCode.INVALID_REQUEST)
        execution_event, _review_event, review_actors = self._require_source_events(candidate)
        if worker == execution_event.actor or worker in review_actors:
            raise AlphaVerificationRuntimeError(AlphaVerificationRuntimeFailureCode.CONFLICT)

        stream_id = alpha_verification_stream(candidate.run_id)
        existing = self.events.read_stream(stream_id)
        previous: AlphaVerificationLifecycleState | None = None
        if existing:
            try:
                previous = fold_alpha_verification_lifecycle(candidate.run_id, existing)
            except AlphaVerificationLifecycleError as error:
                raise AlphaVerificationRuntimeError(
                    AlphaVerificationRuntimeFailureCode.CONFLICT
                ) from error
            if previous.status is not AlphaVerificationLifecycleStatus.REQUEUED:
                raise AlphaVerificationRuntimeError(AlphaVerificationRuntimeFailureCode.CONFLICT)
            if _candidate_identity(candidate) != _lease_identity(previous.lease):
                raise AlphaVerificationRuntimeError(AlphaVerificationRuntimeFailureCode.CONFLICT)

        lease = AlphaVerificationLease(
            run_id=candidate.run_id,
            verification_id=candidate.verification_id,
            attempt=1 if previous is None else previous.lease.attempt + 1,
            fencing_token=1 if previous is None else previous.lease.fencing_token + 1,
            worker_id=worker,
            run_event_id=candidate.run_event_id,
            run_event_digest=candidate.run_event_digest,
            state_digest=candidate.state_digest,
            artifact_evidence_digest=candidate.artifact_evidence_digest,
            review_id=candidate.review_id,
            review_event_id=candidate.review_event_id,
            review_event_digest=candidate.review_event_digest,
            acceptance_digest=candidate.acceptance_digest,
            context_digest=candidate.context_digest,
            proposal_artifact_digest=candidate.proposal_artifact_digest,
            provider_result_artifact_digest=candidate.provider_result_artifact_digest,
            admitted_review_digest=candidate.admitted_review_digest,
            finding_count=candidate.finding_count,
            expires_at=expires_at,
        )
        event = EventEnvelope.create(
            stream_id=stream_id,
            stream_sequence=len(existing) + 1,
            event_type=ALPHA_VERIFICATION_CLAIMED,
            actor=worker,
            source=ALPHA_EVENT_SOURCE,
            payload={
                "principal_id": worker,
                "lease_digest": lease.digest,
                "lease": alpha_verification_lease_payload(lease),
                "status": "claimed",
            },
            recorded_at=at,
            effective_at=at,
            correlation_id=candidate.correlation_id,
            causation_id=(
                candidate.review_event_id if previous is None else previous.latest_event.event_id
            ),
            idempotency_key=f"verification-claimed:{lease.digest}",
        )
        self._append(candidate.run_id, existing, event)
        return AlphaClaimedVerification(lease=lease, claim_event_id=event.event_id)

    def record_completed(
        self,
        lease: AlphaVerificationLease,
        *,
        verdict: AlphaVerificationStatus,
        report_artifact_digest: str,
        matrix_digest: str,
        principal_id: str,
        completed_at: datetime | None = None,
    ) -> AlphaVerificationLifecycleState:
        state, existing = self._require_active(lease, principal_id)
        try:
            if not isinstance(verdict, AlphaVerificationStatus):
                raise ValueError
            report = _digest(report_artifact_digest)
            matrix = _digest(matrix_digest)
            at = _aware(completed_at or utc_now())
        except ValueError as error:
            raise AlphaVerificationRuntimeError(
                AlphaVerificationRuntimeFailureCode.INVALID_REQUEST
            ) from error
        if at > lease.expires_at:
            raise AlphaVerificationRuntimeError(AlphaVerificationRuntimeFailureCode.CONFLICT)
        event = self._transition(
            state,
            ALPHA_VERIFICATION_COMPLETED,
            {
                "run_id": lease.run_id,
                "verification_id": lease.verification_id,
                "lease_digest": lease.digest,
                "verdict": verdict.value,
                "report_artifact_digest": report,
                "matrix_digest": matrix,
                "status": "completed",
            },
            principal_id=principal_id,
            idempotency_key=f"verification-completed:{lease.digest}",
            recorded_at=at,
        )
        self._append(lease.run_id, existing, event)
        return self._require_state(lease.run_id)

    def record_failure(
        self,
        lease: AlphaVerificationLease,
        *,
        failure_code: str,
        result_artifact_digest: str | None,
        principal_id: str,
        failed_at: datetime | None = None,
    ) -> AlphaVerificationLifecycleState:
        state, existing = self._require_active(lease, principal_id)
        try:
            failure = _failure(failure_code)
            result = None if result_artifact_digest is None else _digest(result_artifact_digest)
            at = _aware(failed_at or utc_now())
        except ValueError as error:
            raise AlphaVerificationRuntimeError(
                AlphaVerificationRuntimeFailureCode.INVALID_REQUEST
            ) from error
        if at > lease.expires_at:
            raise AlphaVerificationRuntimeError(AlphaVerificationRuntimeFailureCode.CONFLICT)
        event = self._transition(
            state,
            ALPHA_VERIFICATION_FAILED,
            {
                "run_id": lease.run_id,
                "verification_id": lease.verification_id,
                "lease_digest": lease.digest,
                "failure_code": failure,
                "result_artifact_digest": result,
                "status": "verifier-error",
            },
            principal_id=principal_id,
            idempotency_key=f"verification-failed:{lease.digest}",
            recorded_at=at,
        )
        self._append(lease.run_id, existing, event)
        return self._require_state(lease.run_id)

    def reconcile(self, *, principal_id: str) -> AlphaVerificationReconciliationReport:
        try:
            principal = _identifier(principal_id)
        except ValueError as error:
            raise AlphaVerificationRuntimeError(
                AlphaVerificationRuntimeFailureCode.INVALID_REQUEST
            ) from error
        requeued: list[str] = []
        for run_id in self._verification_run_ids():
            state = self._require_state(run_id)
            if state.status is not AlphaVerificationLifecycleStatus.CLAIMED:
                continue
            if principal == state.lease.worker_id:
                raise AlphaVerificationRuntimeError(AlphaVerificationRuntimeFailureCode.CONFLICT)
            existing = self.events.read_stream(alpha_verification_stream(run_id))
            event = self._transition(
                state,
                ALPHA_VERIFICATION_REQUEUED,
                {
                    "run_id": run_id,
                    "verification_id": state.verification_id,
                    "lease_digest": state.lease.digest,
                    "disposition": "deterministic-retry",
                    "status": "requeued",
                },
                principal_id=principal,
                idempotency_key=f"verification-requeued:{state.lease.digest}",
                recorded_at=utc_now(),
            )
            self._append(run_id, existing, event)
            requeued.append(run_id)
        return AlphaVerificationReconciliationReport(tuple(requeued))

    def _require_active(
        self,
        lease: AlphaVerificationLease,
        principal_id: str,
    ) -> tuple[AlphaVerificationLifecycleState, tuple[EventEnvelope, ...]]:
        if not isinstance(lease, AlphaVerificationLease):
            raise AlphaVerificationRuntimeError(AlphaVerificationRuntimeFailureCode.INVALID_REQUEST)
        try:
            principal = _identifier(principal_id)
        except ValueError as error:
            raise AlphaVerificationRuntimeError(
                AlphaVerificationRuntimeFailureCode.INVALID_REQUEST
            ) from error
        existing = self.events.read_stream(alpha_verification_stream(lease.run_id))
        if not existing:
            raise AlphaVerificationRuntimeError(AlphaVerificationRuntimeFailureCode.NOT_FOUND)
        try:
            state = fold_alpha_verification_lifecycle(lease.run_id, existing)
        except AlphaVerificationLifecycleError as error:
            raise AlphaVerificationRuntimeError(
                AlphaVerificationRuntimeFailureCode.CONFLICT
            ) from error
        if (
            state.status is not AlphaVerificationLifecycleStatus.CLAIMED
            or state.lease.digest != lease.digest
            or state.lease.worker_id != principal
        ):
            raise AlphaVerificationRuntimeError(AlphaVerificationRuntimeFailureCode.CONFLICT)
        return state, existing

    def _require_state(self, run_id: str) -> AlphaVerificationLifecycleState:
        state = self.inspect(run_id)
        if state is None:
            raise AlphaVerificationRuntimeError(AlphaVerificationRuntimeFailureCode.NOT_FOUND)
        return state

    def _require_source_events(
        self,
        candidate: AlphaVerificationCandidate,
    ) -> tuple[EventEnvelope, EventEnvelope, frozenset[str]]:
        execution = self.events.get(candidate.run_event_id)
        review = self.events.get(candidate.review_event_id)
        if (
            execution is None
            or execution.stream_id != f"alpha:run:{candidate.run_id}"
            or execution.event_type != ALPHA_RUN_SUCCEEDED
            or execution.source != ALPHA_EVENT_SOURCE
            or execution.correlation_id != candidate.correlation_id
            or execution.payload_hash != candidate.run_event_digest
            or execution.payload.get("run_id") != candidate.run_id
            or execution.payload.get("status") != "succeeded"
            or review is None
            or review.stream_id != alpha_review_stream(candidate.run_id)
            or review.event_type != ALPHA_REVIEW_SUCCEEDED
            or review.source != ALPHA_EVENT_SOURCE
            or review.correlation_id != candidate.correlation_id
            or review.payload_hash != candidate.review_event_digest
        ):
            raise AlphaVerificationRuntimeError(AlphaVerificationRuntimeFailureCode.CONFLICT)
        try:
            review_stream = self.events.read_stream(alpha_review_stream(candidate.run_id))
            review_state = fold_alpha_review_lifecycle(candidate.run_id, review_stream)
        except AlphaReviewLifecycleError as error:
            raise AlphaVerificationRuntimeError(
                AlphaVerificationRuntimeFailureCode.CONFLICT
            ) from error
        lease = review_state.lease
        if (
            review_state.status is not AlphaReviewLifecycleStatus.SUCCEEDED
            or review_state.latest_event.event_id != review.event_id
            or lease.run_event_id != candidate.run_event_id
            or lease.run_event_digest != candidate.run_event_digest
            or lease.state_digest != candidate.state_digest
            or lease.artifact_evidence_digest != candidate.artifact_evidence_digest
            or review_state.review_id != candidate.review_id
            or review_state.acceptance_digest != candidate.acceptance_digest
            or review_state.context_digest != candidate.context_digest
            or review_state.proposal_artifact_digest != candidate.proposal_artifact_digest
            or review_state.provider_result_artifact_digest
            != candidate.provider_result_artifact_digest
            or review_state.admitted_artifact_digest != candidate.admitted_review_digest
            or review_state.finding_count != candidate.finding_count
        ):
            raise AlphaVerificationRuntimeError(AlphaVerificationRuntimeFailureCode.CONFLICT)
        return execution, review, frozenset(event.actor for event in review_stream)

    @staticmethod
    def _transition(
        state: AlphaVerificationLifecycleState,
        event_type: str,
        payload: Mapping[str, JsonInput],
        *,
        principal_id: str,
        idempotency_key: str,
        recorded_at: datetime,
    ) -> EventEnvelope:
        return EventEnvelope.create(
            stream_id=alpha_verification_stream(state.run_id),
            stream_sequence=state.stream_sequence + 1,
            event_type=event_type,
            actor=principal_id,
            source=ALPHA_EVENT_SOURCE,
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
            fold_alpha_verification_lifecycle(run_id, (*existing, event))
            stored = self.events.append(event, expected_sequence=len(existing))
            fold_alpha_verification_lifecycle(run_id, (*existing, stored))
        except AlphaVerificationLifecycleError as error:
            raise AlphaVerificationRuntimeError(
                AlphaVerificationRuntimeFailureCode.CONFLICT
            ) from error
        except (ConcurrencyError, EventConflictError, IdempotencyConflict) as error:
            raise AlphaVerificationRuntimeError(
                AlphaVerificationRuntimeFailureCode.CONFLICT
            ) from error

    def _verification_run_ids(self) -> tuple[str, ...]:
        run_ids: list[str] = []
        seen: set[str] = set()
        cursor = 0
        while True:
            events = self.events.read_all(after_position=cursor, limit=200)
            if not events:
                break
            for event in events:
                if event.event_type == ALPHA_VERIFICATION_CLAIMED and event.stream_id.startswith(
                    "alpha:verification:"
                ):
                    run_id = event.stream_id.removeprefix("alpha:verification:")
                    if run_id not in seen:
                        seen.add(run_id)
                        run_ids.append(run_id)
            position = events[-1].global_position
            if position is None:
                raise AlphaVerificationRuntimeError(AlphaVerificationRuntimeFailureCode.CONFLICT)
            cursor = position
        return tuple(run_ids)


def _candidate_identity(value: AlphaVerificationCandidate) -> tuple[object, ...]:
    return (
        value.run_id,
        value.verification_id,
        value.run_event_id,
        value.run_event_digest,
        value.state_digest,
        value.artifact_evidence_digest,
        value.review_id,
        value.review_event_id,
        value.review_event_digest,
        value.acceptance_digest,
        value.context_digest,
        value.proposal_artifact_digest,
        value.provider_result_artifact_digest,
        value.admitted_review_digest,
        value.finding_count,
    )


def _lease_identity(value: AlphaVerificationLease) -> tuple[object, ...]:
    return (
        value.run_id,
        value.verification_id,
        value.run_event_id,
        value.run_event_digest,
        value.state_digest,
        value.artifact_evidence_digest,
        value.review_id,
        value.review_event_id,
        value.review_event_digest,
        value.acceptance_digest,
        value.context_digest,
        value.proposal_artifact_digest,
        value.provider_result_artifact_digest,
        value.admitted_review_digest,
        value.finding_count,
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


def _aware(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError
    return value


__all__ = [
    "AlphaClaimedVerification",
    "AlphaVerificationReconciliationReport",
    "AlphaVerificationRuntimeError",
    "AlphaVerificationRuntimeFailureCode",
    "AlphaVerificationRuntimeService",
]
