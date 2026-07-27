"""Strict fenced lifecycle for executor-independent execution review work."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import cast

from blackcell.kernel import EventEnvelope, JsonInput, JsonValue
from blackcell.kernel._json import json_digest
from blackcell.orchestration.run_lifecycle import RUNTIME_EVENT_SOURCE

REVIEW_CLAIMED = "review.claimed"
REVIEW_LEASE_RENEWED = "review.lease-renewed"
REVIEW_PROVIDER_DISPATCH_STARTED = "review.provider-dispatch-started"
REVIEW_SUCCEEDED = "review.succeeded"
REVIEW_FAILED = "review.failed"
REVIEW_REQUEUED = "review.requeued"
REVIEW_RECONCILIATION_REQUIRED = "review.reconciliation-required"
REVIEW_LEASE_SCHEMA = "review-lease/v1"
REVIEW_DISPATCH_AMBIGUOUS = "review-dispatch-ambiguous"

REVIEW_EVENT_TYPES = frozenset(
    {
        REVIEW_CLAIMED,
        REVIEW_LEASE_RENEWED,
        REVIEW_PROVIDER_DISPATCH_STARTED,
        REVIEW_SUCCEEDED,
        REVIEW_FAILED,
        REVIEW_REQUEUED,
        REVIEW_RECONCILIATION_REQUIRED,
    }
)

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_FAILURE_CODE = re.compile(r"[a-z0-9][a-z0-9-]{0,127}\Z")
_MAX_FINDINGS = 64


class ReviewLifecycleError(ValueError):
    """Content-free rejection of an invalid durable review history."""

    def __init__(self) -> None:
        super().__init__("invalid-review-lifecycle")


class ReviewLifecycleStatus(StrEnum):
    CLAIMED = "claimed"
    PROVIDER_DISPATCHED = "provider-dispatched"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REQUEUED = "requeued"
    RECONCILIATION_REQUIRED = "reconciliation-required"


@dataclass(frozen=True, slots=True)
class ReviewCandidate:
    run_id: str
    review_id: str
    correlation_id: str
    run_event_id: str
    run_event_digest: str
    state_digest: str
    artifact_evidence_digest: str

    def __post_init__(self) -> None:
        for value in (
            self.run_id,
            self.review_id,
            self.correlation_id,
            self.run_event_id,
        ):
            _identifier(value)
        for value in (
            self.run_event_digest,
            self.state_digest,
            self.artifact_evidence_digest,
        ):
            _digest(value)
        if self.review_id != review_id(self.run_id, self.run_event_digest):
            raise ReviewLifecycleError()


@dataclass(frozen=True, slots=True)
class ReviewLease:
    run_id: str
    review_id: str
    attempt: int
    fencing_token: int
    worker_id: str
    run_event_id: str
    run_event_digest: str
    state_digest: str
    artifact_evidence_digest: str
    expires_at: datetime
    schema_version: str = REVIEW_LEASE_SCHEMA

    def __post_init__(self) -> None:
        if (
            self.schema_version != REVIEW_LEASE_SCHEMA
            or any(
                not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None
                for value in (
                    self.run_id,
                    self.review_id,
                    self.worker_id,
                    self.run_event_id,
                )
            )
            or isinstance(self.attempt, bool)
            or not isinstance(self.attempt, int)
            or self.attempt < 1
            or isinstance(self.fencing_token, bool)
            or not isinstance(self.fencing_token, int)
            or self.fencing_token < 1
            or any(
                not isinstance(value, str) or _DIGEST.fullmatch(value) is None
                for value in (
                    self.run_event_digest,
                    self.state_digest,
                    self.artifact_evidence_digest,
                )
            )
            or not isinstance(self.expires_at, datetime)
            or self.expires_at.tzinfo is None
            or self.expires_at.utcoffset() is None
        ):
            raise ReviewLifecycleError()

    @property
    def digest(self) -> str:
        return json_digest(review_lease_payload(self))


@dataclass(frozen=True, slots=True)
class ReviewLifecycleState:
    run_id: str
    review_id: str
    status: ReviewLifecycleStatus
    lease: ReviewLease
    acceptance_digest: str | None
    context_digest: str | None
    provider_request_id: str | None
    provider_dispatch_event_id: str | None
    proposal_artifact_digest: str | None
    provider_result_artifact_digest: str | None
    admitted_artifact_digest: str | None
    finding_count: int | None
    failure_code: str | None
    result_artifact_digest: str | None
    latest_event: EventEnvelope

    @property
    def stream_sequence(self) -> int:
        return self.latest_event.stream_sequence

    @property
    def active(self) -> bool:
        return self.status in {
            ReviewLifecycleStatus.CLAIMED,
            ReviewLifecycleStatus.PROVIDER_DISPATCHED,
        }


def review_id(run_id: str, run_event_digest: str) -> str:
    _identifier(run_id)
    _digest(run_event_digest)
    suffix = json_digest(
        {"run_id": run_id, "run_event_digest": run_event_digest, "stage": "review"}
    ).removeprefix("sha256:")
    return f"review-{suffix[:48]}"


def review_provider_request_id(lease_digest: str) -> str:
    _digest(lease_digest)
    return f"review-{lease_digest.removeprefix('sha256:')[:48]}"


def review_stream(run_id: str) -> str:
    return f"review:{_identifier(run_id)}"


def review_lease_payload(value: ReviewLease) -> dict[str, JsonInput]:
    if not isinstance(value, ReviewLease):
        raise ReviewLifecycleError()
    return {
        "schema_version": value.schema_version,
        "run_id": value.run_id,
        "review_id": value.review_id,
        "attempt": value.attempt,
        "fencing_token": value.fencing_token,
        "worker_id": value.worker_id,
        "run_event_id": value.run_event_id,
        "run_event_digest": value.run_event_digest,
        "state_digest": value.state_digest,
        "artifact_evidence_digest": value.artifact_evidence_digest,
        "expires_at": value.expires_at.isoformat(),
    }


def review_lease_from_mapping(value: Mapping[str, object]) -> ReviewLease:
    raw = _mapping(value)
    _exact(
        raw,
        {
            "schema_version",
            "run_id",
            "review_id",
            "attempt",
            "fencing_token",
            "worker_id",
            "run_event_id",
            "run_event_digest",
            "state_digest",
            "artifact_evidence_digest",
            "expires_at",
        },
    )
    if raw.get("schema_version") != REVIEW_LEASE_SCHEMA:
        raise ReviewLifecycleError()
    try:
        expires_at = datetime.fromisoformat(_text(raw.get("expires_at")))
    except ValueError:
        raise ReviewLifecycleError() from None
    return ReviewLease(
        run_id=_identifier(raw.get("run_id")),
        review_id=_identifier(raw.get("review_id")),
        attempt=_positive_integer(raw.get("attempt")),
        fencing_token=_positive_integer(raw.get("fencing_token")),
        worker_id=_identifier(raw.get("worker_id")),
        run_event_id=_identifier(raw.get("run_event_id")),
        run_event_digest=_digest(raw.get("run_event_digest")),
        state_digest=_digest(raw.get("state_digest")),
        artifact_evidence_digest=_digest(raw.get("artifact_evidence_digest")),
        expires_at=expires_at,
    )


def fold_review_lifecycle(
    run_id: str,
    events: Sequence[EventEnvelope],
) -> ReviewLifecycleState:
    """Validate and fold one complete review stream without invoking any live port."""

    _identifier(run_id)
    if not events:
        raise ReviewLifecycleError()
    stream_id = review_stream(run_id)
    ordered = tuple(events)
    for sequence, event in enumerate(ordered, start=1):
        if (
            not isinstance(event, EventEnvelope)
            or event.stream_id != stream_id
            or event.stream_sequence != sequence
            or event.schema_version != 1
            or event.source != RUNTIME_EVENT_SOURCE
            or event.event_type not in REVIEW_EVENT_TYPES
        ):
            raise ReviewLifecycleError()
    correlation_id = ordered[0].correlation_id
    for index, event in enumerate(ordered):
        if event.correlation_id != correlation_id or (
            index > 0 and event.causation_id != ordered[index - 1].event_id
        ):
            raise ReviewLifecycleError()

    status: ReviewLifecycleStatus | None = None
    lease: ReviewLease | None = None
    baseline: ReviewLease | None = None
    acceptance_digest: str | None = None
    context_digest: str | None = None
    provider_request_id: str | None = None
    provider_dispatch_event_id: str | None = None
    proposal_artifact_digest: str | None = None
    provider_result_artifact_digest: str | None = None
    admitted_artifact_digest: str | None = None
    finding_count: int | None = None
    failure_code: str | None = None
    result_artifact_digest: str | None = None

    for event in ordered:
        payload = _payload(event)
        principal = _principal(payload, event)
        if event.event_type == REVIEW_CLAIMED:
            _exact(payload, {"principal_id", "lease_digest", "lease", "status"})
            if status not in {None, ReviewLifecycleStatus.REQUEUED}:
                raise ReviewLifecycleError()
            candidate = review_lease_from_mapping(_mapping(payload.get("lease")))
            if (
                candidate.run_id != run_id
                or candidate.worker_id != principal
                or payload.get("lease_digest") != candidate.digest
                or payload.get("status") != "claimed"
                or candidate.expires_at <= event.recorded_at
                or (lease is None and event.causation_id != candidate.run_event_id)
                or candidate.attempt != (1 if lease is None else lease.attempt + 1)
                or candidate.fencing_token != (1 if lease is None else lease.fencing_token + 1)
            ):
                raise ReviewLifecycleError()
            if baseline is not None and _immutable_lease_identity(
                candidate
            ) != _immutable_lease_identity(baseline):
                raise ReviewLifecycleError()
            baseline = candidate if baseline is None else baseline
            lease = candidate
            status = ReviewLifecycleStatus.CLAIMED
            acceptance_digest = None
            context_digest = None
            provider_request_id = None
            provider_dispatch_event_id = None
            proposal_artifact_digest = None
            provider_result_artifact_digest = None
            admitted_artifact_digest = None
            finding_count = None
            failure_code = None
            result_artifact_digest = None
        elif event.event_type == REVIEW_LEASE_RENEWED:
            _exact(
                payload,
                {
                    "principal_id",
                    "previous_lease_digest",
                    "lease_digest",
                    "lease",
                    "status",
                },
            )
            renewed = review_lease_from_mapping(_mapping(payload.get("lease")))
            if (
                lease is None
                or status is not ReviewLifecycleStatus.CLAIMED
                or principal != lease.worker_id
                or renewed.worker_id != principal
                or payload.get("previous_lease_digest") != lease.digest
                or payload.get("lease_digest") != renewed.digest
                or payload.get("status") != "claimed"
                or renewed.expires_at <= event.recorded_at
                or renewed.expires_at <= lease.expires_at
                or _renewable_lease_identity(renewed) != _renewable_lease_identity(lease)
            ):
                raise ReviewLifecycleError()
            lease = renewed
        elif event.event_type == REVIEW_PROVIDER_DISPATCH_STARTED:
            _exact(
                payload,
                {
                    "principal_id",
                    "run_id",
                    "review_id",
                    "lease_digest",
                    "provider_request_id",
                    "acceptance_digest",
                    "context_digest",
                    "context_artifact_digest",
                    "status",
                },
            )
            active = _active_lease(
                payload,
                principal,
                event,
                lease,
                status,
                ReviewLifecycleStatus.CLAIMED,
            )
            request_id = _identifier(payload.get("provider_request_id"))
            acceptance = _digest(payload.get("acceptance_digest"))
            context = _digest(payload.get("context_digest"))
            if (
                request_id != review_provider_request_id(active.digest)
                or payload.get("context_artifact_digest") != context
                or payload.get("status") != "provider-dispatch-started"
            ):
                raise ReviewLifecycleError()
            acceptance_digest = acceptance
            context_digest = context
            provider_request_id = request_id
            provider_dispatch_event_id = event.event_id
            status = ReviewLifecycleStatus.PROVIDER_DISPATCHED
        elif event.event_type == REVIEW_SUCCEEDED:
            _exact(
                payload,
                {
                    "principal_id",
                    "run_id",
                    "review_id",
                    "lease_digest",
                    "context_digest",
                    "proposal_artifact_digest",
                    "provider_result_artifact_digest",
                    "admitted_artifact_digest",
                    "finding_count",
                    "status",
                },
            )
            _active_lease(
                payload,
                principal,
                event,
                lease,
                status,
                ReviewLifecycleStatus.PROVIDER_DISPATCHED,
            )
            count = _nonnegative_integer(payload.get("finding_count"), maximum=_MAX_FINDINGS)
            if (
                payload.get("context_digest") != context_digest
                or payload.get("status") != "succeeded"
            ):
                raise ReviewLifecycleError()
            proposal_artifact_digest = _digest(payload.get("proposal_artifact_digest"))
            provider_result_artifact_digest = _digest(
                payload.get("provider_result_artifact_digest")
            )
            admitted_artifact_digest = _digest(payload.get("admitted_artifact_digest"))
            finding_count = count
            status = ReviewLifecycleStatus.SUCCEEDED
        elif event.event_type == REVIEW_FAILED:
            _exact(
                payload,
                {
                    "principal_id",
                    "run_id",
                    "review_id",
                    "lease_digest",
                    "failure_code",
                    "result_artifact_digest",
                    "status",
                },
            )
            if status not in {
                ReviewLifecycleStatus.CLAIMED,
                ReviewLifecycleStatus.PROVIDER_DISPATCHED,
            }:
                raise ReviewLifecycleError()
            _active_lease(
                payload,
                principal,
                event,
                lease,
                status,
                status,
                allow_expired=True,
            )
            failure_code = _failure(payload.get("failure_code"))
            result = payload.get("result_artifact_digest")
            result_artifact_digest = None if result is None else _digest(result)
            if payload.get("status") != "reviewer-error":
                raise ReviewLifecycleError()
            status = ReviewLifecycleStatus.FAILED
        elif event.event_type == REVIEW_REQUEUED:
            _exact(
                payload,
                {
                    "principal_id",
                    "run_id",
                    "review_id",
                    "lease_digest",
                    "disposition",
                    "status",
                },
            )
            _host_transition(payload, lease, status, ReviewLifecycleStatus.CLAIMED)
            if payload.get("disposition") != "pre-dispatch" or payload.get("status") != "requeued":
                raise ReviewLifecycleError()
            status = ReviewLifecycleStatus.REQUEUED
        elif event.event_type == REVIEW_RECONCILIATION_REQUIRED:
            _exact(
                payload,
                {
                    "principal_id",
                    "run_id",
                    "review_id",
                    "lease_digest",
                    "failure_code",
                    "status",
                },
            )
            _host_transition(
                payload,
                lease,
                status,
                ReviewLifecycleStatus.PROVIDER_DISPATCHED,
            )
            failure_code = _failure(payload.get("failure_code"))
            if (
                failure_code != REVIEW_DISPATCH_AMBIGUOUS
                or payload.get("status") != "reconciliation-required"
            ):
                raise ReviewLifecycleError()
            status = ReviewLifecycleStatus.RECONCILIATION_REQUIRED
        else:  # pragma: no cover - event type set and branches remain synchronized
            raise ReviewLifecycleError()

    if status is None or lease is None:
        raise ReviewLifecycleError()
    return ReviewLifecycleState(
        run_id=run_id,
        review_id=lease.review_id,
        status=status,
        lease=lease,
        acceptance_digest=acceptance_digest,
        context_digest=context_digest,
        provider_request_id=provider_request_id,
        provider_dispatch_event_id=provider_dispatch_event_id,
        proposal_artifact_digest=proposal_artifact_digest,
        provider_result_artifact_digest=provider_result_artifact_digest,
        admitted_artifact_digest=admitted_artifact_digest,
        finding_count=finding_count,
        failure_code=failure_code,
        result_artifact_digest=result_artifact_digest,
        latest_event=ordered[-1],
    )


def review_lifecycle_payload(value: ReviewLifecycleState) -> dict[str, JsonInput]:
    if not isinstance(value, ReviewLifecycleState):
        raise ReviewLifecycleError()
    return {
        "run_id": value.run_id,
        "review_id": value.review_id,
        "status": value.status.value,
        "lease_digest": value.lease.digest,
        "attempt": value.lease.attempt,
        "fencing_token": value.lease.fencing_token,
        "worker_id": value.lease.worker_id,
        "run_event_id": value.lease.run_event_id,
        "run_event_digest": value.lease.run_event_digest,
        "state_digest": value.lease.state_digest,
        "artifact_evidence_digest": value.lease.artifact_evidence_digest,
        "expires_at": value.lease.expires_at.isoformat(),
        "acceptance_digest": value.acceptance_digest,
        "context_digest": value.context_digest,
        "provider_request_id": value.provider_request_id,
        "provider_dispatch_event_id": value.provider_dispatch_event_id,
        "proposal_artifact_digest": value.proposal_artifact_digest,
        "provider_result_artifact_digest": value.provider_result_artifact_digest,
        "admitted_artifact_digest": value.admitted_artifact_digest,
        "finding_count": value.finding_count,
        "failure_code": value.failure_code,
        "result_artifact_digest": value.result_artifact_digest,
        "stream_sequence": value.stream_sequence,
        "latest_event_id": value.latest_event.event_id,
        "latest_event_digest": value.latest_event.payload_hash,
    }


def _active_lease(
    payload: Mapping[str, JsonValue],
    principal: str,
    event: EventEnvelope,
    lease: ReviewLease | None,
    actual_status: ReviewLifecycleStatus | None,
    expected_status: ReviewLifecycleStatus,
    *,
    allow_expired: bool = False,
) -> ReviewLease:
    if (
        lease is None
        or actual_status is not expected_status
        or payload.get("run_id") != lease.run_id
        or payload.get("review_id") != lease.review_id
        or payload.get("lease_digest") != lease.digest
        or principal != lease.worker_id
        or (not allow_expired and event.recorded_at > lease.expires_at)
    ):
        raise ReviewLifecycleError()
    return lease


def _host_transition(
    payload: Mapping[str, JsonValue],
    lease: ReviewLease | None,
    actual_status: ReviewLifecycleStatus | None,
    expected_status: ReviewLifecycleStatus,
) -> ReviewLease:
    if (
        lease is None
        or actual_status is not expected_status
        or payload.get("run_id") != lease.run_id
        or payload.get("review_id") != lease.review_id
        or payload.get("lease_digest") != lease.digest
    ):
        raise ReviewLifecycleError()
    return lease


def _immutable_lease_identity(value: ReviewLease) -> tuple[str, ...]:
    return (
        value.run_id,
        value.review_id,
        value.run_event_id,
        value.run_event_digest,
        value.state_digest,
        value.artifact_evidence_digest,
    )


def _renewable_lease_identity(value: ReviewLease) -> tuple[object, ...]:
    return (
        value.schema_version,
        value.run_id,
        value.review_id,
        value.attempt,
        value.fencing_token,
        value.worker_id,
        value.run_event_id,
        value.run_event_digest,
        value.state_digest,
        value.artifact_evidence_digest,
    )


def _payload(event: EventEnvelope) -> Mapping[str, JsonValue]:
    if not isinstance(event.payload, Mapping):
        raise ReviewLifecycleError()
    return event.payload


def _principal(payload: Mapping[str, JsonValue], event: EventEnvelope) -> str:
    principal = _identifier(payload.get("principal_id"))
    if principal != event.actor:
        raise ReviewLifecycleError()
    return principal


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ReviewLifecycleError()
    return cast("Mapping[str, object]", value)


def _exact(value: Mapping[str, object], keys: set[str]) -> None:
    if set(value) != keys:
        raise ReviewLifecycleError()


def _text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ReviewLifecycleError()
    return value


def _identifier(value: object) -> str:
    text = _text(value)
    if _IDENTIFIER.fullmatch(text) is None:
        raise ReviewLifecycleError()
    return text


def _digest(value: object) -> str:
    text = _text(value)
    if _DIGEST.fullmatch(text) is None:
        raise ReviewLifecycleError()
    return text


def _failure(value: object) -> str:
    text = _text(value)
    if _FAILURE_CODE.fullmatch(text) is None:
        raise ReviewLifecycleError()
    return text


def _positive_integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ReviewLifecycleError()
    return value


def _nonnegative_integer(value: object, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise ReviewLifecycleError()
    return value


__all__ = [
    "REVIEW_CLAIMED",
    "REVIEW_DISPATCH_AMBIGUOUS",
    "REVIEW_EVENT_TYPES",
    "REVIEW_FAILED",
    "REVIEW_LEASE_RENEWED",
    "REVIEW_LEASE_SCHEMA",
    "REVIEW_PROVIDER_DISPATCH_STARTED",
    "REVIEW_RECONCILIATION_REQUIRED",
    "REVIEW_REQUEUED",
    "REVIEW_SUCCEEDED",
    "ReviewCandidate",
    "ReviewLease",
    "ReviewLifecycleError",
    "ReviewLifecycleState",
    "ReviewLifecycleStatus",
    "fold_review_lifecycle",
    "review_id",
    "review_lease_from_mapping",
    "review_lease_payload",
    "review_lifecycle_payload",
    "review_provider_request_id",
    "review_stream",
]
