from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from blackcell.kernel import EventEnvelope, JsonInput
from blackcell.orchestration.alpha_lifecycle import ALPHA_EVENT_SOURCE
from blackcell.orchestration.alpha_review_lifecycle import alpha_review_id
from blackcell.orchestration.alpha_verify import AlphaVerificationStatus
from blackcell.orchestration.alpha_verify_lifecycle import (
    ALPHA_VERIFICATION_CLAIMED,
    ALPHA_VERIFICATION_COMPLETED,
    ALPHA_VERIFICATION_FAILED,
    ALPHA_VERIFICATION_REQUEUED,
    AlphaVerificationLease,
    AlphaVerificationLifecycleError,
    AlphaVerificationLifecycleStatus,
    alpha_verification_id,
    alpha_verification_lease_payload,
    alpha_verification_stream,
    fold_alpha_verification_lifecycle,
)

NOW = datetime(2026, 7, 22, 20, tzinfo=UTC)
RUN_DIGEST = "sha256:" + "1" * 64
STATE_DIGEST = "sha256:" + "2" * 64
EVIDENCE_DIGEST = "sha256:" + "3" * 64
REVIEW_DIGEST = "sha256:" + "4" * 64
ACCEPTANCE_DIGEST = "sha256:" + "5" * 64
CONTEXT_DIGEST = "sha256:" + "6" * 64
PROPOSAL_DIGEST = "sha256:" + "7" * 64
PROVIDER_DIGEST = "sha256:" + "8" * 64
ADMITTED_DIGEST = "sha256:" + "9" * 64
REPORT_DIGEST = "sha256:" + "a" * 64
MATRIX_DIGEST = "sha256:" + "b" * 64


def test_verification_lifecycle_folds_claim_and_each_completed_verdict() -> None:
    for verdict in AlphaVerificationStatus:
        lease = _lease()
        claimed = _claimed(lease)
        completed = _terminal(
            lease,
            event_type=ALPHA_VERIFICATION_COMPLETED,
            event_id=f"completed-{verdict.value}",
            payload={
                "principal_id": lease.worker_id,
                "run_id": lease.run_id,
                "verification_id": lease.verification_id,
                "lease_digest": lease.digest,
                "verdict": verdict.value,
                "report_artifact_digest": REPORT_DIGEST,
                "matrix_digest": MATRIX_DIGEST,
                "status": "completed",
            },
        )

        state = fold_alpha_verification_lifecycle(lease.run_id, (claimed, completed))

        assert state.status is AlphaVerificationLifecycleStatus.COMPLETED
        assert state.verdict is verdict
        assert state.report_artifact_digest == REPORT_DIGEST
        assert state.matrix_digest == MATRIX_DIGEST
        assert not state.active


