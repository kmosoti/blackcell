from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from blackcell.bootstrap.alpha_review_runtime import AlphaReviewRuntimeService
from blackcell.bootstrap.alpha_review_worker import (
    AlphaReviewerPort,
    AlphaReviewWorker,
    AlphaReviewWorkerPolicy,
)
from blackcell.bootstrap.alpha_runtime import AlphaRuntimeApiService
from blackcell.gateway import GatewayBudget
from blackcell.kernel import ArtifactStore, EventStore
from blackcell.kernel._json import json_digest
from blackcell.orchestration.alpha_review import (
    AlphaProposedReviewFinding,
    AlphaReviewCitation,
    AlphaReviewFindingCategory,
    AlphaReviewProposal,
    AlphaReviewProviderCall,
    AlphaReviewProviderResult,
    AlphaReviewSeverity,
    alpha_review_proposal_payload,
)
from blackcell.orchestration.alpha_review_lifecycle import (
    ALPHA_REVIEW_FAILED,
    ALPHA_REVIEW_LEASE_RENEWED,
    ALPHA_REVIEW_PROVIDER_DISPATCH_STARTED,
    ALPHA_REVIEW_RECONCILIATION_REQUIRED,
    ALPHA_REVIEW_SUCCEEDED,
    AlphaReviewLifecycleStatus,
    alpha_review_provider_request_id,
    alpha_review_stream,
)
from tests.unit.test_alpha_replay import _completed_writer, _linked_digest

NOW = datetime(2026, 7, 22, 19, tzinfo=UTC)


class RecordingReviewer:
    def __init__(self, *, context_mismatch: bool = False) -> None:
        self.context_mismatch = context_mismatch
        self.calls: list[AlphaReviewProviderCall] = []

    def review(self, call: AlphaReviewProviderCall) -> AlphaReviewProviderResult:
        self.calls.append(call)
        evidence = next(item for item in call.context.evidence if item.path == "src/value.py")
        proposal = AlphaReviewProposal(
            context_digest=("sha256:" + "9" * 64 if self.context_mismatch else call.context.digest),
            findings=(
                AlphaProposedReviewFinding(
                    finding_id="finding-1",
                    category=AlphaReviewFindingCategory.CORRECTNESS,
                    severity=AlphaReviewSeverity.HIGH,
                    claim="The bounded source change requires independent verification.",
                    impact="An incorrect value could violate the accepted intent.",
                    recommendation="Verify the cited source and exact check result.",
                    citations=(
                        AlphaReviewCitation(
                            evidence.evidence_id,
                            evidence.start_line,
                            evidence.end_line,
                        ),
                    ),
                ),
            ),
            summary="One source-bound proposed finding.",
        )
        return AlphaReviewProviderResult(
            proposal=proposal,
            provider_output_digest=json_digest(alpha_review_proposal_payload(proposal)),
            profile_id="alpha-review",
            adapter_id="recorded-reviewer",
            model_id="review-model",
            input_tokens=200,
            output_tokens=40,
            latency_ms=20,
            cost_microusd=2,
            completed_at=NOW,
        )


class FailingReviewer:
    def __init__(self) -> None:
        self.calls: list[AlphaReviewProviderCall] = []

    def review(self, call: AlphaReviewProviderCall) -> AlphaReviewProviderResult:
        self.calls.append(call)
        raise RuntimeError("sensitive reviewer failure")


class CrashingReviewer:
    def __init__(self) -> None:
        self.calls: list[AlphaReviewProviderCall] = []

    def review(self, call: AlphaReviewProviderCall) -> AlphaReviewProviderResult:
        self.calls.append(call)
        raise KeyboardInterrupt


