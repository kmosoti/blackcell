from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from blackcell.bootstrap.review_runtime import ReviewRuntimeService
from blackcell.bootstrap.verification_runtime import (
    VerificationRuntimeError,
    VerificationRuntimeFailureCode,
    VerificationRuntimeService,
)
from blackcell.kernel import EventEnvelope, EventStore
from blackcell.orchestration.review_lifecycle import (
    ReviewCandidate,
    review_id,
    review_stream,
)
from blackcell.orchestration.run_lifecycle import RUN_SUCCEEDED, RUNTIME_EVENT_SOURCE
from blackcell.orchestration.verification import VerificationStatus
from blackcell.orchestration.verification_lifecycle import (
    VERIFICATION_CLAIMED,
    VERIFICATION_COMPLETED,
    VERIFICATION_FAILED,
    VERIFICATION_REQUEUED,
    VerificationCandidate,
    VerificationLifecycleStatus,
    verification_id,
    verification_stream,
)

NOW = datetime(2026, 7, 22, 20, tzinfo=UTC)
STATE_DIGEST = "sha256:" + "2" * 64
EVIDENCE_DIGEST = "sha256:" + "3" * 64
ACCEPTANCE_DIGEST = "sha256:" + "5" * 64
CONTEXT_DIGEST = "sha256:" + "6" * 64
PROPOSAL_DIGEST = "sha256:" + "7" * 64
PROVIDER_DIGEST = "sha256:" + "8" * 64
ADMITTED_DIGEST = "sha256:" + "9" * 64
REPORT_DIGEST = "sha256:" + "a" * 64
MATRIX_DIGEST = "sha256:" + "b" * 64
RESULT_DIGEST = "sha256:" + "c" * 64


def test_verification_scheduler_claims_distinct_authority_and_records_completed_verdict(
    tmp_path: Path,
) -> None:
    events, candidate = _events_and_candidate(tmp_path / "events.sqlite3")
    service = VerificationRuntimeService(events)

    for forbidden in ("executor-1", "reviewer-1"):
        with pytest.raises(VerificationRuntimeError) as self_verify:
            service.claim(
                candidate,
                worker_id=forbidden,
                lease_expires_at=NOW + timedelta(minutes=10),
                claimed_at=NOW,
            )
        assert self_verify.value.code is VerificationRuntimeFailureCode.CONFLICT

    claimed = service.claim(
        candidate,
        worker_id="verifier-1",
        lease_expires_at=NOW + timedelta(minutes=10),
        claimed_at=NOW,
    )
    state = service.record_completed(
        claimed.lease,
        verdict=VerificationStatus.FAIL,
        report_artifact_digest=REPORT_DIGEST,
        matrix_digest=MATRIX_DIGEST,
        principal_id="verifier-1",
        completed_at=NOW + timedelta(seconds=1),
    )

    assert state.status is VerificationLifecycleStatus.COMPLETED
    assert state.verdict is VerificationStatus.FAIL
    assert state.failure_code is None
    assert tuple(
        event.event_type for event in events.read_stream(verification_stream(candidate.run_id))
    ) == (VERIFICATION_CLAIMED, VERIFICATION_COMPLETED)
    restarted = VerificationRuntimeService(EventStore(events.path))
    assert restarted.inspect(candidate.run_id) == state


