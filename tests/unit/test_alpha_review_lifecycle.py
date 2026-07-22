from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from blackcell.kernel import EventEnvelope, JsonInput
from blackcell.orchestration.alpha_lifecycle import ALPHA_EVENT_SOURCE
from blackcell.orchestration.alpha_review_lifecycle import (
    ALPHA_REVIEW_CLAIMED,
    ALPHA_REVIEW_PROVIDER_DISPATCH_STARTED,
    ALPHA_REVIEW_SUCCEEDED,
    AlphaReviewLease,
    AlphaReviewLifecycleError,
    AlphaReviewLifecycleStatus,
    alpha_review_id,
    alpha_review_lease_payload,
    alpha_review_lifecycle_payload,
    alpha_review_provider_request_id,
    alpha_review_stream,
    fold_alpha_review_lifecycle,
)

NOW = datetime(2026, 7, 22, 18, tzinfo=UTC)
RUN_EVENT_DIGEST = "sha256:" + "1" * 64
STATE_DIGEST = "sha256:" + "2" * 64
EVIDENCE_DIGEST = "sha256:" + "3" * 64
ACCEPTANCE_DIGEST = "sha256:" + "4" * 64
CONTEXT_DIGEST = "sha256:" + "5" * 64
PROPOSAL_DIGEST = "sha256:" + "6" * 64
PROVIDER_DIGEST = "sha256:" + "7" * 64
ADMITTED_DIGEST = "sha256:" + "8" * 64


def test_review_lifecycle_folds_claim_dispatch_and_source_bound_success() -> None:
    lease = _lease()
    claim = _claim(lease)
    dispatch = _event(
        lease,
        sequence=2,
        event_type=ALPHA_REVIEW_PROVIDER_DISPATCH_STARTED,
        causation_id=claim.event_id,
        payload={
            "run_id": lease.run_id,
            "review_id": lease.review_id,
            "lease_digest": lease.digest,
            "provider_request_id": alpha_review_provider_request_id(lease.digest),
            "acceptance_digest": ACCEPTANCE_DIGEST,
            "context_digest": CONTEXT_DIGEST,
            "context_artifact_digest": CONTEXT_DIGEST,
            "status": "provider-dispatch-started",
        },
    )
    success = _event(
        lease,
        sequence=3,
        event_type=ALPHA_REVIEW_SUCCEEDED,
        causation_id=dispatch.event_id,
        payload={
            "run_id": lease.run_id,
            "review_id": lease.review_id,
            "lease_digest": lease.digest,
            "context_digest": CONTEXT_DIGEST,
            "proposal_artifact_digest": PROPOSAL_DIGEST,
            "provider_result_artifact_digest": PROVIDER_DIGEST,
            "admitted_artifact_digest": ADMITTED_DIGEST,
            "finding_count": 2,
            "status": "succeeded",
        },
    )

    state = fold_alpha_review_lifecycle(lease.run_id, (claim, dispatch, success))
    payload = alpha_review_lifecycle_payload(state)

    assert state.status is AlphaReviewLifecycleStatus.SUCCEEDED
    assert state.lease == lease
    assert state.acceptance_digest == ACCEPTANCE_DIGEST
    assert state.context_digest == CONTEXT_DIGEST
    assert state.provider_request_id == alpha_review_provider_request_id(lease.digest)
    assert state.provider_dispatch_event_id == dispatch.event_id
    assert state.proposal_artifact_digest == PROPOSAL_DIGEST
    assert state.provider_result_artifact_digest == PROVIDER_DIGEST
    assert state.admitted_artifact_digest == ADMITTED_DIGEST
    assert state.finding_count == 2
    assert payload["run_event_id"] == lease.run_event_id
    assert payload["state_digest"] == STATE_DIGEST
    assert payload["artifact_evidence_digest"] == EVIDENCE_DIGEST
    assert "approved" not in payload
    assert "verified" not in payload


def test_review_lifecycle_rejects_stale_fences_unknown_fields_and_self_admission() -> None:
    lease = _lease()

    stale = replace(lease, fencing_token=2)
    with pytest.raises(AlphaReviewLifecycleError):
        fold_alpha_review_lifecycle(stale.run_id, (_claim(stale),))

    wrong_cause = _claim(lease, causation_id="different-event")
    with pytest.raises(AlphaReviewLifecycleError):
        fold_alpha_review_lifecycle(lease.run_id, (wrong_cause,))

    unknown = alpha_review_lease_payload(lease)
    claim_with_admission = EventEnvelope.create(
        stream_id=alpha_review_stream(lease.run_id),
        stream_sequence=1,
        event_type=ALPHA_REVIEW_CLAIMED,
        actor=lease.worker_id,
        source=ALPHA_EVENT_SOURCE,
        payload={
            "principal_id": lease.worker_id,
            "lease_digest": lease.digest,
            "lease": unknown,
            "status": "claimed",
            "admitted": True,
        },
        recorded_at=NOW,
        correlation_id="correlation-1",
        causation_id=lease.run_event_id,
    )
    with pytest.raises(AlphaReviewLifecycleError):
        fold_alpha_review_lifecycle(lease.run_id, (claim_with_admission,))

    substituted = replace(_claim(lease), stream_id=f"alpha:run:{lease.run_id}")
    with pytest.raises(AlphaReviewLifecycleError):
        fold_alpha_review_lifecycle(lease.run_id, (substituted,))


def _lease() -> AlphaReviewLease:
    run_id = "run-1"
    return AlphaReviewLease(
        run_id=run_id,
        review_id=alpha_review_id(run_id, RUN_EVENT_DIGEST),
        attempt=1,
        fencing_token=1,
        worker_id="reviewer-1",
        run_event_id="run-event-1",
        run_event_digest=RUN_EVENT_DIGEST,
        state_digest=STATE_DIGEST,
        artifact_evidence_digest=EVIDENCE_DIGEST,
        expires_at=NOW + timedelta(minutes=10),
    )


def _claim(
    lease: AlphaReviewLease,
    *,
    causation_id: str | None = None,
) -> EventEnvelope:
    return EventEnvelope.create(
        stream_id=alpha_review_stream(lease.run_id),
        stream_sequence=1,
        event_type=ALPHA_REVIEW_CLAIMED,
        actor=lease.worker_id,
        source=ALPHA_EVENT_SOURCE,
        payload={
            "principal_id": lease.worker_id,
            "lease_digest": lease.digest,
            "lease": alpha_review_lease_payload(lease),
            "status": "claimed",
        },
        recorded_at=NOW,
        correlation_id="correlation-1",
        causation_id=causation_id or lease.run_event_id,
    )


def _event(
    lease: AlphaReviewLease,
    *,
    sequence: int,
    event_type: str,
    causation_id: str,
    payload: dict[str, JsonInput],
) -> EventEnvelope:
    return EventEnvelope.create(
        stream_id=alpha_review_stream(lease.run_id),
        stream_sequence=sequence,
        event_type=event_type,
        actor=lease.worker_id,
        source=ALPHA_EVENT_SOURCE,
        payload={"principal_id": lease.worker_id, **payload},
        recorded_at=NOW + timedelta(seconds=sequence),
        correlation_id="correlation-1",
        causation_id=causation_id,
    )
