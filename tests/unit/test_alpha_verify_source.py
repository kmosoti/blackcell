from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from blackcell.bootstrap.alpha_review_runtime import AlphaReviewRuntimeService
from blackcell.bootstrap.alpha_verify_source import (
    AlphaVerificationSourceError,
    AlphaVerificationSourceFailureCode,
    AlphaVerificationSourceService,
)
from tests.unit.test_alpha_replay import _completed_writer
from tests.unit.test_alpha_review_worker import RecordingReviewer, _worker


def test_verification_source_rebuilds_and_validates_complete_review_artifact_chain(
    tmp_path: Path,
) -> None:
    execution, events, artifacts, _, _, _, _, _ = _completed_writer(tmp_path)
    review = _worker(
        execution,
        AlphaReviewRuntimeService(events),
        artifacts,
        RecordingReviewer(),
    ).run_once()
    assert review.status == "review-succeeded"
    source = AlphaVerificationSourceService(events, execution, artifacts)

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


def test_verification_source_rejects_review_tamper_and_snapshot_drift(
    tmp_path: Path,
) -> None:
    tamper_root = tmp_path / "tamper"
    tamper_root.mkdir()
    execution, events, artifacts, _, _, _, _, _ = _completed_writer(tamper_root)
    result = _worker(
        execution,
        AlphaReviewRuntimeService(events),
        artifacts,
        RecordingReviewer(),
    ).run_once()
    assert result.status == "review-succeeded"
    source = AlphaVerificationSourceService(events, execution, artifacts)
    candidate = source.verification_candidate("run-1")
    artifacts.path_for(candidate.admitted_review_digest).write_bytes(b"tampered")

    with pytest.raises(AlphaVerificationSourceError) as tampered:
        source.prepare_verification(candidate)
    assert tampered.value.code is AlphaVerificationSourceFailureCode.REVIEW_ARTIFACT_INVALID

    drift_root = tmp_path / "drift"
    drift_root.mkdir()
    execution, events, artifacts, _, _, _, _, _ = _completed_writer(drift_root)
    result = _worker(
        execution,
        AlphaReviewRuntimeService(events),
        artifacts,
        RecordingReviewer(),
    ).run_once()
    assert result.status == "review-succeeded"
    source = AlphaVerificationSourceService(events, execution, artifacts)
    candidate = source.verification_candidate("run-1")
    drifted = replace(candidate, context_digest="sha256:" + "f" * 64)

    with pytest.raises(AlphaVerificationSourceError) as snapshot:
        source.prepare_verification(drifted)
    assert snapshot.value.code is AlphaVerificationSourceFailureCode.SNAPSHOT_MISMATCH