def test_review_worker_persists_context_dispatch_proposal_provider_and_admission(
    tmp_path: Path,
) -> None:
    execution, events, artifacts, repository, isolation, _, _, _ = _completed_writer(tmp_path)
    scheduler = AlphaReviewRuntimeService(events)
    reviewer = RecordingReviewer()
    worker = _worker(execution, scheduler, artifacts, reviewer)

    result = worker.run_once()

    assert result.status == "review-succeeded"
    assert result.run_id == "run-1"
    assert result.finding_count == 1
    assert result.admitted_artifact_digest is not None
    state = scheduler.inspect("run-1")
    assert state is not None
    assert state.status is AlphaReviewLifecycleStatus.SUCCEEDED
    assert state.finding_count == 1
    assert state.admitted_artifact_digest == result.admitted_artifact_digest

    stream = events.read_stream(alpha_review_stream("run-1"))
    assert tuple(event.event_type for event in stream[1:]) == (
        ALPHA_REVIEW_PROVIDER_DISPATCH_STARTED,
        ALPHA_REVIEW_SUCCEEDED,
    )
    dispatch = stream[1]
    call = reviewer.calls[0]
    assert call.request_id == alpha_review_provider_request_id(state.lease.digest)
    assert call.causation_id == dispatch.event_id
    assert call.context.digest == dispatch.payload["context_digest"]
    assert call.context.acceptance.digest == dispatch.payload["acceptance_digest"]

    context = _json_object(artifacts.get_json(call.context.digest))
    proposal = _json_object(artifacts.get_json(state.proposal_artifact_digest or ""))
    provider = _json_object(artifacts.get_json(state.provider_result_artifact_digest or ""))
    admitted = _json_object(artifacts.get_json(state.admitted_artifact_digest or ""))
    assert context["schema_version"] == "alpha-review-context/v1"
    assert proposal["schema_version"] == "alpha-review-proposal/v1"
    assert provider["schema_version"] == "alpha-review-provider-result/v1"
    assert provider["proposal_digest"] == state.proposal_artifact_digest
    assert admitted["schema_version"] == "alpha-admitted-review/v1"
    assert admitted["acceptance_digest"] == call.context.acceptance.digest
    assert "approved" not in admitted
    assert "verified" not in admitted

    assert worker.run_once().status == "idle"
    reopened_execution = AlphaRuntimeApiService(
        EventStore(events.path),
        repository,
        isolation_root=isolation,
        artifacts=ArtifactStore(artifacts.root, database_path=events.path),
    )
    reopened = _worker(
        reopened_execution,
        AlphaReviewRuntimeService(EventStore(events.path)),
        ArtifactStore(artifacts.root, database_path=events.path),
        RecordingReviewer(),
    )
    assert reopened.run_once().status == "idle"


def test_review_worker_renews_after_preparation_and_preserves_completion_reserve(
    tmp_path: Path,
) -> None:
    execution, events, artifacts, _, _, _, _, _ = _completed_writer(tmp_path)
    reviewer = RecordingReviewer()
    times = iter((NOW, NOW + timedelta(seconds=15), NOW + timedelta(seconds=16)))
    worker = AlphaReviewWorker(
        execution=execution,
        scheduler=AlphaReviewRuntimeService(events),
        artifacts=artifacts,
        reviewer=reviewer,
        policy=AlphaReviewWorkerPolicy(
            worker_id="reviewer-1",
            budget=GatewayBudget(20_000, 2_000, 180_000, 10_000),
            lease_seconds=210,
        ),
        clock=lambda: next(times),
    )

    result = worker.run_once()

    assert result.status == "review-succeeded"
    assert reviewer.calls[0].budget.max_latency_ms == 180_000
    state = AlphaReviewRuntimeService(events).inspect("run-1")
    assert state is not None
    assert state.lease.expires_at == NOW + timedelta(seconds=225)
    assert ALPHA_REVIEW_LEASE_RENEWED in tuple(
        event.event_type for event in events.read_stream(alpha_review_stream("run-1"))
    )
    with pytest.raises(ValueError, match="invalid alpha review worker policy"):
        AlphaReviewWorkerPolicy(
            worker_id="reviewer-1",
            budget=GatewayBudget(20_000, 2_000, 180_000, 10_000),
            lease_seconds=180,
        )


def test_review_worker_renews_after_preparation_outlasts_the_original_lease(
    tmp_path: Path,
) -> None:
    execution, events, artifacts, _, _, _, _, _ = _completed_writer(tmp_path)
    reviewer = RecordingReviewer()
    times = iter((NOW, NOW + timedelta(seconds=211), NOW + timedelta(seconds=212)))
    worker = AlphaReviewWorker(
        execution=execution,
        scheduler=AlphaReviewRuntimeService(events),
        artifacts=artifacts,
        reviewer=reviewer,
        policy=AlphaReviewWorkerPolicy(
            worker_id="reviewer-1",
            budget=GatewayBudget(20_000, 2_000, 180_000, 10_000),
            lease_seconds=210,
        ),
        clock=lambda: next(times),
    )

    result = worker.run_once()

    assert result.status == "review-succeeded"
    assert reviewer.calls[0].budget.max_latency_ms == 180_000
    event_types = tuple(
        event.event_type for event in events.read_stream(alpha_review_stream("run-1"))
    )
    assert event_types[1:] == (
        ALPHA_REVIEW_LEASE_RENEWED,
        ALPHA_REVIEW_PROVIDER_DISPATCH_STARTED,
        ALPHA_REVIEW_SUCCEEDED,
    )


