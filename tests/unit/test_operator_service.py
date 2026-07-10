from __future__ import annotations

import subprocess
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Never

import pytest

from blackcell.control import ActionArgument, ActionProposal, PolicyDecision
from blackcell.domains.repository import ClaimCorrection
from blackcell.models import DecisionResult, ModelInvocation
from blackcell.operator import (
    ACTION_OBSERVED,
    RUN_COMPLETED,
    RUN_FAILED,
    TRACE_RECORDED,
    OperatorRunStatus,
    RepositoryOperator,
)

_NOW = datetime(2026, 7, 9, 12, tzinfo=UTC)


class _UnsafeModel:
    @property
    def name(self) -> str:
        return "unsafe-fixture"

    def decide(
        self,
        context_frame: Mapping[str, Any],
        *,
        output_schema: Mapping[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> DecisionResult[ActionProposal]:
        del output_schema
        proposal = ActionProposal(
            proposal_id="proposal:unsafe",
            context_frame_id=str(context_frame["frame_id"]),
            affordance="write_file",
            arguments=(),
            expected_effects=(),
            rationale="Attempt an undeclared mutation.",
        )
        return DecisionResult(
            proposal,
            ModelInvocation(
                provider="fixture",
                model=None,
                invocation_id=correlation_id or "fixture",
                replayed=True,
                duration_ms=0.0,
            ),
        )


class _ExplodingModel:
    @property
    def name(self) -> str:
        return "exploding"

    def decide(self, *args: object, **kwargs: object) -> DecisionResult[ActionProposal]:
        raise AssertionError("historical replay called the model")


class _ExplodingExecutor:
    def execute(self, proposal: ActionProposal, decision: PolicyDecision) -> Never:
        del proposal, decision
        raise AssertionError("denied action or replay reached the executor")


class _FailingModel:
    @property
    def name(self) -> str:
        return "failing"

    def decide(self, *args: object, **kwargs: object) -> DecisionResult[ActionProposal]:
        raise RuntimeError("fixture model failure")


class _MalformedModel:
    @property
    def name(self) -> str:
        return "malformed"

    def decide(
        self,
        context_frame: Mapping[str, Any],
        *,
        output_schema: Mapping[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> DecisionResult[ActionProposal]:
        del output_schema
        proposal = ActionProposal(
            proposal_id="proposal:malformed",
            context_frame_id=str(context_frame["frame_id"]),
            affordance="git_status",
            arguments=(ActionArgument("path", "README.md"),),
            expected_effects=(),
            rationale="Use an argument that the trusted contract does not allow.",
        )
        return DecisionResult(
            proposal,
            ModelInvocation(
                provider="fixture",
                model=None,
                invocation_id=correlation_id or "fixture",
                replayed=True,
                duration_ms=0.0,
            ),
        )


class _DeclaredCheckModel:
    @property
    def name(self) -> str:
        return "declared-check-fixture"

    def decide(
        self,
        context_frame: Mapping[str, Any],
        *,
        output_schema: Mapping[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> DecisionResult[ActionProposal]:
        del output_schema
        proposal = ActionProposal(
            proposal_id="proposal:declared-check",
            context_frame_id=str(context_frame["frame_id"]),
            affordance="run_check",
            arguments=(ActionArgument("check", "unit"),),
            expected_effects=(),
            rationale="Run a developer-declared check.",
        )
        return DecisionResult(
            proposal,
            ModelInvocation(
                provider="fixture",
                model=None,
                invocation_id=correlation_id or "fixture",
                replayed=True,
                duration_ms=0.0,
            ),
        )


def test_repository_operator_completes_and_persists_the_whole_loop(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path)
    operator = _operator(repo)

    result = operator.run()

    assert result.status is OperatorRunStatus.COMPLETED
    assert result.policy.outcome.value == "allow"
    assert result.execution is not None and result.execution.success
    assert result.evaluation.passed
    assert result.run_event_count == 11
    assert result.trace_span_count == 9
    assert len(result.artifacts.digests()) == 9
    assert all(operator.artifacts.verify(digest) for digest in result.artifacts.digests())

    context = operator.context(result.run_id)
    replay = operator.replay(result.run_id)
    state = operator.current_state()
    assert context.frame_id == result.context_frame_id
    assert context.payload["rendered_context"].startswith("objective:")
    assert context.payload["affordance_contracts"] == [
        "git_status() [read-only; effect=repository-observation]",
        "inspect_file(path:string, max_bytes?:integer) [read-only; effect=file-inspection]",
    ]
    assert replay.status == "completed"
    assert replay.event_count == result.run_event_count
    assert replay.projection_hash_match
    assert replay.events[-1].event_type == RUN_COMPLETED
    assert replay.artifacts and all(artifact.verified for artifact in replay.artifacts)
    assert state.claims
    assert all(
        operator.events.get(evidence.event_id) is not None
        for claim in state.claims
        for evidence in claim.evidence
    )


def test_undeclared_affordance_is_denied_without_execution(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    operator = _operator(
        repo,
        model=_UnsafeModel(),
        executor=_ExplodingExecutor(),
    )

    result = operator.run()

    assert result.status is OperatorRunStatus.DENIED
    assert result.policy.outcome.value == "deny"
    assert result.execution is None
    assert result.evaluation.passed
    assert any(finding.code == "undeclared_affordance" for finding in result.policy.findings)
    assert ACTION_OBSERVED not in {
        event.event_type for event in operator.replay(result.run_id).events
    }


def test_repeated_observations_are_distinct_occurrences(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    operator = _operator(repo)

    first = operator.run()
    first_events = operator.events.read_stream(operator.repository_stream_id)
    second = operator.run()
    all_events = operator.events.read_stream(operator.repository_stream_id)

    assert first.run_id != second.run_id
    assert len(all_events) == len(first_events) * 2
    assert len({event.event_id for event in all_events}) == len(all_events)
    assert [event.stream_sequence for event in all_events] == list(range(1, len(all_events) + 1))


def test_historical_replay_never_calls_observer_model_or_executor(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    completed = _operator(repo).run()

    def exploding_observer(*args: object, **kwargs: object) -> tuple[()]:
        raise AssertionError("historical replay called the observer")

    replay_only = _operator(
        repo,
        model=_ExplodingModel(),
        executor=_ExplodingExecutor(),
        observer=exploding_observer,
    )

    replay = replay_only.replay(completed.run_id)

    assert replay.status == "completed"
    assert replay.event_count == completed.run_event_count
    assert replay.projection_hash_match


def test_failed_run_records_a_replayable_terminal_event(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    operator = _operator(repo, model=_FailingModel())

    with pytest.raises(RuntimeError, match="fixture model failure"):
        operator.run()

    replay = operator.replay()
    assert replay.status == "failed"
    assert replay.events[-1].event_type == RUN_FAILED
    assert TRACE_RECORDED in {event.event_type for event in replay.events}
    assert replay.artifacts[-1].verified


def test_human_correction_is_persisted_as_new_repository_evidence(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    operator = _operator(repo)
    result = operator.run()
    original = next(
        claim
        for claim in operator.current_state().claims
        if claim.subject == "path:README.md" and claim.predicate == "present"
    )
    replacement = replace(
        original,
        claim_id="claim:human-correction",
        value=False,
        observed_at=_NOW,
        effective_at=_NOW,
    )

    stored = operator.append_correction(
        ClaimCorrection(
            correction_id="correction:readme-removed",
            supersedes_claim_ids=(original.claim_id,),
            replacement=replacement,
            effective_at=_NOW,
            reason="Human correction: README is no longer a required path.",
        )
    )

    state = operator.current_state()
    assert stored.event_type == "repository.correction-recorded"
    assert original.claim_id not in {claim.claim_id for claim in state.claims}
    assert state.find_claims("path:README.md", "present")[0].value is False
    assert result.run_id


def test_live_projection_uses_wall_time_for_freshness(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    operator = _operator(repo)
    operator.run()

    state = operator.current_state(as_of_time=_NOW + timedelta(minutes=6))

    assert state.find_claims("repository", "git.clean")[0].is_expired(state.as_of_time)
    assert not any(
        claim.subject == "repository" and claim.predicate == "git.clean"
        for claim in state.current_claims
    )


def test_default_storage_is_inside_git_metadata_not_the_observed_worktree(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    operator = RepositoryOperator(repo, clock=lambda: _NOW)

    result = operator.run()

    assert result.status is OperatorRunStatus.COMPLETED
    assert operator.database_path == repo / ".git" / "blackcell" / "kernel.sqlite3"
    assert operator.database_path.is_file()
    assert not (repo / ".blackcell").exists()


def test_run_lookup_is_scoped_to_its_repository_when_database_is_shared(tmp_path: Path) -> None:
    left = _repository(tmp_path, name="left")
    right = _repository(tmp_path, name="right")
    database = tmp_path / "shared" / "kernel.sqlite3"
    left_operator = RepositoryOperator(left, database_path=database, clock=lambda: _NOW)
    right_operator = RepositoryOperator(right, database_path=database, clock=lambda: _NOW)
    left_run = left_operator.run()
    right_run = right_operator.run()

    assert left_operator.context().run_id == left_run.run_id
    assert right_operator.context().run_id == right_run.run_id
    with pytest.raises(LookupError, match="does not belong"):
        left_operator.replay(right_run.run_id)


def test_invalid_repository_is_rejected_before_any_storage_is_created(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    with pytest.raises(ValueError, match="does not exist"):
        RepositoryOperator(missing)

    assert not missing.exists()


def test_malformed_declared_affordance_arguments_are_denied_at_the_gate(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    result = _operator(repo, model=_MalformedModel(), executor=_ExplodingExecutor()).run()

    assert result.status is OperatorRunStatus.DENIED
    assert any(finding.code == "unexpected_argument" for finding in result.policy.findings)


def test_declared_check_requires_explicit_approval_before_execution(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    operator = RepositoryOperator(
        repo,
        database_path=repo / ".blackcell" / "kernel.sqlite3",
        model=_DeclaredCheckModel(),
        executor=_ExplodingExecutor(),
        check_commands={"unit": ("python", "-m", "pytest")},
        clock=lambda: _NOW,
    )

    result = operator.run()

    assert result.status is OperatorRunStatus.DENIED
    assert result.policy.outcome.value == "require_approval"
    assert any(finding.code == "approval_required" for finding in result.policy.findings)
    assert result.execution is None


def _operator(
    repo: Path,
    *,
    model: object | None = None,
    executor: object | None = None,
    observer: object | None = None,
) -> RepositoryOperator:
    kwargs: dict[str, Any] = {
        "database_path": repo / ".blackcell" / "kernel.sqlite3",
        "clock": lambda: _NOW,
    }
    if model is not None:
        kwargs["model"] = model
    if executor is not None:
        kwargs["executor"] = executor
    if observer is not None:
        kwargs["observer"] = observer
    return RepositoryOperator(repo, **kwargs)


def _repository(tmp_path: Path, *, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    (repo / "README.md").write_text("# Fixture\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "tests").mkdir()
    subprocess.run(
        ["git", "init", "--quiet"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "add", "."],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.test",
            "commit",
            "-qm",
            "initial",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return repo
