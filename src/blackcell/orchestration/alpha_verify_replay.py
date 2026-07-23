"""Live-free replay of deterministic alpha verification evidence."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, cast

from blackcell.kernel import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactRef,
    EventEnvelope,
    EventIntegrityError,
    KernelError,
)
from blackcell.kernel._json import canonical_json_bytes, json_digest
from blackcell.orchestration.alpha_artifacts import ALPHA_VERIFICATION_REPORT_MEDIA_TYPE
from blackcell.orchestration.alpha_lifecycle import ALPHA_EVENT_SOURCE, ALPHA_RUN_SUCCEEDED
from blackcell.orchestration.alpha_replay import (
    AlphaArtifactReaderPort,
    AlphaArtifactReplayStatus,
)
from blackcell.orchestration.alpha_review_lifecycle import (
    ALPHA_REVIEW_SUCCEEDED,
    AlphaReviewLease,
    AlphaReviewLifecycleError,
    AlphaReviewLifecycleStatus,
    alpha_review_stream,
    fold_alpha_review_lifecycle,
)
from blackcell.orchestration.alpha_verify import (
    AlphaVerificationError,
    AlphaVerificationReport,
    AlphaVerificationStatus,
    alpha_verification_report_from_mapping,
)
from blackcell.orchestration.alpha_verify_lifecycle import (
    AlphaVerificationLease,
    AlphaVerificationLifecycleError,
    AlphaVerificationLifecycleState,
    AlphaVerificationLifecycleStatus,
    alpha_verification_stream,
    fold_alpha_verification_lifecycle,
)

_MAX_REPORT_BYTES = 4 * 1024 * 1024


class AlphaVerificationReplayLifecycle(StrEnum):
    NOT_STARTED = "not-started"
    CLAIMED = "claimed"
    REQUEUED = "requeued"
    COMPLETED = "completed"
    VERIFIER_ERROR = "verifier-error"


class AlphaVerificationReplayFindingCode(StrEnum):
    EVENT_STORE_UNAVAILABLE = "alpha-verification-replay-event-store-unavailable"
    LIFECYCLE_INVALID = "alpha-verification-replay-lifecycle-invalid"
    SOURCE_BINDING_MISMATCH = "alpha-verification-replay-source-binding-mismatch"
    ARTIFACT_STORE_UNAVAILABLE = "alpha-verification-replay-artifact-store-unavailable"
    REPORT_MISSING = "alpha-verification-replay-report-missing"
    REPORT_INTEGRITY_FAILED = "alpha-verification-replay-report-integrity-failed"
    REPORT_METADATA_MISMATCH = "alpha-verification-replay-report-metadata-mismatch"
    REPORT_READ_UNAVAILABLE = "alpha-verification-replay-report-read-unavailable"
    REPORT_JSON_INVALID = "alpha-verification-replay-report-json-invalid"
    REPORT_NONCANONICAL = "alpha-verification-replay-report-noncanonical"
    REPORT_INVALID = "alpha-verification-replay-report-invalid"
    REPORT_BINDING_MISMATCH = "alpha-verification-replay-report-binding-mismatch"


class AlphaVerificationEventReaderPort(Protocol):
    def read_stream(self, stream_id: str) -> tuple[EventEnvelope, ...]: ...

    def get(self, event_id: str) -> EventEnvelope | None: ...


@dataclass(frozen=True, slots=True)
class AlphaVerificationReplayReport:
    run_id: str
    lifecycle_status: AlphaVerificationReplayLifecycle
    verification_id: str | None
    review_id: str | None
    attempt: int | None
    fencing_token: int | None
    verdict: AlphaVerificationStatus | None
    failure_code: str | None
    report_artifact_digest: str | None
    report_size_bytes: int | None
    report_media_type: str | None
    report_encoding: str | None
    matrix_digest: str | None
    artifact_integrity: AlphaArtifactReplayStatus
    finding_code: AlphaVerificationReplayFindingCode | None
    processed_events: int
    evidence_digest: str


def replay_alpha_verification(
    events: AlphaVerificationEventReaderPort,
    artifacts: AlphaArtifactReaderPort | None,
    *,
    run_id: str,
) -> AlphaVerificationReplayReport:
    """Project verification state and report integrity without invoking live capabilities."""

    try:
        stream = events.read_stream(alpha_verification_stream(run_id))
    except EventIntegrityError:
        return _empty_failure(
            run_id,
            AlphaArtifactReplayStatus.FAILED,
            AlphaVerificationReplayFindingCode.LIFECYCLE_INVALID,
        )
    except OSError, RuntimeError:
        return _empty_failure(
            run_id,
            AlphaArtifactReplayStatus.INCONCLUSIVE,
            AlphaVerificationReplayFindingCode.EVENT_STORE_UNAVAILABLE,
        )
    if not stream:
        return _result(
            run_id=run_id,
            lifecycle_status=AlphaVerificationReplayLifecycle.NOT_STARTED,
            state=None,
            artifact_integrity=AlphaArtifactReplayStatus.NOT_APPLICABLE,
            finding_code=None,
            report_digest=None,
            reference=None,
            matrix_digest=None,
            processed_events=0,
        )
    try:
        state = fold_alpha_verification_lifecycle(run_id, stream)
    except AlphaVerificationLifecycleError:
        return _empty_failure(
            run_id,
            AlphaArtifactReplayStatus.FAILED,
            AlphaVerificationReplayFindingCode.LIFECYCLE_INVALID,
            processed_events=len(stream),
        )
    if not _source_events_match(events, state):
        return _result(
            run_id=run_id,
            lifecycle_status=_lifecycle_status(state.status),
            state=state,
            artifact_integrity=AlphaArtifactReplayStatus.FAILED,
            finding_code=AlphaVerificationReplayFindingCode.SOURCE_BINDING_MISMATCH,
            report_digest=(state.report_artifact_digest or state.result_artifact_digest),
            reference=None,
            matrix_digest=state.matrix_digest,
            processed_events=len(stream),
        )
    report_digest = (
        state.report_artifact_digest
        if state.status is AlphaVerificationLifecycleStatus.COMPLETED
        else state.result_artifact_digest
    )
    if report_digest is None:
        return _result(
            run_id=run_id,
            lifecycle_status=_lifecycle_status(state.status),
            state=state,
            artifact_integrity=AlphaArtifactReplayStatus.NOT_APPLICABLE,
            finding_code=None,
            report_digest=None,
            reference=None,
            matrix_digest=state.matrix_digest,
            processed_events=len(stream),
        )
    if artifacts is None:
        return _result(
            run_id=run_id,
            lifecycle_status=_lifecycle_status(state.status),
            state=state,
            artifact_integrity=AlphaArtifactReplayStatus.INCONCLUSIVE,
            finding_code=AlphaVerificationReplayFindingCode.ARTIFACT_STORE_UNAVAILABLE,
            report_digest=report_digest,
            reference=None,
            matrix_digest=state.matrix_digest,
            processed_events=len(stream),
        )
    return _replay_report_artifact(
        artifacts,
        state=state,
        report_digest=report_digest,
        processed_events=len(stream),
    )


def _source_events_match(
    events: AlphaVerificationEventReaderPort,
    state: AlphaVerificationLifecycleState,
) -> bool:
    lease = state.lease
    try:
        execution = events.get(lease.run_event_id)
        review = events.get(lease.review_event_id)
        review_events = events.read_stream(alpha_review_stream(lease.run_id))
        review_state = fold_alpha_review_lifecycle(lease.run_id, review_events)
    except AlphaReviewLifecycleError, EventIntegrityError, OSError, RuntimeError:
        return False
    correlation_id = state.latest_event.correlation_id
    return bool(
        execution is not None
        and execution.stream_id == f"alpha:run:{lease.run_id}"
        and execution.event_type == ALPHA_RUN_SUCCEEDED
        and execution.source == ALPHA_EVENT_SOURCE
        and execution.correlation_id == correlation_id
        and execution.payload_hash == lease.run_event_digest
        and execution.payload.get("run_id") == lease.run_id
        and execution.payload.get("status") == "succeeded"
        and review is not None
        and review.stream_id == alpha_review_stream(lease.run_id)
        and review.event_type == ALPHA_REVIEW_SUCCEEDED
        and review.source == ALPHA_EVENT_SOURCE
        and review.correlation_id == correlation_id
        and review.payload_hash == lease.review_event_digest
        and review_state.status is AlphaReviewLifecycleStatus.SUCCEEDED
        and review_state.latest_event.event_id == review.event_id
        and _review_lease_matches_verification(review_state.lease, lease)
        and review_state.review_id == lease.review_id
        and review_state.acceptance_digest == lease.acceptance_digest
        and review_state.context_digest == lease.context_digest
        and review_state.proposal_artifact_digest == lease.proposal_artifact_digest
        and review_state.provider_result_artifact_digest == lease.provider_result_artifact_digest
        and review_state.admitted_artifact_digest == lease.admitted_review_digest
        and review_state.finding_count == lease.finding_count
    )


def _review_lease_matches_verification(
    review: AlphaReviewLease,
    verification: AlphaVerificationLease,
) -> bool:
    return bool(
        review.run_event_id == verification.run_event_id
        and review.run_event_digest == verification.run_event_digest
        and review.state_digest == verification.state_digest
        and review.artifact_evidence_digest == verification.artifact_evidence_digest
    )


def _replay_report_artifact(
    artifacts: AlphaArtifactReaderPort,
    *,
    state: AlphaVerificationLifecycleState,
    report_digest: str,
    processed_events: int,
) -> AlphaVerificationReplayReport:
    try:
        reference = artifacts.stat(report_digest)
    except ArtifactNotFoundError:
        return _artifact_failure(
            state,
            report_digest,
            AlphaVerificationReplayFindingCode.REPORT_MISSING,
            processed_events,
        )
    except ArtifactIntegrityError:
        return _artifact_failure(
            state,
            report_digest,
            AlphaVerificationReplayFindingCode.REPORT_INTEGRITY_FAILED,
            processed_events,
        )
    except OSError, RuntimeError, KernelError:
        return _artifact_failure(
            state,
            report_digest,
            AlphaVerificationReplayFindingCode.REPORT_READ_UNAVAILABLE,
            processed_events,
            status=AlphaArtifactReplayStatus.INCONCLUSIVE,
        )
    if (
        reference.digest != report_digest
        or reference.media_type != ALPHA_VERIFICATION_REPORT_MEDIA_TYPE
        or reference.encoding != "utf-8"
        or not 1 <= reference.size_bytes <= _MAX_REPORT_BYTES
    ):
        return _artifact_failure(
            state,
            report_digest,
            AlphaVerificationReplayFindingCode.REPORT_METADATA_MISMATCH,
            processed_events,
            reference=reference,
        )
    try:
        data = artifacts.get_bytes(reference)
    except ArtifactNotFoundError:
        return _artifact_failure(
            state,
            report_digest,
            AlphaVerificationReplayFindingCode.REPORT_MISSING,
            processed_events,
            reference=reference,
        )
    except ArtifactIntegrityError:
        return _artifact_failure(
            state,
            report_digest,
            AlphaVerificationReplayFindingCode.REPORT_INTEGRITY_FAILED,
            processed_events,
            reference=reference,
        )
    except OSError, RuntimeError, KernelError:
        return _artifact_failure(
            state,
            report_digest,
            AlphaVerificationReplayFindingCode.REPORT_READ_UNAVAILABLE,
            processed_events,
            reference=reference,
            status=AlphaArtifactReplayStatus.INCONCLUSIVE,
        )
    try:
        value = json.loads(data.decode("utf-8"))
    except UnicodeDecodeError, json.JSONDecodeError:
        return _artifact_failure(
            state,
            report_digest,
            AlphaVerificationReplayFindingCode.REPORT_JSON_INVALID,
            processed_events,
            reference=reference,
        )
    if not isinstance(value, dict):
        return _artifact_failure(
            state,
            report_digest,
            AlphaVerificationReplayFindingCode.REPORT_JSON_INVALID,
            processed_events,
            reference=reference,
        )
    if canonical_json_bytes(value) != data:
        return _artifact_failure(
            state,
            report_digest,
            AlphaVerificationReplayFindingCode.REPORT_NONCANONICAL,
            processed_events,
            reference=reference,
        )
    try:
        report = alpha_verification_report_from_mapping(cast("dict[str, object]", value))
    except AlphaVerificationError:
        return _artifact_failure(
            state,
            report_digest,
            AlphaVerificationReplayFindingCode.REPORT_INVALID,
            processed_events,
            reference=reference,
        )
    if not _report_matches_state(report, state, report_digest):
        return _artifact_failure(
            state,
            report_digest,
            AlphaVerificationReplayFindingCode.REPORT_BINDING_MISMATCH,
            processed_events,
            reference=reference,
        )
    return _result(
        run_id=state.run_id,
        lifecycle_status=_lifecycle_status(state.status),
        state=state,
        artifact_integrity=AlphaArtifactReplayStatus.VERIFIED,
        finding_code=None,
        report_digest=report_digest,
        reference=reference,
        matrix_digest=report.matrix_digest,
        processed_events=processed_events,
    )


def _report_matches_state(
    report: AlphaVerificationReport,
    state: AlphaVerificationLifecycleState,
    report_digest: str,
) -> bool:
    lease = state.lease
    bindings_match = (
        report.digest == report_digest
        and report.run_id == lease.run_id
        and report.context_digest == lease.context_digest
        and report.acceptance_digest == lease.acceptance_digest
        and report.state_digest == lease.state_digest
        and report.artifact_evidence_digest == lease.artifact_evidence_digest
        and report.admitted_review_digest == lease.admitted_review_digest
    )
    if state.status is AlphaVerificationLifecycleStatus.COMPLETED:
        return bool(
            bindings_match
            and report.status is state.verdict
            and report.matrix_digest == state.matrix_digest
        )
    return bindings_match and state.status is AlphaVerificationLifecycleStatus.FAILED


def _artifact_failure(
    state: AlphaVerificationLifecycleState,
    report_digest: str,
    code: AlphaVerificationReplayFindingCode,
    processed_events: int,
    *,
    reference: ArtifactRef | None = None,
    status: AlphaArtifactReplayStatus = AlphaArtifactReplayStatus.FAILED,
) -> AlphaVerificationReplayReport:
    return _result(
        run_id=state.run_id,
        lifecycle_status=_lifecycle_status(state.status),
        state=state,
        artifact_integrity=status,
        finding_code=code,
        report_digest=report_digest,
        reference=reference,
        matrix_digest=state.matrix_digest,
        processed_events=processed_events,
    )


def _empty_failure(
    run_id: str,
    status: AlphaArtifactReplayStatus,
    code: AlphaVerificationReplayFindingCode,
    *,
    processed_events: int = 0,
) -> AlphaVerificationReplayReport:
    return _result(
        run_id=run_id,
        lifecycle_status=AlphaVerificationReplayLifecycle.NOT_STARTED,
        state=None,
        artifact_integrity=status,
        finding_code=code,
        report_digest=None,
        reference=None,
        matrix_digest=None,
        processed_events=processed_events,
    )


def _result(
    *,
    run_id: str,
    lifecycle_status: AlphaVerificationReplayLifecycle,
    state: AlphaVerificationLifecycleState | None,
    artifact_integrity: AlphaArtifactReplayStatus,
    finding_code: AlphaVerificationReplayFindingCode | None,
    report_digest: str | None,
    reference: ArtifactRef | None,
    matrix_digest: str | None,
    processed_events: int,
) -> AlphaVerificationReplayReport:
    payload = {
        "run_id": run_id,
        "lifecycle_status": lifecycle_status.value,
        "verification_id": None if state is None else state.verification_id,
        "review_id": None if state is None else state.lease.review_id,
        "attempt": None if state is None else state.lease.attempt,
        "fencing_token": None if state is None else state.lease.fencing_token,
        "verdict": None if state is None or state.verdict is None else state.verdict.value,
        "failure_code": None if state is None else state.failure_code,
        "report_artifact_digest": report_digest,
        "report_size_bytes": None if reference is None else reference.size_bytes,
        "report_media_type": None if reference is None else reference.media_type,
        "report_encoding": None if reference is None else reference.encoding,
        "matrix_digest": matrix_digest,
        "artifact_integrity": artifact_integrity.value,
        "finding_code": None if finding_code is None else finding_code.value,
        "processed_events": processed_events,
    }
    return AlphaVerificationReplayReport(
        run_id=run_id,
        lifecycle_status=lifecycle_status,
        verification_id=None if state is None else state.verification_id,
        review_id=None if state is None else state.lease.review_id,
        attempt=None if state is None else state.lease.attempt,
        fencing_token=None if state is None else state.lease.fencing_token,
        verdict=None if state is None else state.verdict,
        failure_code=None if state is None else state.failure_code,
        report_artifact_digest=report_digest,
        report_size_bytes=None if reference is None else reference.size_bytes,
        report_media_type=None if reference is None else reference.media_type,
        report_encoding=None if reference is None else reference.encoding,
        matrix_digest=matrix_digest,
        artifact_integrity=artifact_integrity,
        finding_code=finding_code,
        processed_events=processed_events,
        evidence_digest=json_digest(payload),
    )


def _lifecycle_status(
    status: AlphaVerificationLifecycleStatus,
) -> AlphaVerificationReplayLifecycle:
    return {
        AlphaVerificationLifecycleStatus.CLAIMED: AlphaVerificationReplayLifecycle.CLAIMED,
        AlphaVerificationLifecycleStatus.REQUEUED: AlphaVerificationReplayLifecycle.REQUEUED,
        AlphaVerificationLifecycleStatus.COMPLETED: AlphaVerificationReplayLifecycle.COMPLETED,
        AlphaVerificationLifecycleStatus.FAILED: AlphaVerificationReplayLifecycle.VERIFIER_ERROR,
    }[status]


__all__ = [
    "AlphaVerificationEventReaderPort",
    "AlphaVerificationReplayFindingCode",
    "AlphaVerificationReplayLifecycle",
    "AlphaVerificationReplayReport",
    "replay_alpha_verification",
]
