from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

import blackcell.orchestration.alpha_replay as alpha_replay
from blackcell.adapters.execution.worktree import WorktreeExecutionSpec
from blackcell.kernel import ArtifactStore
from blackcell.kernel._json import json_digest
from blackcell.orchestration.alpha_acceptance import (
    MAX_ALPHA_ACCEPTANCE_STREAM_BYTES,
    AlphaAcceptanceCommand,
    AlphaAcceptanceResult,
    AlphaAcceptanceStream,
)
from blackcell.orchestration.alpha_changes import AlphaTextOperation
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
from tests.unit.test_alpha_worker import RecordingAcceptance

_TRUNCATION_MARKER = "\n...[truncated; complete artifact retained by digest]\n"


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
    assert by_kind["source-before"].operation is AlphaTextOperation.REPLACE
    assert by_kind["source-before"].excerpt == "VALUE = 1\n"
    assert by_kind["source-after"].path == "src/value.py"
    assert by_kind["source-after"].operation is AlphaTextOperation.REPLACE
    assert by_kind["source-after"].excerpt == "VALUE = 2\n"
    assert by_kind["effect"].operation is AlphaTextOperation.REPLACE
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


def test_review_evidence_truncates_oversized_artifacts_and_enforces_aggregate_budget(
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
    context = _build(artifacts, state_digest, expectation)
    source_before = next(item for item in context.evidence if item.kind.value == "source-before")
    assert len(source_before.excerpt.encode("utf-8")) <= 32 * 1024
    assert source_before.excerpt.endswith(
        "\n...[truncated; complete artifact retained by digest]\n"
    )
    assert source_before.artifact_digest == _linked_digest(outcome, "context_artifact")

    original_run = RecordingAcceptance.run

    def run_with_large_binary_stdout(
        self: RecordingAcceptance,
        command: AlphaAcceptanceCommand,
        spec: WorktreeExecutionSpec,
        *,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> AlphaAcceptanceResult:
        result = original_run(
            self,
            command,
            spec,
            cancel_requested=cancel_requested,
        )
        return replace(result, stdout=AlphaAcceptanceStream(b"\xff" * (64 * 1024)))

    with monkeypatch.context() as patch:
        patch.setattr(RecordingAcceptance, "run", run_with_large_binary_stdout)
        binary_root = tmp_path / "binary"
        binary_root.mkdir()
        binary_runtime, _, binary_artifacts, _, _, binary_outcome, _, _ = _completed_writer(
            binary_root
        )
        binary_expectation = _expectation(
            binary_outcome,
            result_digest=json_digest(binary_outcome),
        )
        binary_context = _build(
            binary_artifacts,
            binary_runtime.replay_run("run-1").state_digest,
            binary_expectation,
        )

    stdout = next(item for item in binary_context.evidence if item.kind.value == "check-stdout")
    assert stdout.excerpt.startswith("base64-prefix:")
    assert stdout.excerpt.endswith("\n...[truncated; complete artifact retained by digest]\n")
    assert len(stdout.excerpt.encode("utf-8")) <= 32 * 1024
    assert stdout.artifact_digest == AlphaAcceptanceStream(b"\xff" * (64 * 1024)).digest

    with monkeypatch.context() as patch:
        patch.setattr(alpha_replay, "_MAX_REVIEW_EVIDENCE_BYTES", 8 * 128)
        constrained = _build(artifacts, state_digest, expectation)
    assert sum(len(item.excerpt.encode("utf-8")) for item in constrained.evidence) <= 8 * 128
    assert any("[truncated;" in item.excerpt for item in constrained.evidence)

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


def test_review_evidence_replays_two_checks_at_full_stream_capacity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_run = RecordingAcceptance.run
    check_ids = ("capacity-one", "capacity-two")
    stream_digests: set[str] = set()

    def run_with_full_streams(
        self: RecordingAcceptance,
        command: AlphaAcceptanceCommand,
        spec: WorktreeExecutionSpec,
        *,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> AlphaAcceptanceResult:
        result = original_run(
            self,
            command,
            spec,
            cancel_requested=cancel_requested,
        )
        offset = check_ids.index(command.check_id) * 2
        stdout = AlphaAcceptanceStream(bytes((0xFC + offset,)) * MAX_ALPHA_ACCEPTANCE_STREAM_BYTES)
        stderr = AlphaAcceptanceStream(bytes((0xFD + offset,)) * MAX_ALPHA_ACCEPTANCE_STREAM_BYTES)
        stream_digests.update((stdout.digest, stderr.digest))
        return replace(result, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(RecordingAcceptance, "run", run_with_full_streams)
    runtime, _, artifacts, _, _, outcome, _, _ = _completed_writer(
        tmp_path,
        writer_check_ids=check_ids,
        stream_limit_bytes=MAX_ALPHA_ACCEPTANCE_STREAM_BYTES,
        artifact_quota_bytes=128 * 1024 * 1024,
    )
    expectation = _expectation(
        outcome,
        result_digest=json_digest(outcome),
        check_ids=check_ids,
    )

    context = _build(artifacts, runtime.replay_run("run-1").state_digest, expectation)

    streams = tuple(
        item for item in context.evidence if item.kind.value in {"check-stdout", "check-stderr"}
    )
    assert len(streams) == 4
    assert {item.artifact_digest for item in streams} == stream_digests
    assert all(item.excerpt.startswith("base64-prefix:") for item in streams)
    assert all(item.excerpt.endswith(_TRUNCATION_MARKER) for item in streams)
    assert all(len(item.excerpt.encode("utf-8")) <= 32 * 1024 for item in streams)


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