def test_review_worker_records_stable_preparation_provider_and_admission_failures(
    tmp_path: Path,
) -> None:
    preparation_root = tmp_path / "preparation"
    preparation_root.mkdir()
    execution, events, artifacts, _, _, outcome, _, _ = _completed_writer(preparation_root)
    artifacts.path_for(_linked_digest(outcome, "context_artifact")).write_bytes(b"tampered")
    preparation_times = iter((NOW, NOW + timedelta(seconds=301)))
    preparation = AlphaReviewWorker(
        execution=execution,
        scheduler=AlphaReviewRuntimeService(events),
        artifacts=artifacts,
        reviewer=RecordingReviewer(),
        policy=AlphaReviewWorkerPolicy(
            worker_id="reviewer-1",
            budget=GatewayBudget(20_000, 2_000, 30_000, 10_000),
        ),
        clock=lambda: next(preparation_times),
    ).run_once()
    assert preparation.status == "review-failed"
    assert preparation.failure_code == "alpha-review-artifacts-not-verified"
    preparation_state = AlphaReviewRuntimeService(events).inspect("run-1")
    assert preparation_state is not None
    assert preparation_state.status is AlphaReviewLifecycleStatus.FAILED

    provider_root = tmp_path / "provider"
    provider_root.mkdir()
    execution, events, artifacts, _, _, _, _, _ = _completed_writer(provider_root)
    failing = FailingReviewer()
    provider = _worker(
        execution,
        AlphaReviewRuntimeService(events),
        artifacts,
        failing,
    ).run_once()
    assert provider.status == "review-failed"
    assert provider.failure_code == "alpha-review-provider-failed"
    assert "sensitive" not in repr(provider)
    assert events.read_stream(alpha_review_stream("run-1"))[-1].event_type == ALPHA_REVIEW_FAILED

    admission_root = tmp_path / "admission"
    admission_root.mkdir()
    execution, events, artifacts, _, _, _, _, _ = _completed_writer(admission_root)
    admission = _worker(
        execution,
        AlphaReviewRuntimeService(events),
        artifacts,
        RecordingReviewer(context_mismatch=True),
    ).run_once()
    assert admission.status == "review-failed"
    assert admission.failure_code == "alpha-review-admission-rejected"


def test_review_worker_restart_does_not_repeat_completed_or_dispatched_review(
    tmp_path: Path,
) -> None:
    execution, events, artifacts, _, _, _, _, _ = _completed_writer(tmp_path)
    scheduler = AlphaReviewRuntimeService(events)
    crashing = CrashingReviewer()
    worker = _worker(execution, scheduler, artifacts, crashing)

    with pytest.raises(KeyboardInterrupt):
        worker.run_once()
    assert len(crashing.calls) == 1
    dispatched = scheduler.inspect("run-1")
    assert dispatched is not None
    assert dispatched.status is AlphaReviewLifecycleStatus.PROVIDER_DISPATCHED

    restarted_reviewer = RecordingReviewer()
    restarted = _worker(
        execution,
        AlphaReviewRuntimeService(EventStore(events.path)),
        artifacts,
        restarted_reviewer,
    )
    assert restarted.run_once().status == "idle"
    assert restarted_reviewer.calls == []

    report = AlphaReviewRuntimeService(EventStore(events.path)).reconcile(
        principal_id="review-supervisor"
    )
    assert report.ambiguous_run_ids == ("run-1",)
    assert events.read_stream(alpha_review_stream("run-1"))[-1].event_type == (
        ALPHA_REVIEW_RECONCILIATION_REQUIRED
    )


def test_review_worker_ports_exclude_execution_and_worktree_authority() -> None:
    assert tuple(item.name for item in fields(AlphaReviewWorker)) == (
        "execution",
        "scheduler",
        "artifacts",
        "reviewer",
        "policy",
        "clock",
    )
    source = AlphaReviewWorker.__dict__
    for forbidden in (
        "change_executor",
        "acceptance",
        "worktrees",
        "shell",
        "network",
    ):
        assert forbidden not in source


def test_review_candidate_snapshot_survives_successful_worktree_cleanup(
    tmp_path: Path,
) -> None:
    execution, _, _, _, _, _, _, _ = _completed_writer(tmp_path)
    before = execution.review_candidates()

    maintenance = execution.maintain_successful_worktrees(
        max_retained=0,
        principal_id="review-supervisor",
    )
    after = execution.review_candidates()

    assert maintenance.cleaned == 1
    assert before == after
    context = execution.prepare_review_context(after[0])
    assert context.state_digest == after[0].state_digest
    assert context.artifact_evidence_digest == after[0].artifact_evidence_digest


def _worker(
    execution: AlphaRuntimeApiService,
    scheduler: AlphaReviewRuntimeService,
    artifacts: ArtifactStore,
    reviewer: AlphaReviewerPort,
) -> AlphaReviewWorker:
    return AlphaReviewWorker(
        execution=execution,
        scheduler=scheduler,
        artifacts=artifacts,
        reviewer=reviewer,
        policy=AlphaReviewWorkerPolicy(
            worker_id="reviewer-1",
            budget=GatewayBudget(20_000, 2_000, 30_000, 10_000),
        ),
        clock=lambda: NOW,
    )


def _json_object(value: object) -> Mapping[str, object]:
    assert isinstance(value, dict)
    assert all(isinstance(key, str) for key in value)
    return cast("Mapping[str, object]", value)
