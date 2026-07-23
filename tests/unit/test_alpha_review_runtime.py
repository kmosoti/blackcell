from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from blackcell.bootstrap.alpha_review_runtime import (
    AlphaReviewCandidate,
    AlphaReviewRuntimeError,
    AlphaReviewRuntimeFailureCode,
    AlphaReviewRuntimeService,
)
from blackcell.kernel import EventEnvelope, EventStore
from blackcell.orchestration.alpha_lifecycle import ALPHA_EVENT_SOURCE, ALPHA_RUN_SUCCEEDED
from blackcell.orchestration.alpha_review_lifecycle import (
    ALPHA_REVIEW_CLAIMED,
    ALPHA_REVIEW_DISPATCH_AMBIGUOUS,
    ALPHA_REVIEW_FAILED,
    ALPHA_REVIEW_LEASE_RENEWED,
    ALPHA_REVIEW_PROVIDER_DISPATCH_STARTED,
    ALPHA_REVIEW_RECONCILIATION_REQUIRED,
    ALPHA_REVIEW_REQUEUED,
    ALPHA_REVIEW_SUCCEEDED,
    AlphaReviewLifecycleStatus,
    alpha_review_id,
    alpha_review_provider_request_id,
    alpha_review_stream,
)

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
    service = AlphaReviewRuntimeService(events)

    with pytest.raises(AlphaReviewRuntimeError) as self_review:
        service.claim(
            candidate,
            worker_id="executor-1",
            lease_expires_at=NOW + timedelta(minutes=10),
            claimed_at=NOW,
        )
    assert self_review.value.code is AlphaReviewRuntimeFailureCode.CONFLICT

    claimed = service.claim(
        candidate,
        worker_id="reviewer-1",
        lease_expires_at=NOW + timedelta(minutes=10),
        claimed_at=NOW,
    )
    with pytest.raises(AlphaReviewRuntimeError) as duplicate:
        service.claim(
            candidate,
            worker_id="reviewer-2",
            lease_expires_at=NOW + timedelta(minutes=10),
            claimed_at=NOW,
        )
    assert duplicate.value.code is AlphaReviewRuntimeFailureCode.CONFLICT

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

    assert state.status is AlphaReviewLifecycleStatus.SUCCEEDED
    assert state.provider_request_id == alpha_review_provider_request_id(claimed.lease.digest)
    assert state.provider_dispatch_event_id == dispatch_event_id
    assert state.admitted_artifact_digest == ADMITTED_DIGEST
    assert state.finding_count == 1

    stored = events.read_stream(alpha_review_stream(candidate.run_id))
    assert tuple(event.event_type for event in stored) == (
        ALPHA_REVIEW_CLAIMED,
        ALPHA_REVIEW_PROVIDER_DISPATCH_STARTED,
        ALPHA_REVIEW_SUCCEEDED,
    )
    assert stored[0].causation_id == candidate.run_event_id
    restarted = AlphaReviewRuntimeService(EventStore(events.path))
    assert restarted.inspect(candidate.run_id) == state


def test_review_scheduler_requeues_only_pre_dispatch_restart(tmp_path: Path) -> None:
    events, candidate = _events_and_candidate(tmp_path / "events.sqlite3")
    service = AlphaReviewRuntimeService(events)
    first = service.claim(
        candidate,
        worker_id="reviewer-1",
        lease_expires_at=NOW + timedelta(minutes=10),
        claimed_at=NOW,
    )

    restarted = AlphaReviewRuntimeService(EventStore(events.path))
    with pytest.raises(AlphaReviewRuntimeError) as self_reconciliation:
        restarted.reconcile(principal_id="reviewer-1")
    assert self_reconciliation.value.code is AlphaReviewRuntimeFailureCode.CONFLICT
    report = restarted.reconcile(principal_id="review-supervisor")
    state = restarted.inspect(candidate.run_id)
    assert report.requeued_run_ids == (candidate.run_id,)
    assert report.ambiguous_run_ids == ()
    assert state is not None
    assert state.status is AlphaReviewLifecycleStatus.REQUEUED
    with pytest.raises(AlphaReviewRuntimeError) as reconciled:
        restarted.renew_lease(
            first.lease,
            lease_expires_at=NOW + timedelta(minutes=20),
            principal_id="reviewer-1",
            renewed_at=NOW + timedelta(minutes=11),
        )
    assert reconciled.value.code is AlphaReviewRuntimeFailureCode.CONFLICT

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
        event.event_type for event in events.read_stream(alpha_review_stream(candidate.run_id))
    ) == (ALPHA_REVIEW_CLAIMED, ALPHA_REVIEW_REQUEUED, ALPHA_REVIEW_CLAIMED)


