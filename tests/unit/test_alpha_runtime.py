from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import msgspec
import pytest

from blackcell.bootstrap.alpha_runtime import AlphaRuntimeApiService
from blackcell.bootstrap.runtime_api import RuntimeApiService
from blackcell.interfaces.http import (
    AlphaAcceptanceCheck,
    AlphaCancelRunRequest,
    AlphaIntentRequest,
    AlphaNodeBudget,
    AlphaPlanNode,
    AlphaPlanRequest,
    AlphaProjectRequest,
    AlphaRunRequest,
    RuntimeApiError,
    RuntimeApiFailureCode,
)
from blackcell.kernel import EventEnvelope, EventStore

_CONFIGURATION_DIGEST = "sha256:" + ("a" * 64)
_OTHER_CONFIGURATION_DIGEST = "sha256:" + ("b" * 64)
_BASE_COMMIT = "b" * 40


def test_alpha_flow_is_idempotent_restart_safe_and_live_free(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    events = EventStore(tmp_path / "data" / "blackcell.sqlite3")
    service = AlphaRuntimeApiService(events, repository)

    project = service.register_project(_project(repository), principal_id="operator")
    intent = service.accept_intent(_intent(), principal_id="operator")
    plan = service.accept_plan(_plan(), principal_id="operator")
    run = service.submit_run(_run(), principal_id="operator")

    assert run.status == "queued"
    assert project.cursor < intent.cursor < plan.cursor < run.cursor
    assert service.submit_run(_run(), principal_id="operator") == run

    restarted = AlphaRuntimeApiService(EventStore(events.path), repository)
    assert restarted.register_project(_project(repository), principal_id="operator") == project
    assert restarted.accept_intent(_intent(), principal_id="operator") == intent
    assert restarted.accept_plan(_plan(), principal_id="operator") == plan
    assert restarted.inspect_run("run-1") == run

    first_replay = restarted.replay_run("run-1")
    second_replay = AlphaRuntimeApiService(EventStore(events.path), repository).replay_run("run-1")
    assert first_replay == second_replay
    assert first_replay.processed_events == 4
    assert first_replay.verification.lifecycle_status == "not-started"
    assert first_replay.verification.processed_events == 0
    assert first_replay.verification.artifact_integrity == "not-applicable"
    assert first_replay.intent.assumptions == ("The existing event ledger is reusable.",)
    assert first_replay.intent.unresolved_questions == ("Which executor is selected in A04?",)
    assert first_replay.plan.topological_order == ("inspect", "verify")
    assert len(events) == 4


def test_alpha_submission_rejects_mismatched_references_and_conflicts(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    service = AlphaRuntimeApiService(EventStore(tmp_path / "state.sqlite3"), repository)
    service.register_project(_project(repository), principal_id="operator")
    service.register_project(
        _project(repository, project_id="project-other"), principal_id="operator"
    )
    service.accept_intent(_intent(), principal_id="operator")

    mismatched = AlphaPlanRequest(
        schema_version="alpha-plan-request/v1",
        plan_id="plan-mismatch",
        project_id="project-other",
        intent_id="intent-1",
        base_commit=_BASE_COMMIT,
        allowed_effects=("repository-read", "process"),
        nodes=_nodes(),
        idempotency_key="plan-mismatch",
    )
    with pytest.raises(RuntimeApiError) as mismatch:
        service.accept_plan(mismatched, principal_id="operator")
    assert mismatch.value.code is RuntimeApiFailureCode.CONFLICT

    conflicting = AlphaProjectRequest(
        schema_version="alpha-project-request/v1",
        project_id="project-1",
        root=str(repository),
        configuration_provider="kernform",
        configuration_version="0.1.0",
        configuration_digest=_OTHER_CONFIGURATION_DIGEST,
        idempotency_key="project-1",
    )
    with pytest.raises(RuntimeApiError) as conflict:
        service.register_project(conflicting, principal_id="operator")
    assert conflict.value.code is RuntimeApiFailureCode.CONFLICT

    with pytest.raises(RuntimeApiError) as absent:
        service.submit_run(_run(), principal_id="operator")
    assert absent.value.code is RuntimeApiFailureCode.NOT_FOUND


def test_alpha_plan_acceptance_enforces_review_evidence_item_capacity(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    events = EventStore(tmp_path / "state.sqlite3")
    service = AlphaRuntimeApiService(events, repository)
    service.register_project(_project(repository), principal_id="operator")
    service.accept_intent(_intent(), principal_id="operator")
    base_node = _nodes()[0]

    def checks(count: int) -> tuple[AlphaAcceptanceCheck, ...]:
        return tuple(
            AlphaAcceptanceCheck(
                check_id=f"check-{index:02d}",
                argv=("python", "-m", "compileall", "src"),
            )
            for index in range(count)
        )

    exact_capacity_node = msgspec.structs.replace(
        base_node,
        budget=msgspec.structs.replace(base_node.budget, max_changed_files=41),
        effects=("repository-read", "repository-write", "process"),
        allowed_paths=("src",),
        checks=checks(1),
    )
    exact_capacity_plan = msgspec.structs.replace(
        _plan(),
        plan_id="plan-capacity",
        allowed_effects=("repository-read", "repository-write", "process"),
        nodes=(exact_capacity_node,),
        idempotency_key="plan-capacity",
    )
    accepted = service.accept_plan(exact_capacity_plan, principal_id="operator")
    assert accepted.plan_id == "plan-capacity"

    def plan(check_count: int, plan_id: str) -> AlphaPlanRequest:
        return msgspec.structs.replace(
            _plan(),
            plan_id=plan_id,
            nodes=(msgspec.structs.replace(base_node, checks=checks(check_count)),),
            idempotency_key=plan_id,
        )

    with pytest.raises(RuntimeApiError) as over_capacity:
        service.accept_plan(plan(32, "plan-over-capacity"), principal_id="operator")

    assert over_capacity.value.code is RuntimeApiFailureCode.INVALID_REQUEST
    assert events.read_stream("alpha:plan:plan-over-capacity") == ()

    aggregate_nodes = tuple(msgspec.structs.replace(node, checks=checks(16)) for node in _nodes())
    aggregate_plan = msgspec.structs.replace(
        _plan(),
        plan_id="plan-over-aggregate-capacity",
        nodes=aggregate_nodes,
        idempotency_key="plan-over-aggregate-capacity",
    )
    with pytest.raises(RuntimeApiError) as aggregate_capacity:
        service.accept_plan(aggregate_plan, principal_id="operator")
    assert aggregate_capacity.value.code is RuntimeApiFailureCode.INVALID_REQUEST
    assert events.read_stream("alpha:plan:plan-over-aggregate-capacity") == ()


def test_queued_cancellation_is_idempotent_and_replayed_without_live_work(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    events = EventStore(tmp_path / "state.sqlite3")
    service = AlphaRuntimeApiService(events, repository)
    service.register_project(_project(repository), principal_id="operator")
    service.accept_intent(_intent(), principal_id="operator")
    service.accept_plan(_plan(), principal_id="operator")
    service.submit_run(_run(), principal_id="operator")
    request = AlphaCancelRunRequest(
        schema_version="alpha-cancel-run-request/v1",
        idempotency_key="cancel-run-1",
    )

    canceled = service.cancel_run("run-1", request, principal_id="operator")
    restarted = AlphaRuntimeApiService(EventStore(events.path), repository)
    retried = restarted.cancel_run("run-1", request, principal_id="operator")

    assert retried == canceled
    assert canceled.status == "canceled"
    assert canceled.cancellation_requested
    assert canceled.active_node_id is None
    assert canceled.attempt == 0
    assert canceled.fencing_token == 0
    assert not canceled.retained_worktree
    replay = restarted.replay_run("run-1")
    assert replay.run == canceled
    assert replay.processed_events == 6
    assert tuple(event.event_type for event in events.read_stream("alpha:run:run-1")) == (
        "alpha.run.queued",
        "alpha.run.cancel-requested",
        "alpha.run.canceled",
    )
    assert len(events) == 6


def test_alpha_event_cursor_resumes_in_global_order(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    events = EventStore(tmp_path / "state.sqlite3")
    events.append(_legacy_event("legacy:one", "one"), expected_sequence=0)
    service = AlphaRuntimeApiService(events, repository)
    project = service.register_project(_project(repository), principal_id="operator")
    events.append(_legacy_event("legacy:two", "two"), expected_sequence=0)
    intent = service.accept_intent(_intent(), principal_id="operator")

    first = service.list_events(after_cursor=0, limit=2)
    assert tuple(item.event_id for item in first.events) == (project.event_id,)
    assert first.next_cursor == project.cursor
    assert first.scanned_events == 2
    assert first.has_more is True
    assert all(item.event_type.startswith("alpha.") for item in first.events)

    second = service.list_events(after_cursor=first.next_cursor, limit=2)
    assert tuple(item.event_id for item in second.events) == (intent.event_id,)
    assert second.next_cursor == intent.cursor
    assert second.scanned_events == 2
    assert second.has_more is False

    tail = service.list_events(after_cursor=second.next_cursor, limit=2)
    assert tail.events == ()
    assert tail.next_cursor == second.next_cursor


def test_runtime_api_alpha_submission_never_invokes_legacy_operator(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    events = EventStore(tmp_path / "state.sqlite3")
    operator = _LegacyOperatorTrap(repository, events.path)
    runtime = RuntimeApiService(
        cast(Any, operator),
        cast(Any, object()),
        events=events,
    )

    runtime.register_alpha_project(_project(repository), principal_id="operator")
    runtime.accept_alpha_intent(_intent(), principal_id="operator")
    runtime.accept_alpha_plan(_plan(), principal_id="operator")
    queued = runtime.submit_alpha_run(_run(), principal_id="operator")

    assert queued.status == "queued"
    assert operator.run_calls == 0
    assert runtime.replay_alpha_run("run-1").run == queued


class _LegacyOperatorTrap:
    def __init__(self, repo_root: Path, database_path: Path) -> None:
        self.repo_root = repo_root
        self.database_path = database_path
        self.run_calls = 0

    def run(self, **_: object) -> None:
        self.run_calls += 1
        raise AssertionError("legacy RepositoryOperator.run must not be called")


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    return repository.resolve()


def _project(repository: Path, *, project_id: str = "project-1") -> AlphaProjectRequest:
    return AlphaProjectRequest(
        schema_version="alpha-project-request/v1",
        project_id=project_id,
        root=str(repository),
        configuration_provider="kernform",
        configuration_version="0.1.0",
        configuration_digest=_CONFIGURATION_DIGEST,
        idempotency_key=project_id,
    )


def _intent() -> AlphaIntentRequest:
    return AlphaIntentRequest(
        schema_version="alpha-intent-request/v1",
        intent_id="intent-1",
        project_id="project-1",
        objective="Create a deterministic alpha contract.",
        constraints=("Do not invoke the legacy runtime.",),
        assumptions=("The existing event ledger is reusable.",),
        unresolved_questions=("Which executor is selected in A04?",),
        idempotency_key="intent-1",
    )


def _plan() -> AlphaPlanRequest:
    return AlphaPlanRequest(
        schema_version="alpha-plan-request/v1",
        plan_id="plan-1",
        project_id="project-1",
        intent_id="intent-1",
        base_commit=_BASE_COMMIT,
        allowed_effects=("repository-read", "process"),
        nodes=_nodes(),
        idempotency_key="plan-1",
    )


def _nodes() -> tuple[AlphaPlanNode, ...]:
    budget = AlphaNodeBudget(
        max_input_tokens=1_000,
        max_output_tokens=1_000,
        timeout_seconds=30,
        max_cost_microusd=0,
        max_changed_files=0,
    )
    return (
        AlphaPlanNode(
            node_id="inspect",
            objective="Inspect bounded source evidence.",
            depends_on=(),
            budget=budget,
            effects=("repository-read", "process"),
            allowed_paths=(),
            checks=(
                AlphaAcceptanceCheck(
                    check_id="inspect-pass",
                    argv=("python", "-m", "compileall", "src"),
                ),
            ),
        ),
        AlphaPlanNode(
            node_id="verify",
            objective="Verify the accepted contract.",
            depends_on=("inspect",),
            budget=budget,
            effects=("repository-read", "process"),
            allowed_paths=(),
            checks=(
                AlphaAcceptanceCheck(
                    check_id="verify-pass",
                    argv=("pytest", "tests/unit/test_alpha_runtime.py", "-q"),
                ),
            ),
        ),
    )


def _run() -> AlphaRunRequest:
    return AlphaRunRequest(
        schema_version="alpha-run-request/v1",
        run_id="run-1",
        project_id="project-1",
        intent_id="intent-1",
        plan_id="plan-1",
        idempotency_key="run-1",
    )


def _legacy_event(stream_id: str, suffix: str) -> EventEnvelope:
    return EventEnvelope.create(
        stream_id=stream_id,
        stream_sequence=1,
        event_type="legacy.event",
        actor="legacy",
        source="test",
        payload={"suffix": suffix},
        idempotency_key=suffix,
    )
