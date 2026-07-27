from __future__ import annotations

from collections.abc import Callable
from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from blackcell.bootstrap.review_runtime import ReviewRuntimeService
from blackcell.bootstrap.review_worker import ReviewerPort
from blackcell.bootstrap.verification_runtime import (
    VerificationRuntimeError,
    VerificationRuntimeFailureCode,
    VerificationRuntimeService,
)
from blackcell.bootstrap.verification_source import VerificationSourceService
from blackcell.bootstrap.verification_worker import (
    VerificationArtifactStorePort,
    VerificationSchedulerPort,
    VerificationWorker,
    VerificationWorkerPolicy,
    VerifierPort,
)
from blackcell.kernel import ArtifactRef, ArtifactStore
from blackcell.kernel._json import json_digest
from blackcell.orchestration.review import (
    AdmittedReview,
    ReviewContext,
    ReviewProposal,
    ReviewProviderCall,
    ReviewProviderResult,
    review_proposal_payload,
)
from blackcell.orchestration.verification import (
    VerificationReasonCode,
    VerificationReport,
    VerificationStatus,
    verification_report_from_mapping,
    verify_review,
)
from blackcell.orchestration.verification_lifecycle import (
    VerificationLease,
    VerificationLifecycleState,
    VerificationLifecycleStatus,
)
from tests.unit.test_replay import _completed_writer
from tests.unit.test_review_contracts import _assessments
from tests.unit.test_review_worker import _worker as review_worker

NOW = datetime(2026, 7, 22, 20, tzinfo=UTC)


class ClearReviewer:
    def review(self, call: ReviewProviderCall) -> ReviewProviderResult:
        proposal = ReviewProposal(
            context_digest=call.context.digest,
            findings=(),
            summary="No source-bound findings.",
            epistemic_assessments=_assessments(call.context),
        )
        return ReviewProviderResult(
            proposal=proposal,
            provider_output_digest=json_digest(review_proposal_payload(proposal)),
            profile_id="review",
            adapter_id="recorded-clear-reviewer",
            model_id="review-model",
            input_tokens=200,
            output_tokens=20,
            latency_ms=10,
            cost_microusd=1,
            completed_at=NOW,
        )


class StatusVerifier:
    def __init__(self, status: VerificationStatus) -> None:
        self.status = status

    def verify(
        self,
        context: ReviewContext,
        admitted_review: AdmittedReview,
    ) -> VerificationReport:
        report = verify_review(context, admitted_review)
        assert report.status is VerificationStatus.PASS
        if self.status is VerificationStatus.PASS:
            return report
        reason = (
            VerificationReasonCode.CHECK_FAILED
            if self.status is VerificationStatus.FAIL
            else VerificationReasonCode.EVIDENCE_AMBIGUOUS
        )
        first = replace(
            report.matrix[0],
            status=self.status,
            reason_codes=(reason,),
        )
        return replace(report, status=self.status, matrix=(first, *report.matrix[1:]))


class CrashingVerifier:
    def verify(
        self,
        context: ReviewContext,
        admitted_review: AdmittedReview,
    ) -> VerificationReport:
        del context, admitted_review
        raise RuntimeError("sensitive verifier failure")


class FailingArtifactStore:
    def put_bytes(
        self,
        data: bytes,
        *,
        media_type: str = "application/octet-stream",
        encoding: str | None = None,
    ) -> ArtifactRef:
        del data, media_type, encoding
        raise OSError("sensitive artifact failure")


class FailingCompletionScheduler:
    def __init__(self, delegate: VerificationRuntimeService) -> None:
        self.delegate = delegate

    def __getattr__(self, name: str) -> object:
        return getattr(self.delegate, name)

    def record_completed(
        self,
        lease: VerificationLease,
        *,
        verdict: VerificationStatus,
        report_artifact_digest: str,
        matrix_digest: str,
        principal_id: str,
        completed_at: datetime | None = None,
    ) -> VerificationLifecycleState:
        del (
            lease,
            verdict,
            report_artifact_digest,
            matrix_digest,
            principal_id,
            completed_at,
        )
        raise VerificationRuntimeError(VerificationRuntimeFailureCode.CONFLICT)


def test_verifier_worker_persists_each_completed_verdict_and_never_reselects(
    tmp_path: Path,
) -> None:
    for status in VerificationStatus:
        root = tmp_path / status.value
        root.mkdir()
        source, scheduler, artifacts = _completed_clear_review(root)
        worker = _worker(source, scheduler, artifacts, StatusVerifier(status))

        result = worker.run_once()

        assert result.status == "verification-completed"
        assert result.verdict is status
        assert result.report_artifact_digest is not None
        state = scheduler.inspect("run-1")
        assert state is not None
        assert state.status is VerificationLifecycleStatus.COMPLETED
        assert state.verdict is status
        assert state.report_artifact_digest == result.report_artifact_digest
        report = verification_report_from_mapping(
            cast("dict[str, object]", artifacts.get_json(result.report_artifact_digest))
        )
        assert report.status is status
        assert report.matrix_digest == state.matrix_digest
        assert worker.run_once().status == "idle"


