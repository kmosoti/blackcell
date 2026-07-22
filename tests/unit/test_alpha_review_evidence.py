from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import blackcell.orchestration.alpha_replay as alpha_replay
from blackcell.kernel import ArtifactStore
from blackcell.kernel._json import json_digest
from blackcell.orchestration.alpha_replay import (
    AlphaReviewEvidenceError,
    AlphaReviewEvidenceFailureCode,
    build_alpha_review_context_from_artifacts,
)
from blackcell.orchestration.alpha_review import AlphaReviewContext
from tests.unit.test_alpha_replay import (
    _completed_writer,
    _expectation,
    _linked_digest,
)


def test_review_evidence_builds_exact_acceptance_and_all_verified_excerpts(
    tmp_path: Path,
) -> None:
    runtime, _, artifacts, _, _, outcome, _, _ = _completed_writer(tmp_path)
    expectation = _expectation(outcome, result_digest=json_digest(outcome))
    replay = runtime.replay_run("run-1")

    context = _build(artifacts, replay.state_digest, expectation)
    repeated = _build(artifacts, replay.state_digest, expectation)

    assert context == repeated
    assert context.artifact_evidence_digest == replay.artifact_evidence_digest
    assert context.state_digest == replay.state_digest
    assert context.acceptance.run_id == "run-1"
    assert context.acceptance.project_id == "project-1"
    assert context.acceptance.intent_id == "intent-1"
    assert context.acceptance.plan_id == "plan-1"
    assert context.acceptance.objective == "Apply and verify one bounded alpha change."
    assert context.acceptance.constraints == ("Only change the admitted file.",)
    assert context.acceptance.base_commit == expectation.base_commit

    node = context.acceptance.nodes[0]
    check = node.checks[0]
    assert node.node_id == expectation.node_id
    assert node.depends_on == expectation.depends_on
    assert node.effects == expectation.effects
    assert node.allowed_paths == expectation.allowed_paths
    assert check.argv == expectation.checks[0].argv
    assert check.expected_exit_code == expectation.checks[0].expected_exit_code
    assert check.passed is True

    artifacts_by_role = {
        (artifact.role, artifact.check_id): artifact.digest for artifact in replay.artifacts
    }
    assert check.command_digest == artifacts_by_role[("check-command", "write-check")]
    assert check.result_digest == artifacts_by_role[("check-result", "write-check")]

    by_kind = {item.kind.value: item for item in context.evidence}
    assert {
        "outcome",
        "source-before",
        "source-after",
        "effect",
        "check-command",
        "check-result",
        "check-stdout",
        "check-stderr",
    }.issubset(by_kind)
    assert by_kind["source-before"].path == "src/value.py"
    assert by_kind["source-before"].excerpt == "VALUE = 1\n"
    assert by_kind["source-after"].path == "src/value.py"
    assert by_kind["source-after"].excerpt == "VALUE = 2\n"
    assert '"changed_paths":["src/value.py"]' in by_kind["effect"].excerpt
    assert by_kind["check-stdout"].excerpt == "write-check\n"
    assert by_kind["check-command"].check_id == "write-check"
    assert by_kind["check-result"].check_id == "write-check"
    assert "repository_root" not in repr(context.evidence)
    assert "worktree_path" not in repr(context.evidence)


def test_review_evidence_rejects_tamper_definition_drift_and_non_success(
    tmp_path: Path,
) -> None:
    runtime, _, artifacts, _, _, outcome, _, _ = _completed_writer(tmp_path)
    expectation = _expectation(outcome, result_digest=json_digest(outcome))
    state_digest = runtime.replay_run("run-1").state_digest

    with pytest.raises(AlphaReviewEvidenceError) as drift:
        build_alpha_review_context_from_artifacts(
            artifacts,
            run_id="run-1",
            project_id="project-1",
            intent_id="intent-1",
            plan_id="plan-1",
            objective="Apply and verify one bounded alpha change.",
            constraints=("Changed after acceptance.",),
            base_commit=expectation.base_commit or "",
            state_digest=state_digest,
            nodes=(expectation,),
        )
    assert drift.value.code is AlphaReviewEvidenceFailureCode.DEFINITION_MISMATCH

    with pytest.raises(AlphaReviewEvidenceError) as non_success:
        _build(
            artifacts,
            state_digest,
            replace(expectation, status="failed", failure_code="forced-failure"),
        )
    assert non_success.value.code is AlphaReviewEvidenceFailureCode.ARTIFACTS_NOT_VERIFIED

    context_digest = _linked_digest(outcome, "context_artifact")
    artifacts.path_for(context_digest).write_bytes(b"tampered")
    with pytest.raises(AlphaReviewEvidenceError) as tampered:
        _build(artifacts, state_digest, expectation)
    assert tampered.value.code is AlphaReviewEvidenceFailureCode.ARTIFACTS_NOT_VERIFIED


def test_review_evidence_fails_closed_on_excerpt_and_aggregate_budgets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    large_root = tmp_path / "large"
    large_root.mkdir()
    runtime, _, artifacts, _, _, outcome, _, _ = _completed_writer(
        large_root,
        source_content="VALUE = 1\n" + "#" * (32 * 1024),
    )
    expectation = _expectation(outcome, result_digest=json_digest(outcome))
    state_digest = runtime.replay_run("run-1").state_digest
    with pytest.raises(AlphaReviewEvidenceError) as oversized:
        _build(artifacts, state_digest, expectation)
    assert oversized.value.code is AlphaReviewEvidenceFailureCode.EVIDENCE_BUDGET_EXCEEDED

    aggregate_root = tmp_path / "aggregate"
    aggregate_root.mkdir()
    clean_runtime, _, clean_artifacts, _, _, clean_outcome, _, _ = _completed_writer(aggregate_root)
    clean_expectation = _expectation(
        clean_outcome,
        result_digest=json_digest(clean_outcome),
    )
    clean_state_digest = clean_runtime.replay_run("run-1").state_digest
    monkeypatch.setattr(alpha_replay, "_MAX_REVIEW_EVIDENCE_BYTES", 1)
    with pytest.raises(AlphaReviewEvidenceError) as aggregate:
        _build(clean_artifacts, clean_state_digest, clean_expectation)
    assert aggregate.value.code is AlphaReviewEvidenceFailureCode.EVIDENCE_BUDGET_EXCEEDED


def _build(
    artifacts: ArtifactStore,
    state_digest: str,
    expectation: alpha_replay.AlphaReplayNodeExpectation,
) -> AlphaReviewContext:
    return build_alpha_review_context_from_artifacts(
        artifacts,
        run_id="run-1",
        project_id="project-1",
        intent_id="intent-1",
        plan_id="plan-1",
        objective="Apply and verify one bounded alpha change.",
        constraints=("Only change the admitted file.",),
        base_commit=expectation.base_commit or "",
        state_digest=state_digest,
        nodes=(expectation,),
    )
