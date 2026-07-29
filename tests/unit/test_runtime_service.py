from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import cast

import msgspec
import pytest

from blackcell.adapters.execution.worktree import (
    GitWorktreeLifecycle,
    WorktreeFailureCode,
    WorktreeLifecycleError,
)
from blackcell.bootstrap.runtime_service import RuntimeService
from blackcell.gateway import DataClassification, GatewayBudget, LocalityPolicy
from blackcell.interfaces.http import (
    MAX_RUN_QUERY_SCAN_EVENTS,
    AcceptanceCheck,
    CancelRunRequest,
    IntentRequest,
    NodeBudget,
    PlanNode,
    PlanRequest,
    ProjectRequest,
    RunQueryRequest,
    RunRequest,
    RuntimeApiError,
    RuntimeApiFailureCode,
)
from blackcell.kernel import ArtifactStore, CheckpointStore, EventEnvelope, EventStore, JsonValue
from blackcell.orchestration.execution_plan import (
    EXECUTION_PLAN_DRAFT_SCHEMA,
    ExecutionPolicyKernel,
    PlanningRequest,
    PlanningResult,
    TaskAttemptExecutor,
)
from blackcell.orchestration.execution_runtime import (
    EventBackedExecutionRunJournal,
    ExecutionCoordinator,
)

_CONFIGURATION_DIGEST = "sha256:" + ("a" * 64)
_OTHER_CONFIGURATION_DIGEST = "sha256:" + ("b" * 64)
_BASE_COMMIT = "b" * 40


class RefusingPlanRetentionWorktrees(GitWorktreeLifecycle):
    def retain_plan_base_commit(
        self,
        repository_root: Path,
        *,
        plan_id: str,
        base_commit: str,
    ) -> str:
        raise WorktreeLifecycleError(WorktreeFailureCode.BASE_COMMIT_RETENTION_FAILED)


class RacingPresentationEventStore(EventStore):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.after_descending_read: Callable[[], None] | None = None

    def read_type_descending(
        self,
        event_type: str,
        *,
        through_position: int,
        limit: int,
    ) -> tuple[EventEnvelope, ...]:
        events = super().read_type_descending(
            event_type,
            through_position=through_position,
            limit=limit,
        )
        callback, self.after_descending_read = self.after_descending_read, None
        if callback is not None:
            callback()
        return events


class UnknownUsagePlanner:
    def propose_plan(self, request: PlanningRequest) -> PlanningResult:
        del request
        draft = {
            "schema_version": EXECUTION_PLAN_DRAFT_SCHEMA,
            "tasks": [
                {
                    "task_id": "verify",
                    "objective": "Run the admitted verification checks.",
                    "depends_on": [],
                    "allowed_paths": [],
                    "checks": ["inspect-pass", "verify-pass"],
                }
            ],
        }
        return PlanningResult(
            draft=cast("dict[str, JsonValue]", draft),
            provider_output_digest=_CONFIGURATION_DIGEST,
            profile_id="unknown-usage-planner",
            adapter_id="recorded-test",
            model_id="recorded-test",
            input_tokens=None,
            output_tokens=None,
            latency_ms=7,
            cost_microusd=None,
        )