def test_verifier_worker_records_completion_after_lease_expiry_while_claim_is_active(
    tmp_path: Path,
) -> None:
    source, scheduler, artifacts = _completed_clear_review(tmp_path)
    times = iter((NOW, NOW + timedelta(seconds=2), NOW + timedelta(seconds=2)))
    worker = _worker(
        source,
        scheduler,
        artifacts,
        StatusVerifier(VerificationStatus.PASS),
        lease_seconds=1,
        clock=lambda: next(times),
    )

    result = worker.run_once()

    assert result.status == "verification-completed"
    state = scheduler.inspect("run-1")
    assert state is not None
    assert state.status is VerificationLifecycleStatus.COMPLETED


def test_verifier_worker_records_stable_source_verifier_artifact_and_persistence_errors(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    source, scheduler, artifacts = _completed_clear_review(source_root)
    candidate = source.verification_candidate("run-1")
    artifacts.path_for(candidate.admitted_review_digest).write_bytes(b"tampered")
    source_failure = _worker(
        source, scheduler, artifacts, StatusVerifier(VerificationStatus.PASS)
    ).run_once()
    assert source_failure.status == "verification-error"
    assert source_failure.failure_code == "verification-review-artifact-invalid"

    verifier_root = tmp_path / "verifier"
    verifier_root.mkdir()
    source, scheduler, artifacts = _completed_clear_review(verifier_root)
    verifier_failure = _worker(source, scheduler, artifacts, CrashingVerifier()).run_once()
    assert verifier_failure.status == "verification-error"
    assert verifier_failure.failure_code == "verifier-failed"
    assert "sensitive" not in repr(verifier_failure)

    artifact_root = tmp_path / "artifact"
    artifact_root.mkdir()
    source, scheduler, _ = _completed_clear_review(artifact_root)
    artifact_failure = _worker(
        source,
        scheduler,
        cast("VerificationArtifactStorePort", FailingArtifactStore()),
        StatusVerifier(VerificationStatus.PASS),
    ).run_once()
    assert artifact_failure.status == "verification-error"
    assert artifact_failure.failure_code == "verification-report-artifact-failed"

    persistence_root = tmp_path / "persistence"
    persistence_root.mkdir()
    source, scheduler, artifacts = _completed_clear_review(persistence_root)
    persistence_failure = _worker(
        source,
        cast("VerificationSchedulerPort", FailingCompletionScheduler(scheduler)),
        artifacts,
        StatusVerifier(VerificationStatus.PASS),
    ).run_once()
    assert persistence_failure.status == "verification-error"
    assert persistence_failure.failure_code == "verification-persistence-failed"
    state = scheduler.inspect("run-1")
    assert state is not None
    assert state.status is VerificationLifecycleStatus.FAILED
    assert state.result_artifact_digest is not None


def test_verifier_worker_ports_exclude_execution_review_and_supervisor_authority() -> None:
    assert tuple(item.name for item in fields(VerificationWorker)) == (
        "source",
        "scheduler",
        "artifacts",
        "verifier",
        "policy",
        "clock",
    )
    worker_source = VerificationWorker.__dict__
    for forbidden in (
        "execution",
        "reviewer",
        "supervisor",
        "change_executor",
        "worktrees",
        "shell",
        "network",
    ):
        assert forbidden not in worker_source


def _completed_clear_review(
    tmp_path: Path,
) -> tuple[
    VerificationSourceService,
    VerificationRuntimeService,
    ArtifactStore,
]:
    execution, events, artifacts, _, _, _, _, _ = _completed_writer(tmp_path)
    result = review_worker(
        execution,
        ReviewRuntimeService(events),
        artifacts,
        cast("ReviewerPort", ClearReviewer()),
    ).run_once()
    assert result.status == "review-succeeded"
    return (
        VerificationSourceService(events, execution, artifacts),
        VerificationRuntimeService(events),
        artifacts,
    )


def _worker(
    source: VerificationSourceService,
    scheduler: VerificationSchedulerPort,
    artifacts: VerificationArtifactStorePort,
    verifier: VerifierPort,
    *,
    lease_seconds: int = 300,
    clock: Callable[[], datetime] | None = None,
) -> VerificationWorker:
    return VerificationWorker(
        source=source,
        scheduler=scheduler,
        artifacts=artifacts,
        verifier=verifier,
        policy=VerificationWorkerPolicy("verifier-1", lease_seconds),
        clock=(lambda: NOW) if clock is None else clock,
    )
