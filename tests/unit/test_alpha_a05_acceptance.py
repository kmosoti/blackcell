from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import cast

from blackcell.bootstrap.alpha_review_runtime import AlphaReviewRuntimeService
from blackcell.bootstrap.alpha_runtime import AlphaRuntimeApiService
from blackcell.bootstrap.alpha_verify_runtime import AlphaVerificationRuntimeService
from blackcell.bootstrap.alpha_verify_source import AlphaVerificationSourceService
from blackcell.bootstrap.alpha_verify_worker import DeterministicAlphaVerifier
from blackcell.kernel import ArtifactStore, EventStore
from blackcell.kernel._json import json_digest
from blackcell.orchestration.alpha_review import (
    AlphaReviewFindingCategory,
    AlphaReviewProviderCall,
    AlphaReviewProviderResult,
    alpha_review_proposal_payload,
)
from blackcell.orchestration.alpha_review_lifecycle import AlphaReviewLifecycleStatus
from blackcell.orchestration.alpha_verify import AlphaVerificationStatus
from blackcell.orchestration.alpha_verify_lifecycle import AlphaVerificationLifecycleStatus
from tests.unit.test_alpha_replay import _completed_writer
from tests.unit.test_alpha_review_worker import (
    FailingReviewer,
    RecordingReviewer,
)
from tests.unit.test_alpha_review_worker import (
    _worker as review_worker,
)
from tests.unit.test_alpha_verify_worker import (
    ClearReviewer,
    CrashingVerifier,
)
from tests.unit.test_alpha_verify_worker import (
    _worker as verify_worker,
)


class HiddenShortcutReviewer(RecordingReviewer):
    def review(self, call: AlphaReviewProviderCall) -> AlphaReviewProviderResult:
        result = super().review(call)
        finding = replace(
            result.proposal.findings[0],
            category=AlphaReviewFindingCategory.HIDDEN_SHORTCUT,
            claim="The change may hardcode the accepted example instead of the required behavior.",
            impact="A narrow shortcut could pass the declared check without satisfying the intent.",
            recommendation=(
                "Reject the shortcut until the cited implementation satisfies the intent."
            ),
        )
        proposal = replace(
            result.proposal,
            findings=(finding,),
            summary="One source-cited hidden-shortcut finding.",
        )
        return replace(
            result,
            proposal=proposal,
            provider_output_digest=json_digest(alpha_review_proposal_payload(proposal)),
        )


def test_a05_hidden_shortcut_finding_deterministically_fails_and_replays(
    tmp_path: Path,
) -> None:
    execution, events, artifacts, repository, isolation, _, _, _ = _completed_writer(tmp_path)
    review = review_worker(
        execution,
        AlphaReviewRuntimeService(events),
        artifacts,
        HiddenShortcutReviewer(),
    ).run_once()
    assert review.status == "review-succeeded"
    assert review.finding_count == 1
    source = AlphaVerificationSourceService(events, execution, artifacts)
    scheduler = AlphaVerificationRuntimeService(events)

    verification = verify_worker(
        source,
        scheduler,
        artifacts,
        DeterministicAlphaVerifier(),
    ).run_once()

    assert verification.status == "verification-completed"
    assert verification.verdict is AlphaVerificationStatus.FAIL
    state = scheduler.inspect("run-1")
    assert state is not None
    assert state.status is AlphaVerificationLifecycleStatus.COMPLETED
    assert state.verdict is AlphaVerificationStatus.FAIL
    admitted = cast("dict[str, object]", artifacts.get_json(review.admitted_artifact_digest or ""))
    findings = admitted["findings"]
    assert isinstance(findings, list)
    assert cast("dict[str, object]", findings[0])["category"] == "hidden-shortcut"
    assert "approved" not in admitted
    assert "verified" not in admitted

    reopened_events = EventStore(events.path)
    replay = AlphaRuntimeApiService(
        reopened_events,
        repository,
        isolation_root=isolation,
        artifacts=ArtifactStore(artifacts.root, database_path=reopened_events.path),
    ).replay_run("run-1")
    assert replay.verification.lifecycle_status == "completed"
    assert replay.verification.verdict == "fail"
    assert replay.verification.artifact_integrity == "verified"
    assert replay.verification.finding_code is None


def test_a05_reviewer_and_verifier_errors_remain_durable_non_verdicts(
    tmp_path: Path,
) -> None:
    review_root = tmp_path / "reviewer-error"
    review_root.mkdir()
    execution, events, artifacts, repository, isolation, _, _, _ = _completed_writer(review_root)
    review = review_worker(
        execution,
        AlphaReviewRuntimeService(events),
        artifacts,
        FailingReviewer(),
    ).run_once()
    assert review.status == "review-failed"
    assert review.failure_code == "alpha-review-provider-failed"

    restarted_events = EventStore(events.path)
    review_state = AlphaReviewRuntimeService(restarted_events).inspect("run-1")
    assert review_state is not None
    assert review_state.status is AlphaReviewLifecycleStatus.FAILED
    assert review_state.failure_code == "alpha-review-provider-failed"
    replacement = RecordingReviewer()
    restarted_execution = AlphaRuntimeApiService(
        restarted_events,
        repository,
        isolation_root=isolation,
        artifacts=ArtifactStore(artifacts.root, database_path=restarted_events.path),
    )
    assert (
        review_worker(
            restarted_execution,
            AlphaReviewRuntimeService(restarted_events),
            ArtifactStore(artifacts.root, database_path=restarted_events.path),
            replacement,
        )
        .run_once()
        .status
        == "idle"
    )
    assert replacement.calls == []
    assert restarted_execution.replay_run("run-1").verification.lifecycle_status == "not-started"

    verify_root = tmp_path / "verifier-error"
    verify_root.mkdir()
    execution, events, artifacts, repository, isolation, _, _, _ = _completed_writer(verify_root)
    review = review_worker(
        execution,
        AlphaReviewRuntimeService(events),
        artifacts,
        ClearReviewer(),
    ).run_once()
    assert review.status == "review-succeeded"
    source = AlphaVerificationSourceService(events, execution, artifacts)
    verification = verify_worker(
        source,
        AlphaVerificationRuntimeService(events),
        artifacts,
        CrashingVerifier(),
    ).run_once()
    assert verification.status == "verification-error"
    assert verification.failure_code == "alpha-verifier-failed"

    restarted_events = EventStore(events.path)
    verification_state = AlphaVerificationRuntimeService(restarted_events).inspect("run-1")
    assert verification_state is not None
    assert verification_state.status is AlphaVerificationLifecycleStatus.FAILED
    assert verification_state.verdict is None
    replay = AlphaRuntimeApiService(
        restarted_events,
        repository,
        isolation_root=isolation,
        artifacts=ArtifactStore(artifacts.root, database_path=restarted_events.path),
    ).replay_run("run-1")
    assert replay.verification.lifecycle_status == "verifier-error"
    assert replay.verification.verdict is None
    assert replay.verification.failure_code == "alpha-verifier-failed"
    assert replay.verification.artifact_integrity == "not-applicable"
