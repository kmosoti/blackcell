from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import cast

import msgspec
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from blackcell.adapters.models.alpha_planner import GatewayAlphaPlanner
from blackcell.adapters.telemetry import AlphaV2TraceObserver
from blackcell.bootstrap.alpha_runtime import AlphaRuntimeApiService
from blackcell.bootstrap.alpha_v2_kernel import ProductionAlphaV2Kernel
from blackcell.gateway import (
    DataClassification,
    GatewayBudget,
    GatewayResult,
    LocalityPolicy,
    ModelCapability,
    ModelRequest,
    ModelResponse,
    RoutingDecision,
)
from blackcell.interfaces.http import AlphaCancelRunRequest, AlphaRunQueryRequest
from blackcell.kernel import CheckpointStore, EventEnvelope, EventStore, JsonValue
from blackcell.kernel._json import json_digest, thaw_json
from blackcell.orchestration.alpha_lifecycle import (
    ALPHA_EVENT_SOURCE,
    ALPHA_RUN_CANCELED,
    ALPHA_RUN_FAILED,
    ALPHA_RUN_RECONCILIATION_REQUIRED,
    ALPHA_RUN_SUCCEEDED,
)
from blackcell.orchestration.alpha_v2 import (
    ALPHA_V2_EVENT_SOURCE,
    ALPHA_V2_PLAN_DRAFT_SCHEMA,
    ALPHA_V2_REPLAN_STARTED,
    ALPHA_V2_RUN_TERMINATED,
    ALPHA_V2_TASK_BLOCKED,
    ALPHA_V2_TASK_VERIFIED,
    ALPHA_V2_TASK_VERIFYING,
    AlphaGoalSpec,
    AlphaPlanningRequest,
    AlphaPlanningResult,
    AlphaPlanVersion,
    AlphaTaskSpec,
    AlphaV2ContractError,
    AlphaV2PolicyKernel,
    AlphaVerificationCheck,
    AttemptEvidence,
    AttemptRoute,
    FailureClass,
    PolicyDecision,
    PraxisPromotionCandidate,
    RunLifecycleStatus,
    TaskLifecycleStatus,
    ToolActionRequest,
    alpha_goal_payload,
    alpha_plan_payload,
    compile_alpha_plan,
)
from blackcell.orchestration.alpha_v2_runtime import (
    ALPHA_V2_GOAL_ADMITTED,
    ALPHA_V2_PLAN_ADMITTED,
    ALPHA_V2_PLAN_DRAFT_RECEIVED,
    ALPHA_V2_POLICY_DECIDED,
    ALPHA_V2_TASK_READY,
    ALPHA_V2_TASK_STARTED,
    AlphaV2Coordinator,
    AlphaV2RunProjection,
    AlphaV2RuntimeError,
    EventBackedAlphaV2RunJournal,
)
from blackcell.telemetry import TraceRecorder
from tests.unit.test_alpha_runtime import _intent, _plan, _project, _repository, _run

NOW = datetime(2026, 7, 26, 12, tzinfo=UTC)
DIGEST = "sha256:" + "d" * 64
BASE_COMMIT = "a" * 40
FAILED_HEAD = "b" * 40
SUCCESS_HEAD = "c" * 40


def test_plan_compiler_is_deterministic_compositional_and_versioned() -> None:
    goal = _goal(allowed_paths=("src", "tests"))
    draft = _draft(
        tasks=(
            _task("implement", allowed_paths=("src",)),
            _task("verify", depends_on=("implement",), allowed_paths=("tests",)),
        )
    )

    first = compile_alpha_plan(goal, draft)
    repeated = compile_alpha_plan(goal, draft)
    revised = compile_alpha_plan(
        goal,
        _draft(tasks=(_task("implement", allowed_paths=("src",)),)),
        previous=first,
    )

    assert first == repeated
    assert first.plan_digest == repeated.plan_digest
    assert first.plan_version == 1
    assert first.supersedes_plan_id is None
    assert first.topological_order == ("implement", "verify")
    assert revised.plan_version == 2
    assert revised.supersedes_plan_id == first.plan_id
    assert revised.plan_id != first.plan_id


def test_plan_compiler_rejects_provider_scope_escalation_cycles_and_parallel_writers() -> None:
    goal = _goal(allowed_paths=("src", "tests"))
    with pytest.raises(AlphaV2ContractError, match="plan-path-outside-goal"):
        compile_alpha_plan(goal, _draft(tasks=(_task("escape", allowed_paths=("secrets",)),)))
    untrusted_check = _task("check", allowed_paths=())
    untrusted_check["checks"] = ["provider-command"]
    with pytest.raises(AlphaV2ContractError, match="plan-check-outside-goal"):
        compile_alpha_plan(goal, _draft(tasks=(untrusted_check,)))

    cyclic = _draft(
        tasks=(
            _task("left", depends_on=("right",), allowed_paths=()),
            _task("right", depends_on=("left",), allowed_paths=()),
        )
    )
    with pytest.raises(AlphaV2ContractError, match="cyclic-alpha-plan"):
        compile_alpha_plan(goal, cyclic)

    parallel = _draft(
        tasks=(
            _task("left", allowed_paths=("src",)),
            _task("right", allowed_paths=("tests",)),
        )
    )
    with pytest.raises(AlphaV2ContractError, match="parallel-writer-plan"):
        compile_alpha_plan(goal, parallel)


def test_gateway_planner_returns_only_an_untrusted_draft() -> None:
    secret_command_token = "host-owned-verifier-secret"
    goal = replace(
        _goal(),
        verification_checks=(
            AlphaVerificationCheck("check", ("pytest", "-q", secret_command_token)),
        ),
    )
    gateway = _Gateway(_draft(tasks=(_task("implement", allowed_paths=("src",)),)))
    planner = GatewayAlphaPlanner(gateway)
    request = replace(_planning_request("run-planner"), goal=goal)

    result = planner.propose_plan(request)

    assert result.draft["schema_version"] == ALPHA_V2_PLAN_DRAFT_SCHEMA
    assert result.input_tokens is None
    assert result.cost_microusd is None
    assert gateway.request is not None
    assert gateway.request.capability is ModelCapability.REASON
    assert gateway.request.tools_allowed is False
    assert gateway.request.input["goal_id"] == request.goal.goal_id
    assert gateway.request.input["verification_check_ids"] == ("check",)
    assert "verification_checks" not in gateway.request.input
    assert "base_commit" not in gateway.request.input
    assert secret_command_token not in repr(gateway.request.input)


def test_runtime_executes_one_bounded_repair_then_succeeds_from_snapshot_tail(
    tmp_path: Path,
) -> None:
    provider = _Provider(_draft(tasks=(_task("implement", allowed_paths=("src",)),)))
    executor = _Executor(
        (
            AttemptEvidence(
                workspace_clean=True,
                verifier_exit_code=1,
                required_checks_passed=False,
                failure_class=FailureClass.LOGIC_BUG,
                failure_summary="Assertion failed at line 41",
                artifact_digests=(DIGEST,),
                progress_digests=(DIGEST,),
                head_commit=FAILED_HEAD,
            ),
            _success(),
        )
    )
    journal = _journal(tmp_path)
    coordinator = AlphaV2Coordinator(journal, provider, executor, AlphaV2PolicyKernel())
    request = _planning_request("run-repair-success")
    plan, _ = coordinator.compile_and_admit("run-repair-success", request, actor="daemon:planner")
    checkpoint = journal.snapshot("run-repair-success")

    state = coordinator.execute(
        "run-repair-success",
        request,
        plan,
        actor="daemon:worker",
    )
    rehydrated = EventBackedAlphaV2RunJournal(
        EventStore(tmp_path / "kernel.sqlite3"),
        CheckpointStore(tmp_path / "kernel.sqlite3"),
    ).rehydrate("run-repair-success")

    assert checkpoint.last_stream_sequence == 3
    assert state == rehydrated
    assert state.status is RunLifecycleStatus.SUCCEEDED
    assert state.task("implement").status is TaskLifecycleStatus.SUCCEEDED
    assert state.task("implement").attempts == 2
    assert state.task("implement").head_commit == SUCCESS_HEAD
    assert state.input_tokens == 0
    assert state.input_tokens_complete is False
    assert state.cost_microusd_complete is False
    assert len(set(executor.workspaces)) == 2
    assert executor.base_commits == [BASE_COMMIT, FAILED_HEAD]
    assert executor.decisions and all(item.allowed for item in executor.decisions)
    events = journal.events("run-repair-success")
    policy_positions = [
        item.stream_sequence for item in events if item.event_type == ALPHA_V2_POLICY_DECIDED
    ]
    start_positions = [
        item.stream_sequence for item in events if item.event_type == ALPHA_V2_TASK_STARTED
    ]
    assert len(policy_positions) == len(start_positions) == 2
    assert all(
        policy < started for policy, started in zip(policy_positions, start_positions, strict=True)
    )


