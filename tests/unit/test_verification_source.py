from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from blackcell.bootstrap.review_runtime import ReviewRuntimeService
from blackcell.bootstrap.verification_source import (
    VerificationSourceError,
    VerificationSourceFailureCode,
    VerificationSourceService,
)
from blackcell.kernel._json import canonical_json_bytes, json_digest
from blackcell.orchestration.execution_artifacts import (
    ADMITTED_REVIEW_MEDIA_TYPE,
    REVIEW_CONTEXT_MEDIA_TYPE,
    REVIEW_PROPOSAL_MEDIA_TYPE,
    REVIEW_PROVIDER_MEDIA_TYPE,
)
from blackcell.orchestration.review import (
    ReviewProposal,
    ReviewProviderResult,
    admit_review,
    admitted_review_payload,
    review_context_payload,
    review_proposal_payload,
    review_provider_result_payload,
)
from blackcell.orchestration.verification import VerificationStatus, verify_review
from tests.unit.test_replay import _completed_writer
from tests.unit.test_review_contracts import _prior_assessments
from tests.unit.test_review_worker import RecordingReviewer, _worker

NOW = datetime(2026, 7, 27, 20, tzinfo=UTC)


def test_verification_source_rebuilds_and_validates_complete_review_artifact_chain(
    tmp_path: Path,
) -> None:
    execution, events, artifacts, _, _, _, _, _ = _completed_writer(tmp_path)
    review = _worker(
        execution,
        ReviewRuntimeService(events),
        artifacts,
        RecordingReviewer(),
    ).run_once()
    assert review.status == "review-succeeded"
    source = VerificationSourceService(events, execution, artifacts)

    assert source.verification_run_ids() == ("run-1",)
    candidate = source.verification_candidate("run-1")
    prepared = source.prepare_verification(candidate)

    assert prepared.candidate == candidate
    assert prepared.context.digest == candidate.context_digest
    assert prepared.context.acceptance.digest == candidate.acceptance_digest
    assert prepared.context.state_digest == candidate.state_digest
    assert prepared.context.artifact_evidence_digest == candidate.artifact_evidence_digest
    assert prepared.admitted_review.digest == candidate.admitted_review_digest
    assert len(prepared.admitted_review.findings) == candidate.finding_count


def test_verification_source_prepares_prior_contract_artifacts_after_upgrade(
    tmp_path: Path,
) -> None:
    execution, events, artifacts, _, _, _, _, _ = _completed_writer(tmp_path)
    scheduler = ReviewRuntimeService(events)
    candidate = execution.review_candidate("run-1")
    claimed = scheduler.claim(
        candidate,
        worker_id="prior-reviewer",
        lease_expires_at=NOW + timedelta(minutes=5),
        claimed_at=NOW,
    )
    context = replace(
        execution.prepare_review_context(candidate),
        schema_version="review-context/v1",
    )
    context_ref = artifacts.put_bytes(
        canonical_json_bytes(review_context_payload(context)),
        media_type=REVIEW_CONTEXT_MEDIA_TYPE,
        encoding="utf-8",
    )
    scheduler.record_provider_dispatch(
        claimed.lease,
        acceptance_digest=context.acceptance.digest,
        context_digest=context.digest,
        context_artifact_digest=context_ref.digest,
        principal_id="prior-reviewer",
        dispatched_at=NOW,
    )
    proposal = ReviewProposal(
        context_digest=context.digest,
        findings=(),
        summary="No findings under the prior six-dimension contract.",
        epistemic_assessments=_prior_assessments(context),
        schema_version="review-proposal/v1",
    )
    provider = ReviewProviderResult(
        proposal=proposal,
        provider_output_digest=json_digest(review_proposal_payload(proposal)),
        profile_id="review",
        adapter_id="recorded-reviewer",
        model_id="review-model",
        input_tokens=200,
        output_tokens=20,
        latency_ms=10,
        cost_microusd=1,
        completed_at=NOW,
    )
    admitted = admit_review(context, proposal)
    proposal_ref = artifacts.put_bytes(
        canonical_json_bytes(review_proposal_payload(proposal)),
        media_type=REVIEW_PROPOSAL_MEDIA_TYPE,
        encoding="utf-8",
    )
    provider_ref = artifacts.put_bytes(
        canonical_json_bytes(review_provider_result_payload(provider)),
        media_type=REVIEW_PROVIDER_MEDIA_TYPE,
        encoding="utf-8",
    )
    admitted_ref = artifacts.put_bytes(
        canonical_json_bytes(admitted_review_payload(admitted)),
        media_type=ADMITTED_REVIEW_MEDIA_TYPE,
        encoding="utf-8",
    )
    scheduler.record_success(
        claimed.lease,
        context_digest=context.digest,
        proposal_artifact_digest=proposal_ref.digest,
        provider_result_artifact_digest=provider_ref.digest,
        admitted_artifact_digest=admitted_ref.digest,
        finding_count=0,
        principal_id="prior-reviewer",
        completed_at=NOW,
    )

    source = VerificationSourceService(events, execution, artifacts)
    prepared = source.prepare_verification(source.verification_candidate("run-1"))
    report = verify_review(prepared.context, prepared.admitted_review)

    assert prepared.context == context
    assert prepared.context.schema_version == "review-context/v1"
    assert prepared.admitted_review == admitted
    assert prepared.admitted_review.schema_version == "execution-admitted-review/v1"
    assert len(prepared.admitted_review.epistemic_assessments) == 6
    assert report.status is VerificationStatus.PASS


def test_verification_source_rejects_review_tamper_and_snapshot_drift(
    tmp_path: Path,
) -> None:
    tamper_root = tmp_path / "tamper"
    tamper_root.mkdir()
    execution, events, artifacts, _, _, _, _, _ = _completed_writer(tamper_root)
    result = _worker(
        execution,
        ReviewRuntimeService(events),
        artifacts,
        RecordingReviewer(),
    ).run_once()
    assert result.status == "review-succeeded"
    source = VerificationSourceService(events, execution, artifacts)
    candidate = source.verification_candidate("run-1")
    artifacts.path_for(candidate.admitted_review_digest).write_bytes(b"tampered")

    with pytest.raises(VerificationSourceError) as tampered:
        source.prepare_verification(candidate)
    assert tampered.value.code is VerificationSourceFailureCode.REVIEW_ARTIFACT_INVALID

    drift_root = tmp_path / "drift"
    drift_root.mkdir()
    execution, events, artifacts, _, _, _, _, _ = _completed_writer(drift_root)
    result = _worker(
        execution,
        ReviewRuntimeService(events),
        artifacts,
        RecordingReviewer(),
    ).run_once()
    assert result.status == "review-succeeded"
    source = VerificationSourceService(events, execution, artifacts)
    candidate = source.verification_candidate("run-1")
    drifted = replace(candidate, context_digest="sha256:" + "f" * 64)

    with pytest.raises(VerificationSourceError) as snapshot:
        source.prepare_verification(drifted)
    assert snapshot.value.code is VerificationSourceFailureCode.SNAPSHOT_MISMATCH