def test_verification_lifecycle_rejects_stale_fences_unknown_fields_and_error_confusion() -> None:
    lease = _lease()
    claimed = _claimed(lease)
    stale = replace(lease, fencing_token=2)
    malformed = (
        _terminal(
            lease,
            event_type=ALPHA_VERIFICATION_COMPLETED,
            event_id="stale-completed",
            payload={
                "principal_id": lease.worker_id,
                "run_id": lease.run_id,
                "verification_id": lease.verification_id,
                "lease_digest": stale.digest,
                "verdict": "pass",
                "report_artifact_digest": REPORT_DIGEST,
                "matrix_digest": MATRIX_DIGEST,
                "status": "completed",
            },
        ),
        _terminal(
            lease,
            event_type=ALPHA_VERIFICATION_FAILED,
            event_id="confused-failure",
            payload={
                "principal_id": lease.worker_id,
                "run_id": lease.run_id,
                "verification_id": lease.verification_id,
                "lease_digest": lease.digest,
                "failure_code": "verifier-crashed",
                "result_artifact_digest": None,
                "status": "completed",
            },
        ),
        _terminal(
            lease,
            event_type=ALPHA_VERIFICATION_COMPLETED,
            event_id="unknown-field",
            payload={
                "principal_id": lease.worker_id,
                "run_id": lease.run_id,
                "verification_id": lease.verification_id,
                "lease_digest": lease.digest,
                "verdict": "pass",
                "report_artifact_digest": REPORT_DIGEST,
                "matrix_digest": MATRIX_DIGEST,
                "status": "completed",
                "approved": True,
            },
        ),
    )
    for event in malformed:
        with pytest.raises(AlphaVerificationLifecycleError):
            fold_alpha_verification_lifecycle(lease.run_id, (claimed, event))

    requeued = _terminal(
        lease,
        event_type=ALPHA_VERIFICATION_REQUEUED,
        event_id="requeued",
        actor="verification-supervisor",
        payload={
            "principal_id": "verification-supervisor",
            "run_id": lease.run_id,
            "verification_id": lease.verification_id,
            "lease_digest": lease.digest,
            "disposition": "deterministic-retry",
            "status": "requeued",
        },
    )
    next_lease = replace(
        lease,
        attempt=2,
        fencing_token=2,
        worker_id="verifier-2",
        expires_at=NOW + timedelta(minutes=20),
    )
    next_claim = EventEnvelope.create(
        event_id="claim-2",
        stream_id=alpha_verification_stream(lease.run_id),
        stream_sequence=3,
        event_type=ALPHA_VERIFICATION_CLAIMED,
        actor=next_lease.worker_id,
        source=ALPHA_EVENT_SOURCE,
        payload={
            "principal_id": next_lease.worker_id,
            "lease_digest": next_lease.digest,
            "lease": alpha_verification_lease_payload(next_lease),
            "status": "claimed",
        },
        recorded_at=NOW + timedelta(minutes=1),
        correlation_id="correlation-1",
        causation_id=requeued.event_id,
    )
    state = fold_alpha_verification_lifecycle(
        lease.run_id,
        (claimed, requeued, next_claim),
    )
    assert state.status is AlphaVerificationLifecycleStatus.CLAIMED
    assert state.lease.fencing_token == 2


def _lease() -> AlphaVerificationLease:
    run_id = "run-1"
    review_id = alpha_review_id(run_id, RUN_DIGEST)
    return AlphaVerificationLease(
        run_id=run_id,
        verification_id=alpha_verification_id(run_id, REVIEW_DIGEST),
        attempt=1,
        fencing_token=1,
        worker_id="verifier-1",
        run_event_id="run-event-1",
        run_event_digest=RUN_DIGEST,
        state_digest=STATE_DIGEST,
        artifact_evidence_digest=EVIDENCE_DIGEST,
        review_id=review_id,
        review_event_id="review-event-1",
        review_event_digest=REVIEW_DIGEST,
        acceptance_digest=ACCEPTANCE_DIGEST,
        context_digest=CONTEXT_DIGEST,
        proposal_artifact_digest=PROPOSAL_DIGEST,
        provider_result_artifact_digest=PROVIDER_DIGEST,
        admitted_review_digest=ADMITTED_DIGEST,
        finding_count=1,
        expires_at=NOW + timedelta(minutes=10),
    )


def _claimed(lease: AlphaVerificationLease) -> EventEnvelope:
    return EventEnvelope.create(
        event_id="claim-1",
        stream_id=alpha_verification_stream(lease.run_id),
        stream_sequence=1,
        event_type=ALPHA_VERIFICATION_CLAIMED,
        actor=lease.worker_id,
        source=ALPHA_EVENT_SOURCE,
        payload={
            "principal_id": lease.worker_id,
            "lease_digest": lease.digest,
            "lease": alpha_verification_lease_payload(lease),
            "status": "claimed",
        },
        recorded_at=NOW,
        correlation_id="correlation-1",
        causation_id=lease.review_event_id,
    )


def _terminal(
    lease: AlphaVerificationLease,
    *,
    event_type: str,
    event_id: str,
    payload: dict[str, JsonInput],
    actor: str | None = None,
) -> EventEnvelope:
    return EventEnvelope.create(
        event_id=event_id,
        stream_id=alpha_verification_stream(lease.run_id),
        stream_sequence=2,
        event_type=event_type,
        actor=actor or lease.worker_id,
        source=ALPHA_EVENT_SOURCE,
        payload=payload,
        recorded_at=NOW + timedelta(seconds=1),
        correlation_id="correlation-1",
        causation_id="claim-1",
    )