def test_runtime_flow_is_idempotent_restart_safe_and_live_free(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    events = EventStore(tmp_path / "data" / "blackcell.sqlite3")
    service = RuntimeService(events, repository)

    project = service.register_project(_project(repository), principal_id="operator")
    intent = service.accept_intent(_intent(), principal_id="operator")
    plan = service.accept_plan(_plan(repository), principal_id="operator")
    run = service.submit_run(_run(), principal_id="operator")

    assert run.status == "queued"
    assert project.cursor < intent.cursor < plan.cursor < run.cursor
    assert service.submit_run(_run(), principal_id="operator") == run

    restarted = RuntimeService(EventStore(events.path), repository)
    assert restarted.register_project(_project(repository), principal_id="operator") == project
    assert restarted.accept_intent(_intent(), principal_id="operator") == intent
    assert restarted.accept_plan(_plan(repository), principal_id="operator") == plan
    assert restarted.inspect_run("run-1") == run

    first_replay = restarted.replay_run("run-1")
    second_replay = RuntimeService(EventStore(events.path), repository).replay_run("run-1")
    assert first_replay == second_replay
    assert first_replay.processed_events == 4
    assert first_replay.verification.lifecycle_status == "not-started"
    assert first_replay.verification.processed_events == 0
    assert first_replay.verification.artifact_integrity == "not-applicable"
    assert first_replay.intent.assumptions == ("The existing event ledger is reusable.",)
    assert first_replay.intent.unresolved_questions == ("Which execution provider is selected?",)
    assert first_replay.plan.topological_order == ("inspect", "verify")
    assert len(events) == 4
    assert not restarted.should_cancel_generated_run("run-1")
    assert restarted.should_cancel_generated_run("missing-run")


def test_generated_plan_run_uses_asynchronous_execution_selection(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    events = EventStore(tmp_path / "generated.sqlite3")
    service = RuntimeService(events, repository)
    service.register_project(_project(repository), principal_id="operator")
    intent = IntentRequest(
        schema_version="intent-request/v1",
        intent_id="intent-1",
        project_id="project-1",
        objective="Generate, execute, and verify the bounded plan.",
        constraints=("Preserve public contracts.",),
        assumptions=("The repository base is immutable.",),
        unresolved_questions=(),
        idempotency_key="intent-generated",
    )
    service.accept_intent(intent, principal_id="operator")
    declared = _plan(repository)
    generated = PlanRequest(
        schema_version=declared.schema_version,
        plan_id=declared.plan_id,
        project_id=declared.project_id,
        intent_id=declared.intent_id,
        base_commit=declared.base_commit,
        allowed_effects=declared.allowed_effects,
        nodes=tuple(
            PlanNode(
                node_id=node.node_id,
                objective=node.objective,
                depends_on=node.depends_on,
                budget=NodeBudget(1_000, 1_000, 30, 50, 0),
                effects=node.effects,
                allowed_paths=node.allowed_paths,
                checks=node.checks,
            )
            for node in declared.nodes
        ),
        idempotency_key=declared.idempotency_key,
        planning_mode="generated",
    )
    service.accept_plan(generated, principal_id="operator")
    service.submit_run(_run(), principal_id="operator")

    selected = service.next_generated_run()

    assert service.next_ready_node() is None
    assert selected is not None
    assert selected.run_id == "run-1"
    assert selected.goal.objective == intent.objective
    assert selected.goal.base_commit == generated.base_commit
    assert selected.authority.check_timeout_seconds == 30
    assert selected.authority.max_changed_paths == 0
    assert selected.authority.budget.max_input_tokens == 2_000
    assert selected.authority.budget.max_output_tokens == 2_000
    assert selected.authority.budget.max_latency_ms == 60_000
    assert selected.authority.budget.max_cost_microusd == 100
    assert service.generated_execution_authority(selected.run_id) == selected.authority
    assert tuple(item.check_id for item in selected.goal.verification_checks) == (
        "inspect-pass",
        "verify-pass",
    )

    coordinator = ExecutionCoordinator(
        EventBackedExecutionRunJournal(events, CheckpointStore(events.path)),
        UnknownUsagePlanner(),
        cast("TaskAttemptExecutor", object()),
        ExecutionPolicyKernel(),
    )
    coordinator.compile_and_admit(
        selected.run_id,
        PlanningRequest(
            goal=selected.goal,
            classification=DataClassification.PRIVATE,
            locality=LocalityPolicy.REMOTE_ALLOWED,
            budget=selected.authority.budget,
            estimated_input_tokens=1,
            correlation_id=selected.run_id,
            run_id=selected.run_id,
        ),
        actor="test:planner",
    )

    resumed = service.generated_execution_authority(selected.run_id)

    assert resumed.consumed_budget == GatewayBudget(2_000, 2_000, 7, 100)
    assert not resumed.input_tokens_complete
    assert not resumed.output_tokens_complete
    assert not resumed.cost_microusd_complete


def test_runtime_submission_rejects_mismatched_references_and_conflicts(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    service = RuntimeService(EventStore(tmp_path / "state.sqlite3"), repository)
    service.register_project(_project(repository), principal_id="operator")
    service.register_project(
        _project(repository, project_id="project-other"), principal_id="operator"
    )
    service.accept_intent(_intent(), principal_id="operator")

    mismatched = PlanRequest(
        schema_version="plan-request/v1",
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

    conflicting = ProjectRequest(
        schema_version="project-request/v1",
        project_id="project-1",
        root=str(repository),
        configuration_provider="kernform",
        configuration_version="0.2.0",
        configuration_digest=_OTHER_CONFIGURATION_DIGEST,
        idempotency_key="project-1",
    )
    with pytest.raises(RuntimeApiError) as conflict:
        service.register_project(conflicting, principal_id="operator")
    assert conflict.value.code is RuntimeApiFailureCode.CONFLICT

    with pytest.raises(RuntimeApiError) as absent:
        service.submit_run(_run(), principal_id="operator")
    assert absent.value.code is RuntimeApiFailureCode.NOT_FOUND


def test_plan_acceptance_enforces_review_evidence_item_capacity(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    events = EventStore(tmp_path / "state.sqlite3")
    service = RuntimeService(events, repository)
    service.register_project(_project(repository), principal_id="operator")
    service.accept_intent(_intent(), principal_id="operator")
    base_node = _nodes()[0]

    def checks(count: int) -> tuple[AcceptanceCheck, ...]:
        return tuple(
            AcceptanceCheck(
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
        _plan(repository),
        plan_id="plan-capacity",
        allowed_effects=("repository-read", "repository-write", "process"),
        nodes=(exact_capacity_node,),
        idempotency_key="plan-capacity",
    )
    accepted = service.accept_plan(exact_capacity_plan, principal_id="operator")
    assert accepted.plan_id == "plan-capacity"

    def plan(check_count: int, plan_id: str) -> PlanRequest:
        return msgspec.structs.replace(
            _plan(repository),
            plan_id=plan_id,
            nodes=(msgspec.structs.replace(base_node, checks=checks(check_count)),),
            idempotency_key=plan_id,
        )

    with pytest.raises(RuntimeApiError) as over_capacity:
        service.accept_plan(plan(32, "plan-over-capacity"), principal_id="operator")

    assert over_capacity.value.code is RuntimeApiFailureCode.INVALID_REQUEST
    assert events.read_stream("plan:plan-over-capacity") == ()

    aggregate_nodes = tuple(msgspec.structs.replace(node, checks=checks(16)) for node in _nodes())
    aggregate_plan = msgspec.structs.replace(
        _plan(repository),
        plan_id="plan-over-aggregate-capacity",
        nodes=aggregate_nodes,
        idempotency_key="plan-over-aggregate-capacity",
    )
    with pytest.raises(RuntimeApiError) as aggregate_capacity:
        service.accept_plan(aggregate_plan, principal_id="operator")
    assert aggregate_capacity.value.code is RuntimeApiFailureCode.INVALID_REQUEST
    assert events.read_stream("plan:plan-over-aggregate-capacity") == ()


def test_plan_rejects_a_missing_base_commit_without_persisting(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    events = EventStore(tmp_path / "state.sqlite3")
    service = RuntimeService(events, repository)
    service.register_project(_project(repository), principal_id="operator")
    service.accept_intent(_intent(), principal_id="operator")
    request = msgspec.structs.replace(
        _plan(repository),
        plan_id="plan-missing-base",
        base_commit="f" * 40,
        idempotency_key="plan-missing-base",
    )

    with pytest.raises(RuntimeApiError) as missing:
        service.accept_plan(request, principal_id="operator")

    assert missing.value.code is RuntimeApiFailureCode.INVALID_REQUEST
    assert events.read_stream("plan:plan-missing-base") == ()
    assert len(events) == 2


def test_plan_retains_an_unreachable_base_before_persisting(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    events = EventStore(tmp_path / "state.sqlite3")
    service = RuntimeService(events, repository)
    service.register_project(_project(repository), principal_id="operator")
    service.accept_intent(_intent(), principal_id="operator")
    tree = _git(repository, "rev-parse", "HEAD^{tree}").stdout.decode("ascii").strip()
    orphan = (
        _git(repository, "commit-tree", tree, "-m", "unreachable base")
        .stdout.decode("ascii")
        .strip()
    )
    request = msgspec.structs.replace(_plan(repository), base_commit=orphan)

    accepted = service.accept_plan(request, principal_id="operator")
    _git(repository, "reflog", "expire", "--expire=now", "--all")
    _git(repository, "gc", "--prune=now")

    retained = (
        _git(
            repository,
            "for-each-ref",
            "--format=%(objectname)",
            "refs/blackcell/execution/plans",
        )
        .stdout.decode("ascii")
        .splitlines()
    )
    assert accepted.plan_id == request.plan_id
    assert retained == [orphan]
    assert _git(repository, "cat-file", "-e", f"{orphan}^{{commit}}").returncode == 0
    assert service.accept_plan(request, principal_id="operator") == accepted


def test_plan_retention_failure_precedes_the_accepted_event(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    events = EventStore(tmp_path / "state.sqlite3")
    service = RuntimeService(
        events,
        repository,
        worktrees=RefusingPlanRetentionWorktrees(),
    )
    service.register_project(_project(repository), principal_id="operator")
    service.accept_intent(_intent(), principal_id="operator")

    with pytest.raises(RuntimeApiError) as unavailable:
        service.accept_plan(_plan(repository), principal_id="operator")

    assert unavailable.value.code is RuntimeApiFailureCode.NOT_READY
    assert events.read_stream("plan:plan-1") == ()
    assert len(events) == 2


def test_queued_cancellation_is_idempotent_and_replayed_without_live_work(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    events = EventStore(tmp_path / "state.sqlite3")
    service = RuntimeService(events, repository)
    service.register_project(_project(repository), principal_id="operator")
    service.accept_intent(_intent(), principal_id="operator")
    service.accept_plan(_plan(repository), principal_id="operator")
    service.submit_run(_run(), principal_id="operator")
    request = CancelRunRequest(
        schema_version="execution-cancel-run-request/v1",
        idempotency_key="cancel-run-1",
    )

    canceled = service.cancel_run("run-1", request, principal_id="operator")
    restarted = RuntimeService(EventStore(events.path), repository)
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
    assert tuple(event.event_type for event in events.read_stream("run:run-1")) == (
        "run.queued",
        "run.cancel-requested",
        "run.canceled",
    )
    assert len(events) == 6
    assert restarted.should_cancel_generated_run("run-1")


def test_runtime_event_cursor_resumes_in_global_order(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    events = EventStore(tmp_path / "state.sqlite3")
    events.append(_unrelated_event("external:one", "one"), expected_sequence=0)
    service = RuntimeService(events, repository)
    project = service.register_project(_project(repository), principal_id="operator")
    events.append(_unrelated_event("external:two", "two"), expected_sequence=0)
    intent = service.accept_intent(_intent(), principal_id="operator")

    first = service.list_events(after_cursor=0, limit=2)
    assert tuple(item.event_id for item in first.events) == (project.event_id,)
    assert first.next_cursor == project.cursor
    assert first.scanned_events == 2
    assert first.has_more is True
    assert tuple(item.event_type for item in first.events) == ("project.registered",)

    second = service.list_events(after_cursor=first.next_cursor, limit=2)
    assert tuple(item.event_id for item in second.events) == (intent.event_id,)
    assert second.next_cursor == intent.cursor
    assert second.scanned_events == 2
    assert second.has_more is False

    tail = service.list_events(after_cursor=second.next_cursor, limit=2)
    assert tail.events == ()
    assert tail.next_cursor == second.next_cursor


def test_runtime_readiness_fails_closed_for_storage_and_event_failures(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    ready = RuntimeService(EventStore(tmp_path / "ready.sqlite3"), repository)
    constrained = RuntimeService(
        EventStore(tmp_path / "constrained.sqlite3"),
        repository,
        storage_quota=_NoMutationCapacity(),
    )
    unavailable = RuntimeService(
        _UnreadableEventStore(tmp_path / "unavailable.sqlite3"), repository
    )

    assert ready.readiness().status == "ready"
    assert constrained.readiness().status == "not-ready"
    assert unavailable.readiness().status == "not-ready"


def test_runtime_constructor_rejects_ambiguous_storage_authority(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    events = EventStore(tmp_path / "events.sqlite3")
    file_path = tmp_path / "not-a-directory"
    file_path.write_text("bounded", encoding="utf-8")

    for invalid_repository in (tmp_path / "missing", file_path):
        with pytest.raises(ValueError, match="repository root"):
            RuntimeService(events, invalid_repository)
    with pytest.raises(ValueError, match="isolation root must be absolute"):
        RuntimeService(events, repository, isolation_root=Path("relative"))
    with pytest.raises(ValueError, match="isolation parent must exist"):
        RuntimeService(
            events,
            repository,
            isolation_root=tmp_path / "missing-parent" / "worktrees",
        )
    with pytest.raises(ValueError, match="artifact store does not match"):
        RuntimeService(
            events,
            repository,
            artifacts=ArtifactStore(
                tmp_path / "artifacts",
                database_path=tmp_path / "different.sqlite3",
            ),
        )


def test_project_registration_rejects_noncanonical_repository_roots(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    other = tmp_path / "other-repository"
    other.mkdir()
    events = EventStore(tmp_path / "project-roots.sqlite3")
    service = RuntimeService(events, repository)

    for root in ("relative", str(tmp_path / "missing-root"), str(other)):
        with pytest.raises(RuntimeApiError) as caught:
            service.register_project(
                msgspec.structs.replace(_project(repository), root=root),
                principal_id="operator",
            )
        assert caught.value.code is RuntimeApiFailureCode.INVALID_REQUEST

    assert len(events) == 0


def test_run_query_bounds_empty_filtered_and_paginated_results(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    events = EventStore(tmp_path / "queries.sqlite3")
    service = RuntimeService(events, repository)

    empty = service.query_runs(RunQueryRequest(schema_version="run-query-request/v1"))
    assert empty.runs == ()
    assert not empty.has_more

    service.register_project(_project(repository), principal_id="operator")
    service.accept_intent(_intent(), principal_id="operator")
    service.accept_plan(_plan(repository), principal_id="operator")
    service.submit_run(_run(), principal_id="operator")
    events.append(_unrelated_event("external:tail", "tail"), expected_sequence=0)

    filtered = service.query_runs(
        RunQueryRequest(
            schema_version="run-query-request/v1",
            project_ids=("different-project",),
        )
    )
    first = service.query_runs(RunQueryRequest(schema_version="run-query-request/v1", limit=1))

    assert filtered.runs == ()
    assert tuple(item.run.run_id for item in first.runs) == ("run-1",)
    assert first.has_more
    with pytest.raises(RuntimeApiError) as invalid:
        service.query_runs(cast("RunQueryRequest", object()))
    assert invalid.value.code is RuntimeApiFailureCode.INVALID_REQUEST


def test_presentation_run_sources_are_indexed_bounded_and_snapshot_consistent(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    events = EventStore(tmp_path / "presentation-runs.sqlite3")
    service = RuntimeService(events, repository)
    service.register_project(_project(repository), principal_id="operator")
    service.accept_intent(_intent(), principal_id="operator")
    service.accept_plan(_plan(repository), principal_id="operator")
    service.submit_run(_run(), principal_id="operator")
    unrelated = tuple(
        _unrelated_event(f"external:{index}", f"tail-{index}")
        for index in range(MAX_RUN_QUERY_SCAN_EVENTS + 1)
    )
    events.append_many(
        unrelated,
        expected_sequences={event.stream_id: 0 for event in unrelated},
    )
    service.submit_run(
        msgspec.structs.replace(_run(), run_id="run-2", idempotency_key="run-2"),
        principal_id="operator",
    )

    window = service.presentation_run_window(limit=1)
    selected = service.presentation_run_item("run-1")

    assert tuple(item.run.run_id for item in window.runs) == ("run-2",)
    assert window.scanned_events == 2
    assert window.event_cursor == events.current_position()
    assert window.has_older_runs
    assert selected.run.run_id == "run-1"


def test_presentation_run_window_bounds_projections_to_its_captured_cursor(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    events = RacingPresentationEventStore(tmp_path / "presentation-race.sqlite3")
    service = RuntimeService(events, repository)
    service.register_project(_project(repository), principal_id="operator")
    service.accept_intent(_intent(), principal_id="operator")
    service.accept_plan(_plan(repository), principal_id="operator")
    service.submit_run(_run(), principal_id="operator")
    captured_cursor = events.current_position()

    def cancel_after_snapshot_capture() -> None:
        service.cancel_run(
            "run-1",
            CancelRunRequest(
                schema_version="execution-cancel-run-request/v1",
                idempotency_key="racing-cancel",
            ),
            principal_id="operator",
        )

    events.after_descending_read = cancel_after_snapshot_capture

    window = service.presentation_run_window(limit=1)

    assert window.event_cursor == captured_cursor
    assert window.runs[0].run.cursor <= window.event_cursor
    assert window.runs[0].run.status == "queued"
    assert events.current_position() > window.event_cursor


class _NoMutationCapacity:
    def has_mutation_capacity(self) -> bool:
        return False


class _UnreadableEventStore(EventStore):
    def read_all(
        self,
        *,
        after_position: int = 0,
        limit: int | None = None,
    ) -> tuple[EventEnvelope, ...]:
        del after_position, limit
        raise OSError("storage detail must remain isolated")


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.name", "BlackCell Test")
    _git(repository, "config", "user.email", "blackcell@example.invalid")
    (repository / "README.md").write_text("# Runtime fixture\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "initial")
    return repository.resolve()


def _base_commit(repository: Path) -> str:
    return _git(repository, "rev-parse", "HEAD").stdout.decode("ascii").strip()


def _project(repository: Path, *, project_id: str = "project-1") -> ProjectRequest:
    return ProjectRequest(
        schema_version="project-request/v1",
        project_id=project_id,
        root=str(repository),
        configuration_provider="kernform",
        configuration_version="0.2.0",
        configuration_digest=_CONFIGURATION_DIGEST,
        idempotency_key=project_id,
    )


def _intent() -> IntentRequest:
    return IntentRequest(
        schema_version="intent-request/v1",
        intent_id="intent-1",
        project_id="project-1",
        objective="Create a deterministic execution contract.",
        constraints=("Use only the accepted execution plan.",),
        assumptions=("The existing event ledger is reusable.",),
        unresolved_questions=("Which execution provider is selected?",),
        idempotency_key="intent-1",
    )


def _plan(repository: Path) -> PlanRequest:
    return PlanRequest(
        schema_version="plan-request/v1",
        plan_id="plan-1",
        project_id="project-1",
        intent_id="intent-1",
        base_commit=_base_commit(repository),
        allowed_effects=("repository-read", "process"),
        nodes=_nodes(),
        idempotency_key="plan-1",
    )


def _nodes() -> tuple[PlanNode, ...]:
    budget = NodeBudget(
        max_input_tokens=1_000,
        max_output_tokens=1_000,
        timeout_seconds=30,
        max_cost_microusd=0,
        max_changed_files=0,
    )
    return (
        PlanNode(
            node_id="inspect",
            objective="Inspect bounded source evidence.",
            depends_on=(),
            budget=budget,
            effects=("repository-read", "process"),
            allowed_paths=(),
            checks=(
                AcceptanceCheck(
                    check_id="inspect-pass",
                    argv=("python", "-m", "compileall", "src"),
                ),
            ),
        ),
        PlanNode(
            node_id="verify",
            objective="Verify the accepted contract.",
            depends_on=("inspect",),
            budget=budget,
            effects=("repository-read", "process"),
            allowed_paths=(),
            checks=(
                AcceptanceCheck(
                    check_id="verify-pass",
                    argv=("pytest", "tests/unit/test_runtime.py", "-q"),
                ),
            ),
        ),
    )


def _run() -> RunRequest:
    return RunRequest(
        schema_version="run-request/v1",
        run_id="run-1",
        project_id="project-1",
        intent_id="intent-1",
        plan_id="plan-1",
        idempotency_key="run-1",
    )


def _unrelated_event(stream_id: str, suffix: str) -> EventEnvelope:
    return EventEnvelope.create(
        stream_id=stream_id,
        stream_sequence=1,
        event_type="external.event",
        actor="external:test",
        source="test",
        payload={"suffix": suffix},
        idempotency_key=suffix,
    )


def _git(cwd: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ("git", "--no-pager", *arguments),
        cwd=cwd,
        check=True,
        capture_output=True,
    )