def test_public_runtime_and_versioned_kernel_share_one_canonical_run_stream(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    database = tmp_path / "shared.sqlite3"
    events = EventStore(database)
    runtime = AlphaRuntimeApiService(events, repository)
    project = _project(repository)
    intent = _intent()
    plan_request = _plan(repository)
    run_request = _run()
    runtime.register_project(project, principal_id="client:test")
    runtime.accept_intent(intent, principal_id="client:test")
    runtime.accept_plan(plan_request, principal_id="client:test")
    runtime.submit_run(run_request, principal_id="client:test")

    goal = replace(
        _goal(),
        project_id=project.project_id,
        intent_id=intent.intent_id,
        base_commit=plan_request.base_commit,
    )
    request = replace(_planning_request(run_request.run_id), goal=goal)
    journal = EventBackedAlphaV2RunJournal(events, CheckpointStore(database))
    coordinator = AlphaV2Coordinator(
        journal,
        _Provider(_draft(tasks=(_task("implement", allowed_paths=("src",)),))),
        _Executor((_success(),)),
        AlphaV2PolicyKernel(),
    )

    coordinator.compile_and_admit(run_request.run_id, request, actor="daemon:planner")

    stream = journal.events(run_request.run_id)
    assert {item.stream_id for item in stream} == {f"alpha:run:{run_request.run_id}"}
    assert not any("alpha:v2:run" in item.stream_id for item in events.read_all())
    assert all(current.causation_id == previous.event_id for previous, current in pairwise(stream))
    assert runtime.next_ready_node() is None
    query = runtime.query_runs(
        AlphaRunQueryRequest(
            schema_version="alpha-run-query-request/v1",
            run_ids=(run_request.run_id,),
        )
    )
    assert query.runs[0].run.status == "running"
    assert tuple(item.node_id for item in query.runs[0].nodes) == ("implement",)

    canceled = runtime.cancel_run(
        run_request.run_id,
        AlphaCancelRunRequest(
            schema_version="alpha-cancel-run-request/v1",
            idempotency_key="cancel-versioned-run",
        ),
        principal_id="client:test",
    )

    assert canceled.status == "canceled"
    assert journal.rehydrate(run_request.run_id).status is RunLifecycleStatus.CANCELED
    assert all(
        not item.event_type.startswith("alpha.v2.")
        for item in runtime.list_events(after_cursor=0, limit=20).events
    )


def test_existing_public_daemon_admits_and_processes_generated_plan(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    database = tmp_path / "public-generated.sqlite3"
    events = EventStore(database)
    runtime = AlphaRuntimeApiService(events, repository)
    project = _project(repository)
    intent = msgspec.structs.replace(_intent(), unresolved_questions=())
    bounds = msgspec.structs.replace(_plan(repository), planning_mode="generated")
    run = _run()
    runtime.register_project(project, principal_id="client:test")
    runtime.accept_intent(intent, principal_id="client:test")
    runtime.accept_plan(bounds, principal_id="client:test")
    runtime.submit_run(run, principal_id="client:test")
    generated = runtime.next_generated_run()
    assert generated is not None
    request = AlphaPlanningRequest(
        goal=generated.goal,
        classification=DataClassification.PRIVATE,
        locality=LocalityPolicy.REMOTE_ALLOWED,
        budget=GatewayBudget(32_000, 4_096, 120_000, 0),
        estimated_input_tokens=1_000,
        correlation_id=run.run_id,
        run_id=run.run_id,
    )
    task = _task("verify", allowed_paths=())
    task["checks"] = ["verify-pass"]
    kernel = ProductionAlphaV2Kernel(
        AlphaV2Coordinator(
            EventBackedAlphaV2RunJournal(events, CheckpointStore(database)),
            _Provider(_draft(tasks=(task,))),
            _Executor((_success(),)),
            AlphaV2PolicyKernel(),
        )
    )

    state = kernel.process(request, actor="daemon:worker")
    query = runtime.query_runs(
        AlphaRunQueryRequest(
            schema_version="alpha-run-query-request/v1",
            run_ids=(run.run_id,),
        )
    )

    assert state.status is RunLifecycleStatus.SUCCEEDED
    assert runtime.next_generated_run() is None
    assert runtime.inspect_run(run.run_id).status == "succeeded"
    assert query.runs[0].usage is not None
    assert query.runs[0].usage.input_tokens_complete is False
    assert query.runs[0].usage.max_input_tokens == request.budget.max_input_tokens
    assert query.runs[0].nodes[0].max_attempts == request.goal.max_attempts
    assert {event.stream_id for event in events.read_stream(f"alpha:run:{run.run_id}")} == {
        f"alpha:run:{run.run_id}"
    }


def test_public_cancellation_during_attempt_stops_verification_and_terminates(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    database = tmp_path / "cancel-during-attempt.sqlite3"
    events = EventStore(database)
    runtime = AlphaRuntimeApiService(events, repository)
    project = _project(repository)
    intent = _intent()
    bounds = _plan(repository)
    run = _run()
    runtime.register_project(project, principal_id="client:test")
    runtime.accept_intent(intent, principal_id="client:test")
    runtime.accept_plan(bounds, principal_id="client:test")
    runtime.submit_run(run, principal_id="client:test")
    goal = replace(
        _goal(),
        project_id=project.project_id,
        intent_id=intent.intent_id,
        base_commit=bounds.base_commit,
    )
    request = replace(_planning_request(run.run_id), goal=goal)
    task = _task("implement", allowed_paths=("src",))
    journal = EventBackedAlphaV2RunJournal(events, CheckpointStore(database))
    kernel = ProductionAlphaV2Kernel(
        AlphaV2Coordinator(
            journal,
            _Provider(_draft(tasks=(task,))),
            _CancelingExecutor(runtime),
            AlphaV2PolicyKernel(),
        )
    )

    state = kernel.process(request, actor="daemon:worker")

    assert state.status is RunLifecycleStatus.CANCELED
    assert runtime.inspect_run(run.run_id).status == "canceled"
    assert not any(
        event.event_type in {ALPHA_V2_TASK_VERIFYING, ALPHA_V2_TASK_VERIFIED}
        for event in journal.events(run.run_id)
    )


def test_journal_rejects_invalid_transition_before_append_and_retries_idempotently(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    goal = replace(_goal(), goal_id="goal-safe")
    payload = {
        "goal_id": goal.goal_id,
        "goal_digest": goal.digest,
        "goal": alpha_goal_payload(goal),
        "classification": DataClassification.PRIVATE.value,
        "locality": LocalityPolicy.REMOTE_ALLOWED.value,
        "budget": {
            "max_input_tokens": 32_000,
            "max_output_tokens": 4_096,
            "max_latency_ms": 120_000,
            "max_cost_microusd": 0,
        },
    }
    admitted = journal.append(
        "run-safe",
        ALPHA_V2_GOAL_ADMITTED,
        payload,
        actor="daemon:planner",
    )

    assert (
        journal.append(
            "run-safe",
            ALPHA_V2_GOAL_ADMITTED,
            payload,
            actor="daemon:planner",
        )
        == admitted
    )
    before = journal.events("run-safe")
    with pytest.raises(AlphaV2RuntimeError, match="task-not-found"):
        journal.append(
            "run-safe",
            ALPHA_V2_TASK_STARTED,
            {
                "task_id": "missing",
                "attempt": 1,
                "workspace_id": "workspace-invalid",
                "base_commit": BASE_COMMIT,
                "action_digest": DIGEST,
                "decision_id": DIGEST,
            },
            actor="daemon:worker",
        )
    assert journal.events("run-safe") == before


def test_journal_rejects_unbound_policy_and_attempt_events_before_append(
    tmp_path: Path,
) -> None:
    request = _planning_request("run-policy-binding")
    journal = _named_journal(tmp_path, "policy-binding")
    coordinator = AlphaV2Coordinator(
        journal,
        _Provider(_draft(tasks=(_task("implement", allowed_paths=("src",)),))),
        _Executor((_success(),)),
        AlphaV2PolicyKernel(),
    )
    plan, _ = coordinator.compile_and_admit("run-policy-binding", request, actor="daemon:planner")
    journal.append(
        "run-policy-binding",
        ALPHA_V2_TASK_READY,
        {"task_id": "implement", "attempt": 1},
        actor="daemon:worker",
    )
    task = plan.tasks[0]
    action = ToolActionRequest(
        run_id="run-policy-binding",
        plan_id=plan.plan_id,
        task_id=task.task_id,
        attempt=1,
        capability="repository-task",
        allowed_paths=task.allowed_paths,
    )
    decision = AlphaV2PolicyKernel().authorize(request.goal, plan, task, action)
    before = journal.events("run-policy-binding")

    wrong_action = replace(action, allowed_paths=())
    wrong_decision = PolicyDecision(
        True,
        "bounded-task-authorized",
        wrong_action.digest,
    )
    with pytest.raises(AlphaV2RuntimeError, match="invalid-alpha-v2-event"):
        journal.append(
            "run-policy-binding",
            ALPHA_V2_POLICY_DECIDED,
            {
                "task_id": "implement",
                "attempt": 1,
                "allowed": wrong_decision.allowed,
                "reason": wrong_decision.reason,
                "action_digest": wrong_decision.action_digest,
                "decision_id": wrong_decision.decision_id,
            },
            actor="daemon:worker",
        )
    assert journal.events("run-policy-binding") == before

    with pytest.raises(AlphaV2RuntimeError, match="invalid-alpha-v2-event"):
        journal.append(
            "run-policy-binding",
            ALPHA_V2_POLICY_DECIDED,
            {
                "task_id": "implement",
                "attempt": 1,
                "allowed": decision.allowed,
                "reason": decision.reason,
                "action_digest": decision.action_digest,
                "decision_id": DIGEST,
            },
            actor="daemon:worker",
        )
    assert journal.events("run-policy-binding") == before

    journal.append(
        "run-policy-binding",
        ALPHA_V2_POLICY_DECIDED,
        {
            "task_id": "implement",
            "attempt": 1,
            "allowed": decision.allowed,
            "reason": decision.reason,
            "action_digest": decision.action_digest,
            "decision_id": decision.decision_id,
        },
        actor="daemon:worker",
    )
    before_start = journal.events("run-policy-binding")
    expected_workspace = (
        "workspace-"
        + json_digest({"plan_id": plan.plan_id, "task_id": "implement", "attempt": 1}).removeprefix(
            "sha256:"
        )[:32]
    )
    with pytest.raises(AlphaV2RuntimeError, match="invalid-alpha-v2-transition"):
        journal.append(
            "run-policy-binding",
            ALPHA_V2_TASK_STARTED,
            {
                "task_id": "implement",
                "attempt": 1,
                "workspace_id": "workspace-wrong-binding",
                "base_commit": BASE_COMMIT,
                "action_digest": decision.action_digest,
                "decision_id": decision.decision_id,
            },
            actor="daemon:worker",
        )
    assert journal.events("run-policy-binding") == before_start
    with pytest.raises(AlphaV2RuntimeError, match="invalid-alpha-v2-transition"):
        journal.append(
            "run-policy-binding",
            ALPHA_V2_TASK_STARTED,
            {
                "task_id": "implement",
                "attempt": 1,
                "workspace_id": expected_workspace,
                "base_commit": FAILED_HEAD,
                "action_digest": decision.action_digest,
                "decision_id": decision.decision_id,
            },
            actor="daemon:worker",
        )
    assert journal.events("run-policy-binding") == before_start
    with pytest.raises(AlphaV2RuntimeError, match="invalid-alpha-v2-transition"):
        journal.append(
            "run-policy-binding",
            ALPHA_V2_TASK_STARTED,
            {
                "task_id": "implement",
                "attempt": 2,
                "workspace_id": "workspace-wrong-attempt",
                "base_commit": BASE_COMMIT,
                "action_digest": decision.action_digest,
                "decision_id": decision.decision_id,
            },
            actor="daemon:worker",
        )
    assert journal.events("run-policy-binding") == before_start


def test_journal_binds_plan_to_goal_draft_and_dependency_order_before_append(
    tmp_path: Path,
) -> None:
    request = _planning_request("run-plan-binding")
    draft = _draft(
        tasks=(
            _task("implement", allowed_paths=("src",)),
            _task("verify", depends_on=("implement",), allowed_paths=()),
        )
    )
    journal = _named_journal(tmp_path, "plan-binding")
    goal = request.goal
    journal.append(
        "run-plan-binding",
        ALPHA_V2_GOAL_ADMITTED,
        {
            "goal_id": goal.goal_id,
            "goal_digest": goal.digest,
            "goal": alpha_goal_payload(goal),
            "classification": request.classification.value,
            "locality": request.locality.value,
            "budget": {
                "max_input_tokens": request.budget.max_input_tokens,
                "max_output_tokens": request.budget.max_output_tokens,
                "max_latency_ms": request.budget.max_latency_ms,
                "max_cost_microusd": request.budget.max_cost_microusd,
            },
        },
        actor="daemon:planner",
    )
    draft_digest = json_digest(cast("dict[str, JsonValue]", draft))
    journal.append(
        "run-plan-binding",
        ALPHA_V2_PLAN_DRAFT_RECEIVED,
        {
            "draft_digest": draft_digest,
            "provider_output_digest": draft_digest,
            "profile_id": "recorded-plan",
            "adapter_id": "recorded-plan",
            "model_id": "recorded-plan",
            "input_tokens": 1,
            "output_tokens": 1,
            "latency_ms": 1,
            "cost_microusd": 0,
        },
        actor="daemon:planner",
    )
    foreign = compile_alpha_plan(replace(goal, goal_id="goal-foreign"), draft)
    before_plan = journal.events("run-plan-binding")
    with pytest.raises(AlphaV2RuntimeError, match="invalid-alpha-v2-event"):
        journal.append(
            "run-plan-binding",
            ALPHA_V2_PLAN_ADMITTED,
            {
                "plan_id": foreign.plan_id,
                "plan_digest": foreign.plan_digest,
                "plan_version": foreign.plan_version,
                "supersedes_plan_id": foreign.supersedes_plan_id,
                "plan": alpha_plan_payload(foreign),
            },
            actor="daemon:planner",
        )
    assert journal.events("run-plan-binding") == before_plan

    plan = compile_alpha_plan(goal, draft)
    journal.append(
        "run-plan-binding",
        ALPHA_V2_PLAN_ADMITTED,
        {
            "plan_id": plan.plan_id,
            "plan_digest": plan.plan_digest,
            "plan_version": plan.plan_version,
            "supersedes_plan_id": plan.supersedes_plan_id,
            "plan": alpha_plan_payload(plan),
        },
        actor="daemon:planner",
    )
    before_ready = journal.events("run-plan-binding")
    with pytest.raises(AlphaV2RuntimeError, match="invalid-alpha-v2-transition"):
        journal.append(
            "run-plan-binding",
            ALPHA_V2_TASK_READY,
            {"task_id": "verify", "attempt": 1},
            actor="daemon:worker",
        )
    assert journal.events("run-plan-binding") == before_ready


def test_projection_rejects_malformed_checkpoint_fields_and_task_state(
    tmp_path: Path,
) -> None:
    request = _planning_request("run-checkpoint-validation")
    journal = _named_journal(tmp_path, "checkpoint-validation")
    coordinator = AlphaV2Coordinator(
        journal,
        _Provider(_draft(tasks=(_task("implement", allowed_paths=("src",)),))),
        _Executor((_success(),)),
        AlphaV2PolicyKernel(),
    )
    plan, _ = coordinator.compile_and_admit(
        "run-checkpoint-validation", request, actor="daemon:planner"
    )
    state = coordinator.execute(
        "run-checkpoint-validation",
        request,
        plan,
        actor="daemon:worker",
    )
    projection = AlphaV2RunProjection()
    checkpoint = cast("dict[str, object]", projection.dump_state(state))
    assert projection.load_state(checkpoint) == state

    def top_level(field: str, value: object) -> Callable[[dict[str, object]], None]:
        def mutate(payload: dict[str, object]) -> None:
            payload[field] = value

        return mutate

    def task_level(field: str, value: object) -> Callable[[dict[str, object]], None]:
        def mutate(payload: dict[str, object]) -> None:
            tasks = cast("list[object]", payload["tasks"])
            cast("dict[str, object]", tasks[0])[field] = value

        return mutate

    def remove_run_id(payload: dict[str, object]) -> None:
        payload.pop("run_id")

    def add_unknown_field(payload: dict[str, object]) -> None:
        payload["unknown"] = True

    def partial_pending_policy(payload: dict[str, object]) -> None:
        tasks = cast("list[object]", payload["tasks"])
        cast("dict[str, object]", tasks[0])["pending_policy_decision_id"] = DIGEST

    def misplaced_pending_policy(payload: dict[str, object]) -> None:
        tasks = cast("list[object]", payload["tasks"])
        task = cast("dict[str, object]", tasks[0])
        task["pending_policy_decision_id"] = DIGEST
        task["pending_policy_action_digest"] = DIGEST
        task["pending_policy_allowed"] = True
        task["pending_policy_reason"] = "bounded-task-authorized"

    mutations = (
        remove_run_id,
        add_unknown_field,
        top_level("run_id", ""),
        top_level("goal_digest", "bad"),
        top_level("goal_base_commit", "bad"),
        top_level("classification", True),
        top_level("budget", {"bad": 0}),
        top_level("plan_version", True),
        top_level("pending_draft_digest", "bad"),
        top_level("tasks", "not-a-list"),
        task_level("depends_on", "not-a-list"),
        task_level("evidence_event_ids", [1]),
        task_level("max_attempts", 4),
        task_level("attempts", 4),
        task_level("depends_on", ["implement"]),
        task_level("depends_on", ["left", "left"]),
        task_level("allowed_paths", ["src", "src"]),
        task_level("last_artifact_digests", "not-a-list"),
        task_level("last_artifact_digests", [DIGEST, DIGEST]),
        task_level("head_commit", "bad"),
        task_level("pending_policy_allowed", "true"),
        partial_pending_policy,
        misplaced_pending_policy,
    )
    for mutate in mutations:
        candidate = deepcopy(checkpoint)
        mutate(candidate)
        with pytest.raises((AlphaV2RuntimeError, ValueError)):
            projection.load_state(candidate)


def test_projection_rejects_malformed_attempt_evidence_before_append(
    tmp_path: Path,
) -> None:
    request = _planning_request("run-evidence-validation")
    journal = _named_journal(tmp_path, "evidence-validation")
    coordinator = AlphaV2Coordinator(
        journal,
        _Provider(_draft(tasks=(_task("implement", allowed_paths=("src",)),))),
        _Executor((_success(),)),
        AlphaV2PolicyKernel(),
    )
    plan, _ = coordinator.compile_and_admit(
        "run-evidence-validation", request, actor="daemon:planner"
    )
    journal.append(
        "run-evidence-validation",
        ALPHA_V2_TASK_READY,
        {"task_id": "implement", "attempt": 1},
        actor="daemon:worker",
    )
    task = plan.tasks[0]
    action = ToolActionRequest(
        run_id="run-evidence-validation",
        plan_id=plan.plan_id,
        task_id=task.task_id,
        attempt=1,
        capability="repository-task",
        allowed_paths=task.allowed_paths,
    )
    decision = AlphaV2PolicyKernel().authorize(request.goal, plan, task, action)
    journal.append(
        "run-evidence-validation",
        ALPHA_V2_POLICY_DECIDED,
        {
            "task_id": task.task_id,
            "attempt": 1,
            "allowed": decision.allowed,
            "reason": decision.reason,
            "action_digest": decision.action_digest,
            "decision_id": decision.decision_id,
        },
        actor="daemon:worker",
    )
    workspace_id = (
        "workspace-"
        + json_digest(
            {"plan_id": plan.plan_id, "task_id": task.task_id, "attempt": 1}
        ).removeprefix("sha256:")[:32]
    )
    journal.append(
        "run-evidence-validation",
        ALPHA_V2_TASK_STARTED,
        {
            "task_id": task.task_id,
            "attempt": 1,
            "workspace_id": workspace_id,
            "base_commit": BASE_COMMIT,
            "action_digest": decision.action_digest,
            "decision_id": decision.decision_id,
        },
        actor="daemon:worker",
    )
    journal.append(
        "run-evidence-validation",
        ALPHA_V2_TASK_VERIFYING,
        {"task_id": task.task_id, "attempt": 1, "workspace_id": workspace_id},
        actor="daemon:worker",
    )
    valid: dict[str, object] = {
        "task_id": task.task_id,
        "attempt": 1,
        "workspace_id": workspace_id,
        "route": AttemptRoute.SUCCEEDED.value,
        "verifier_exit_code": 0,
        "required_checks_passed": True,
        "workspace_clean": True,
        "error_signature": None,
        "same_error_count": 0,
        "artifact_digests": [DIGEST],
        "progress_digests": [DIGEST],
        "new_evidence": True,
        "failure_class": None,
        "failure_summary": None,
        "head_commit": SUCCESS_HEAD,
        "input_tokens": None,
        "output_tokens": None,
        "latency_ms": 0,
        "cost_microusd": None,
    }
    malformed = (
        {"unexpected": True},
        {"attempt": 2},
        {"workspace_id": "workspace-wrong"},
        {"verifier_exit_code": 256},
        {"same_error_count": 1},
        {"failure_class": FailureClass.LOGIC_BUG.value, "failure_summary": "unexpected"},
        {
            "route": AttemptRoute.REPAIRABLE.value,
            "verifier_exit_code": 1,
            "required_checks_passed": False,
        },
        {
            "route": AttemptRoute.REPAIRABLE.value,
            "verifier_exit_code": 1,
            "required_checks_passed": False,
            "failure_class": FailureClass.LOGIC_BUG.value,
            "failure_summary": "x" * 4_097,
        },
        {"progress_digests": ["sha256:" + "e" * 64]},
        {"new_evidence": False},
        {"workspace_clean": False},
        {"artifact_digests": []},
        {"artifact_digests": [DIGEST, DIGEST]},
        {"required_checks_passed": 1},
        {"error_signature": DIGEST, "same_error_count": 1},
    )
    before = journal.events("run-evidence-validation")
    for changes in malformed:
        candidate = {**valid, **changes}
        with pytest.raises((AlphaV2RuntimeError, ValueError)):
            journal.append(
                "run-evidence-validation",
                ALPHA_V2_TASK_VERIFIED,
                cast("dict[str, JsonValue]", candidate),
                actor="daemon:worker",
            )
        assert journal.events("run-evidence-validation") == before


def test_projection_rejects_invalid_or_contradictory_public_terminal_events(
    tmp_path: Path,
) -> None:
    request = _planning_request("run-public-terminal")
    journal = _named_journal(tmp_path, "public-terminal")
    coordinator = AlphaV2Coordinator(
        journal,
        _Provider(_draft(tasks=(_task("implement", allowed_paths=("src",)),))),
        _Executor((_success(),)),
        AlphaV2PolicyKernel(),
    )
    coordinator.compile_and_admit("run-public-terminal", request, actor="daemon:planner")
    state = journal.rehydrate("run-public-terminal")
    projection = AlphaV2RunProjection()

    def public_event(
        event_type: str,
        *,
        current=state,
        stream_id: str = "alpha:run:run-public-terminal",
        sequence: int | None = None,
    ) -> EventEnvelope:
        return EventEnvelope.create(
            stream_id=stream_id,
            stream_sequence=(current.last_stream_sequence + 1 if sequence is None else sequence),
            event_type=event_type,
            actor="daemon:public",
            source=ALPHA_EVENT_SOURCE,
            payload={"run_id": current.run_id},
            correlation_id=current.run_id,
            causation_id=current.latest_event_id,
        )

    with pytest.raises(AlphaV2RuntimeError, match="invalid-alpha-v2-transition"):
        projection.apply(state, public_event(ALPHA_RUN_SUCCEEDED))
    with pytest.raises(AlphaV2RuntimeError, match="invalid-alpha-v2-event"):
        projection.apply(
            state,
            public_event(ALPHA_RUN_FAILED, stream_id="wrong-stream"),
        )
    with pytest.raises(AlphaV2RuntimeError, match="invalid-alpha-v2-event"):
        projection.apply(
            state,
            public_event(ALPHA_RUN_FAILED, sequence=state.last_stream_sequence + 2),
        )

    failed = projection.apply(state, public_event(ALPHA_RUN_FAILED))
    assert failed is not None
    assert failed.status is RunLifecycleStatus.TERMINAL_FAILURE
    assert failed.task("implement").status is TaskLifecycleStatus.TERMINAL_FAILURE
    with pytest.raises(AlphaV2RuntimeError, match="event-after-alpha-v2-terminal"):
        projection.apply(
            failed,
            public_event(ALPHA_RUN_CANCELED, current=failed),
        )
    escalated = projection.apply(state, public_event(ALPHA_RUN_RECONCILIATION_REQUIRED))
    assert escalated is not None
    assert escalated.status is RunLifecycleStatus.ESCALATED
    canceled = projection.apply(state, public_event(ALPHA_RUN_CANCELED))
    assert canceled is not None
    assert canceled.status is RunLifecycleStatus.CANCELED

    legacy_without_v2 = EventEnvelope.create(
        stream_id="alpha:run:public-only",
        stream_sequence=1,
        event_type=ALPHA_RUN_SUCCEEDED,
        actor="daemon:public",
        source=ALPHA_EVENT_SOURCE,
        payload={"run_id": "public-only"},
    )
    assert projection.apply(None, legacy_without_v2) is None
    unknown = EventEnvelope.create(
        stream_id="alpha:run:run-public-terminal",
        stream_sequence=state.last_stream_sequence + 1,
        event_type="unknown.event",
        actor="daemon:test",
        source="unknown.source",
        payload={"run_id": state.run_id},
    )
    with pytest.raises(AlphaV2RuntimeError, match="invalid-alpha-v2-event"):
        projection.apply(state, unknown)


def test_journal_and_coordinator_fail_closed_across_remaining_boundary_errors(
    tmp_path: Path,
) -> None:
    projection = AlphaV2RunProjection()
    with pytest.raises(AlphaV2RuntimeError, match="invalid-alpha-v2-event"):
        projection.load_state("not-a-checkpoint")
    with pytest.raises(ValueError, match="share one database"):
        EventBackedAlphaV2RunJournal(
            EventStore(tmp_path / "events.sqlite3"),
            CheckpointStore(tmp_path / "checkpoints.sqlite3"),
        )

    empty = _named_journal(tmp_path, "empty-boundaries")
    with pytest.raises(AlphaV2RuntimeError, match="goal-not-found"):
        empty.goal("run-empty-boundaries")
    with pytest.raises(AlphaV2RuntimeError, match="plan-not-found"):
        empty.plan("run-empty-boundaries")
    with pytest.raises(AlphaV2RuntimeError, match="invalid-alpha-v2-event"):
        empty.append(
            "run-empty-boundaries",
            "unknown.event",
            {},
            actor="daemon:test",
        )
    with pytest.raises(AlphaV2RuntimeError, match="invalid-alpha-v2-event"):
        empty.append(
            "run-empty-boundaries",
            ALPHA_V2_GOAL_ADMITTED,
            {"run_id": "injected"},
            actor="daemon:test",
        )
    with pytest.raises(AlphaV2RuntimeError, match="invalid-alpha-v2-run-id"):
        empty.append("../bad-run", ALPHA_V2_GOAL_ADMITTED, {}, actor="daemon:test")

    admitted = _named_journal(tmp_path, "admitted-boundaries")
    request = _planning_request("run-admitted-boundaries")
    goal = request.goal
    goal_payload = {
        "goal_id": goal.goal_id,
        "goal_digest": goal.digest,
        "goal": alpha_goal_payload(goal),
        "classification": request.classification.value,
        "locality": request.locality.value,
        "budget": {
            "max_input_tokens": request.budget.max_input_tokens,
            "max_output_tokens": request.budget.max_output_tokens,
            "max_latency_ms": request.budget.max_latency_ms,
            "max_cost_microusd": request.budget.max_cost_microusd,
        },
    }
    admitted.append(
        "run-admitted-boundaries",
        ALPHA_V2_GOAL_ADMITTED,
        goal_payload,
        actor="daemon:planner",
    )
    assert admitted.goal("run-admitted-boundaries") == goal
    with pytest.raises(AlphaV2RuntimeError, match="idempotency-conflict"):
        admitted.append(
            "run-admitted-boundaries",
            ALPHA_V2_GOAL_ADMITTED,
            goal_payload,
            actor="other:planner",
        )
    admitted_coordinator = AlphaV2Coordinator(
        admitted,
        _Provider(_draft(tasks=(_task("implement", allowed_paths=("src",)),))),
        _Executor((_success(),)),
        AlphaV2PolicyKernel(),
    )
    reconciled = admitted_coordinator.reconcile_incomplete_planning(
        "run-admitted-boundaries", actor="daemon:worker"
    )
    assert reconciled.status is RunLifecycleStatus.ESCALATED
    with pytest.raises(AlphaV2RuntimeError, match="planning-not-ambiguous"):
        admitted_coordinator.reconcile_incomplete_planning(
            "run-admitted-boundaries", actor="daemon:worker"
        )

    running = _named_journal(tmp_path, "running-boundaries")
    running_request = _planning_request("run-running-boundaries")
    running_coordinator = AlphaV2Coordinator(
        running,
        _Provider(_draft(tasks=(_task("implement", allowed_paths=("src",)),))),
        _Executor((_success(),)),
        AlphaV2PolicyKernel(),
    )
    running_plan, _ = running_coordinator.compile_and_admit(
        "run-running-boundaries", running_request, actor="daemon:planner"
    )
    assert running.plan("run-running-boundaries") == running_plan
    with pytest.raises(AlphaV2RuntimeError, match="invalid-alpha-v2-transition"):
        running.append(
            "run-running-boundaries",
            ALPHA_V2_PLAN_DRAFT_RECEIVED,
            {
                "draft_digest": running_plan.draft_digest,
                "provider_output_digest": running_plan.draft_digest,
                "profile_id": "recorded-plan",
                "adapter_id": "recorded-plan",
                "model_id": "recorded-plan",
                "input_tokens": 1,
                "output_tokens": 1,
                "latency_ms": 1,
                "cost_microusd": 0,
            },
            actor="daemon:planner",
        )
    with pytest.raises(AlphaV2RuntimeError, match="invalid-alpha-v2-transition"):
        running.append(
            "run-running-boundaries",
            ALPHA_V2_REPLAN_STARTED,
            {
                "previous_plan_id": running_plan.plan_id,
                "previous_plan_version": 1,
                "next_plan_version": 2,
            },
            actor="daemon:planner",
        )
    with pytest.raises(AlphaV2RuntimeError, match="invalid-alpha-v2-transition"):
        running.append(
            "run-running-boundaries",
            ALPHA_V2_RUN_TERMINATED,
            {"status": RunLifecycleStatus.RUNNING.value, "reason": "not-terminal"},
            actor="daemon:worker",
        )
    with pytest.raises(AlphaV2RuntimeError, match="invalid-alpha-v2-transition"):
        running.append(
            "run-running-boundaries",
            ALPHA_V2_RUN_TERMINATED,
            {"status": RunLifecycleStatus.SUCCEEDED.value, "reason": "premature"},
            actor="daemon:worker",
        )
    with pytest.raises(AlphaV2RuntimeError, match="invalid-alpha-v2-transition"):
        running.append(
            "run-running-boundaries",
            ALPHA_V2_TASK_VERIFYING,
            {
                "task_id": "implement",
                "attempt": 1,
                "workspace_id": "workspace-not-started",
            },
            actor="daemon:worker",
        )
    running.append(
        "run-running-boundaries",
        ALPHA_V2_TASK_READY,
        {"task_id": "implement", "attempt": 1},
        actor="daemon:worker",
    )
    with pytest.raises(AlphaV2RuntimeError, match="invalid-alpha-v2-transition"):
        running.append(
            "run-running-boundaries",
            ALPHA_V2_TASK_BLOCKED,
            {"task_id": "implement", "reason": "unbound-policy"},
            actor="daemon:worker",
        )
    running_task = running_plan.tasks[0]
    running_action = ToolActionRequest(
        run_id="run-running-boundaries",
        plan_id=running_plan.plan_id,
        task_id=running_task.task_id,
        attempt=1,
        capability="repository-task",
        allowed_paths=running_task.allowed_paths,
    )
    running_decision = AlphaV2PolicyKernel().authorize(
        running_request.goal,
        running_plan,
        running_task,
        running_action,
    )
    running.append(
        "run-running-boundaries",
        ALPHA_V2_POLICY_DECIDED,
        {
            "task_id": running_task.task_id,
            "attempt": 1,
            "allowed": running_decision.allowed,
            "reason": running_decision.reason,
            "action_digest": running_decision.action_digest,
            "decision_id": running_decision.decision_id,
        },
        actor="daemon:worker",
    )
    alternate_decision = PolicyDecision(
        True,
        "alternate-bounded-authorization",
        running_action.digest,
    )
    with pytest.raises(AlphaV2RuntimeError, match="invalid-alpha-v2-transition"):
        running.append(
            "run-running-boundaries",
            ALPHA_V2_POLICY_DECIDED,
            {
                "task_id": running_task.task_id,
                "attempt": 1,
                "allowed": alternate_decision.allowed,
                "reason": alternate_decision.reason,
                "action_digest": alternate_decision.action_digest,
                "decision_id": alternate_decision.decision_id,
            },
            actor="daemon:worker",
        )
    with pytest.raises(AlphaV2RuntimeError, match="replan-not-required"):
        running_coordinator.exhaust_replan_budget("run-running-boundaries", actor="daemon:worker")
    with pytest.raises(AlphaV2RuntimeError, match="run-binding-mismatch"):
        running_coordinator.compile_and_admit(
            "run-running-boundaries",
            running_request,
            actor="daemon:planner",
            previous=running_plan,
        )
    with pytest.raises(AlphaV2RuntimeError, match="run-binding-mismatch"):
        running_coordinator.execute(
            "different-run",
            running_request,
            running_plan,
            actor="daemon:worker",
        )
    with pytest.raises(AlphaV2RuntimeError, match="run-binding-mismatch"):
        running_coordinator.execute(
            "run-running-boundaries",
            replace(running_request, budget=GatewayBudget(1, 1, 1, 1)),
            running_plan,
            actor="daemon:worker",
        )

    exhausted_request = replace(
        _planning_request("run-budget-exhausted"),
        budget=GatewayBudget(0, 4_096, 120_000, 0),
    )
    exhausted_executor = _Executor((_success(),))
    exhausted = AlphaV2Coordinator(
        _named_journal(tmp_path, "budget-exhausted"),
        _Provider(_draft(tasks=(_task("implement", allowed_paths=("src",)),))),
        exhausted_executor,
        AlphaV2PolicyKernel(),
    )
    exhausted_plan, _ = exhausted.compile_and_admit(
        "run-budget-exhausted", exhausted_request, actor="daemon:planner"
    )
    exhausted_state = exhausted.execute(
        "run-budget-exhausted",
        exhausted_request,
        exhausted_plan,
        actor="daemon:worker",
    )
    assert exhausted_state.status is RunLifecycleStatus.ESCALATED
    assert exhausted_executor.workspaces == []
    assert exhausted.journal.promotion_candidate("run-budget-exhausted") is None

    overdrawn_request = replace(
        _planning_request("run-budget-overdrawn"),
        budget=GatewayBudget(1, 4_096, 120_000, 0),
    )
    overdrawn = AlphaV2Coordinator(
        _named_journal(tmp_path, "budget-overdrawn"),
        _Provider(_draft(tasks=(_task("implement", allowed_paths=("src",)),))),
        _Executor((replace(_success(), input_tokens=2, output_tokens=0, cost_microusd=0),)),
        AlphaV2PolicyKernel(),
    )
    overdrawn_plan, _ = overdrawn.compile_and_admit(
        "run-budget-overdrawn", overdrawn_request, actor="daemon:planner"
    )
    overdrawn_state = overdrawn.execute(
        "run-budget-overdrawn",
        overdrawn_request,
        overdrawn_plan,
        actor="daemon:worker",
    )
    assert overdrawn_state.status is RunLifecycleStatus.ESCALATED

    public_database = tmp_path / "public-prefix.sqlite3"
    public_events = EventStore(public_database)
    first = EventEnvelope.create(
        stream_id="alpha:run:run-public-prefix",
        stream_sequence=1,
        event_type=ALPHA_RUN_FAILED,
        actor="daemon:public",
        source=ALPHA_EVENT_SOURCE,
        payload={"run_id": "run-public-prefix"},
    )
    public_events.append(first, expected_sequence=0)
    second = EventEnvelope.create(
        stream_id="alpha:run:run-public-prefix",
        stream_sequence=2,
        event_type=ALPHA_RUN_CANCELED,
        actor="daemon:public",
        source=ALPHA_EVENT_SOURCE,
        payload={"run_id": "run-public-prefix"},
        causation_id=first.event_id,
    )
    public_events.append(second, expected_sequence=1)
    public_journal = EventBackedAlphaV2RunJournal(
        public_events,
        CheckpointStore(public_database),
    )
    with pytest.raises(AlphaV2RuntimeError, match="public-run-already-active"):
        public_journal.append(
            "run-public-prefix",
            ALPHA_V2_GOAL_ADMITTED,
            goal_payload,
            actor="daemon:planner",
        )

    corrupt_database = tmp_path / "corrupt-stream.sqlite3"
    corrupt_events = EventStore(corrupt_database)
    corrupt_events.append(
        EventEnvelope.create(
            stream_id="alpha:run:run-corrupt",
            stream_sequence=1,
            event_type="unknown.event",
            actor="daemon:test",
            source="unknown.source",
            payload={"run_id": "run-corrupt"},
        ),
        expected_sequence=0,
    )
    corrupt_journal = EventBackedAlphaV2RunJournal(
        corrupt_events,
        CheckpointStore(corrupt_database),
    )
    with pytest.raises(AlphaV2RuntimeError, match="invalid-alpha-v2-event"):
        corrupt_journal.events("run-corrupt")


def test_journal_surfaces_concurrent_append_without_persisting_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "concurrent.sqlite3"
    events = EventStore(database)
    journal = EventBackedAlphaV2RunJournal(events, CheckpointStore(database))
    goal = replace(_goal(), goal_id="goal-concurrent")
    admitted = journal.append(
        "run-concurrent",
        ALPHA_V2_GOAL_ADMITTED,
        {
            "goal_id": goal.goal_id,
            "goal_digest": goal.digest,
            "goal": alpha_goal_payload(goal),
            "classification": DataClassification.PRIVATE.value,
            "locality": LocalityPolicy.REMOTE_ALLOWED.value,
            "budget": {
                "max_input_tokens": 32_000,
                "max_output_tokens": 4_096,
                "max_latency_ms": 120_000,
                "max_cost_microusd": 0,
            },
        },
        actor="daemon:planner",
    )
    original_append = events.append

    def race(event: EventEnvelope, *, expected_sequence: int) -> EventEnvelope:
        competing = EventEnvelope.create(
            stream_id="alpha:run:run-concurrent",
            stream_sequence=2,
            event_type=ALPHA_V2_PLAN_DRAFT_RECEIVED,
            actor="other:planner",
            source=ALPHA_V2_EVENT_SOURCE,
            payload={
                "run_id": "run-concurrent",
                "draft_digest": DIGEST,
                "provider_output_digest": DIGEST,
                "profile_id": "agy-plan",
                "adapter_id": "agy-cli",
                "model_id": "gemini-plan-model",
                "input_tokens": None,
                "output_tokens": None,
                "latency_ms": 1,
                "cost_microusd": None,
            },
            correlation_id="run-concurrent",
            causation_id=admitted.event_id,
            idempotency_key="competing-draft",
        )
        original_append(competing, expected_sequence=expected_sequence)
        return original_append(event, expected_sequence=expected_sequence)

    monkeypatch.setattr(events, "append", race)

    with pytest.raises(AlphaV2RuntimeError, match="concurrency-conflict"):
        journal.append(
            "run-concurrent",
            ALPHA_V2_PLAN_DRAFT_RECEIVED,
            {
                "draft_digest": "sha256:" + "e" * 64,
                "provider_output_digest": DIGEST,
                "profile_id": "agy-plan",
                "adapter_id": "agy-cli",
                "model_id": "gemini-plan-model",
                "input_tokens": None,
                "output_tokens": None,
                "latency_ms": 1,
                "cost_microusd": None,
            },
            actor="daemon:planner",
        )

    assert tuple(item.actor for item in events.read_stream("alpha:run:run-concurrent")) == (
        "daemon:planner",
        "other:planner",
    )


def test_no_progress_breaker_escalates_and_emits_typed_praxis_candidate(
    tmp_path: Path,
) -> None:
    failure = AttemptEvidence(
        workspace_clean=True,
        verifier_exit_code=1,
        required_checks_passed=False,
        failure_class=FailureClass.CONTRACT_MISMATCH,
        failure_summary="Expected contract 9 but received contract 10",
        artifact_digests=(DIGEST,),
        progress_digests=(DIGEST,),
        head_commit=FAILED_HEAD,
    )
    provider = _Provider(_draft(tasks=(_task("implement", allowed_paths=("src",)),)))
    executor = _Executor((failure, failure, _success()))
    journal = _journal(tmp_path)
    coordinator = AlphaV2Coordinator(journal, provider, executor, AlphaV2PolicyKernel())
    request = _planning_request("run-no-progress")
    plan, _ = coordinator.compile_and_admit("run-no-progress", request, actor="daemon:planner")

    state = coordinator.execute("run-no-progress", request, plan, actor="daemon:worker")
    candidate = journal.promotion_candidate("run-no-progress")

    assert state.status is RunLifecycleStatus.ESCALATED
    assert state.task("implement").attempts == 2
    assert state.task("implement").same_error_count == 2
    assert len(executor.workspaces) == 2
    assert candidate is not None
    assert candidate.schema_version == "praxis-promotion-candidate/v1"
    assert candidate.kind == "repeated-error-signature"
    assert len(candidate.evidence_event_ids) == len(candidate.evidence_digests) == 2


@settings(max_examples=40, deadline=None)
@given(first=st.integers(min_value=0), second=st.integers(min_value=0))
def test_error_signature_ignores_volatile_numeric_details(first: int, second: int) -> None:
    baseline = AttemptEvidence(
        workspace_clean=True,
        verifier_exit_code=1,
        required_checks_passed=False,
        failure_class=FailureClass.CONTRACT_MISMATCH,
        failure_summary=f"expected contract {first} at line {second}",
        artifact_digests=(DIGEST,),
        progress_digests=(DIGEST,),
        head_commit=FAILED_HEAD,
    )
    changed = replace(
        baseline,
        failure_summary=f"expected contract {second + 1} at line {first + 1}",
    )

    assert baseline.error_signature == changed.error_signature


def test_cumulative_known_usage_stops_later_repair_effects(tmp_path: Path) -> None:
    failure = AttemptEvidence(
        workspace_clean=True,
        verifier_exit_code=1,
        required_checks_passed=False,
        failure_class=FailureClass.LOGIC_BUG,
        failure_summary="bounded failure",
        artifact_digests=(DIGEST,),
        progress_digests=(DIGEST,),
        head_commit=FAILED_HEAD,
        input_tokens=7,
        output_tokens=1,
        latency_ms=10,
        cost_microusd=0,
    )
    provider = _Provider(
        _draft(tasks=(_task("implement", allowed_paths=("src",)),)),
        input_tokens=4,
        output_tokens=1,
        cost_microusd=0,
    )
    executor = _Executor((failure, _success()))
    journal = _journal(tmp_path)
    coordinator = AlphaV2Coordinator(journal, provider, executor, AlphaV2PolicyKernel())
    request = replace(
        _planning_request("run-budget"),
        budget=GatewayBudget(10, 10, 1_000, 0),
    )
    plan, _ = coordinator.compile_and_admit("run-budget", request, actor="daemon:planner")

    state = coordinator.execute("run-budget", request, plan, actor="daemon:worker")

    assert state.status is RunLifecycleStatus.ESCALATED
    assert state.input_tokens == 11
    assert state.input_tokens_complete is True
    assert len(executor.workspaces) == 1
    assert executor.budgets[0].max_input_tokens == 6


def test_replan_creates_one_immutable_successor_then_executes(tmp_path: Path) -> None:
    first_failure = AttemptEvidence(
        workspace_clean=True,
        verifier_exit_code=1,
        required_checks_passed=False,
        failure_class=FailureClass.INVALID_ASSUMPTION,
        failure_summary="the admitted assumption is invalid",
        artifact_digests=(DIGEST,),
        progress_digests=(DIGEST,),
        head_commit=FAILED_HEAD,
        input_tokens=1,
        output_tokens=1,
        latency_ms=1,
        cost_microusd=0,
    )
    provider = _SequenceProvider(
        (
            _draft(tasks=(_task("discover", allowed_paths=("src",)),)),
            _draft(tasks=(_task("implement", allowed_paths=("src",)),)),
        )
    )
    executor = _SequentialExecutor((first_failure, _success()))
    journal = _journal(tmp_path)
    kernel = ProductionAlphaV2Kernel(
        AlphaV2Coordinator(journal, provider, executor, AlphaV2PolicyKernel())
    )
    request = _planning_request("run-replan")

    first = kernel.process(request, actor="daemon:worker")
    first_plan = journal.plan("run-replan")
    second = kernel.process(request, actor="daemon:worker")
    second_plan = journal.plan("run-replan")

    assert first.status is RunLifecycleStatus.REPLAN_REQUIRED
    assert second.status is RunLifecycleStatus.SUCCEEDED
    assert first_plan.plan_version == 1
    assert second_plan.plan_version == 2
    assert second_plan.supersedes_plan_id == first_plan.plan_id
    assert second_plan.plan_id != first_plan.plan_id
    assert provider.calls == 2
    assert executor.calls == 2
    assert kernel.process(request, actor="daemon:worker") == second
    assert provider.calls == 2


def test_second_replan_request_exhausts_budget_without_another_provider_call(
    tmp_path: Path,
) -> None:
    invalid_assumption = AttemptEvidence(
        workspace_clean=True,
        verifier_exit_code=1,
        required_checks_passed=False,
        failure_class=FailureClass.INVALID_ASSUMPTION,
        failure_summary="the admitted assumption remains invalid",
        artifact_digests=(DIGEST,),
        progress_digests=(DIGEST,),
        head_commit=FAILED_HEAD,
        input_tokens=1,
        output_tokens=1,
        latency_ms=1,
        cost_microusd=0,
    )
    provider = _SequenceProvider(
        (
            _draft(tasks=(_task("discover", allowed_paths=("src",)),)),
            _draft(tasks=(_task("discover", allowed_paths=("src",)),)),
        )
    )
    executor = _SequentialExecutor((invalid_assumption, invalid_assumption))
    journal = _journal(tmp_path)
    kernel = ProductionAlphaV2Kernel(
        AlphaV2Coordinator(journal, provider, executor, AlphaV2PolicyKernel())
    )
    request = _planning_request("run-replan-exhausted")

    first = kernel.process(request, actor="daemon:worker")
    second = kernel.process(request, actor="daemon:worker")
    terminal = kernel.process(request, actor="daemon:worker")
    terminal_payload = cast(
        "dict[str, JsonValue]",
        thaw_json(journal.events(request.run_id)[-1].payload),
    )

    assert first.status is RunLifecycleStatus.REPLAN_REQUIRED
    assert second.status is RunLifecycleStatus.REPLAN_REQUIRED
    assert journal.plan(request.run_id).plan_version == 2
    assert terminal.status is RunLifecycleStatus.ESCALATED
    assert terminal_payload["reason"] == "replan-budget-exhausted"
    assert provider.calls == executor.calls == 2
    assert kernel.process(request, actor="daemon:worker") == terminal
    assert provider.calls == executor.calls == 2


def test_dependency_heads_cover_diamond_redundant_and_divergent_edges(tmp_path: Path) -> None:
    draft = _draft(
        tasks=(
            _task("write", allowed_paths=("src",)),
            _task("left", depends_on=("write",)),
            _task("right", depends_on=("write",)),
            _task(
                "merge",
                depends_on=("write", "left", "right"),
                allowed_paths=("src",),
            ),
        )
    )
    first_head = "1" * 40
    final_head = "2" * 40
    outcomes = {
        "write": replace(_success(), head_commit=first_head),
        "left": replace(_success(), head_commit=first_head),
        "right": replace(_success(), head_commit=first_head),
        "merge": replace(_success(), head_commit=final_head),
    }
    executor = _TaskExecutor(outcomes)
    coordinator = AlphaV2Coordinator(
        _named_journal(tmp_path, "diamond"),
        _Provider(draft),
        executor,
        AlphaV2PolicyKernel(),
    )
    request = _planning_request("run-diamond")
    plan, _ = coordinator.compile_and_admit("run-diamond", request, actor="daemon:planner")

    state = coordinator.execute("run-diamond", request, plan, actor="daemon:worker")

    assert state.status is RunLifecycleStatus.SUCCEEDED
    assert executor.base_commits == {
        "write": BASE_COMMIT,
        "left": first_head,
        "right": first_head,
        "merge": first_head,
    }

    divergent = _TaskExecutor(
        {
            **outcomes,
            "right": replace(_success(), head_commit="3" * 40),
        }
    )
    divergent_coordinator = AlphaV2Coordinator(
        _named_journal(tmp_path, "divergent"),
        _Provider(draft),
        divergent,
        AlphaV2PolicyKernel(),
    )
    divergent_request = _planning_request("run-divergent")
    divergent_plan, _ = divergent_coordinator.compile_and_admit(
        "run-divergent", divergent_request, actor="daemon:planner"
    )

    divergent_state = divergent_coordinator.execute(
        "run-divergent",
        divergent_request,
        divergent_plan,
        actor="daemon:worker",
    )

    assert divergent_state.status is RunLifecycleStatus.REPLAN_REQUIRED
    assert "merge" not in divergent.base_commits


def test_policy_kernel_denies_scope_before_execution_and_routes_failure_classes() -> None:
    goal = _goal()
    plan = compile_alpha_plan(
        goal,
        _draft(tasks=(_task("implement", allowed_paths=("src",)),)),
    )
    task = plan.tasks[0]
    policy = AlphaV2PolicyKernel()
    denied = policy.authorize(
        goal,
        plan,
        task,
        ToolActionRequest(
            run_id="run-policy",
            plan_id=plan.plan_id,
            task_id=task.task_id,
            attempt=1,
            capability="repository-task",
            allowed_paths=("outside",),
        ),
    )

    assert denied.allowed is False
    assert denied.reason == "intent-scope-violation"
    first_action = ToolActionRequest(
        run_id="run-policy",
        plan_id=plan.plan_id,
        task_id=task.task_id,
        attempt=1,
        capability="repository-task",
        allowed_paths=task.allowed_paths,
    )
    second_action = replace(first_action, attempt=2)
    first_decision = policy.authorize(goal, plan, task, first_action)
    second_decision = policy.authorize(goal, plan, task, second_action)
    assert first_decision.allowed is True
    assert first_decision.action_digest == first_action.digest
    assert first_decision.decision_id != second_decision.decision_id
    assert (
        policy.route(
            goal,
            task,
            AttemptEvidence(
                True,
                1,
                False,
                FailureClass.INVALID_ASSUMPTION,
                "dependency is unavailable",
                (DIGEST,),
                (DIGEST,),
                FAILED_HEAD,
            ),
            attempt=1,
            same_error_count=1,
            new_evidence=True,
        )
        is AttemptRoute.REPLAN_REQUIRED
    )
    failed_required_check = AttemptEvidence(
        True,
        0,
        False,
        FailureClass.LOGIC_BUG,
        "required verifier rejected the artifact",
        (DIGEST,),
        (DIGEST,),
        FAILED_HEAD,
    )
    assert (
        policy.route(
            goal,
            task,
            failed_required_check,
            attempt=1,
            same_error_count=1,
            new_evidence=True,
        )
        is AttemptRoute.REPAIRABLE
    )
    assert (
        policy.route(
            goal,
            task,
            failed_required_check,
            attempt=min(goal.max_attempts, task.max_attempts),
            same_error_count=1,
            new_evidence=True,
        )
        is AttemptRoute.ESCALATED
    )


def test_event_journal_exports_run_plan_task_and_attempt_correlations(tmp_path: Path) -> None:
    recorder = TraceRecorder()
    path = tmp_path / "kernel.sqlite3"
    journal = EventBackedAlphaV2RunJournal(
        EventStore(path),
        CheckpointStore(path),
        observer=AlphaV2TraceObserver(recorder),
    )
    provider = _Provider(_draft(tasks=(_task("implement", allowed_paths=("src",)),)))
    evidence = replace(
        _success(),
        input_tokens=17,
        output_tokens=5,
        latency_ms=23,
        cost_microusd=41,
    )
    coordinator = AlphaV2Coordinator(
        journal,
        provider,
        _Executor((evidence,)),
        AlphaV2PolicyKernel(),
    )
    request = replace(
        _planning_request("run-telemetry"),
        budget=GatewayBudget(32_000, 4_096, 120_000, 100),
    )
    plan, _ = coordinator.compile_and_admit("run-telemetry", request, actor="daemon:planner")

    coordinator.execute("run-telemetry", request, plan, actor="daemon:worker")

    records = recorder.records(trace_id="run-telemetry")
    assert records
    assert {item.correlation_ids["run_id"] for item in records} == {"run-telemetry"}
    durable_events = {item.event_id: item for item in journal.events("run-telemetry")}
    for record in records:
        event = durable_events[cast("str", record.attributes["event.id"])]
        assert record.attributes["event.type"] == event.event_type
        assert record.attributes["event.sequence"] == event.stream_sequence
        assert record.attributes["event.payload_digest"] == event.payload_hash
    task_records = [item for item in records if "task_id" in item.correlation_ids]
    assert task_records
    assert {item.correlation_ids["task_id"] for item in task_records} == {"implement"}
    assert any(item.attributes.get("attempt") == 1 for item in task_records)
    plan_record = next(item for item in records if "plan_id" in item.correlation_ids)
    assert plan_record.correlation_ids["plan_id"] == plan.plan_id
    started = next(
        item for item in records if item.attributes["event.type"] == ALPHA_V2_TASK_STARTED
    )
    assert started.correlation_ids["workspace_id"].startswith("workspace-")
    planning = next(
        item for item in records if item.attributes["event.type"] == ALPHA_V2_PLAN_DRAFT_RECEIVED
    )
    assert planning.attributes["usage.input_tokens.known"] is False
    assert planning.attributes["usage.output_tokens.known"] is False
    assert planning.attributes["usage.cost_microusd.known"] is False
    assert "usage.input_tokens" not in planning.attributes
    verified = next(
        item for item in records if item.attributes["event.type"] == ALPHA_V2_TASK_VERIFIED
    )
    assert verified.attributes["usage.input_tokens.known"] is True
    assert verified.attributes["usage.input_tokens"] == 17
    assert verified.attributes["usage.output_tokens.known"] is True
    assert verified.attributes["usage.output_tokens"] == 5
    assert verified.attributes["usage.latency_ms"] == 23
    assert verified.attributes["usage.cost_microusd.known"] is True
    assert verified.attributes["usage.cost_microusd"] == 41
    assert verified.attributes["route"] == "succeeded"
    terminal = next(
        item for item in records if item.attributes["event.type"] == ALPHA_V2_RUN_TERMINATED
    )
    assert terminal.attributes["status"] == "succeeded"
    assert terminal.attributes["reason"] == "verification-passed"


def test_trace_observer_prefers_payload_run_and_omits_non_typed_optional_metadata() -> None:
    recorder = TraceRecorder()
    observer = AlphaV2TraceObserver(recorder)
    event = EventEnvelope.create(
        stream_id="alpha:run:payload-run",
        stream_sequence=1,
        event_type=ALPHA_V2_TASK_STARTED,
        actor="daemon:test",
        source=ALPHA_V2_EVENT_SOURCE,
        payload={
            "run_id": "payload-run",
            "plan_id": 17,
            "task_id": "",
            "workspace_id": False,
            "attempt": True,
            "latency_ms": True,
        },
        correlation_id="fallback-run",
    )

    observer.record(event)

    (record,) = recorder.records(trace_id="payload-run")
    assert record.correlation_ids == {"run_id": "payload-run"}
    assert "attempt" not in record.attributes
    assert "usage.latency_ms" not in record.attributes
    assert recorder.records(trace_id="fallback-run") == ()


def test_alpha_v2_contracts_reject_malformed_identity_scope_and_evidence() -> None:
    goal = _goal()
    check = AlphaVerificationCheck("check-valid", ("pytest", "-q"))
    task = AlphaTaskSpec("task-valid", "Do bounded work.", (), ("src",), (check,), 3)
    plan = compile_alpha_plan(goal, _draft(tasks=(_task("implement", allowed_paths=("src",)),)))
    result = _Provider(_draft(tasks=(_task("implement", allowed_paths=("src",)),))).propose_plan(
        _planning_request("run-contracts")
    )

    invalid_goals = (
        lambda: replace(goal, schema_version="blackcell.alpha-goal/v1"),
        lambda: replace(goal, objective=" "),
        lambda: replace(goal, objective="x" * 16_385),
        lambda: replace(goal, base_commit="not-a-commit"),
        lambda: replace(goal, constraints=("duplicate", "duplicate")),
        lambda: replace(goal, allowed_paths=("../escape",)),
        lambda: replace(goal, verification_checks=()),
        lambda: replace(
            goal,
            verification_checks=(goal.verification_checks[0], goal.verification_checks[0]),
        ),
        lambda: replace(goal, max_attempts=4),
        lambda: replace(goal, same_error_limit=3),
    )
    for construct in invalid_goals:
        with pytest.raises(AlphaV2ContractError):
            construct()

    invalid_checks = (
        ("empty argv", lambda: AlphaVerificationCheck("check", ())),
        ("unsafe executable", lambda: AlphaVerificationCheck("check", ("../pytest",))),
        ("nul token", lambda: AlphaVerificationCheck("check", ("pytest", "bad\x00token"))),
        ("exit range", lambda: AlphaVerificationCheck("check", ("pytest",), 256)),
    )
    for _, construct in invalid_checks:
        with pytest.raises(AlphaV2ContractError):
            construct()

    invalid_tasks = (
        lambda: replace(task, objective=""),
        lambda: replace(task, objective="x" * 16_385),
        lambda: replace(task, depends_on=(task.task_id,)),
        lambda: replace(task, checks=()),
        lambda: replace(task, checks=(check, check)),
        lambda: replace(task, max_attempts=4),
    )
    for construct in invalid_tasks:
        with pytest.raises(AlphaV2ContractError):
            construct()

    unknown_dependency = replace(task, depends_on=("missing-task",))
    invalid_plans = (
        lambda: replace(plan, schema_version="blackcell.alpha-plan/v1"),
        lambda: replace(plan, plan_version=0),
        lambda: replace(plan, plan_version=2, supersedes_plan_id=None),
        lambda: replace(plan, plan_version=2, supersedes_plan_id=plan.plan_id),
        lambda: replace(plan, base_commit="bad"),
        lambda: replace(plan, draft_digest="bad"),
        lambda: replace(plan, tasks=()),
        lambda: replace(plan, tasks=(plan.tasks[0], plan.tasks[0])),
        lambda: replace(plan, tasks=(unknown_dependency,)),
    )
    for construct in invalid_plans:
        with pytest.raises(AlphaV2ContractError):
            construct()

    with pytest.raises(AlphaV2ContractError):
        replace(_planning_request("run-contracts"), estimated_input_tokens=-1)
    with pytest.raises(AlphaV2ContractError):
        replace(result, provider_output_digest="bad")
    with pytest.raises(AlphaV2ContractError):
        replace(result, input_tokens=-1)
    with pytest.raises(AlphaV2ContractError):
        replace(result, latency_ms=-1)
    with pytest.raises(AlphaV2ContractError):
        PolicyDecision(True, "not a bounded reason", DIGEST)

    successful = _success()
    invalid_evidence = (
        lambda: replace(successful, schema_version="blackcell.alpha-attempt-evidence/v0"),
        lambda: replace(successful, verifier_exit_code=256),
        lambda: replace(successful, failure_class=FailureClass.LOGIC_BUG),
        lambda: AttemptEvidence(
            True,
            1,
            False,
            None,
            None,
            (DIGEST,),
            (DIGEST,),
            FAILED_HEAD,
        ),
        lambda: replace(successful, artifact_digests=("bad",)),
        lambda: replace(successful, progress_digests=("bad",)),
        lambda: replace(successful, progress_digests=("sha256:" + "e" * 64,)),
        lambda: replace(successful, head_commit="bad"),
    )
    for construct in invalid_evidence:
        with pytest.raises(AlphaV2ContractError):
            construct()

    candidate = PraxisPromotionCandidate(
        "candidate-valid",
        "run-valid",
        plan.plan_id,
        plan.tasks[0].task_id,
        "repeated-error-signature",
        ("event-valid",),
        (DIGEST,),
    )
    with pytest.raises(AlphaV2ContractError):
        replace(candidate, evidence_event_ids=())
    with pytest.raises(AlphaV2ContractError):
        replace(candidate, evidence_digests=("bad",))


def test_runtime_fails_closed_on_denial_executor_error_and_ambiguous_recovery(
    tmp_path: Path,
) -> None:
    request = _planning_request("run-denied")
    denied_executor = _Executor((_success(),))
    denied_journal = _named_journal(tmp_path, "denied")
    denied = AlphaV2Coordinator(
        denied_journal,
        _Provider(_draft(tasks=(_task("implement", allowed_paths=("src",)),))),
        denied_executor,
        _DenyPolicy(),
    )
    denied_plan, _ = denied.compile_and_admit("run-denied", request, actor="daemon:planner")

    denied_state = denied.execute("run-denied", request, denied_plan, actor="daemon:worker")

    assert denied_state.status is RunLifecycleStatus.BLOCKED
    assert denied_state.task("implement").status is TaskLifecycleStatus.BLOCKED
    assert denied_executor.workspaces == []
    assert denied_journal.promotion_candidate("run-denied") is None
    assert denied.execute("run-denied", request, denied_plan, actor="daemon:worker") == denied_state

    error_request = _planning_request("run-executor-error")
    error_journal = _named_journal(tmp_path, "executor-error")
    error_coordinator = AlphaV2Coordinator(
        error_journal,
        _Provider(_draft(tasks=(_task("implement", allowed_paths=("src",)),))),
        _RaisingExecutor(),
        AlphaV2PolicyKernel(),
    )
    error_plan, _ = error_coordinator.compile_and_admit(
        "run-executor-error", error_request, actor="daemon:planner"
    )
    error_state = error_coordinator.execute(
        "run-executor-error", error_request, error_plan, actor="daemon:worker"
    )
    assert error_state.status is RunLifecycleStatus.ESCALATED

    recovery_request = _planning_request("run-ambiguous")
    recovery_journal = _named_journal(tmp_path, "ambiguous")
    recovery_coordinator = AlphaV2Coordinator(
        recovery_journal,
        _Provider(_draft(tasks=(_task("implement", allowed_paths=("src",)),))),
        _Executor((_success(),)),
        AlphaV2PolicyKernel(),
    )
    recovery_plan, _ = recovery_coordinator.compile_and_admit(
        "run-ambiguous", recovery_request, actor="daemon:planner"
    )
    recovery_journal.append(
        "run-ambiguous",
        ALPHA_V2_TASK_READY,
        {"task_id": "implement", "attempt": 1},
        actor="daemon:worker",
    )
    recovery_task = recovery_plan.tasks[0]
    recovery_action = ToolActionRequest(
        run_id="run-ambiguous",
        plan_id=recovery_plan.plan_id,
        task_id=recovery_task.task_id,
        attempt=1,
        capability="repository-task",
        allowed_paths=recovery_task.allowed_paths,
    )
    recovery_decision = AlphaV2PolicyKernel().authorize(
        recovery_request.goal,
        recovery_plan,
        recovery_task,
        recovery_action,
    )
    recovery_journal.append(
        "run-ambiguous",
        ALPHA_V2_POLICY_DECIDED,
        {
            "task_id": "implement",
            "attempt": 1,
            "allowed": recovery_decision.allowed,
            "reason": recovery_decision.reason,
            "action_digest": recovery_decision.action_digest,
            "decision_id": recovery_decision.decision_id,
        },
        actor="daemon:worker",
    )
    recovery_journal.append(
        "run-ambiguous",
        ALPHA_V2_TASK_STARTED,
        {
            "task_id": "implement",
            "attempt": 1,
            "workspace_id": "workspace-"
            + json_digest(
                {
                    "plan_id": recovery_plan.plan_id,
                    "task_id": "implement",
                    "attempt": 1,
                }
            ).removeprefix("sha256:")[:32],
            "base_commit": BASE_COMMIT,
            "action_digest": recovery_decision.action_digest,
            "decision_id": recovery_decision.decision_id,
        },
        actor="daemon:worker",
    )

    recovery_state = recovery_coordinator.execute(
        "run-ambiguous", recovery_request, recovery_plan, actor="daemon:worker"
    )
    assert recovery_state.status is RunLifecycleStatus.ESCALATED

    with pytest.raises(AlphaV2RuntimeError, match="binding-mismatch"):
        recovery_coordinator.compile_and_admit(
            "different-run", recovery_request, actor="daemon:planner"
        )
    with pytest.raises(AlphaV2RuntimeError, match="task-not-found"):
        recovery_state.task("unknown")
    projection = AlphaV2RunProjection()
    assert projection.load_state(None) is None
    with pytest.raises(AlphaV2RuntimeError, match="checkpoint"):
        projection.load_state({"tasks": "not-a-list"})


@dataclass
class _Gateway:
    output: dict[str, object]
    request: ModelRequest | None = None

    def invoke(self, request: ModelRequest) -> GatewayResult:
        self.request = request
        return GatewayResult(
            RoutingDecision(
                "agy-plan",
                "agy-cli",
                "gemini-plan-model",
                ModelCapability.REASON,
                False,
                False,
            ),
            ModelResponse(
                request.request_id,
                cast("dict[str, JsonValue]", self.output),
                "agy-plan",
                "agy-cli",
                "gemini-plan-model",
                None,
                None,
                100,
                None,
                False,
                NOW,
            ),
        )


@dataclass
class _Provider:
    draft: dict[str, object]
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_microusd: int | None = None

    def propose_plan(self, request: AlphaPlanningRequest) -> AlphaPlanningResult:
        del request
        return AlphaPlanningResult(
            draft=cast("dict[str, JsonValue]", self.draft),
            provider_output_digest=json_digest(cast("dict[str, JsonValue]", self.draft)),
            profile_id="agy-plan",
            adapter_id="agy-cli",
            model_id="gemini-plan-model",
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            latency_ms=100,
            cost_microusd=self.cost_microusd,
        )


@dataclass
class _SequenceProvider:
    drafts: tuple[dict[str, object], ...]
    calls: int = 0

    def propose_plan(self, request: AlphaPlanningRequest) -> AlphaPlanningResult:
        del request
        draft = self.drafts[self.calls]
        self.calls += 1
        return AlphaPlanningResult(
            draft=cast("dict[str, JsonValue]", draft),
            provider_output_digest=json_digest(cast("dict[str, JsonValue]", draft)),
            profile_id="agy-plan",
            adapter_id="agy-cli",
            model_id="gemini-plan-model",
            input_tokens=1,
            output_tokens=1,
            latency_ms=1,
            cost_microusd=0,
        )


@dataclass
class _Executor:
    outcomes: tuple[AttemptEvidence, ...]
    workspaces: list[str] = field(default_factory=list)
    base_commits: list[str] = field(default_factory=list)
    decisions: list[PolicyDecision] = field(default_factory=list)
    budgets: list[GatewayBudget] = field(default_factory=list)

    def execute(
        self,
        *,
        run_id: str,
        goal: AlphaGoalSpec,
        plan: object,
        task: AlphaTaskSpec,
        attempt: int,
        workspace_id: str,
        base_commit: str,
        prior_failure_class: FailureClass | None,
        prior_failure_summary: str | None,
        policy_decision: PolicyDecision,
        remaining_budget: GatewayBudget,
    ) -> AttemptEvidence:
        del run_id, goal, plan, task, prior_failure_class, prior_failure_summary
        self.workspaces.append(workspace_id)
        self.base_commits.append(base_commit)
        self.decisions.append(policy_decision)
        self.budgets.append(remaining_budget)
        return self.outcomes[attempt - 1]


@dataclass
class _SequentialExecutor:
    outcomes: tuple[AttemptEvidence, ...]
    calls: int = 0

    def execute(
        self,
        *,
        run_id: str,
        goal: AlphaGoalSpec,
        plan: AlphaPlanVersion,
        task: AlphaTaskSpec,
        attempt: int,
        workspace_id: str,
        base_commit: str,
        prior_failure_class: FailureClass | None,
        prior_failure_summary: str | None,
        policy_decision: PolicyDecision,
        remaining_budget: GatewayBudget,
    ) -> AttemptEvidence:
        del (
            run_id,
            goal,
            plan,
            task,
            attempt,
            workspace_id,
            base_commit,
            prior_failure_class,
            prior_failure_summary,
            policy_decision,
            remaining_budget,
        )
        outcome = self.outcomes[self.calls]
        self.calls += 1
        return outcome


@dataclass
class _TaskExecutor:
    outcomes: dict[str, AttemptEvidence]
    base_commits: dict[str, str] = field(default_factory=dict)

    def execute(
        self,
        *,
        run_id: str,
        goal: AlphaGoalSpec,
        plan: AlphaPlanVersion,
        task: AlphaTaskSpec,
        attempt: int,
        workspace_id: str,
        base_commit: str,
        prior_failure_class: FailureClass | None,
        prior_failure_summary: str | None,
        policy_decision: PolicyDecision,
        remaining_budget: GatewayBudget,
    ) -> AttemptEvidence:
        del (
            run_id,
            goal,
            plan,
            attempt,
            workspace_id,
            prior_failure_class,
            prior_failure_summary,
            policy_decision,
            remaining_budget,
        )
        self.base_commits[task.task_id] = base_commit
        return self.outcomes[task.task_id]


@dataclass
class _CancelingExecutor:
    runtime: AlphaRuntimeApiService

    def execute(
        self,
        *,
        run_id: str,
        goal: AlphaGoalSpec,
        plan: AlphaPlanVersion,
        task: AlphaTaskSpec,
        attempt: int,
        workspace_id: str,
        base_commit: str,
        prior_failure_class: FailureClass | None,
        prior_failure_summary: str | None,
        policy_decision: PolicyDecision,
        remaining_budget: GatewayBudget,
    ) -> AttemptEvidence:
        del (
            goal,
            plan,
            task,
            attempt,
            workspace_id,
            base_commit,
            prior_failure_class,
            prior_failure_summary,
            policy_decision,
            remaining_budget,
        )
        self.runtime.cancel_run(
            run_id,
            AlphaCancelRunRequest(
                schema_version="alpha-cancel-run-request/v1",
                idempotency_key="cancel-during-attempt",
            ),
            principal_id="client:test",
        )
        return _success()


class _DenyPolicy(AlphaV2PolicyKernel):
    def authorize(
        self,
        goal: AlphaGoalSpec,
        plan: AlphaPlanVersion,
        task: AlphaTaskSpec,
        request: ToolActionRequest,
    ) -> PolicyDecision:
        del goal, plan, task
        return PolicyDecision(False, "test-policy-denial", request.digest)


class _RaisingExecutor:
    def execute(
        self,
        *,
        run_id: str,
        goal: AlphaGoalSpec,
        plan: AlphaPlanVersion,
        task: AlphaTaskSpec,
        attempt: int,
        workspace_id: str,
        base_commit: str,
        prior_failure_class: FailureClass | None,
        prior_failure_summary: str | None,
        policy_decision: PolicyDecision,
        remaining_budget: GatewayBudget,
    ) -> AttemptEvidence:
        del (
            run_id,
            goal,
            plan,
            task,
            attempt,
            workspace_id,
            base_commit,
            prior_failure_class,
            prior_failure_summary,
            policy_decision,
            remaining_budget,
        )
        raise RuntimeError("simulated executor boundary failure")


def _journal(tmp_path: Path) -> EventBackedAlphaV2RunJournal:
    path = tmp_path / "kernel.sqlite3"
    return EventBackedAlphaV2RunJournal(EventStore(path), CheckpointStore(path))


def _named_journal(tmp_path: Path, name: str) -> EventBackedAlphaV2RunJournal:
    path = tmp_path / f"{name}.sqlite3"
    return EventBackedAlphaV2RunJournal(EventStore(path), CheckpointStore(path))


def _planning_request(run_id: str) -> AlphaPlanningRequest:
    return AlphaPlanningRequest(
        goal=_goal(),
        classification=DataClassification.PRIVATE,
        locality=LocalityPolicy.REMOTE_ALLOWED,
        budget=GatewayBudget(32_000, 4_096, 120_000, 0),
        estimated_input_tokens=1_000,
        correlation_id=run_id,
        run_id=run_id,
    )


def _goal(*, allowed_paths: tuple[str, ...] = ("src",)) -> AlphaGoalSpec:
    return AlphaGoalSpec(
        goal_id="goal-alpha-v2",
        project_id="project-alpha-v2",
        intent_id="intent-alpha-v2",
        objective="Implement and verify the bounded repository change.",
        base_commit=BASE_COMMIT,
        constraints=("preserve public contracts",),
        allowed_paths=allowed_paths,
        verification_checks=(AlphaVerificationCheck("check", ("pytest", "-q")),),
    )


def _draft(*, tasks: tuple[dict[str, object], ...]) -> dict[str, object]:
    return {"schema_version": ALPHA_V2_PLAN_DRAFT_SCHEMA, "tasks": list(tasks)}


def _task(
    task_id: str,
    *,
    depends_on: tuple[str, ...] = (),
    allowed_paths: tuple[str, ...] = (),
) -> dict[str, object]:
    return {
        "task_id": task_id,
        "objective": f"Complete {task_id}.",
        "depends_on": list(depends_on),
        "allowed_paths": list(allowed_paths),
        "checks": ["check"],
    }


def _success() -> AttemptEvidence:
    return AttemptEvidence(
        workspace_clean=True,
        verifier_exit_code=0,
        required_checks_passed=True,
        failure_class=None,
        failure_summary=None,
        artifact_digests=(DIGEST,),
        progress_digests=(DIGEST,),
        head_commit=SUCCESS_HEAD,
    )
