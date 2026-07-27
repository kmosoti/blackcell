from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from blackcell.bootstrap.review_runtime import (
    ReviewCandidate,
    ReviewRuntimeError,
    ReviewRuntimeFailureCode,
    ReviewRuntimeService,
)
from blackcell.kernel import EventEnvelope, EventStore
from blackcell.orchestration.review_lifecycle import (
    REVIEW_CLAIMED,
    REVIEW_DISPATCH_AMBIGUOUS,
    REVIEW_FAILED,
    REVIEW_LEASE_RENEWED,
    REVIEW_PROVIDER_DISPATCH_STARTED,
    REVIEW_RECONCILIATION_REQUIRED,
    REVIEW_REQUEUED,
    REVIEW_SUCCEEDED,
    ReviewLifecycleStatus,
    review_id,
    review_provider_request_id,
    review_stream,
)
from blackcell.orchestration.run_lifecycle import RUN_SUCCEEDED, RUNTIME_EVENT_SOURCE

NOW = datetime(2026, 7, 22, 18, tzinfo=UTC)
STATE_DIGEST = "sha256:" + "2" * 64
EVIDENCE_DIGEST = "sha256:" + "3" * 64
ACCEPTANCE_DIGEST = "sha256:" + "4" * 64
CONTEXT_DIGEST = "sha256:" + "5" * 64
PROPOSAL_DIGEST = "sha256:" + "6" * 64
PROVIDER_DIGEST = "sha256:" + "7" * 64
ADMITTED_DIGEST = "sha256:" + "8" * 64
RESULT_DIGEST = "sha256:" + "9" * 64


def test_review_scheduler_claims_once_and_records_durable_success(tmp_path: Path) -> None:
    events, candidate = _events_and_candidate(tmp_path / "events.sqlite3")
    service = ReviewRuntimeService(events)

    with pytest.raises(ReviewRuntimeError) as self_review:
        service.claim(
            candidate,
            worker_id="executor-1",
            lease_expires_at=NOW + timedelta(minutes=10),
            claimed_at=NOW,
        )
    assert self_review.value.code is ReviewRuntimeFailureCode.CONFLICT

    claimed = service.claim(
        candidate,
        worker_id="reviewer-1",
        lease_expires_at=NOW + timedelta(minutes=10),
        claimed_at=NOW,
    )
    with pytest.raises(ReviewRuntimeError) as duplicate:
        service.claim(
            candidate,
            worker_id="reviewer-2",
            lease_expires_at=NOW + timedelta(minutes=10),
            claimed_at=NOW,
        )
    assert duplicate.value.code is ReviewRuntimeFailureCode.CONFLICT

    dispatch_event_id = service.record_provider_dispatch(
        claimed.lease,
        acceptance_digest=ACCEPTANCE_DIGEST,
        context_digest=CONTEXT_DIGEST,
        context_artifact_digest=CONTEXT_DIGEST,
        principal_id="reviewer-1",
        dispatched_at=NOW + timedelta(seconds=1),
    )
    state = service.record_success(
        claimed.lease,
        context_digest=CONTEXT_DIGEST,
        proposal_artifact_digest=PROPOSAL_DIGEST,
        provider_result_artifact_digest=PROVIDER_DIGEST,
        admitted_artifact_digest=ADMITTED_DIGEST,
        finding_count=1,
        principal_id="reviewer-1",
        completed_at=NOW + timedelta(seconds=2),
    )

    assert state.status is ReviewLifecycleStatus.SUCCEEDED
    assert state.provider_request_id == review_provider_request_id(claimed.lease.digest)
    assert state.provider_dispatch_event_id == dispatch_event_id
    assert state.admitted_artifact_digest == ADMITTED_DIGEST
    assert state.finding_count == 1

    stored = events.read_stream(review_stream(candidate.run_id))
    assert tuple(event.event_type for event in stored) == (
        REVIEW_CLAIMED,
        REVIEW_PROVIDER_DISPATCH_STARTED,
        REVIEW_SUCCEEDED,
    )
    assert stored[0].causation_id == candidate.run_event_id
    restarted = ReviewRuntimeService(EventStore(events.path))
    assert restarted.inspect(candidate.run_id) == state