def test_review_scheduler_renews_and_closes_the_exact_lease_after_expiry(
    tmp_path: Path,
) -> None:
    events, candidate = _events_and_candidate(tmp_path / "events.sqlite3")
    service = AlphaReviewRuntimeService(events)
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
    with pytest.raises(AlphaReviewRuntimeError) as stale:
        service.renew_lease(
            claimed.lease,
            lease_expires_at=NOW + timedelta(minutes=20),
            principal_id="reviewer-1",
            renewed_at=NOW + timedelta(seconds=3),
        )
    assert stale.value.code is AlphaReviewRuntimeFailureCode.CONFLICT

    failed = service.record_failure(
        renewed,
        failure_code="alpha-review-preparation-failed",
        result_artifact_digest=None,
        principal_id="reviewer-1",
        failed_at=NOW + timedelta(minutes=11),
    )

    assert failed.status is AlphaReviewLifecycleStatus.FAILED
    assert tuple(
        event.event_type for event in events.read_stream(alpha_review_stream(candidate.run_id))
    ) == (ALPHA_REVIEW_CLAIMED, ALPHA_REVIEW_LEASE_RENEWED, ALPHA_REVIEW_FAILED)


def test_review_scheduler_marks_post_dispatch_restart_ambiguous(tmp_path: Path) -> None:
    events, candidate = _events_and_candidate(tmp_path / "events.sqlite3")
    service = AlphaReviewRuntimeService(events)
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

    restarted = AlphaReviewRuntimeService(EventStore(events.path))
    report = restarted.reconcile(principal_id="review-supervisor")
    state = restarted.inspect(candidate.run_id)

    assert report.requeued_run_ids == ()
    assert report.ambiguous_run_ids == (candidate.run_id,)
    assert state is not None
    assert state.status is AlphaReviewLifecycleStatus.RECONCILIATION_REQUIRED
    assert state.failure_code == ALPHA_REVIEW_DISPATCH_AMBIGUOUS
    assert tuple(
        event.event_type for event in events.read_stream(alpha_review_stream(candidate.run_id))
    ) == (
        ALPHA_REVIEW_CLAIMED,
        ALPHA_REVIEW_PROVIDER_DISPATCH_STARTED,
        ALPHA_REVIEW_RECONCILIATION_REQUIRED,
    )
    with pytest.raises(AlphaReviewRuntimeError) as redispatch:
        restarted.claim(
            candidate,
            worker_id="reviewer-2",
            lease_expires_at=NOW + timedelta(minutes=20),
            claimed_at=NOW + timedelta(minutes=1),
        )
    assert redispatch.value.code is AlphaReviewRuntimeFailureCode.CONFLICT


def test_review_scheduler_records_stable_reviewer_error_and_rejects_stale_worker(
    tmp_path: Path,
) -> None:
    events, candidate = _events_and_candidate(tmp_path / "events.sqlite3")
    service = AlphaReviewRuntimeService(events)
    claimed = service.claim(
        candidate,
        worker_id="reviewer-1",
        lease_expires_at=NOW + timedelta(minutes=10),
        claimed_at=NOW,
    )

    stale = replace(claimed.lease, fencing_token=claimed.lease.fencing_token + 1)
    with pytest.raises(AlphaReviewRuntimeError) as stale_error:
        service.record_failure(
            stale,
            failure_code="alpha-reviewer-error",
            result_artifact_digest=None,
            principal_id="reviewer-1",
            failed_at=NOW + timedelta(seconds=1),
        )
    assert stale_error.value.code is AlphaReviewRuntimeFailureCode.CONFLICT

    with pytest.raises(AlphaReviewRuntimeError) as wrong_worker:
        service.record_failure(
            claimed.lease,
            failure_code="alpha-reviewer-error",
            result_artifact_digest=None,
            principal_id="executor-1",
            failed_at=NOW + timedelta(seconds=1),
        )
    assert wrong_worker.value.code is AlphaReviewRuntimeFailureCode.CONFLICT

    state = service.record_failure(
        claimed.lease,
        failure_code="alpha-reviewer-error",
        result_artifact_digest=RESULT_DIGEST,
        principal_id="reviewer-1",
        failed_at=NOW + timedelta(seconds=1),
    )
    assert state.status is AlphaReviewLifecycleStatus.FAILED
    assert state.failure_code == "alpha-reviewer-error"
    assert state.result_artifact_digest == RESULT_DIGEST
    assert events.read_stream(alpha_review_stream(claimed.lease.run_id))[-1].event_type == (
        ALPHA_REVIEW_FAILED
    )
    assert "executor" not in str(state.failure_code)


def _events_and_candidate(path: Path) -> tuple[EventStore, AlphaReviewCandidate]:
    events = EventStore(path)
    run_id = "run-1"
    run_event = EventEnvelope.create(
        event_id="run-event-1",
        stream_id=f"alpha:run:{run_id}",
        stream_sequence=1,
        event_type=ALPHA_RUN_SUCCEEDED,
        actor="executor-1",
        source=ALPHA_EVENT_SOURCE,
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
    candidate = AlphaReviewCandidate(
        run_id=run_id,
        review_id=alpha_review_id(run_id, stored.payload_hash),
        correlation_id="correlation-1",
        run_event_id=stored.event_id,
        run_event_digest=stored.payload_hash,
        state_digest=STATE_DIGEST,
        artifact_evidence_digest=EVIDENCE_DIGEST,
    )
    return events, candidate