def test_verification_scheduler_requeues_incomplete_deterministic_claim(tmp_path: Path) -> None:
    events, candidate = _events_and_candidate(tmp_path / "events.sqlite3")
    service = VerificationRuntimeService(events)
    first = service.claim(
        candidate,
        worker_id="verifier-1",
        lease_expires_at=NOW + timedelta(minutes=10),
        claimed_at=NOW,
    )

    with pytest.raises(VerificationRuntimeError) as self_reconcile:
        service.reconcile(principal_id="verifier-1")
    assert self_reconcile.value.code is VerificationRuntimeFailureCode.CONFLICT
    report = service.reconcile(principal_id="verification-supervisor")
    assert report.requeued_run_ids == (candidate.run_id,)
    requeued = service.inspect(candidate.run_id)
    assert requeued is not None
    assert requeued.status is VerificationLifecycleStatus.REQUEUED

    second = service.claim(
        candidate,
        worker_id="verifier-2",
        lease_expires_at=NOW + timedelta(minutes=20),
        claimed_at=NOW + timedelta(minutes=1),
    )
    assert second.lease.attempt == first.lease.attempt + 1
    assert second.lease.fencing_token == first.lease.fencing_token + 1
    assert tuple(
        event.event_type for event in events.read_stream(verification_stream(candidate.run_id))
    ) == (
        VERIFICATION_CLAIMED,
        VERIFICATION_REQUEUED,
        VERIFICATION_CLAIMED,
    )


def test_verification_scheduler_records_stable_verifier_error_and_rejects_stale_worker(
    tmp_path: Path,
) -> None:
    events, candidate = _events_and_candidate(tmp_path / "events.sqlite3")
    service = VerificationRuntimeService(events)
    claimed = service.claim(
        candidate,
        worker_id="verifier-1",
        lease_expires_at=NOW + timedelta(minutes=10),
        claimed_at=NOW,
    )
    stale = replace(claimed.lease, fencing_token=2)

    with pytest.raises(VerificationRuntimeError) as stale_error:
        service.record_failure(
            stale,
            failure_code="verifier-error",
            result_artifact_digest=None,
            principal_id="verifier-1",
            failed_at=NOW + timedelta(seconds=1),
        )
    assert stale_error.value.code is VerificationRuntimeFailureCode.CONFLICT

    with pytest.raises(VerificationRuntimeError) as wrong_worker:
        service.record_failure(
            claimed.lease,
            failure_code="verifier-error",
            result_artifact_digest=None,
            principal_id="reviewer-1",
            failed_at=NOW + timedelta(seconds=1),
        )
    assert wrong_worker.value.code is VerificationRuntimeFailureCode.CONFLICT

    state = service.record_failure(
        claimed.lease,
        failure_code="verifier-error",
        result_artifact_digest=RESULT_DIGEST,
        principal_id="verifier-1",
        failed_at=NOW + timedelta(seconds=1),
    )
    assert state.status is VerificationLifecycleStatus.FAILED
    assert state.verdict is None
    assert state.failure_code == "verifier-error"
    assert state.result_artifact_digest == RESULT_DIGEST
    assert events.read_stream(verification_stream(candidate.run_id))[-1].event_type == (
        VERIFICATION_FAILED
    )


def test_verification_scheduler_accepts_late_terminal_events_only_for_active_lease(
    tmp_path: Path,
) -> None:
    completed_events, completed_candidate = _events_and_candidate(tmp_path / "completed.sqlite3")
    completed_service = VerificationRuntimeService(completed_events)
    completed_claim = completed_service.claim(
        completed_candidate,
        worker_id="verifier-1",
        lease_expires_at=NOW + timedelta(seconds=1),
        claimed_at=NOW,
    )
    completed = completed_service.record_completed(
        completed_claim.lease,
        verdict=VerificationStatus.PASS,
        report_artifact_digest=RESULT_DIGEST,
        matrix_digest=MATRIX_DIGEST,
        principal_id="verifier-1",
        completed_at=NOW + timedelta(seconds=2),
    )
    assert completed.status is VerificationLifecycleStatus.COMPLETED

    failed_events, failed_candidate = _events_and_candidate(tmp_path / "failed.sqlite3")
    failed_service = VerificationRuntimeService(failed_events)
    failed_claim = failed_service.claim(
        failed_candidate,
        worker_id="verifier-1",
        lease_expires_at=NOW + timedelta(seconds=1),
        claimed_at=NOW,
    )
    failed = failed_service.record_failure(
        failed_claim.lease,
        failure_code="verifier-error",
        result_artifact_digest=None,
        principal_id="verifier-1",
        failed_at=NOW + timedelta(seconds=2),
    )
    assert failed.status is VerificationLifecycleStatus.FAILED

    stale_events, stale_candidate = _events_and_candidate(tmp_path / "stale.sqlite3")
    stale_service = VerificationRuntimeService(stale_events)
    stale_claim = stale_service.claim(
        stale_candidate,
        worker_id="verifier-1",
        lease_expires_at=NOW + timedelta(seconds=1),
        claimed_at=NOW,
    )
    stale_service.reconcile(principal_id="verification-supervisor")
    with pytest.raises(VerificationRuntimeError) as stale:
        stale_service.record_completed(
            stale_claim.lease,
            verdict=VerificationStatus.PASS,
            report_artifact_digest=RESULT_DIGEST,
            matrix_digest=MATRIX_DIGEST,
            principal_id="verifier-1",
            completed_at=NOW + timedelta(seconds=2),
        )
    assert stale.value.code is VerificationRuntimeFailureCode.CONFLICT