def test_review_scheduler_requeues_only_pre_dispatch_restart(tmp_path: Path) -> None:
    events, candidate = _events_and_candidate(tmp_path / "events.sqlite3")
    service = ReviewRuntimeService(events)
    first = service.claim(
        candidate,
        worker_id="reviewer-1",
        lease_expires_at=NOW + timedelta(minutes=10),
        claimed_at=NOW,
    )

    restarted = ReviewRuntimeService(EventStore(events.path))
    with pytest.raises(ReviewRuntimeError) as self_reconciliation:
        restarted.reconcile(principal_id="reviewer-1")
    assert self_reconciliation.value.code is ReviewRuntimeFailureCode.CONFLICT
    report = restarted.reconcile(principal_id="review-supervisor")
    state = restarted.inspect(candidate.run_id)
    assert report.requeued_run_ids == (candidate.run_id,)
    assert report.ambiguous_run_ids == ()
    assert state is not None
    assert state.status is ReviewLifecycleStatus.REQUEUED
    with pytest.raises(ReviewRuntimeError) as reconciled:
        restarted.renew_lease(
            first.lease,
            lease_expires_at=NOW + timedelta(minutes=20),
            principal_id="reviewer-1",
            renewed_at=NOW + timedelta(minutes=11),
        )
    assert reconciled.value.code is ReviewRuntimeFailureCode.CONFLICT

    second = restarted.claim(
        candidate,
        worker_id="reviewer-2",
        lease_expires_at=NOW + timedelta(minutes=30),
        claimed_at=NOW + timedelta(minutes=1),
    )
    assert second.lease.attempt == first.lease.attempt + 1
    assert second.lease.fencing_token == first.lease.fencing_token + 1
    assert second.lease.worker_id == "reviewer-2"
    assert tuple(
        event.event_type for event in events.read_stream(review_stream(candidate.run_id))
    ) == (REVIEW_CLAIMED, REVIEW_REQUEUED, REVIEW_CLAIMED)


def test_review_scheduler_renews_and_closes_the_exact_lease_after_expiry(
    tmp_path: Path,
) -> None:
    events, candidate = _events_and_candidate(tmp_path / "events.sqlite3")
    service = ReviewRuntimeService(events)
    claimed = service.claim(
        candidate,
        worker_id="reviewer-1",
        lease_expires_at=NOW + timedelta(seconds=1),
        claimed_at=NOW,
    )

    renewed = service.renew_lease(
        claimed.lease,
        lease_expires_at=NOW + timedelta(minutes=10),
        principal_id="reviewer-1",
        renewed_at=NOW + timedelta(seconds=2),
    )

    assert renewed.expires_at == NOW + timedelta(minutes=10)
    state = service.inspect(candidate.run_id)
    assert state is not None
    assert state.lease == renewed
    with pytest.raises(ReviewRuntimeError) as stale:
        service.renew_lease(
            claimed.lease,
            lease_expires_at=NOW + timedelta(minutes=20),
            principal_id="reviewer-1",
            renewed_at=NOW + timedelta(seconds=3),
        )
    assert stale.value.code is ReviewRuntimeFailureCode.CONFLICT

    failed = service.record_failure(
        renewed,
        failure_code="review-preparation-failed",
        result_artifact_digest=None,
        principal_id="reviewer-1",
        failed_at=NOW + timedelta(minutes=11),
    )

    assert failed.status is ReviewLifecycleStatus.FAILED
    assert tuple(
        event.event_type for event in events.read_stream(review_stream(candidate.run_id))
    ) == (REVIEW_CLAIMED, REVIEW_LEASE_RENEWED, REVIEW_FAILED)


