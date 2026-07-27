"""Durable execution run journal and bounded repair coordinator."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from typing import Protocol, cast

from blackcell.gateway import DataClassification, GatewayBudget, LocalityPolicy
from blackcell.kernel import (
    CheckpointStore,
    ConcurrencyError,
    EventConflictError,
    EventEnvelope,
    EventStore,
    IdempotencyConflict,
    JsonInput,
    ProjectionCheckpoint,
    ProjectionRunner,
)
from blackcell.kernel._json import json_digest, thaw_json
from blackcell.orchestration.execution_plan import (
    EXECUTION_EVENT_SOURCE,
    EXECUTION_EVENT_TYPES,
    EXECUTION_GOAL_ADMITTED,
    EXECUTION_PLAN_ADMITTED,
    EXECUTION_PLAN_DRAFT_RECEIVED,
    EXECUTION_POLICY_DECIDED,
    EXECUTION_REPLAN_STARTED,
    EXECUTION_RUN_TERMINATED,
    EXECUTION_TASK_BLOCKED,
    EXECUTION_TASK_READY,
    EXECUTION_TASK_STARTED,
    EXECUTION_TASK_VERIFIED,
    EXECUTION_TASK_VERIFYING,
    AttemptEvidence,
    AttemptRoute,
    ExecutionPolicyKernel,
    FailureClass,
    GoalSpec,
    Plan,
    PlanningProvider,
    PlanningRequest,
    PlanningResult,
    PolicyDecision,
    PraxisPromotionCandidate,
    RunLifecycleStatus,
    TaskAttemptExecutor,
    TaskLifecycleStatus,
    TaskSpec,
    ToolActionRequest,
    compile_plan,
    goal_from_payload,
    goal_payload,
    plan_from_payload,
    plan_payload,
)
from blackcell.orchestration.run_lifecycle import (
    RUN_CANCEL_REQUESTED,
    RUN_CANCELED,
    RUN_EVENT_TYPES,
    RUN_FAILED,
    RUN_RECONCILIATION_REQUIRED,
    RUN_SUCCEEDED,
    RUNTIME_EVENT_SOURCE,
)

_RUN_OUTCOME_STATUSES = frozenset(
    {
        RunLifecycleStatus.SUCCEEDED,
        RunLifecycleStatus.REPLAN_REQUIRED,
        RunLifecycleStatus.BLOCKED,
        RunLifecycleStatus.CANCELED,
        RunLifecycleStatus.ESCALATED,
        RunLifecycleStatus.TERMINAL_FAILURE,
    }
)
_FINAL_RUN_STATUSES = _RUN_OUTCOME_STATUSES - {RunLifecycleStatus.REPLAN_REQUIRED}
_EVENT_PAYLOAD_FIELDS = {
    EXECUTION_GOAL_ADMITTED: frozenset(
        {
            "run_id",
            "goal_id",
            "goal_digest",
            "goal",
            "classification",
            "locality",
            "budget",
        }
    ),
    EXECUTION_REPLAN_STARTED: frozenset(
        {"run_id", "previous_plan_id", "previous_plan_version", "next_plan_version"}
    ),
    EXECUTION_PLAN_DRAFT_RECEIVED: frozenset(
        {
            "run_id",
            "draft_digest",
            "provider_output_digest",
            "profile_id",
            "adapter_id",
            "model_id",
            "input_tokens",
            "output_tokens",
            "latency_ms",
            "cost_microusd",
        }
    ),
    EXECUTION_PLAN_ADMITTED: frozenset(
        {
            "run_id",
            "plan_id",
            "plan_digest",
            "plan_version",
            "supersedes_plan_id",
            "plan",
        }
    ),
    EXECUTION_POLICY_DECIDED: frozenset(
        {
            "run_id",
            "task_id",
            "attempt",
            "allowed",
            "reason",
            "action_digest",
            "decision_id",
        }
    ),
    EXECUTION_TASK_READY: frozenset({"run_id", "task_id", "attempt"}),
    EXECUTION_TASK_STARTED: frozenset(
        {
            "run_id",
            "task_id",
            "attempt",
            "workspace_id",
            "base_commit",
            "action_digest",
            "decision_id",
        }
    ),
    EXECUTION_TASK_VERIFYING: frozenset({"run_id", "task_id", "attempt", "workspace_id"}),
    EXECUTION_TASK_VERIFIED: frozenset(
        {
            "run_id",
            "task_id",
            "attempt",
            "workspace_id",
            "route",
            "verifier_exit_code",
            "required_checks_passed",
            "workspace_clean",
            "error_signature",
            "same_error_count",
            "artifact_digests",
            "progress_digests",
            "new_evidence",
            "failure_class",
            "failure_summary",
            "head_commit",
            "input_tokens",
            "output_tokens",
            "latency_ms",
            "cost_microusd",
        }
    ),
    EXECUTION_TASK_BLOCKED: frozenset({"run_id", "task_id", "reason"}),
    EXECUTION_RUN_TERMINATED: frozenset({"run_id", "status", "reason"}),
}


class ExecutionRuntimeError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ExecutionTaskState:
    task_id: str
    status: TaskLifecycleStatus
    depends_on: tuple[str, ...] = ()
    allowed_paths: tuple[str, ...] = ()
    max_attempts: int = 3
    attempts: int = 0
    last_error_signature: str | None = None
    same_error_count: int = 0
    active_workspace_id: str | None = None
    retained_workspace_id: str | None = None
    blocked_reason: str | None = None
    evidence_event_ids: tuple[str, ...] = ()
    last_failure_class: FailureClass | None = None
    last_failure_summary: str | None = None
    last_artifact_digests: tuple[str, ...] = ()
    last_progress_digests: tuple[str, ...] = ()
    head_commit: str | None = None
    pending_policy_decision_id: str | None = None
    pending_policy_action_digest: str | None = None
    pending_policy_allowed: bool | None = None
    pending_policy_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ExecutionRunState:
    run_id: str
    goal_id: str
    goal_digest: str
    project_id: str
    intent_id: str
    goal_base_commit: str
    classification: DataClassification
    locality: LocalityPolicy
    budget: GatewayBudget
    plan_id: str | None
    plan_digest: str | None
    plan_revision: int | None
    status: RunLifecycleStatus
    tasks: tuple[ExecutionTaskState, ...]
    retained_workspace_ids: tuple[str, ...]
    latest_event_id: str
    last_stream_sequence: int
    input_tokens: int = 0
    input_tokens_complete: bool = True
    output_tokens: int = 0
    output_tokens_complete: bool = True
    latency_ms: int = 0
    cost_microusd: int = 0
    cost_microusd_complete: bool = True
    pending_draft_digest: str | None = None

    def task(self, task_id: str) -> ExecutionTaskState:
        try:
            return next(item for item in self.tasks if item.task_id == task_id)
        except StopIteration as error:
            raise ExecutionRuntimeError("execution-task-not-found") from error


class ExecutionEventObserver(Protocol):
    def record(self, event: EventEnvelope) -> None: ...


class ExecutionRunProjection:
    name = "run-kernel"
    version = 6

    def initial_state(self) -> ExecutionRunState | None:
        return None

    def apply(
        self,
        state: ExecutionRunState | None,
        event: EventEnvelope,
    ) -> ExecutionRunState | None:
        if event.source == RUNTIME_EVENT_SOURCE and event.event_type in RUN_EVENT_TYPES:
            if not event.stream_id.startswith("run:"):
                raise ExecutionRuntimeError("invalid-execution-event")
            if state is None:
                return None
            if (
                event.stream_id != _stream_id(state.run_id)
                or event.stream_sequence != state.last_stream_sequence + 1
            ):
                raise ExecutionRuntimeError("invalid-execution-event")
            status = state.status
            tasks = state.tasks
            terminal_task_status: TaskLifecycleStatus | None = None
            if event.event_type in {RUN_CANCEL_REQUESTED, RUN_CANCELED}:
                status = RunLifecycleStatus.CANCELED
                terminal_task_status = TaskLifecycleStatus.CANCELED
            elif event.event_type == RUN_SUCCEEDED:
                status = RunLifecycleStatus.SUCCEEDED
            elif event.event_type == RUN_FAILED:
                status = RunLifecycleStatus.TERMINAL_FAILURE
                terminal_task_status = TaskLifecycleStatus.TERMINAL_FAILURE
            elif event.event_type == RUN_RECONCILIATION_REQUIRED:
                status = RunLifecycleStatus.ESCALATED
                terminal_task_status = TaskLifecycleStatus.ESCALATED
            if state.status in _FINAL_RUN_STATUSES and status is not state.status:
                raise ExecutionRuntimeError("event-after-execution-terminal")
            if status is RunLifecycleStatus.SUCCEEDED and (
                not tasks or any(item.status is not TaskLifecycleStatus.SUCCEEDED for item in tasks)
            ):
                raise ExecutionRuntimeError("invalid-execution-transition")
            if terminal_task_status is not None:
                tasks = tuple(
                    item
                    if item.status is TaskLifecycleStatus.SUCCEEDED
                    else replace(
                        item,
                        status=terminal_task_status,
                        active_workspace_id=None,
                        pending_policy_decision_id=None,
                        pending_policy_action_digest=None,
                        pending_policy_allowed=None,
                        pending_policy_reason=None,
                    )
                    for item in tasks
                )
            return replace(
                state,
                status=status,
                tasks=tasks,
                latest_event_id=event.event_id,
                last_stream_sequence=event.stream_sequence,
            )
        if event.source != EXECUTION_EVENT_SOURCE or event.event_type not in EXECUTION_EVENT_TYPES:
            raise ExecutionRuntimeError("invalid-execution-event")
        payload = cast("Mapping[str, object]", thaw_json(event.payload))
        _require_event_payload(event.event_type, payload)
        run_id = _text(payload, "run_id")
        if event.stream_id != _stream_id(run_id):
            raise ExecutionRuntimeError("invalid-execution-event")
        if event.event_type == EXECUTION_GOAL_ADMITTED:
            if state is not None:
                raise ExecutionRuntimeError("invalid-execution-event")
            try:
                goal = goal_from_payload(payload.get("goal"))
            except (TypeError, ValueError) as error:
                raise ExecutionRuntimeError("invalid-execution-event") from error
            if goal.goal_id != _text(payload, "goal_id") or goal.digest != _digest(
                payload, "goal_digest"
            ):
                raise ExecutionRuntimeError("invalid-execution-event")
            return ExecutionRunState(
                run_id=run_id,
                goal_id=goal.goal_id,
                goal_digest=goal.digest,
                project_id=goal.project_id,
                intent_id=goal.intent_id,
                goal_base_commit=goal.base_commit,
                classification=DataClassification(_integer(payload, "classification")),
                locality=LocalityPolicy(_text(payload, "locality")),
                budget=_gateway_budget(payload.get("budget")),
                plan_id=None,
                plan_digest=None,
                plan_revision=None,
                status=RunLifecycleStatus.ADMITTED,
                tasks=(),
                retained_workspace_ids=(),
                latest_event_id=event.event_id,
                last_stream_sequence=event.stream_sequence,
            )
        if (
            state is None
            or state.run_id != run_id
            or event.stream_sequence != (state.last_stream_sequence + 1)
        ):
            raise ExecutionRuntimeError("invalid-execution-event")
        if state.status in _FINAL_RUN_STATUSES:
            raise ExecutionRuntimeError("event-after-execution-terminal")
        updated = state
        if event.event_type == EXECUTION_REPLAN_STARTED:
            if (
                state.status is not RunLifecycleStatus.REPLAN_REQUIRED
                or state.pending_draft_digest is not None
                or state.plan_id is None
                or state.plan_revision is None
                or _text(payload, "previous_plan_id") != state.plan_id
                or _integer(payload, "previous_plan_version") != state.plan_revision
                or _integer(payload, "next_plan_version") != state.plan_revision + 1
            ):
                raise ExecutionRuntimeError("invalid-execution-transition")
            updated = replace(state, status=RunLifecycleStatus.REPLANNING)
        elif event.event_type == EXECUTION_PLAN_DRAFT_RECEIVED:
            if (
                state.status not in {RunLifecycleStatus.ADMITTED, RunLifecycleStatus.REPLANNING}
                or state.pending_draft_digest is not None
            ):
                raise ExecutionRuntimeError("invalid-execution-transition")
            draft_digest = _digest(payload, "draft_digest")
            _digest(payload, "provider_output_digest")
            _text(payload, "profile_id")
            _text(payload, "adapter_id")
            _text(payload, "model_id")
            input_tokens = _optional_integer(payload, "input_tokens")
            output_tokens = _optional_integer(payload, "output_tokens")
            cost_microusd = _optional_integer(payload, "cost_microusd")
            updated = replace(
                state,
                pending_draft_digest=draft_digest,
                input_tokens=state.input_tokens + (input_tokens or 0),
                input_tokens_complete=(state.input_tokens_complete and input_tokens is not None),
                output_tokens=state.output_tokens + (output_tokens or 0),
                output_tokens_complete=(state.output_tokens_complete and output_tokens is not None),
                latency_ms=state.latency_ms + _integer(payload, "latency_ms"),
                cost_microusd=state.cost_microusd + (cost_microusd or 0),
                cost_microusd_complete=(state.cost_microusd_complete and cost_microusd is not None),
            )
        elif event.event_type == EXECUTION_PLAN_ADMITTED:
            fresh = state.plan_id is None and state.status is RunLifecycleStatus.ADMITTED
            revised = state.plan_id is not None and state.status is RunLifecycleStatus.REPLANNING
            if not (fresh or revised):
                raise ExecutionRuntimeError("invalid-execution-event")
            try:
                plan = plan_from_payload(payload.get("plan"))
            except (TypeError, ValueError) as error:
                raise ExecutionRuntimeError("invalid-execution-event") from error
            if (
                plan.plan_id != _text(payload, "plan_id")
                or plan.plan_digest != _digest(payload, "plan_digest")
                or plan.plan_revision != _integer(payload, "plan_version")
                or plan.supersedes_plan_id != _optional_text(payload, "supersedes_plan_id")
                or plan.goal_id != state.goal_id
                or plan.project_id != state.project_id
                or plan.intent_id != state.intent_id
                or plan.base_commit != state.goal_base_commit
                or plan.draft_digest != state.pending_draft_digest
            ):
                raise ExecutionRuntimeError("invalid-execution-event")
            if revised and (
                state.plan_revision is None
                or plan.supersedes_plan_id != state.plan_id
                or plan.plan_revision != state.plan_revision + 1
            ):
                raise ExecutionRuntimeError("invalid-execution-transition")
            if fresh and (plan.plan_revision != 1 or plan.supersedes_plan_id is not None):
                raise ExecutionRuntimeError("invalid-execution-transition")
            tasks = tuple(
                ExecutionTaskState(
                    item.task_id,
                    TaskLifecycleStatus.PENDING,
                    item.depends_on,
                    item.allowed_paths,
                    item.max_attempts,
                )
                for item in plan.tasks
            )
            if len({item.task_id for item in tasks}) != len(tasks):
                raise ExecutionRuntimeError("invalid-execution-event")
            updated = replace(
                state,
                plan_id=plan.plan_id,
                plan_digest=plan.plan_digest,
                plan_revision=plan.plan_revision,
                status=RunLifecycleStatus.RUNNING,
                tasks=tuple(sorted(tasks, key=lambda item: item.task_id)),
                pending_draft_digest=None,
            )
        elif event.event_type == EXECUTION_POLICY_DECIDED:
            task = _task(state, _text(payload, "task_id"))
            attempt = _integer(payload, "attempt")
            if (
                task.status is not TaskLifecycleStatus.READY
                or attempt != task.attempts + 1
                or task.pending_policy_decision_id is not None
            ):
                raise ExecutionRuntimeError("invalid-execution-transition")
            try:
                if state.plan_id is None:
                    raise ExecutionRuntimeError("invalid-execution-transition")
                expected_action = ToolActionRequest(
                    run_id=state.run_id,
                    plan_id=state.plan_id,
                    task_id=task.task_id,
                    attempt=attempt,
                    capability="repository-task",
                    allowed_paths=task.allowed_paths,
                )
                decision = PolicyDecision(
                    _boolean(payload, "allowed"),
                    _text(payload, "reason"),
                    _digest(payload, "action_digest"),
                )
            except (ExecutionRuntimeError, TypeError, ValueError) as error:
                raise ExecutionRuntimeError("invalid-execution-event") from error
            if decision.action_digest != expected_action.digest or decision.decision_id != _digest(
                payload, "decision_id"
            ):
                raise ExecutionRuntimeError("invalid-execution-event")
            updated = _replace_task(
                state,
                replace(
                    task,
                    pending_policy_decision_id=decision.decision_id,
                    pending_policy_action_digest=decision.action_digest,
                    pending_policy_allowed=decision.allowed,
                    pending_policy_reason=decision.reason,
                ),
            )
        elif event.event_type == EXECUTION_TASK_READY:
            task = _task(state, _text(payload, "task_id"))
            attempt = _integer(payload, "attempt")
            if (
                state.status is not RunLifecycleStatus.RUNNING
                or task.status not in {TaskLifecycleStatus.PENDING, TaskLifecycleStatus.REPAIRABLE}
                or attempt != task.attempts + 1
                or attempt > task.max_attempts
                or task.pending_policy_decision_id is not None
                or any(
                    state.task(dependency).status is not TaskLifecycleStatus.SUCCEEDED
                    for dependency in task.depends_on
                )
            ):
                raise ExecutionRuntimeError("invalid-execution-transition")
            updated = _replace_task(state, replace(task, status=TaskLifecycleStatus.READY))
        elif event.event_type == EXECUTION_TASK_STARTED:
            task = _task(state, _text(payload, "task_id"))
            attempt = _integer(payload, "attempt")
            workspace_id = _text(payload, "workspace_id")
            if (
                task.status is not TaskLifecycleStatus.READY
                or attempt != task.attempts + 1
                or state.plan_id is None
                or workspace_id != _workspace_id_from_ids(state.plan_id, task.task_id, attempt)
                or _commit(payload, "base_commit") != _expected_base_commit(state, task)
                or task.pending_policy_allowed is not True
                or task.pending_policy_decision_id != _digest(payload, "decision_id")
                or task.pending_policy_action_digest != _digest(payload, "action_digest")
            ):
                raise ExecutionRuntimeError("invalid-execution-transition")
            updated = _replace_task(
                state,
                replace(
                    task,
                    status=TaskLifecycleStatus.RUNNING,
                    attempts=attempt,
                    active_workspace_id=workspace_id,
                    pending_policy_decision_id=None,
                    pending_policy_action_digest=None,
                    pending_policy_allowed=None,
                    pending_policy_reason=None,
                ),
            )
        elif event.event_type == EXECUTION_TASK_VERIFYING:
            task = _task(state, _text(payload, "task_id"))
            if (
                task.status is not TaskLifecycleStatus.RUNNING
                or _integer(payload, "attempt") != task.attempts
                or _text(payload, "workspace_id") != task.active_workspace_id
            ):
                raise ExecutionRuntimeError("invalid-execution-transition")
            updated = _replace_task(state, replace(task, status=TaskLifecycleStatus.VERIFYING))
        elif event.event_type == EXECUTION_TASK_VERIFIED:
            task = _task(state, _text(payload, "task_id"))
            if (
                task.status is not TaskLifecycleStatus.VERIFYING
                or _integer(payload, "attempt") != task.attempts
                or _text(payload, "workspace_id") != task.active_workspace_id
            ):
                raise ExecutionRuntimeError("invalid-execution-transition")
            route = AttemptRoute(_text(payload, "route"))
            verifier_exit_code = _integer(payload, "verifier_exit_code")
            if verifier_exit_code > 255:
                raise ExecutionRuntimeError("invalid-execution-transition")
            required_checks_passed = _boolean(payload, "required_checks_passed")
            workspace_clean = _boolean(payload, "workspace_clean")
            declared_new_evidence = _boolean(payload, "new_evidence")
            signature_value = payload.get("error_signature")
            signature = None if signature_value is None else _digest(payload, "error_signature")
            same_error_count = _integer(payload, "same_error_count")
            expected_count = (
                0
                if signature is None
                else task.same_error_count + 1
                if signature == task.last_error_signature
                else 1
            )
            if same_error_count != expected_count:
                raise ExecutionRuntimeError("invalid-execution-transition")
            failure_class_value = payload.get("failure_class")
            failure_class = (
                None
                if failure_class_value is None
                else FailureClass(_text(payload, "failure_class"))
            )
            failure_summary = _optional_text(payload, "failure_summary")
            if route is AttemptRoute.SUCCEEDED and (
                failure_class is not None or failure_summary is not None
            ):
                raise ExecutionRuntimeError("invalid-execution-transition")
            if route is not AttemptRoute.SUCCEEDED and (
                failure_class is None or failure_summary is None
            ):
                raise ExecutionRuntimeError("invalid-execution-transition")
            artifact_digests = _digest_tuple(payload, "artifact_digests", require_nonempty=True)
            progress_digests = _digest_tuple(payload, "progress_digests", require_nonempty=True)
            if not set(progress_digests).issubset(artifact_digests):
                raise ExecutionRuntimeError("invalid-execution-transition")
            input_tokens = _optional_integer(payload, "input_tokens")
            output_tokens = _optional_integer(payload, "output_tokens")
            cost_microusd = _optional_integer(payload, "cost_microusd")
            try:
                evidence = AttemptEvidence(
                    workspace_clean=workspace_clean,
                    verifier_exit_code=verifier_exit_code,
                    required_checks_passed=required_checks_passed,
                    failure_class=failure_class,
                    failure_summary=failure_summary,
                    artifact_digests=artifact_digests,
                    progress_digests=progress_digests,
                    head_commit=_commit(payload, "head_commit"),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    latency_ms=_integer(payload, "latency_ms"),
                    cost_microusd=cost_microusd,
                )
            except (TypeError, ValueError) as error:
                raise ExecutionRuntimeError("invalid-execution-event") from error
            new_evidence = (
                not task.last_progress_digests
                or evidence.progress_digests != task.last_progress_digests
            )
            if (
                evidence.error_signature != signature
                or declared_new_evidence is not new_evidence
                or (route is AttemptRoute.SUCCEEDED)
                != (
                    evidence.workspace_clean
                    and evidence.verifier_exit_code == 0
                    and evidence.required_checks_passed
                )
            ):
                raise ExecutionRuntimeError("invalid-execution-transition")
            status = TaskLifecycleStatus(route.value)
            updated = _replace_task(
                state,
                replace(
                    task,
                    status=status,
                    last_error_signature=signature,
                    same_error_count=same_error_count,
                    active_workspace_id=None,
                    retained_workspace_id=(
                        task.retained_workspace_id
                        if route is AttemptRoute.SUCCEEDED
                        else task.active_workspace_id
                    ),
                    evidence_event_ids=(*task.evidence_event_ids, event.event_id),
                    last_failure_class=failure_class,
                    last_failure_summary=failure_summary,
                    last_artifact_digests=artifact_digests,
                    last_progress_digests=progress_digests,
                    head_commit=evidence.head_commit,
                ),
            )
            retained_workspace_ids = updated.retained_workspace_ids
            if (
                route is not AttemptRoute.SUCCEEDED
                and task.active_workspace_id is not None
                and task.active_workspace_id not in retained_workspace_ids
            ):
                retained_workspace_ids = (*retained_workspace_ids, task.active_workspace_id)
            updated = replace(
                updated,
                retained_workspace_ids=retained_workspace_ids,
                input_tokens=updated.input_tokens + (input_tokens or 0),
                input_tokens_complete=(updated.input_tokens_complete and input_tokens is not None),
                output_tokens=updated.output_tokens + (output_tokens or 0),
                output_tokens_complete=(
                    updated.output_tokens_complete and output_tokens is not None
                ),
                latency_ms=updated.latency_ms + evidence.latency_ms,
                cost_microusd=updated.cost_microusd + (cost_microusd or 0),
                cost_microusd_complete=(
                    updated.cost_microusd_complete and cost_microusd is not None
                ),
            )
        elif event.event_type == EXECUTION_TASK_BLOCKED:
            task = _task(state, _text(payload, "task_id"))
            reason = _text(payload, "reason")
            if (
                task.status is not TaskLifecycleStatus.READY
                or task.pending_policy_allowed is not False
                or task.pending_policy_reason != reason
            ):
                raise ExecutionRuntimeError("invalid-execution-transition")
            updated = _replace_task(
                state,
                replace(
                    task,
                    status=TaskLifecycleStatus.BLOCKED,
                    blocked_reason=reason,
                    pending_policy_decision_id=None,
                    pending_policy_action_digest=None,
                    pending_policy_allowed=None,
                    pending_policy_reason=None,
                ),
            )
        elif event.event_type == EXECUTION_RUN_TERMINATED:
            status = RunLifecycleStatus(_text(payload, "status"))
            if status not in _RUN_OUTCOME_STATUSES:
                raise ExecutionRuntimeError("invalid-execution-transition")
            _text(payload, "reason")
            if status is RunLifecycleStatus.SUCCEEDED and (
                not state.tasks
                or any(item.status is not TaskLifecycleStatus.SUCCEEDED for item in state.tasks)
            ):
                raise ExecutionRuntimeError("invalid-execution-transition")
            updated = replace(state, status=status)
        return replace(
            updated,
            latest_event_id=event.event_id,
            last_stream_sequence=event.stream_sequence,
        )

    def dump_state(self, state: ExecutionRunState | None) -> JsonInput:
        if state is None:
            return None
        return {
            "run_id": state.run_id,
            "goal_id": state.goal_id,
            "goal_digest": state.goal_digest,
            "project_id": state.project_id,
            "intent_id": state.intent_id,
            "goal_base_commit": state.goal_base_commit,
            "classification": state.classification.value,
            "locality": state.locality.value,
            "budget": _gateway_budget_payload(state.budget),
            "plan_id": state.plan_id,
            "plan_digest": state.plan_digest,
            "plan_version": state.plan_revision,
            "status": state.status.value,
            "retained_workspace_ids": list(state.retained_workspace_ids),
            "tasks": [
                {
                    "task_id": item.task_id,
                    "status": item.status.value,
                    "depends_on": list(item.depends_on),
                    "allowed_paths": list(item.allowed_paths),
                    "max_attempts": item.max_attempts,
                    "attempts": item.attempts,
                    "last_error_signature": item.last_error_signature,
                    "same_error_count": item.same_error_count,
                    "active_workspace_id": item.active_workspace_id,
                    "retained_workspace_id": item.retained_workspace_id,
                    "blocked_reason": item.blocked_reason,
                    "evidence_event_ids": list(item.evidence_event_ids),
                    "last_failure_class": (
                        None if item.last_failure_class is None else item.last_failure_class.value
                    ),
                    "last_failure_summary": item.last_failure_summary,
                    "last_artifact_digests": list(item.last_artifact_digests),
                    "last_progress_digests": list(item.last_progress_digests),
                    "head_commit": item.head_commit,
                    "pending_policy_decision_id": item.pending_policy_decision_id,
                    "pending_policy_action_digest": item.pending_policy_action_digest,
                    "pending_policy_allowed": item.pending_policy_allowed,
                    "pending_policy_reason": item.pending_policy_reason,
                }
                for item in state.tasks
            ],
            "latest_event_id": state.latest_event_id,
            "last_stream_sequence": state.last_stream_sequence,
            "input_tokens": state.input_tokens,
            "input_tokens_complete": state.input_tokens_complete,
            "output_tokens": state.output_tokens,
            "output_tokens_complete": state.output_tokens_complete,
            "latency_ms": state.latency_ms,
            "cost_microusd": state.cost_microusd,
            "cost_microusd_complete": state.cost_microusd_complete,
            "pending_draft_digest": state.pending_draft_digest,
        }

    def load_state(self, value: object) -> ExecutionRunState | None:
        if value is None:
            return None
        raw = _mapping(value)
        _require_fields(
            raw,
            {
                "run_id",
                "goal_id",
                "goal_digest",
                "project_id",
                "intent_id",
                "goal_base_commit",
                "classification",
                "locality",
                "budget",
                "plan_id",
                "plan_digest",
                "plan_version",
                "status",
                "retained_workspace_ids",
                "tasks",
                "latest_event_id",
                "last_stream_sequence",
                "input_tokens",
                "input_tokens_complete",
                "output_tokens",
                "output_tokens_complete",
                "latency_ms",
                "cost_microusd",
                "cost_microusd_complete",
                "pending_draft_digest",
            },
            "invalid-execution-checkpoint",
        )
        raw_tasks = raw.get("tasks")
        if not isinstance(raw_tasks, list):
            raise ExecutionRuntimeError("invalid-execution-checkpoint")
        tasks = tuple(_task_state_from_checkpoint(item) for item in raw_tasks)
        plan_revision_value = raw.get("plan_version")
        if plan_revision_value is not None and (
            isinstance(plan_revision_value, bool) or not isinstance(plan_revision_value, int)
        ):
            raise ExecutionRuntimeError("invalid-execution-checkpoint")
        retained_workspace_ids = _text_tuple(raw, "retained_workspace_ids")
        if len(set(retained_workspace_ids)) != len(retained_workspace_ids):
            raise ExecutionRuntimeError("invalid-execution-checkpoint")
        return ExecutionRunState(
            run_id=_text(raw, "run_id"),
            goal_id=_text(raw, "goal_id"),
            goal_digest=_digest(raw, "goal_digest"),
            project_id=_text(raw, "project_id"),
            intent_id=_text(raw, "intent_id"),
            goal_base_commit=_commit(raw, "goal_base_commit"),
            classification=DataClassification(_integer(raw, "classification")),
            locality=LocalityPolicy(_text(raw, "locality")),
            budget=_gateway_budget(raw.get("budget")),
            plan_id=_optional_text(raw, "plan_id"),
            plan_digest=_optional_digest(raw, "plan_digest"),
            plan_revision=cast("int | None", plan_revision_value),
            status=RunLifecycleStatus(_text(raw, "status")),
            retained_workspace_ids=retained_workspace_ids,
            tasks=tasks,
            latest_event_id=_text(raw, "latest_event_id"),
            last_stream_sequence=_integer(raw, "last_stream_sequence"),
            input_tokens=_integer(raw, "input_tokens"),
            input_tokens_complete=_boolean(raw, "input_tokens_complete"),
            output_tokens=_integer(raw, "output_tokens"),
            output_tokens_complete=_boolean(raw, "output_tokens_complete"),
            latency_ms=_integer(raw, "latency_ms"),
            cost_microusd=_integer(raw, "cost_microusd"),
            cost_microusd_complete=_boolean(raw, "cost_microusd_complete"),
            pending_draft_digest=_optional_digest(raw, "pending_draft_digest"),
        )


class EventBackedExecutionRunJournal:
    """Append-only run journal with snapshots every 100 events and at terminal states."""

    def __init__(
        self,
        events: EventStore,
        checkpoints: CheckpointStore,
        *,
        observer: ExecutionEventObserver | None = None,
    ) -> None:
        if events.path.resolve() != checkpoints.path.resolve():
            raise ValueError("execution event and checkpoint stores must share one database")
        self._events = events
        self._checkpoints = checkpoints
        self._projection = ExecutionRunProjection()
        self._runner = ProjectionRunner()
        self._observer = observer

    def append(
        self,
        run_id: str,
        event_type: str,
        payload: Mapping[str, JsonInput],
        *,
        actor: str,
    ) -> EventEnvelope:
        if (
            event_type not in EXECUTION_EVENT_TYPES
            or not isinstance(actor, str)
            or not actor.strip()
            or "run_id" in payload
        ):
            raise ExecutionRuntimeError("invalid-execution-event")
        stream_id = _stream_id(run_id)
        existing_events = self._events.read_stream(stream_id)
        _validate_stream(existing_events, stream_id)
        if event_type == EXECUTION_GOAL_ADMITTED:
            public_events = tuple(
                item
                for item in existing_events
                if item.source == RUNTIME_EVENT_SOURCE and item.event_type in RUN_EVENT_TYPES
            )
            if len(public_events) > 1:
                raise ExecutionRuntimeError("execution-public-run-already-active")
        current = self._rebuild(run_id).state
        if event_type == EXECUTION_PLAN_ADMITTED:
            scoped_plan_id = payload.get("plan_id")
            scoped_plan_revision = payload.get("plan_version")
        else:
            scoped_plan_id = None if current is None else current.plan_id
            scoped_plan_revision = None if current is None else current.plan_revision
        idempotency_scope: dict[str, JsonInput] = {
            "plan_id": scoped_plan_id,
            "plan_version": scoped_plan_revision,
            "payload": dict(payload),
        }
        idempotency_key = f"{event_type}:{json_digest(idempotency_scope)}"
        prior = next(
            (item for item in existing_events if item.idempotency_key == idempotency_key),
            None,
        )
        expected_payload = {"run_id": run_id, **payload}
        if prior is not None:
            if (
                prior.event_type != event_type
                or prior.source != EXECUTION_EVENT_SOURCE
                or prior.actor != actor
                or thaw_json(prior.payload) != expected_payload
            ):
                raise ExecutionRuntimeError("execution-idempotency-conflict")
            return prior
        sequence = len(existing_events)
        event = EventEnvelope.create(
            stream_id=stream_id,
            stream_sequence=sequence + 1,
            event_type=event_type,
            actor=actor,
            source=EXECUTION_EVENT_SOURCE,
            payload=expected_payload,
            idempotency_key=idempotency_key,
            correlation_id=run_id,
            causation_id=None if not existing_events else existing_events[-1].event_id,
        )
        self._projection.apply(current, event)
        try:
            stored = self._events.append(event, expected_sequence=sequence)
        except (ConcurrencyError, EventConflictError, IdempotencyConflict) as error:
            raise ExecutionRuntimeError("execution-concurrency-conflict") from error
        if self._observer is not None:
            with suppress(Exception):
                self._observer.record(stored)
        state = self.rehydrate(run_id)
        if state.last_stream_sequence % 100 == 0 or state.status in _RUN_OUTCOME_STATUSES:
            with suppress(Exception):
                self._save_checkpoint(run_id)
        return stored

    def rehydrate(self, run_id: str) -> ExecutionRunState:
        result = self._rebuild(run_id)
        if result.state is None:
            raise ExecutionRuntimeError("execution-run-not-found")
        return result.state

    def events(self, run_id: str) -> tuple[EventEnvelope, ...]:
        events = self._events.read_stream(_stream_id(run_id))
        _validate_stream(events, _stream_id(run_id))
        return events

    def promotion_candidate(self, run_id: str) -> PraxisPromotionCandidate | None:
        state = self.rehydrate(run_id)
        if state.status is not RunLifecycleStatus.ESCALATED or state.plan_id is None:
            return None
        task = next((item for item in state.tasks if item.same_error_count >= 2), None)
        if task is None:
            return None
        events = tuple(
            item for item in self.events(run_id) if item.event_id in task.evidence_event_ids
        )
        if len(events) < 2:
            return None
        identity = json_digest(
            {
                "run_id": run_id,
                "plan_id": state.plan_id,
                "task_id": task.task_id,
                "evidence_event_ids": [item.event_id for item in events],
            }
        )
        return PraxisPromotionCandidate(
            candidate_id=f"candidate-{identity.removeprefix('sha256:')[:32]}",
            run_id=run_id,
            plan_id=state.plan_id,
            task_id=task.task_id,
            kind="repeated-error-signature",
            evidence_event_ids=tuple(item.event_id for item in events),
            evidence_digests=tuple(item.payload_hash for item in events),
        )

    def goal(self, run_id: str) -> GoalSpec:
        event = next(
            (item for item in self.events(run_id) if item.event_type == EXECUTION_GOAL_ADMITTED),
            None,
        )
        if event is None:
            raise ExecutionRuntimeError("execution-goal-not-found")
        try:
            goal = goal_from_payload(_mapping(thaw_json(event.payload)).get("goal"))
        except (ExecutionRuntimeError, TypeError, ValueError) as error:
            raise ExecutionRuntimeError("invalid-execution-event") from error
        state = self.rehydrate(run_id)
        if goal.goal_id != state.goal_id or goal.digest != state.goal_digest:
            raise ExecutionRuntimeError("execution-run-binding-mismatch")
        return goal

    def plan(self, run_id: str) -> Plan:
        event = next(
            (
                item
                for item in reversed(self.events(run_id))
                if item.event_type == EXECUTION_PLAN_ADMITTED
            ),
            None,
        )
        if event is None:
            raise ExecutionRuntimeError("execution-plan-not-found")
        try:
            plan = plan_from_payload(_mapping(thaw_json(event.payload)).get("plan"))
        except (ExecutionRuntimeError, TypeError, ValueError) as error:
            raise ExecutionRuntimeError("invalid-execution-event") from error
        state = self.rehydrate(run_id)
        if plan.plan_id != state.plan_id or plan.plan_digest != state.plan_digest:
            raise ExecutionRuntimeError("execution-run-binding-mismatch")
        return plan

    def snapshot(self, run_id: str) -> ProjectionCheckpoint:
        """Persist a non-authoritative acceleration snapshot at an explicit safe point."""

        self.rehydrate(run_id)
        return self._save_checkpoint(run_id)

    def _save_checkpoint(self, run_id: str) -> ProjectionCheckpoint:
        stream_id = _stream_id(run_id)
        prior = self._checkpoints.load(
            self._projection.name,
            self._projection.version,
            stream_id=stream_id,
        )
        result = self._runner.rebuild(
            self._events,
            self._projection,
            stream_id=stream_id,
            checkpoint=prior,
        )
        checkpoint = result.checkpoint(self._projection, stream_id=stream_id)
        return self._checkpoints.save(
            checkpoint,
            expected_position=0 if prior is None else prior.last_global_position,
        )

    def _rebuild(self, run_id: str):
        stream_id = _stream_id(run_id)
        _validate_stream(self._events.read_stream(stream_id), stream_id)
        checkpoint = self._checkpoints.load(
            self._projection.name,
            self._projection.version,
            stream_id=stream_id,
        )
        try:
            return self._runner.rebuild(
                self._events,
                self._projection,
                stream_id=stream_id,
                checkpoint=checkpoint,
            )
        except ExecutionRuntimeError, TypeError, ValueError:
            if checkpoint is None:
                raise
            return self._runner.rebuild(
                self._events,
                self._projection,
                stream_id=stream_id,
            )


@dataclass(frozen=True, slots=True)
class ExecutionCoordinator:
    journal: EventBackedExecutionRunJournal
    provider: PlanningProvider
    executor: TaskAttemptExecutor
    policy: ExecutionPolicyKernel

    def reconcile_incomplete_planning(self, run_id: str, *, actor: str) -> ExecutionRunState:
        """Fail closed when a durable planning fence has no admitted result after restart."""

        state = self.journal.rehydrate(run_id)
        if state.status is RunLifecycleStatus.CANCELED:
            return state
        if state.status not in {
            RunLifecycleStatus.ADMITTED,
            RunLifecycleStatus.REPLANNING,
        }:
            raise ExecutionRuntimeError("execution-planning-not-ambiguous")
        return self._terminate(
            run_id,
            RunLifecycleStatus.ESCALATED,
            actor=actor,
            reason="ambiguous-planning-dispatch",
        )

    def exhaust_replan_budget(self, run_id: str, *, actor: str) -> ExecutionRunState:
        state = self.journal.rehydrate(run_id)
        if state.status is not RunLifecycleStatus.REPLAN_REQUIRED:
            raise ExecutionRuntimeError("execution-replan-not-required")
        return self._terminate(
            run_id,
            RunLifecycleStatus.ESCALATED,
            actor=actor,
            reason="replan-budget-exhausted",
        )

    def exhaust_provider_budget(self, run_id: str, *, actor: str) -> ExecutionRunState:
        """Terminate a replan before redispatch when canonical provider authority is spent."""

        state = self.journal.rehydrate(run_id)
        if state.status is not RunLifecycleStatus.REPLAN_REQUIRED:
            raise ExecutionRuntimeError("execution-replan-not-required")
        return self._terminate(
            run_id,
            RunLifecycleStatus.ESCALATED,
            actor=actor,
            reason="cumulative-budget-exhausted",
        )

    def compile_and_admit(
        self,
        run_id: str,
        request: PlanningRequest,
        *,
        actor: str,
        previous: Plan | None = None,
        provider_budget: GatewayBudget | None = None,
    ) -> tuple[Plan, PlanningResult]:
        if request.run_id != run_id:
            raise ExecutionRuntimeError("execution-run-binding-mismatch")
        if provider_budget is not None and not _budget_within(provider_budget, request.budget):
            raise ExecutionRuntimeError("execution-provider-budget-invalid")
        goal = request.goal
        if previous is None:
            self.journal.append(
                run_id,
                EXECUTION_GOAL_ADMITTED,
                {
                    "goal_id": goal.goal_id,
                    "goal_digest": goal.digest,
                    "goal": goal_payload(goal),
                    "classification": request.classification.value,
                    "locality": request.locality.value,
                    "budget": _gateway_budget_payload(request.budget),
                },
                actor=actor,
            )
        else:
            state = self.journal.rehydrate(run_id)
            if (
                state.status is not RunLifecycleStatus.REPLAN_REQUIRED
                or state.goal_digest != goal.digest
                or state.classification is not request.classification
                or state.locality is not request.locality
                or state.budget != request.budget
                or state.plan_id != previous.plan_id
                or state.plan_digest != previous.plan_digest
                or state.plan_revision != previous.plan_revision
            ):
                raise ExecutionRuntimeError("execution-run-binding-mismatch")
            self.journal.append(
                run_id,
                EXECUTION_REPLAN_STARTED,
                {
                    "previous_plan_id": previous.plan_id,
                    "previous_plan_version": previous.plan_revision,
                    "next_plan_version": previous.plan_revision + 1,
                },
                actor=actor,
            )
        provider_request = (
            request if provider_budget is None else replace(request, budget=provider_budget)
        )
        result = self.provider.propose_plan(provider_request)
        plan = compile_plan(
            goal,
            cast("Mapping[str, object]", result.draft),
            previous=previous,
        )
        self.journal.append(
            run_id,
            EXECUTION_PLAN_DRAFT_RECEIVED,
            {
                "draft_digest": plan.draft_digest,
                "provider_output_digest": result.provider_output_digest,
                "profile_id": result.profile_id,
                "adapter_id": result.adapter_id,
                "model_id": result.model_id,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "latency_ms": result.latency_ms,
                "cost_microusd": result.cost_microusd,
            },
            actor=actor,
        )
        self.journal.append(
            run_id,
            EXECUTION_PLAN_ADMITTED,
            {
                "plan_id": plan.plan_id,
                "plan_digest": plan.plan_digest,
                "plan_version": plan.plan_revision,
                "supersedes_plan_id": plan.supersedes_plan_id,
                "plan": plan_payload(plan),
            },
            actor=actor,
        )
        return plan, result

    def execute(
        self,
        run_id: str,
        request: PlanningRequest,
        plan: Plan,
        *,
        actor: str,
    ) -> ExecutionRunState:
        if request.run_id != run_id:
            raise ExecutionRuntimeError("execution-run-binding-mismatch")
        goal = request.goal
        state = self.journal.rehydrate(run_id)
        if (
            state.goal_digest != goal.digest
            or state.classification is not request.classification
            or state.locality is not request.locality
            or state.budget != request.budget
            or state.plan_id != plan.plan_id
            or state.plan_digest != plan.plan_digest
        ):
            raise ExecutionRuntimeError("execution-run-binding-mismatch")
        if state.status in _RUN_OUTCOME_STATUSES:
            return state
        blocked = next(
            (item for item in state.tasks if item.status is TaskLifecycleStatus.BLOCKED),
            None,
        )
        if blocked is not None:
            if blocked.blocked_reason is None:
                raise ExecutionRuntimeError("execution-blocked-task-invalid")
            return self._terminate(
                run_id,
                RunLifecycleStatus.BLOCKED,
                actor=actor,
                reason=blocked.blocked_reason,
            )
        active = next((item for item in state.tasks if item.active_workspace_id is not None), None)
        if active is not None:
            return self._terminate(
                run_id,
                RunLifecycleStatus.ESCALATED,
                actor=actor,
                reason="ambiguous-active-attempt",
            )
        by_id = {item.task_id: item for item in plan.tasks}
        for task_id in plan.topological_order:
            task = by_id[task_id]
            state = self.journal.rehydrate(run_id)
            task_state = state.task(task_id)
            if task_state.status is TaskLifecycleStatus.SUCCEEDED:
                continue
            if any(
                state.task(dependency).status is not TaskLifecycleStatus.SUCCEEDED
                for dependency in task.depends_on
            ):
                return self._terminate(
                    run_id,
                    RunLifecycleStatus.BLOCKED,
                    actor=actor,
                    reason="dependency-not-satisfied",
                )
            while True:
                task_state = self.journal.rehydrate(run_id).task(task_id)
                if task_state.status in {
                    TaskLifecycleStatus.PENDING,
                    TaskLifecycleStatus.REPAIRABLE,
                }:
                    self.journal.append(
                        run_id,
                        EXECUTION_TASK_READY,
                        {"task_id": task_id, "attempt": task_state.attempts + 1},
                        actor=actor,
                    )
                    task_state = self.journal.rehydrate(run_id).task(task_id)
                attempt = task_state.attempts + 1
                try:
                    base_commit = _attempt_base_commit(
                        self.journal.rehydrate(run_id),
                        task,
                        goal.base_commit,
                    )
                except ExecutionRuntimeError:
                    return self._terminate(
                        run_id,
                        RunLifecycleStatus.REPLAN_REQUIRED,
                        actor=actor,
                        reason="base-commit-ambiguous",
                    )
                state = self.journal.rehydrate(run_id)
                remaining_budget = _remaining_budget(request.budget, state)
                if task.allowed_paths and (
                    _provider_usage_incomplete(state)
                    or _provider_budget_exhausted(remaining_budget)
                ):
                    return self._terminate(
                        run_id,
                        RunLifecycleStatus.ESCALATED,
                        actor=actor,
                        reason="cumulative-budget-exhausted",
                    )
                action = ToolActionRequest(
                    run_id=run_id,
                    plan_id=plan.plan_id,
                    task_id=task_id,
                    attempt=attempt,
                    capability="repository-task",
                    allowed_paths=task.allowed_paths,
                )
                decision = _pending_policy_decision(task_state, action)
                if decision is None:
                    decision = self.policy.authorize(goal, plan, task, action)
                    self._record_policy(run_id, task_id, attempt, decision, actor=actor)
                if not decision.allowed:
                    self.journal.append(
                        run_id,
                        EXECUTION_TASK_BLOCKED,
                        {"task_id": task_id, "reason": decision.reason},
                        actor=actor,
                    )
                    return self._terminate(
                        run_id,
                        RunLifecycleStatus.BLOCKED,
                        actor=actor,
                        reason=decision.reason,
                    )
                workspace_id = _workspace_id(plan, task, attempt)
                self.journal.append(
                    run_id,
                    EXECUTION_TASK_STARTED,
                    {
                        "task_id": task_id,
                        "attempt": attempt,
                        "workspace_id": workspace_id,
                        "base_commit": base_commit,
                        "action_digest": decision.action_digest,
                        "decision_id": decision.decision_id,
                    },
                    actor=actor,
                )
                try:
                    evidence = self.executor.execute(
                        run_id=run_id,
                        goal=goal,
                        plan=plan,
                        task=task,
                        attempt=attempt,
                        workspace_id=workspace_id,
                        base_commit=base_commit,
                        prior_failure_class=task_state.last_failure_class,
                        prior_failure_summary=task_state.last_failure_summary,
                        policy_decision=decision,
                        remaining_budget=remaining_budget,
                    )
                except Exception as error:
                    state = self.journal.rehydrate(run_id)
                    if state.status is RunLifecycleStatus.CANCELED:
                        return state
                    return self._terminate(
                        run_id,
                        RunLifecycleStatus.ESCALATED,
                        actor=actor,
                        reason=f"executor-{type(error).__name__.casefold()}",
                    )
                state = self.journal.rehydrate(run_id)
                if state.status is RunLifecycleStatus.CANCELED:
                    return state
                self.journal.append(
                    run_id,
                    EXECUTION_TASK_VERIFYING,
                    {
                        "task_id": task_id,
                        "attempt": attempt,
                        "workspace_id": workspace_id,
                    },
                    actor=actor,
                )
                prior = self.journal.rehydrate(run_id).task(task_id)
                signature = evidence.error_signature
                new_evidence = (
                    not prior.last_progress_digests
                    or evidence.progress_digests != prior.last_progress_digests
                )
                same_error_count = (
                    0
                    if signature is None
                    else prior.same_error_count + 1
                    if signature == prior.last_error_signature
                    else 1
                )
                route = self.policy.route(
                    goal,
                    task,
                    evidence,
                    attempt=attempt,
                    same_error_count=same_error_count,
                    new_evidence=new_evidence,
                )
                self.journal.append(
                    run_id,
                    EXECUTION_TASK_VERIFIED,
                    {
                        "task_id": task_id,
                        "attempt": attempt,
                        "workspace_id": workspace_id,
                        "route": route.value,
                        "verifier_exit_code": evidence.verifier_exit_code,
                        "required_checks_passed": evidence.required_checks_passed,
                        "workspace_clean": evidence.workspace_clean,
                        "error_signature": signature,
                        "same_error_count": same_error_count,
                        "artifact_digests": list(evidence.artifact_digests),
                        "progress_digests": list(evidence.progress_digests),
                        "new_evidence": new_evidence,
                        "failure_class": (
                            None if evidence.failure_class is None else evidence.failure_class.value
                        ),
                        "failure_summary": evidence.failure_summary,
                        "head_commit": evidence.head_commit,
                        "input_tokens": evidence.input_tokens,
                        "output_tokens": evidence.output_tokens,
                        "latency_ms": evidence.latency_ms,
                        "cost_microusd": evidence.cost_microusd,
                    },
                    actor=actor,
                )
                if route is AttemptRoute.SUCCEEDED:
                    if _budget_overdrawn(request.budget, self.journal.rehydrate(run_id)):
                        return self._terminate(
                            run_id,
                            RunLifecycleStatus.ESCALATED,
                            actor=actor,
                            reason="cumulative-budget-exhausted",
                        )
                    break
                if route is AttemptRoute.REPAIRABLE:
                    if _budget_overdrawn(request.budget, self.journal.rehydrate(run_id)):
                        return self._terminate(
                            run_id,
                            RunLifecycleStatus.ESCALATED,
                            actor=actor,
                            reason="cumulative-budget-exhausted",
                        )
                    continue
                return self._terminate(
                    run_id,
                    RunLifecycleStatus(route.value),
                    actor=actor,
                    reason=route.value,
                )
        return self._terminate(
            run_id,
            RunLifecycleStatus.SUCCEEDED,
            actor=actor,
            reason="verification-passed",
        )

    def _record_policy(
        self,
        run_id: str,
        task_id: str,
        attempt: int,
        decision: PolicyDecision,
        *,
        actor: str,
    ) -> None:
        self.journal.append(
            run_id,
            EXECUTION_POLICY_DECIDED,
            {
                "task_id": task_id,
                "attempt": attempt,
                "allowed": decision.allowed,
                "reason": decision.reason,
                "action_digest": decision.action_digest,
                "decision_id": decision.decision_id,
            },
            actor=actor,
        )

    def _terminate(
        self,
        run_id: str,
        status: RunLifecycleStatus,
        *,
        actor: str,
        reason: str,
    ) -> ExecutionRunState:
        self.journal.append(
            run_id,
            EXECUTION_RUN_TERMINATED,
            {"status": status.value, "reason": reason},
            actor=actor,
        )
        return self.journal.rehydrate(run_id)


def _replace_task(state: ExecutionRunState, replacement: ExecutionTaskState) -> ExecutionRunState:
    return replace(
        state,
        tasks=tuple(
            replacement if item.task_id == replacement.task_id else item for item in state.tasks
        ),
    )


def _task(state: ExecutionRunState, task_id: str) -> ExecutionTaskState:
    return state.task(task_id)


def _workspace_id(plan: Plan, task: TaskSpec, attempt: int) -> str:
    return _workspace_id_from_ids(plan.plan_id, task.task_id, attempt)


def _workspace_id_from_ids(plan_id: str, task_id: str, attempt: int) -> str:
    identity = json_digest({"plan_id": plan_id, "task_id": task_id, "attempt": attempt})
    return f"workspace-{identity.removeprefix('sha256:')[:32]}"


def _expected_base_commit(state: ExecutionRunState, task: ExecutionTaskState) -> str:
    if not task.depends_on:
        return state.goal_base_commit
    dependency_heads = tuple(state.task(item).head_commit for item in task.depends_on)
    if any(item is None for item in dependency_heads) or len(set(dependency_heads)) > 1:
        raise ExecutionRuntimeError("execution-base-commit-ambiguous")
    return cast("str", dependency_heads[0])


def _attempt_base_commit(
    state: ExecutionRunState,
    task: TaskSpec,
    goal_base_commit: str,
) -> str:
    if state.goal_base_commit != goal_base_commit:
        raise ExecutionRuntimeError("execution-run-binding-mismatch")
    return _expected_base_commit(state, state.task(task.task_id))


def _remaining_budget(budget: GatewayBudget, state: ExecutionRunState) -> GatewayBudget:
    return GatewayBudget(
        max(0, budget.max_input_tokens - state.input_tokens),
        max(0, budget.max_output_tokens - state.output_tokens),
        max(0, budget.max_latency_ms - state.latency_ms),
        max(0, budget.max_cost_microusd - state.cost_microusd),
    )


def _budget_within(candidate: GatewayBudget, maximum: GatewayBudget) -> bool:
    return (
        candidate.max_input_tokens <= maximum.max_input_tokens
        and candidate.max_output_tokens <= maximum.max_output_tokens
        and candidate.max_latency_ms <= maximum.max_latency_ms
        and candidate.max_cost_microusd <= maximum.max_cost_microusd
    )


def _pending_policy_decision(
    task: ExecutionTaskState,
    action: ToolActionRequest,
) -> PolicyDecision | None:
    decision_id = task.pending_policy_decision_id
    if decision_id is None:
        return None
    action_digest = task.pending_policy_action_digest
    allowed = task.pending_policy_allowed
    reason = task.pending_policy_reason
    if action_digest is None or allowed is None or reason is None:
        raise ExecutionRuntimeError("execution-pending-policy-invalid")
    try:
        decision = PolicyDecision(allowed, reason, action_digest)
    except (TypeError, ValueError) as error:
        raise ExecutionRuntimeError("execution-pending-policy-invalid") from error
    if decision.action_digest != action.digest or decision.decision_id != decision_id:
        raise ExecutionRuntimeError("execution-pending-policy-invalid")
    return decision


def _provider_budget_exhausted(budget: GatewayBudget) -> bool:
    return (
        budget.max_input_tokens == 0 or budget.max_output_tokens == 0 or budget.max_latency_ms == 0
    )


def _provider_usage_incomplete(state: ExecutionRunState) -> bool:
    return (
        not state.input_tokens_complete
        or not state.output_tokens_complete
        or not state.cost_microusd_complete
    )


def _budget_overdrawn(budget: GatewayBudget, state: ExecutionRunState) -> bool:
    return (
        state.input_tokens > budget.max_input_tokens
        or state.output_tokens > budget.max_output_tokens
        or state.latency_ms > budget.max_latency_ms
        or state.cost_microusd > budget.max_cost_microusd
    )


def _stream_id(run_id: str) -> str:
    if (
        not isinstance(run_id, str)
        or not run_id
        or len(run_id) > 128
        or any(
            not (character.isascii() and (character.isalnum() or character in "-._:"))
            for character in run_id
        )
    ):
        raise ExecutionRuntimeError("invalid-execution-run-id")
    return f"run:{run_id}"


def _validate_stream(events: tuple[EventEnvelope, ...], stream_id: str) -> None:
    for sequence, event in enumerate(events, start=1):
        known_event = (
            event.source == RUNTIME_EVENT_SOURCE and event.event_type in RUN_EVENT_TYPES
        ) or (event.source == EXECUTION_EVENT_SOURCE and event.event_type in EXECUTION_EVENT_TYPES)
        if (
            event.stream_id != stream_id
            or event.stream_sequence != sequence
            or event.schema_version != 1
            or not known_event
            or (sequence > 1 and event.causation_id != events[sequence - 2].event_id)
        ):
            raise ExecutionRuntimeError("invalid-execution-event")


def _require_event_payload(event_type: str, payload: Mapping[str, object]) -> None:
    expected = _EVENT_PAYLOAD_FIELDS.get(event_type)
    if expected is None or set(payload) != expected:
        raise ExecutionRuntimeError("invalid-execution-event")


def _require_fields(value: Mapping[str, object], expected: set[str], error_code: str) -> None:
    if set(value) != expected:
        raise ExecutionRuntimeError(error_code)


def _task_state_from_checkpoint(value: object) -> ExecutionTaskState:
    raw = _mapping(value)
    fields = {
        "task_id",
        "status",
        "depends_on",
        "allowed_paths",
        "max_attempts",
        "attempts",
        "last_error_signature",
        "same_error_count",
        "active_workspace_id",
        "retained_workspace_id",
        "blocked_reason",
        "evidence_event_ids",
        "last_failure_class",
        "last_failure_summary",
        "last_artifact_digests",
        "last_progress_digests",
        "head_commit",
        "pending_policy_decision_id",
        "pending_policy_action_digest",
        "pending_policy_allowed",
        "pending_policy_reason",
    }
    _require_fields(raw, fields, "invalid-execution-checkpoint")
    task = ExecutionTaskState(
        task_id=_text(raw, "task_id"),
        status=TaskLifecycleStatus(_text(raw, "status")),
        depends_on=_text_tuple(raw, "depends_on"),
        allowed_paths=_text_tuple(raw, "allowed_paths"),
        max_attempts=_integer(raw, "max_attempts"),
        attempts=_integer(raw, "attempts"),
        last_error_signature=_optional_digest(raw, "last_error_signature"),
        same_error_count=_integer(raw, "same_error_count"),
        active_workspace_id=_optional_text(raw, "active_workspace_id"),
        retained_workspace_id=_optional_text(raw, "retained_workspace_id"),
        blocked_reason=_optional_text(raw, "blocked_reason"),
        evidence_event_ids=_text_tuple(raw, "evidence_event_ids"),
        last_failure_class=(
            None
            if raw.get("last_failure_class") is None
            else FailureClass(_text(raw, "last_failure_class"))
        ),
        last_failure_summary=_optional_text(raw, "last_failure_summary"),
        last_artifact_digests=_digest_tuple(raw, "last_artifact_digests"),
        last_progress_digests=_digest_tuple(raw, "last_progress_digests"),
        head_commit=_optional_commit(raw, "head_commit"),
        pending_policy_decision_id=_optional_digest(raw, "pending_policy_decision_id"),
        pending_policy_action_digest=_optional_digest(raw, "pending_policy_action_digest"),
        pending_policy_allowed=_optional_boolean(raw, "pending_policy_allowed"),
        pending_policy_reason=_optional_text(raw, "pending_policy_reason"),
    )
    pending = (
        task.pending_policy_decision_id,
        task.pending_policy_action_digest,
        task.pending_policy_allowed,
        task.pending_policy_reason,
    )
    if (any(item is None for item in pending) and any(item is not None for item in pending)) or (
        any(item is not None for item in pending) and task.status is not TaskLifecycleStatus.READY
    ):
        raise ExecutionRuntimeError("invalid-execution-checkpoint")
    if (task.status is TaskLifecycleStatus.BLOCKED) != (task.blocked_reason is not None):
        raise ExecutionRuntimeError("invalid-execution-checkpoint")
    if (
        not 1 <= task.max_attempts <= 3
        or task.attempts > task.max_attempts
        or task.task_id in task.depends_on
        or len(set(task.depends_on)) != len(task.depends_on)
        or len(set(task.allowed_paths)) != len(task.allowed_paths)
    ):
        raise ExecutionRuntimeError("invalid-execution-checkpoint")
    return task


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ExecutionRuntimeError("invalid-execution-event")
    return cast("Mapping[str, object]", value)


def _text(value: Mapping[str, object], field: str) -> str:
    item = value.get(field)
    if not isinstance(item, str) or not item:
        raise ExecutionRuntimeError("invalid-execution-event")
    return item


def _optional_text(value: Mapping[str, object], field: str) -> str | None:
    item = value.get(field)
    if item is None:
        return None
    return _text(value, field)


def _integer(value: Mapping[str, object], field: str) -> int:
    item = value.get(field)
    if isinstance(item, bool) or not isinstance(item, int) or item < 0:
        raise ExecutionRuntimeError("invalid-execution-event")
    return item


def _optional_integer(value: Mapping[str, object], field: str) -> int | None:
    item = value.get(field)
    if item is None:
        return None
    return _integer(value, field)


def _gateway_budget(value: object) -> GatewayBudget:
    raw = _mapping(value)
    if set(raw) != {
        "max_input_tokens",
        "max_output_tokens",
        "max_latency_ms",
        "max_cost_microusd",
    }:
        raise ExecutionRuntimeError("invalid-execution-event")
    return GatewayBudget(
        _integer(raw, "max_input_tokens"),
        _integer(raw, "max_output_tokens"),
        _integer(raw, "max_latency_ms"),
        _integer(raw, "max_cost_microusd"),
    )


def _gateway_budget_payload(value: GatewayBudget) -> dict[str, int]:
    return {
        "max_input_tokens": value.max_input_tokens,
        "max_output_tokens": value.max_output_tokens,
        "max_latency_ms": value.max_latency_ms,
        "max_cost_microusd": value.max_cost_microusd,
    }


def _boolean(value: Mapping[str, object], field: str) -> bool:
    item = value.get(field)
    if not isinstance(item, bool):
        raise ExecutionRuntimeError("invalid-execution-event")
    return item


def _optional_boolean(value: Mapping[str, object], field: str) -> bool | None:
    if value.get(field) is None:
        return None
    return _boolean(value, field)


def _digest(value: Mapping[str, object], field: str) -> str:
    item = _text(value, field)
    if not item.startswith("sha256:") or len(item) != 71:
        raise ExecutionRuntimeError("invalid-execution-event")
    return item


def _optional_digest(value: Mapping[str, object], field: str) -> str | None:
    if value.get(field) is None:
        return None
    return _digest(value, field)


def _commit(value: Mapping[str, object], field: str) -> str:
    item = _text(value, field)
    if len(item) != 40 or any(character not in "0123456789abcdef" for character in item):
        raise ExecutionRuntimeError("invalid-execution-event")
    return item


def _optional_commit(value: Mapping[str, object], field: str) -> str | None:
    if value.get(field) is None:
        return None
    return _commit(value, field)


def _digest_tuple(
    value: Mapping[str, object],
    field: str,
    *,
    require_nonempty: bool = False,
) -> tuple[str, ...]:
    item = value.get(field)
    if (
        not isinstance(item, list)
        or (require_nonempty and not item)
        or any(
            not isinstance(child, str) or not child.startswith("sha256:") or len(child) != 71
            for child in item
        )
    ):
        raise ExecutionRuntimeError("invalid-execution-event")
    digests = tuple(cast("list[str]", item))
    if digests != tuple(sorted(set(digests))):
        raise ExecutionRuntimeError("invalid-execution-event")
    return digests


def _text_tuple(value: Mapping[str, object], field: str) -> tuple[str, ...]:
    item = value.get(field)
    if not isinstance(item, list) or any(not isinstance(child, str) for child in item):
        raise ExecutionRuntimeError("invalid-execution-event")
    return tuple(cast("list[str]", item))


__all__ = [
    "EXECUTION_EVENT_SOURCE",
    "EXECUTION_GOAL_ADMITTED",
    "EXECUTION_PLAN_ADMITTED",
    "EXECUTION_PLAN_DRAFT_RECEIVED",
    "EXECUTION_POLICY_DECIDED",
    "EXECUTION_RUN_TERMINATED",
    "EXECUTION_TASK_BLOCKED",
    "EXECUTION_TASK_READY",
    "EXECUTION_TASK_STARTED",
    "EXECUTION_TASK_VERIFIED",
    "EXECUTION_TASK_VERIFYING",
    "EventBackedExecutionRunJournal",
    "ExecutionCoordinator",
    "ExecutionEventObserver",
    "ExecutionRunProjection",
    "ExecutionRunState",
    "ExecutionRuntimeError",
    "ExecutionTaskState",
]