def _events_and_candidate(path: Path) -> tuple[EventStore, VerificationCandidate]:
    events = EventStore(path)
    run_id = "run-1"
    execution = events.append(
        EventEnvelope.create(
            event_id="run-event-1",
            stream_id=f"run:{run_id}",
            stream_sequence=1,
            event_type=RUN_SUCCEEDED,
            actor="executor-1",
            source=RUNTIME_EVENT_SOURCE,
            payload={"run_id": run_id, "status": "succeeded"},
            recorded_at=NOW - timedelta(minutes=2),
            correlation_id="correlation-1",
        ),
        expected_sequence=0,
    )
    review_candidate = ReviewCandidate(
        run_id=run_id,
        review_id=review_id(run_id, execution.payload_hash),
        correlation_id="correlation-1",
        run_event_id=execution.event_id,
        run_event_digest=execution.payload_hash,
        state_digest=STATE_DIGEST,
        artifact_evidence_digest=EVIDENCE_DIGEST,
    )
    reviews = ReviewRuntimeService(events)
    claimed = reviews.claim(
        review_candidate,
        worker_id="reviewer-1",
        lease_expires_at=NOW - timedelta(seconds=30),
        claimed_at=NOW - timedelta(minutes=1),
    )
    reviews.record_provider_dispatch(
        claimed.lease,
        acceptance_digest=ACCEPTANCE_DIGEST,
        context_digest=CONTEXT_DIGEST,
        context_artifact_digest=CONTEXT_DIGEST,
        principal_id="reviewer-1",
        dispatched_at=NOW - timedelta(seconds=50),
    )
    review_state = reviews.record_success(
        claimed.lease,
        context_digest=CONTEXT_DIGEST,
        proposal_artifact_digest=PROPOSAL_DIGEST,
        provider_result_artifact_digest=PROVIDER_DIGEST,
        admitted_artifact_digest=ADMITTED_DIGEST,
        finding_count=1,
        principal_id="reviewer-1",
        completed_at=NOW - timedelta(seconds=40),
    )
    review_event = events.read_stream(review_stream(run_id))[-1]
    assert review_state.latest_event.event_id == review_event.event_id
    return events, VerificationCandidate(
        run_id=run_id,
        verification_id=verification_id(run_id, review_event.payload_hash),
        correlation_id="correlation-1",
        run_event_id=execution.event_id,
        run_event_digest=execution.payload_hash,
        state_digest=STATE_DIGEST,
        artifact_evidence_digest=EVIDENCE_DIGEST,
        review_id=review_candidate.review_id,
        review_event_id=review_event.event_id,
        review_event_digest=review_event.payload_hash,
        acceptance_digest=ACCEPTANCE_DIGEST,
        context_digest=CONTEXT_DIGEST,
        proposal_artifact_digest=PROPOSAL_DIGEST,
        provider_result_artifact_digest=PROVIDER_DIGEST,
        admitted_review_digest=ADMITTED_DIGEST,
        finding_count=1,
    )
