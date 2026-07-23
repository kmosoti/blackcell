"""Strict fenced lifecycle for executor- and reviewer-independent verification."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import cast

from blackcell.kernel import EventEnvelope, JsonInput, JsonValue
from blackcell.kernel._json import json_digest
from blackcell.orchestration.alpha_lifecycle import ALPHA_EVENT_SOURCE
from blackcell.orchestration.alpha_review_lifecycle import alpha_review_id
from blackcell.orchestration.alpha_verify import AlphaVerificationStatus

ALPHA_VERIFICATION_CLAIMED = "alpha.verification.claimed"
ALPHA_VERIFICATION_COMPLETED = "alpha.verification.completed"
ALPHA_VERIFICATION_FAILED = "alpha.verification.failed"
ALPHA_VERIFICATION_REQUEUED = "alpha.verification.requeued"
ALPHA_VERIFICATION_LEASE_SCHEMA = "alpha-verification-lease/v1"

ALPHA_VERIFICATION_EVENT_TYPES = frozenset(
    {
        ALPHA_VERIFICATION_CLAIMED,
        ALPHA_VERIFICATION_COMPLETED,
        ALPHA_VERIFICATION_FAILED,
        ALPHA_VERIFICATION_REQUEUED,
    }
)

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_FAILURE_CODE = re.compile(r"[a-z0-9][a-z0-9-]{0,127}\Z")
_MAX_FINDINGS = 64


class AlphaVerificationLifecycleError(ValueError):
    """Content-free rejection of an invalid durable verification history."""

    def __init__(self) -> None:
        super().__init__("invalid-alpha-verification-lifecycle")


class AlphaVerificationLifecycleStatus(StrEnum):
    CLAIMED = "claimed"
    COMPLETED = "completed"
    FAILED = "failed"
    REQUEUED = "requeued"


@dataclass(frozen=True, slots=True)
class AlphaVerificationCandidate:
    run_id: str
    verification_id: str
    correlation_id: str
    run_event_id: str
    run_event_digest: str
    state_digest: str
    artifact_evidence_digest: str
    review_id: str
    review_event_id: str
    review_event_digest: str
    acceptance_digest: str
    context_digest: str
    proposal_artifact_digest: str
    provider_result_artifact_digest: str
    admitted_review_digest: str
    finding_count: int

    def __post_init__(self) -> None:
        identifiers = (
            self.run_id,
            self.verification_id,
            self.correlation_id,
            self.run_event_id,
            self.review_id,
            self.review_event_id,
        )
        digests = (
            self.run_event_digest,
            self.state_digest,
            self.artifact_evidence_digest,
            self.review_event_digest,
            self.acceptance_digest,
            self.context_digest,
            self.proposal_artifact_digest,
            self.provider_result_artifact_digest,
            self.admitted_review_digest,
        )
        if (
            any(
                not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None
                for value in identifiers
            )
            or any(
                not isinstance(value, str) or _DIGEST.fullmatch(value) is None for value in digests
            )
            or isinstance(self.finding_count, bool)
            or not isinstance(self.finding_count, int)
            or not 0 <= self.finding_count <= _MAX_FINDINGS
            or self.review_id != alpha_review_id(self.run_id, self.run_event_digest)
            or self.verification_id != alpha_verification_id(self.run_id, self.review_event_digest)
        ):
            raise AlphaVerificationLifecycleError()


@dataclass(frozen=True, slots=True)
class AlphaVerificationLease:
    run_id: str
    verification_id: str
    attempt: int
    fencing_token: int
    worker_id: str
    run_event_id: str
    run_event_digest: str
    state_digest: str
    artifact_evidence_digest: str
    review_id: str
    review_event_id: str
    review_event_digest: str
    acceptance_digest: str
    context_digest: str
    proposal_artifact_digest: str
    provider_result_artifact_digest: str
    admitted_review_digest: str
    finding_count: int
    expires_at: datetime
    schema_version: str = ALPHA_VERIFICATION_LEASE_SCHEMA

    def __post_init__(self) -> None:
        try:
            candidate = AlphaVerificationCandidate(
                run_id=self.run_id,
                verification_id=self.verification_id,
                correlation_id="lease-validation",
                run_event_id=self.run_event_id,
                run_event_digest=self.run_event_digest,
                state_digest=self.state_digest,
                artifact_evidence_digest=self.artifact_evidence_digest,
                review_id=self.review_id,
                review_event_id=self.review_event_id,
                review_event_digest=self.review_event_digest,
                acceptance_digest=self.acceptance_digest,
                context_digest=self.context_digest,
                proposal_artifact_digest=self.proposal_artifact_digest,
                provider_result_artifact_digest=self.provider_result_artifact_digest,
                admitted_review_digest=self.admitted_review_digest,
                finding_count=self.finding_count,
            )
        except AlphaVerificationLifecycleError:
            raise
        if (
            self.schema_version != ALPHA_VERIFICATION_LEASE_SCHEMA
            or not isinstance(self.worker_id, str)
            or _IDENTIFIER.fullmatch(self.worker_id) is None
            or isinstance(self.attempt, bool)
            or not isinstance(self.attempt, int)
            or self.attempt < 1
            or isinstance(self.fencing_token, bool)
            or not isinstance(self.fencing_token, int)
            or self.fencing_token < 1
            or not isinstance(self.expires_at, datetime)
            or self.expires_at.tzinfo is None
            or self.expires_at.utcoffset() is None
            or candidate.run_id != self.run_id
        ):
            raise AlphaVerificationLifecycleError()

    @property
    def digest(self) -> str:
        return json_digest(alpha_verification_lease_payload(self))


@dataclass(frozen=True, slots=True)
class AlphaVerificationLifecycleState:
    run_id: str
    verification_id: str
    status: AlphaVerificationLifecycleStatus
    lease: AlphaVerificationLease
    verdict: AlphaVerificationStatus | None
    report_artifact_digest: str | None
    matrix_digest: str | None
    failure_code: str | None
    result_artifact_digest: str | None
    latest_event: EventEnvelope

    @property
    def stream_sequence(self) -> int:
        return self.latest_event.stream_sequence

    @property
    def active(self) -> bool:
        return self.status is AlphaVerificationLifecycleStatus.CLAIMED


def alpha_verification_id(run_id: str, review_event_digest: str) -> str:
    _identifier(run_id)
    _digest(review_event_digest)
    suffix = json_digest(
        {
            "run_id": run_id,
            "review_event_digest": review_event_digest,
            "stage": "verification",
        }
    ).removeprefix("sha256:")
    return f"verification-{suffix[:48]}"


def alpha_verification_stream(run_id: str) -> str:
    return f"alpha:verification:{_identifier(run_id)}"


def alpha_verification_lease_payload(value: AlphaVerificationLease) -> dict[str, JsonInput]:
    if not isinstance(value, AlphaVerificationLease):
        raise AlphaVerificationLifecycleError()
    return {
        "schema_version": value.schema_version,
        "run_id": value.run_id,
        "verification_id": value.verification_id,
        "attempt": value.attempt,
        "fencing_token": value.fencing_token,
        "worker_id": value.worker_id,
        "run_event_id": value.run_event_id,
        "run_event_digest": value.run_event_digest,
        "state_digest": value.state_digest,
        "artifact_evidence_digest": value.artifact_evidence_digest,
        "review_id": value.review_id,
        "review_event_id": value.review_event_id,
        "review_event_digest": value.review_event_digest,
        "acceptance_digest": value.acceptance_digest,
        "context_digest": value.context_digest,
        "proposal_artifact_digest": value.proposal_artifact_digest,
        "provider_result_artifact_digest": value.provider_result_artifact_digest,
        "admitted_review_digest": value.admitted_review_digest,
        "finding_count": value.finding_count,
        "expires_at": value.expires_at.isoformat(),
    }


def alpha_verification_lease_from_mapping(
    value: Mapping[str, object],
) -> AlphaVerificationLease:
    raw = _mapping(value)
    _exact(
        raw,
        {
            "schema_version",
            "run_id",
            "verification_id",
            "attempt",
            "fencing_token",
            "worker_id",
            "run_event_id",
            "run_event_digest",
            "state_digest",
            "artifact_evidence_digest",
            "review_id",
            "review_event_id",
            "review_event_digest",
            "acceptance_digest",
            "context_digest",
            "proposal_artifact_digest",
            "provider_result_artifact_digest",
            "admitted_review_digest",
            "finding_count",
            "expires_at",
        },
    )
    if raw.get("schema_version") != ALPHA_VERIFICATION_LEASE_SCHEMA:
        raise AlphaVerificationLifecycleError()
    try:
        expires_at = datetime.fromisoformat(_text(raw.get("expires_at")))
    except ValueError:
        raise AlphaVerificationLifecycleError() from None
    return AlphaVerificationLease(
        run_id=_identifier(raw.get("run_id")),
        verification_id=_identifier(raw.get("verification_id")),
        attempt=_positive_integer(raw.get("attempt")),
        fencing_token=_positive_integer(raw.get("fencing_token")),
        worker_id=_identifier(raw.get("worker_id")),
        run_event_id=_identifier(raw.get("run_event_id")),
        run_event_digest=_digest(raw.get("run_event_digest")),
        state_digest=_digest(raw.get("state_digest")),
        artifact_evidence_digest=_digest(raw.get("artifact_evidence_digest")),
        review_id=_identifier(raw.get("review_id")),
        review_event_id=_identifier(raw.get("review_event_id")),
        review_event_digest=_digest(raw.get("review_event_digest")),
        acceptance_digest=_digest(raw.get("acceptance_digest")),
        context_digest=_digest(raw.get("context_digest")),
        proposal_artifact_digest=_digest(raw.get("proposal_artifact_digest")),
        provider_result_artifact_digest=_digest(raw.get("provider_result_artifact_digest")),
        admitted_review_digest=_digest(raw.get("admitted_review_digest")),
        finding_count=_nonnegative_integer(raw.get("finding_count"), maximum=_MAX_FINDINGS),
        expires_at=expires_at,
    )


def fold_alpha_verification_lifecycle(
    run_id: str,
    events: Sequence[EventEnvelope],
) -> AlphaVerificationLifecycleState:
    """Validate and fold one complete verification stream without a live port."""

    _identifier(run_id)
    if not events:
        raise AlphaVerificationLifecycleError()
    ordered = tuple(events)
    stream_id = alpha_verification_stream(run_id)
    for sequence, event in enumerate(ordered, start=1):
        if (
            not isinstance(event, EventEnvelope)
            or event.stream_id != stream_id
            or event.stream_sequence != sequence
            or event.schema_version != 1
            or event.source != ALPHA_EVENT_SOURCE
            or event.event_type not in ALPHA_VERIFICATION_EVENT_TYPES
        ):
            raise AlphaVerificationLifecycleError()
    correlation_id = ordered[0].correlation_id
    for index, event in enumerate(ordered):
        if event.correlation_id != correlation_id or (
            index > 0 and event.causation_id != ordered[index - 1].event_id
        ):
            raise AlphaVerificationLifecycleError()

    status: AlphaVerificationLifecycleStatus | None = None
    lease: AlphaVerificationLease | None = None
    baseline: AlphaVerificationLease | None = None
    verdict: AlphaVerificationStatus | None = None
    report_artifact_digest: str | None = None
    matrix_digest: str | None = None
    failure_code: str | None = None
    result_artifact_digest: str | None = None

    for event in ordered:
        payload = _payload(event)
        principal = _principal(payload, event)
        if event.event_type == ALPHA_VERIFICATION_CLAIMED:
            _exact(payload, {"principal_id", "lease_digest", "lease", "status"})
            if status not in {None, AlphaVerificationLifecycleStatus.REQUEUED}:
                raise AlphaVerificationLifecycleError()
            candidate = alpha_verification_lease_from_mapping(_mapping(payload.get("lease")))
            if (
                candidate.run_id != run_id
                or candidate.worker_id != principal
                or payload.get("lease_digest") != candidate.digest
                or payload.get("status") != "claimed"
                or candidate.expires_at <= event.recorded_at
                or (lease is None and event.causation_id != candidate.review_event_id)
                or candidate.attempt != (1 if lease is None else lease.attempt + 1)
                or candidate.fencing_token != (1 if lease is None else lease.fencing_token + 1)
                or (
                    baseline is not None
                    and _immutable_lease_identity(candidate) != _immutable_lease_identity(baseline)
                )
            ):
                raise AlphaVerificationLifecycleError()
            baseline = candidate if baseline is None else baseline
            lease = candidate
            status = AlphaVerificationLifecycleStatus.CLAIMED
            verdict = None
            report_artifact_digest = None
            matrix_digest = None
            failure_code = None
            result_artifact_digest = None
        elif event.event_type == ALPHA_VERIFICATION_COMPLETED:
            _exact(
                payload,
                {
                    "principal_id",
                    "run_id",
                    "verification_id",
                    "lease_digest",
                    "verdict",
                    "report_artifact_digest",
                    "matrix_digest",
                    "status",
                },
            )
            active = _active_lease(payload, principal, lease, status)
            try:
                completed_verdict = AlphaVerificationStatus(payload.get("verdict"))
            except TypeError, ValueError:
                raise AlphaVerificationLifecycleError() from None
            report = _digest(payload.get("report_artifact_digest"))
            matrix = _digest(payload.get("matrix_digest"))
            if payload.get("status") != "completed":
                raise AlphaVerificationLifecycleError()
            lease = active
            verdict = completed_verdict
            report_artifact_digest = report
            matrix_digest = matrix
            status = AlphaVerificationLifecycleStatus.COMPLETED
        elif event.event_type == ALPHA_VERIFICATION_FAILED:
            _exact(
                payload,
                {
                    "principal_id",
                    "run_id",
                    "verification_id",
                    "lease_digest",
                    "failure_code",
                    "result_artifact_digest",
                    "status",
                },
            )
            _active_lease(payload, principal, lease, status)
            failure_code = _failure(payload.get("failure_code"))
            result = payload.get("result_artifact_digest")
            result_artifact_digest = None if result is None else _digest(result)
            if payload.get("status") != "verifier-error":
                raise AlphaVerificationLifecycleError()
            status = AlphaVerificationLifecycleStatus.FAILED
        elif event.event_type == ALPHA_VERIFICATION_REQUEUED:
            _exact(
                payload,
                {
                    "principal_id",
                    "run_id",
                    "verification_id",
                    "lease_digest",
                    "disposition",
                    "status",
                },
            )
            active = _host_transition(payload, lease, status)
            if (
                principal == active.worker_id
                or payload.get("disposition") != "deterministic-retry"
                or payload.get("status") != "requeued"
            ):
                raise AlphaVerificationLifecycleError()
            status = AlphaVerificationLifecycleStatus.REQUEUED
        else:  # pragma: no cover - event type set and branches remain synchronized
            raise AlphaVerificationLifecycleError()

    if status is None or lease is None:
        raise AlphaVerificationLifecycleError()
    return AlphaVerificationLifecycleState(
        run_id=run_id,
        verification_id=lease.verification_id,
        status=status,
        lease=lease,
        verdict=verdict,
        report_artifact_digest=report_artifact_digest,
        matrix_digest=matrix_digest,
        failure_code=failure_code,
        result_artifact_digest=result_artifact_digest,
        latest_event=ordered[-1],
    )


def alpha_verification_lifecycle_payload(
    value: AlphaVerificationLifecycleState,
) -> dict[str, JsonInput]:
    if not isinstance(value, AlphaVerificationLifecycleState):
        raise AlphaVerificationLifecycleError()
    return {
        "run_id": value.run_id,
        "verification_id": value.verification_id,
        "status": value.status.value,
        "lease_digest": value.lease.digest,
        "attempt": value.lease.attempt,
        "fencing_token": value.lease.fencing_token,
        "worker_id": value.lease.worker_id,
        "run_event_id": value.lease.run_event_id,
        "run_event_digest": value.lease.run_event_digest,
        "state_digest": value.lease.state_digest,
        "artifact_evidence_digest": value.lease.artifact_evidence_digest,
        "review_id": value.lease.review_id,
        "review_event_id": value.lease.review_event_id,
        "review_event_digest": value.lease.review_event_digest,
        "acceptance_digest": value.lease.acceptance_digest,
        "context_digest": value.lease.context_digest,
        "proposal_artifact_digest": value.lease.proposal_artifact_digest,
        "provider_result_artifact_digest": value.lease.provider_result_artifact_digest,
        "admitted_review_digest": value.lease.admitted_review_digest,
        "finding_count": value.lease.finding_count,
        "expires_at": value.lease.expires_at.isoformat(),
        "verdict": None if value.verdict is None else value.verdict.value,
        "report_artifact_digest": value.report_artifact_digest,
        "matrix_digest": value.matrix_digest,
        "failure_code": value.failure_code,
        "result_artifact_digest": value.result_artifact_digest,
        "stream_sequence": value.stream_sequence,
        "latest_event_id": value.latest_event.event_id,
        "latest_event_digest": value.latest_event.payload_hash,
    }


def _active_lease(
    payload: Mapping[str, JsonValue],
    principal: str,
    lease: AlphaVerificationLease | None,
    status: AlphaVerificationLifecycleStatus | None,
) -> AlphaVerificationLease:
    # Wall-clock expiry alone does not supersede deterministic work. The exact active
    # lease and optimistic append fence decide whether a late terminal event may win.
    if (
        lease is None
        or status is not AlphaVerificationLifecycleStatus.CLAIMED
        or payload.get("run_id") != lease.run_id
        or payload.get("verification_id") != lease.verification_id
        or payload.get("lease_digest") != lease.digest
        or principal != lease.worker_id
    ):
        raise AlphaVerificationLifecycleError()
    return lease


def _host_transition(
    payload: Mapping[str, JsonValue],
    lease: AlphaVerificationLease | None,
    status: AlphaVerificationLifecycleStatus | None,
) -> AlphaVerificationLease:
    if (
        lease is None
        or status is not AlphaVerificationLifecycleStatus.CLAIMED
        or payload.get("run_id") != lease.run_id
        or payload.get("verification_id") != lease.verification_id
        or payload.get("lease_digest") != lease.digest
    ):
        raise AlphaVerificationLifecycleError()
    return lease


def _immutable_lease_identity(value: AlphaVerificationLease) -> tuple[object, ...]:
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


def _payload(event: EventEnvelope) -> Mapping[str, JsonValue]:
    if not isinstance(event.payload, Mapping):
        raise AlphaVerificationLifecycleError()
    return event.payload


def _principal(payload: Mapping[str, JsonValue], event: EventEnvelope) -> str:
    principal = _identifier(payload.get("principal_id"))
    if principal != event.actor:
        raise AlphaVerificationLifecycleError()
    return principal


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise AlphaVerificationLifecycleError()
    return cast("Mapping[str, object]", value)


def _exact(value: Mapping[str, object], keys: set[str]) -> None:
    if set(value) != keys:
        raise AlphaVerificationLifecycleError()


def _text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise AlphaVerificationLifecycleError()
    return value


def _identifier(value: object) -> str:
    text = _text(value)
    if _IDENTIFIER.fullmatch(text) is None:
        raise AlphaVerificationLifecycleError()
    return text


def _digest(value: object) -> str:
    text = _text(value)
    if _DIGEST.fullmatch(text) is None:
        raise AlphaVerificationLifecycleError()
    return text


def _failure(value: object) -> str:
    text = _text(value)
    if _FAILURE_CODE.fullmatch(text) is None:
        raise AlphaVerificationLifecycleError()
    return text


def _positive_integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise AlphaVerificationLifecycleError()
    return value


def _nonnegative_integer(value: object, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise AlphaVerificationLifecycleError()
    return value


__all__ = [
    "ALPHA_VERIFICATION_CLAIMED",
    "ALPHA_VERIFICATION_COMPLETED",
    "ALPHA_VERIFICATION_EVENT_TYPES",
    "ALPHA_VERIFICATION_FAILED",
    "ALPHA_VERIFICATION_LEASE_SCHEMA",
    "ALPHA_VERIFICATION_REQUEUED",
    "AlphaVerificationCandidate",
    "AlphaVerificationLease",
    "AlphaVerificationLifecycleError",
    "AlphaVerificationLifecycleState",
    "AlphaVerificationLifecycleStatus",
    "alpha_verification_id",
    "alpha_verification_lease_from_mapping",
    "alpha_verification_lease_payload",
    "alpha_verification_lifecycle_payload",
    "alpha_verification_stream",
    "fold_alpha_verification_lifecycle",
]