def test_review_scheduler_marks_post_dispatch_restart_ambiguous(tmp_path: Path) -> None:
    events, candidate = _events_and_candidate(tmp_path / "events.sqlite3")
    service = ReviewRuntimeService(events)
    claimed = service.claim(
        candidate,
        worker_id="reviewer-1",
        lease_expires_at=NOW + timedelta(minutes=10),
        claimed_at=NOW,
    )
    service.record_provider_dispatch(
        claimed.lease,
        acceptance_digest=ACCEPTANCE_DIGEST,
        context_digest=CONTEXT_DIGEST,
        context_artifact_digest=CONTEXT_DIGEST,
        principal_id="reviewer-1",
        dispatched_at=NOW + timedelta(seconds=1),
    )

    restarted = ReviewRuntimeService(EventStore(events.path))
    report = restarted.reconcile(principal_id="review-supervisor")
    state = restarted.inspect(candidate.run_id)

    assert report.requeued_run_ids == ()
    assert report.ambiguous_run_ids == (candidate.run_id,)
    assert state is not None
    assert state.status is ReviewLifecycleStatus.RECONCILIATION_REQUIRED
    assert state.failure_code == REVIEW_DISPATCH_AMBIGUOUS
    assert tuple(
        event.event_type for event in events.read_stream(review_stream(candidate.run_id))
    ) == (
        REVIEW_CLAIMED,
        REVIEW_PROVIDER_DISPATCH_STARTED,
        REVIEW_RECONCILIATION_REQUIRED,
    )
    with pytest.raises(ReviewRuntimeError) as redispatch:
        restarted.claim(
            candidate,
            worker_id="reviewer-2",
            lease_expires_at=NOW + timedelta(minutes=20),
            claimed_at=NOW + timedelta(minutes=1),
        )
    assert redispatch.value.code is ReviewRuntimeFailureCode.CONFLICT


def test_review_scheduler_records_stable_reviewer_error_and_rejects_stale_worker(
    tmp_path: Path,
) -> None:
    events, candidate = _events_and_candidate(tmp_path / "events.sqlite3")
    service = ReviewRuntimeService(events)
    claimed = service.claim(
        candidate,
        worker_id="reviewer-1",
        lease_expires_at=NOW + timedelta(minutes=10),
        claimed_at=NOW,
    )

    stale = replace(claimed.lease, fencing_token=claimed.lease.fencing_token + 1)
    with pytest.raises(ReviewRuntimeError) as stale_error:
        service.record_failure(
            stale,
            failure_code="reviewer-error",
            result_artifact_digest=None,
            principal_id="reviewer-1",
            failed_at=NOW + timedelta(seconds=1),
        )
    assert stale_error.value.code is ReviewRuntimeFailureCode.CONFLICT

    with pytest.raises(ReviewRuntimeError) as wrong_worker:
        service.record_failure(
            claimed.lease,
            failure_code="reviewer-error",
            result_artifact_digest=None,
            principal_id="executor-1",
            failed_at=NOW + timedelta(seconds=1),
        )
    assert wrong_worker.value.code is ReviewRuntimeFailureCode.CONFLICT

    state = service.record_failure(
        claimed.lease,
        failure_code="reviewer-error",
        result_artifact_digest=RESULT_DIGEST,
        principal_id="reviewer-1",
        failed_at=NOW + timedelta(seconds=1),
    )
    assert state.status is ReviewLifecycleStatus.FAILED
    assert state.failure_code == "reviewer-error"
    assert state.result_artifact_digest == RESULT_DIGEST
    assert events.read_stream(review_stream(claimed.lease.run_id))[-1].event_type == (REVIEW_FAILED)
    assert "executor" not in str(state.failure_code)


def _events_and_candidate(path: Path) -> tuple[EventStore, ReviewCandidate]:
    events = EventStore(path)
    run_id = "run-1"
    run_event = EventEnvelope.create(
        event_id="run-event-1",
        stream_id=f"run:{run_id}",
        stream_sequence=1,
        event_type=RUN_SUCCEEDED,
        actor="executor-1",
        source=RUNTIME_EVENT_SOURCE,
        payload={
            "principal_id": "executor-1",
            "run_id": run_id,
            "status": "succeeded",
            "retained_worktree": True,
        },
        recorded_at=NOW - timedelta(seconds=1),
        correlation_id="correlation-1",
    )
    stored = events.append(run_event, expected_sequence=0)
    candidate = ReviewCandidate(
        run_id=run_id,
        review_id=review_id(run_id, stored.payload_hash),
        correlation_id="correlation-1",
        run_event_id=stored.event_id,
        run_event_digest=stored.payload_hash,
        state_digest=STATE_DIGEST,
        artifact_evidence_digest=EVIDENCE_DIGEST,
    )
    return events, candidate
