"""Durable alpha-v2 run journal and bounded repair coordinator."""

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
from blackcell.orchestration.alpha_lifecycle import (
    ALPHA_EVENT_SOURCE,
    ALPHA_RUN_CANCEL_REQUESTED,
    ALPHA_RUN_CANCELED,
    ALPHA_RUN_EVENT_TYPES,
    ALPHA_RUN_FAILED,
    ALPHA_RUN_RECONCILIATION_REQUIRED,
    ALPHA_RUN_SUCCEEDED,
)
from blackcell.orchestration.alpha_v2 import (
    ALPHA_V2_EVENT_SOURCE,
    ALPHA_V2_EVENT_TYPES,
    ALPHA_V2_GOAL_ADMITTED,
    ALPHA_V2_PLAN_ADMITTED,
    ALPHA_V2_PLAN_DRAFT_RECEIVED,
    ALPHA_V2_POLICY_DECIDED,
    ALPHA_V2_REPLAN_STARTED,
    ALPHA_V2_RUN_TERMINATED,
    ALPHA_V2_TASK_BLOCKED,
    ALPHA_V2_TASK_READY,
    ALPHA_V2_TASK_STARTED,
    ALPHA_V2_TASK_VERIFIED,
    ALPHA_V2_TASK_VERIFYING,
    AlphaGoalSpec,
    AlphaPlanningProvider,
    AlphaPlanningRequest,
    AlphaPlanningResult,
    AlphaPlanVersion,
    AlphaTaskSpec,
    AlphaV2PolicyKernel,
    AttemptEvidence,
    AttemptRoute,
    FailureClass,
    PolicyDecision,
    PraxisPromotionCandidate,
    RunLifecycleStatus,
    TaskAttemptExecutor,
    TaskLifecycleStatus,
    ToolActionRequest,
    alpha_goal_from_payload,
    alpha_goal_payload,
    alpha_plan_from_payload,
    alpha_plan_payload,
    compile_alpha_plan,
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
    ALPHA_V2_GOAL_ADMITTED: frozenset(
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
    ALPHA_V2_REPLAN_STARTED: frozenset(
        {"run_id", "previous_plan_id", "previous_plan_version", "next_plan_version"}
    ),
    ALPHA_V2_PLAN_DRAFT_RECEIVED: frozenset(
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
    ALPHA_V2_PLAN_ADMITTED: frozenset(
        {
            "run_id",
            "plan_id",
            "plan_digest",
            "plan_version",
            "supersedes_plan_id",
            "plan",
        }
    ),
    ALPHA_V2_POLICY_DECIDED: frozenset(
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
    ALPHA_V2_TASK_READY: frozenset({"run_id", "task_id", "attempt"}),
    ALPHA_V2_TASK_STARTED: frozenset(
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
    ALPHA_V2_TASK_VERIFYING: frozenset({"run_id", "task_id", "attempt", "workspace_id"}),
    ALPHA_V2_TASK_VERIFIED: frozenset(
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
    ALPHA_V2_TASK_BLOCKED: frozenset({"run_id", "task_id", "reason"}),
    ALPHA_V2_RUN_TERMINATED: frozenset({"run_id", "status", "reason"}),
}


class AlphaV2RuntimeError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class AlphaV2TaskState:
    task_id: str
    status: TaskLifecycleStatus
    depends_on: tuple[str, ...] = ()
    allowed_paths: tuple[str, ...] = ()
    max_attempts: int = 3
    attempts: int = 0
    last_error_signature: str | None = None
    same_error_count: int = 0
    active_workspace_id: str | None = None
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
class AlphaV2RunState:
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
    plan_version: int | None
    status: RunLifecycleStatus
    tasks: tuple[AlphaV2TaskState, ...]
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

    def task(self, task_id: str) -> AlphaV2TaskState:
        try:
            return next(item for item in self.tasks if item.task_id == task_id)
        except StopIteration as error:
            raise AlphaV2RuntimeError("alpha-v2-task-not-found") from error


class AlphaV2EventObserver(Protocol):
    def record(self, event: EventEnvelope) -> None: ...


class AlphaV2RunProjection:
    name = "alpha-run-kernel"
    version = 5

    def initial_state(self) -> AlphaV2RunState | None:
        return None

    def apply(
        self,
        state: AlphaV2RunState | None,
        event: EventEnvelope,
    ) -> AlphaV2RunState | None:
        if event.source == ALPHA_EVENT_SOURCE and event.event_type in ALPHA_RUN_EVENT_TYPES:
            if not event.stream_id.startswith("alpha:run:"):
                raise AlphaV2RuntimeError("invalid-alpha-v2-event")
            if state is None:
                return None
            if (
                event.stream_id != _stream_id(state.run_id)
                or event.stream_sequence != state.last_stream_sequence + 1
            ):
                raise AlphaV2RuntimeError("invalid-alpha-v2-event")
            status = state.status
            tasks = state.tasks
            terminal_task_status: TaskLifecycleStatus | None = None
            if event.event_type in {ALPHA_RUN_CANCEL_REQUESTED, ALPHA_RUN_CANCELED}:
                status = RunLifecycleStatus.CANCELED
                terminal_task_status = TaskLifecycleStatus.CANCELED
            elif event.event_type == ALPHA_RUN_SUCCEEDED:
                status = RunLifecycleStatus.SUCCEEDED
            elif event.event_type == ALPHA_RUN_FAILED:
                status = RunLifecycleStatus.TERMINAL_FAILURE
                terminal_task_status = TaskLifecycleStatus.TERMINAL_FAILURE
            elif event.event_type == ALPHA_RUN_RECONCILIATION_REQUIRED:
                status = RunLifecycleStatus.ESCALATED
                terminal_task_status = TaskLifecycleStatus.ESCALATED
            if state.status in _FINAL_RUN_STATUSES and status is not state.status:
                raise AlphaV2RuntimeError("event-after-alpha-v2-terminal")
            if status is RunLifecycleStatus.SUCCEEDED and (
                not tasks or any(item.status is not TaskLifecycleStatus.SUCCEEDED for item in tasks)
            ):
                raise AlphaV2RuntimeError("invalid-alpha-v2-transition")
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
        if event.source != ALPHA_V2_EVENT_SOURCE or event.event_type not in ALPHA_V2_EVENT_TYPES:
            raise AlphaV2RuntimeError("invalid-alpha-v2-event")
        payload = cast("Mapping[str, object]", thaw_json(event.payload))
        _require_event_payload(event.event_type, payload)
        run_id = _text(payload, "run_id")
        if event.stream_id != _stream_id(run_id):
            raise AlphaV2RuntimeError("invalid-alpha-v2-event")
        if event.event_type == ALPHA_V2_GOAL_ADMITTED:
            if state is not None:
                raise AlphaV2RuntimeError("invalid-alpha-v2-event")
            try:
                goal = alpha_goal_from_payload(payload.get("goal"))
            except (TypeError, ValueError) as error:
                raise AlphaV2RuntimeError("invalid-alpha-v2-event") from error
            if goal.goal_id != _text(payload, "goal_id") or goal.digest != _digest(
                payload, "goal_digest"
            ):
                raise AlphaV2RuntimeError("invalid-alpha-v2-event")
            return AlphaV2RunState(
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
                plan_version=None,
                status=RunLifecycleStatus.ADMITTED,
                tasks=(),
                latest_event_id=event.event_id,
                last_stream_sequence=event.stream_sequence,
            )
        if (
            state is None
            or state.run_id != run_id
            or event.stream_sequence != (state.last_stream_sequence + 1)
        ):
            raise AlphaV2RuntimeError("invalid-alpha-v2-event")
        if state.status in _FINAL_RUN_STATUSES:
            raise AlphaV2RuntimeError("event-after-alpha-v2-terminal")
        updated = state
        if event.event_type == ALPHA_V2_REPLAN_STARTED:
            if (
                state.status is not RunLifecycleStatus.REPLAN_REQUIRED
                or state.pending_draft_digest is not None
                or state.plan_id is None
                or state.plan_version is None
                or _text(payload, "previous_plan_id") != state.plan_id
                or _integer(payload, "previous_plan_version") != state.plan_version
                or _integer(payload, "next_plan_version") != state.plan_version + 1
            ):
                raise AlphaV2RuntimeError("invalid-alpha-v2-transition")
            updated = replace(state, status=RunLifecycleStatus.REPLANNING)
        elif event.event_type == ALPHA_V2_PLAN_DRAFT_RECEIVED:
            if (
                state.status not in {RunLifecycleStatus.ADMITTED, RunLifecycleStatus.REPLANNING}
                or state.pending_draft_digest is not None
            ):
                raise AlphaV2RuntimeError("invalid-alpha-v2-transition")
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
        elif event.event_type == ALPHA_V2_PLAN_ADMITTED:
            fresh = state.plan_id is None and state.status is RunLifecycleStatus.ADMITTED
            revised = state.plan_id is not None and state.status is RunLifecycleStatus.REPLANNING
            if not (fresh or revised):
                raise AlphaV2RuntimeError("invalid-alpha-v2-event")
            try:
                plan = alpha_plan_from_payload(payload.get("plan"))
            except (TypeError, ValueError) as error:
                raise AlphaV2RuntimeError("invalid-alpha-v2-event") from error
            if (
                plan.plan_id != _text(payload, "plan_id")
                or plan.plan_digest != _digest(payload, "plan_digest")
                or plan.plan_version != _integer(payload, "plan_version")
                or plan.supersedes_plan_id != _optional_text(payload, "supersedes_plan_id")
                or plan.goal_id != state.goal_id
                or plan.project_id != state.project_id
                or plan.intent_id != state.intent_id
                or plan.base_commit != state.goal_base_commit
                or plan.draft_digest != state.pending_draft_digest
            ):
                raise AlphaV2RuntimeError("invalid-alpha-v2-event")
            if revised and (
                state.plan_version is None
                or plan.supersedes_plan_id != state.plan_id
                or plan.plan_version != state.plan_version + 1
            ):
                raise AlphaV2RuntimeError("invalid-alpha-v2-transition")
            if fresh and (plan.plan_version != 1 or plan.supersedes_plan_id is not None):
                raise AlphaV2RuntimeError("invalid-alpha-v2-transition")
            tasks = tuple(
                AlphaV2TaskState(
                    item.task_id,
                    TaskLifecycleStatus.PENDING,
                    item.depends_on,
                    item.allowed_paths,
                    item.max_attempts,
                )
                for item in plan.tasks
            )
            if len({item.task_id for item in tasks}) != len(tasks):
                raise AlphaV2RuntimeError("invalid-alpha-v2-event")
            updated = replace(
                state,
                plan_id=plan.plan_id,
                plan_digest=plan.plan_digest,
                plan_version=plan.plan_version,
                status=RunLifecycleStatus.RUNNING,
                tasks=tuple(sorted(tasks, key=lambda item: item.task_id)),
                pending_draft_digest=None,
            )
        elif event.event_type == ALPHA_V2_POLICY_DECIDED:
            task = _task(state, _text(payload, "task_id"))
            attempt = _integer(payload, "attempt")
            if (
                task.status is not TaskLifecycleStatus.READY
                or attempt != task.attempts + 1
                or task.pending_policy_decision_id is not None
            ):
                raise AlphaV2RuntimeError("invalid-alpha-v2-transition")
            try:
                if state.plan_id is None:
                    raise AlphaV2RuntimeError("invalid-alpha-v2-transition")
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
            except (AlphaV2RuntimeError, TypeError, ValueError) as error:
                raise AlphaV2RuntimeError("invalid-alpha-v2-event") from error
            if decision.action_digest != expected_action.digest or decision.decision_id != _digest(
                payload, "decision_id"
            ):
                raise AlphaV2RuntimeError("invalid-alpha-v2-event")
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
        elif event.event_type == ALPHA_V2_TASK_READY:
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
                raise AlphaV2RuntimeError("invalid-alpha-v2-transition")
            updated = _replace_task(state, replace(task, status=TaskLifecycleStatus.READY))
        elif event.event_type == ALPHA_V2_TASK_STARTED:
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
                raise AlphaV2RuntimeError("invalid-alpha-v2-transition")
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
        elif event.event_type == ALPHA_V2_TASK_VERIFYING:
            task = _task(state, _text(payload, "task_id"))
            if (
                task.status is not TaskLifecycleStatus.RUNNING
                or _integer(payload, "attempt") != task.attempts
                or _text(payload, "workspace_id") != task.active_workspace_id
            ):
                raise AlphaV2RuntimeError("invalid-alpha-v2-transition")
            updated = _replace_task(state, replace(task, status=TaskLifecycleStatus.VERIFYING))
        elif event.event_type == ALPHA_V2_TASK_VERIFIED:
            task = _task(state, _text(payload, "task_id"))
            if (
                task.status is not TaskLifecycleStatus.VERIFYING
                or _integer(payload, "attempt") != task.attempts
                or _text(payload, "workspace_id") != task.active_workspace_id
            ):
                raise AlphaV2RuntimeError("invalid-alpha-v2-transition")
            route = AttemptRoute(_text(payload, "route"))
            verifier_exit_code = _integer(payload, "verifier_exit_code")
            if verifier_exit_code > 255:
                raise AlphaV2RuntimeError("invalid-alpha-v2-transition")
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
                raise AlphaV2RuntimeError("invalid-alpha-v2-transition")
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
                raise AlphaV2RuntimeError("invalid-alpha-v2-transition")
            if route is not AttemptRoute.SUCCEEDED and (
                failure_class is None or failure_summary is None
            ):
                raise AlphaV2RuntimeError("invalid-alpha-v2-transition")
            artifact_digests = _digest_tuple(payload, "artifact_digests", require_nonempty=True)
            progress_digests = _digest_tuple(payload, "progress_digests", require_nonempty=True)
            if not set(progress_digests).issubset(artifact_digests):
                raise AlphaV2RuntimeError("invalid-alpha-v2-transition")
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
                raise AlphaV2RuntimeError("invalid-alpha-v2-event") from error
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
                raise AlphaV2RuntimeError("invalid-alpha-v2-transition")
            status = TaskLifecycleStatus(route.value)
            updated = _replace_task(
                state,
                replace(
                    task,
                    status=status,
                    last_error_signature=signature,
                    same_error_count=same_error_count,
                    active_workspace_id=None,
                    evidence_event_ids=(*task.evidence_event_ids, event.event_id),
                    last_failure_class=failure_class,
                    last_failure_summary=failure_summary,
                    last_artifact_digests=artifact_digests,
                    last_progress_digests=progress_digests,
                    head_commit=evidence.head_commit,
                ),
            )
            updated = replace(
                updated,
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
        elif event.event_type == ALPHA_V2_TASK_BLOCKED:
            task = _task(state, _text(payload, "task_id"))
            reason = _text(payload, "reason")
            if (
                task.status is not TaskLifecycleStatus.READY
                or task.pending_policy_allowed is not False
                or task.pending_policy_reason != reason
            ):
                raise AlphaV2RuntimeError("invalid-alpha-v2-transition")
            updated = _replace_task(
                state,
                replace(
                    task,
                    status=TaskLifecycleStatus.BLOCKED,
                    pending_policy_decision_id=None,
                    pending_policy_action_digest=None,
                    pending_policy_allowed=None,
                    pending_policy_reason=None,
                ),
            )
        elif event.event_type == ALPHA_V2_RUN_TERMINATED:
            status = RunLifecycleStatus(_text(payload, "status"))
            if status not in _RUN_OUTCOME_STATUSES:
                raise AlphaV2RuntimeError("invalid-alpha-v2-transition")
            _text(payload, "reason")
            if status is RunLifecycleStatus.SUCCEEDED and (
                not state.tasks
                or any(item.status is not TaskLifecycleStatus.SUCCEEDED for item in state.tasks)
            ):
                raise AlphaV2RuntimeError("invalid-alpha-v2-transition")
            updated = replace(state, status=status)
        return replace(
            updated,
            latest_event_id=event.event_id,
            last_stream_sequence=event.stream_sequence,
        )

    def dump_state(self, state: AlphaV2RunState | None) -> JsonInput:
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
            "plan_version": state.plan_version,
            "status": state.status.value,
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

    def load_state(self, value: object) -> AlphaV2RunState | None:
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
            "invalid-alpha-v2-checkpoint",
        )
        raw_tasks = raw.get("tasks")
        if not isinstance(raw_tasks, list):
            raise AlphaV2RuntimeError("invalid-alpha-v2-checkpoint")
        tasks = tuple(_task_state_from_checkpoint(item) for item in raw_tasks)
        plan_version_value = raw.get("plan_version")
        if plan_version_value is not None and (
            isinstance(plan_version_value, bool) or not isinstance(plan_version_value, int)
        ):
            raise AlphaV2RuntimeError("invalid-alpha-v2-checkpoint")
        return AlphaV2RunState(
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
            plan_version=cast("int | None", plan_version_value),
            status=RunLifecycleStatus(_text(raw, "status")),
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


class EventBackedAlphaV2RunJournal:
    """Append-only run journal with snapshots every 100 events and at terminal states."""

    def __init__(
        self,
        events: EventStore,
        checkpoints: CheckpointStore,
        *,
        observer: AlphaV2EventObserver | None = None,
    ) -> None:
        if events.path.resolve() != checkpoints.path.resolve():
            raise ValueError("alpha-v2 event and checkpoint stores must share one database")
        self._events = events
        self._checkpoints = checkpoints
        self._projection = AlphaV2RunProjection()
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
            event_type not in ALPHA_V2_EVENT_TYPES
            or not isinstance(actor, str)
            or not actor.strip()
            or "run_id" in payload
        ):
            raise AlphaV2RuntimeError("invalid-alpha-v2-event")
        stream_id = _stream_id(run_id)
        existing_events = self._events.read_stream(stream_id)
        _validate_stream(existing_events, stream_id)
        if event_type == ALPHA_V2_GOAL_ADMITTED:
            public_events = tuple(
                item
                for item in existing_events
                if item.source == ALPHA_EVENT_SOURCE and item.event_type in ALPHA_RUN_EVENT_TYPES
            )
            if len(public_events) > 1:
                raise AlphaV2RuntimeError("alpha-v2-public-run-already-active")
        current = self._rebuild(run_id).state
        if event_type == ALPHA_V2_PLAN_ADMITTED:
            scoped_plan_id = payload.get("plan_id")
            scoped_plan_version = payload.get("plan_version")
        else:
            scoped_plan_id = None if current is None else current.plan_id
            scoped_plan_version = None if current is None else current.plan_version
        idempotency_scope: dict[str, JsonInput] = {
            "plan_id": scoped_plan_id,
            "plan_version": scoped_plan_version,
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
                or prior.source != ALPHA_V2_EVENT_SOURCE
                or prior.actor != actor
                or thaw_json(prior.payload) != expected_payload
            ):
                raise AlphaV2RuntimeError("alpha-v2-idempotency-conflict")
            return prior
        sequence = len(existing_events)
        event = EventEnvelope.create(
            stream_id=stream_id,
            stream_sequence=sequence + 1,
            event_type=event_type,
            actor=actor,
            source=ALPHA_V2_EVENT_SOURCE,
            payload=expected_payload,
            idempotency_key=idempotency_key,
            correlation_id=run_id,
            causation_id=None if not existing_events else existing_events[-1].event_id,
        )
        self._projection.apply(current, event)
        try:
            stored = self._events.append(event, expected_sequence=sequence)
        except (ConcurrencyError, EventConflictError, IdempotencyConflict) as error:
            raise AlphaV2RuntimeError("alpha-v2-concurrency-conflict") from error
        if self._observer is not None:
            with suppress(Exception):
                self._observer.record(stored)
        state = self.rehydrate(run_id)
        if state.last_stream_sequence % 100 == 0 or state.status in _RUN_OUTCOME_STATUSES:
            with suppress(Exception):
                self._save_checkpoint(run_id)
        return stored

    def rehydrate(self, run_id: str) -> AlphaV2RunState:
        result = self._rebuild(run_id)
        if result.state is None:
            raise AlphaV2RuntimeError("alpha-v2-run-not-found")
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

    def goal(self, run_id: str) -> AlphaGoalSpec:
        event = next(
            (item for item in self.events(run_id) if item.event_type == ALPHA_V2_GOAL_ADMITTED),
            None,
        )
        if event is None:
            raise AlphaV2RuntimeError("alpha-v2-goal-not-found")
        try:
            goal = alpha_goal_from_payload(_mapping(thaw_json(event.payload)).get("goal"))
        except (AlphaV2RuntimeError, TypeError, ValueError) as error:
            raise AlphaV2RuntimeError("invalid-alpha-v2-event") from error
        state = self.rehydrate(run_id)
        if goal.goal_id != state.goal_id or goal.digest != state.goal_digest:
            raise AlphaV2RuntimeError("alpha-v2-run-binding-mismatch")
        return goal

    def plan(self, run_id: str) -> AlphaPlanVersion:
        event = next(
            (
                item
                for item in reversed(self.events(run_id))
                if item.event_type == ALPHA_V2_PLAN_ADMITTED
            ),
            None,
        )
        if event is None:
            raise AlphaV2RuntimeError("alpha-v2-plan-not-found")
        try:
            plan = alpha_plan_from_payload(_mapping(thaw_json(event.payload)).get("plan"))
        except (AlphaV2RuntimeError, TypeError, ValueError) as error:
            raise AlphaV2RuntimeError("invalid-alpha-v2-event") from error
        state = self.rehydrate(run_id)
        if plan.plan_id != state.plan_id or plan.plan_digest != state.plan_digest:
            raise AlphaV2RuntimeError("alpha-v2-run-binding-mismatch")
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
        except AlphaV2RuntimeError, TypeError, ValueError:
            if checkpoint is None:
                raise
            return self._runner.rebuild(
                self._events,
                self._projection,
                stream_id=stream_id,
            )


@dataclass(frozen=True, slots=True)
class AlphaV2Coordinator:
    journal: EventBackedAlphaV2RunJournal
    provider: AlphaPlanningProvider
    executor: TaskAttemptExecutor
    policy: AlphaV2PolicyKernel

    def reconcile_incomplete_planning(self, run_id: str, *, actor: str) -> AlphaV2RunState:
        """Fail closed when a durable planning fence has no admitted result after restart."""

        state = self.journal.rehydrate(run_id)
        if state.status is RunLifecycleStatus.CANCELED:
            return state
        if state.status not in {
            RunLifecycleStatus.ADMITTED,
            RunLifecycleStatus.REPLANNING,
        }:
            raise AlphaV2RuntimeError("alpha-v2-planning-not-ambiguous")
        return self._terminate(
            run_id,
            RunLifecycleStatus.ESCALATED,
            actor=actor,
            reason="ambiguous-planning-dispatch",
        )

    def exhaust_replan_budget(self, run_id: str, *, actor: str) -> AlphaV2RunState:
        state = self.journal.rehydrate(run_id)
        if state.status is not RunLifecycleStatus.REPLAN_REQUIRED:
            raise AlphaV2RuntimeError("alpha-v2-replan-not-required")
        return self._terminate(
            run_id,
            RunLifecycleStatus.ESCALATED,
            actor=actor,
            reason="replan-budget-exhausted",
        )

    def compile_and_admit(
        self,
        run_id: str,
        request: AlphaPlanningRequest,
        *,
        actor: str,
        previous: AlphaPlanVersion | None = None,
    ) -> tuple[AlphaPlanVersion, AlphaPlanningResult]:
        if request.run_id != run_id:
            raise AlphaV2RuntimeError("alpha-v2-run-binding-mismatch")
        goal = request.goal
        if previous is None:
            self.journal.append(
                run_id,
                ALPHA_V2_GOAL_ADMITTED,
                {
                    "goal_id": goal.goal_id,
                    "goal_digest": goal.digest,
                    "goal": alpha_goal_payload(goal),
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
                or state.plan_version != previous.plan_version
            ):
                raise AlphaV2RuntimeError("alpha-v2-run-binding-mismatch")
            self.journal.append(
                run_id,
                ALPHA_V2_REPLAN_STARTED,
                {
                    "previous_plan_id": previous.plan_id,
                    "previous_plan_version": previous.plan_version,
                    "next_plan_version": previous.plan_version + 1,
                },
                actor=actor,
            )
        result = self.provider.propose_plan(request)
        plan = compile_alpha_plan(
            goal,
            cast("Mapping[str, object]", result.draft),
            previous=previous,
        )
        self.journal.append(
            run_id,
            ALPHA_V2_PLAN_DRAFT_RECEIVED,
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
            ALPHA_V2_PLAN_ADMITTED,
            {
                "plan_id": plan.plan_id,
                "plan_digest": plan.plan_digest,
                "plan_version": plan.plan_version,
                "supersedes_plan_id": plan.supersedes_plan_id,
                "plan": alpha_plan_payload(plan),
            },
            actor=actor,
        )
        return plan, result

    def execute(
        self,
        run_id: str,
        request: AlphaPlanningRequest,
        plan: AlphaPlanVersion,
        *,
        actor: str,
    ) -> AlphaV2RunState:
        if request.run_id != run_id:
            raise AlphaV2RuntimeError("alpha-v2-run-binding-mismatch")
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
            raise AlphaV2RuntimeError("alpha-v2-run-binding-mismatch")
        if state.status in _RUN_OUTCOME_STATUSES:
            return state
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
                        ALPHA_V2_TASK_READY,
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
                except AlphaV2RuntimeError:
                    return self._terminate(
                        run_id,
                        RunLifecycleStatus.REPLAN_REQUIRED,
                        actor=actor,
                        reason="base-commit-ambiguous",
                    )
                state = self.journal.rehydrate(run_id)
                remaining_budget = _remaining_budget(request.budget, state)
                if task.allowed_paths and _provider_budget_exhausted(remaining_budget):
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
                decision = self.policy.authorize(goal, plan, task, action)
                self._record_policy(run_id, task_id, attempt, decision, actor=actor)
                if not decision.allowed:
                    self.journal.append(
                        run_id,
                        ALPHA_V2_TASK_BLOCKED,
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
                    ALPHA_V2_TASK_STARTED,
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
                    ALPHA_V2_TASK_VERIFYING,
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
                    ALPHA_V2_TASK_VERIFIED,
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
            ALPHA_V2_POLICY_DECIDED,
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
    ) -> AlphaV2RunState:
        self.journal.append(
            run_id,
            ALPHA_V2_RUN_TERMINATED,
            {"status": status.value, "reason": reason},
            actor=actor,
        )
        return self.journal.rehydrate(run_id)


def _replace_task(state: AlphaV2RunState, replacement: AlphaV2TaskState) -> AlphaV2RunState:
    return replace(
        state,
        tasks=tuple(
            replacement if item.task_id == replacement.task_id else item for item in state.tasks
        ),
    )


def _task(state: AlphaV2RunState, task_id: str) -> AlphaV2TaskState:
    return state.task(task_id)


def _workspace_id(plan: AlphaPlanVersion, task: AlphaTaskSpec, attempt: int) -> str:
    return _workspace_id_from_ids(plan.plan_id, task.task_id, attempt)


def _workspace_id_from_ids(plan_id: str, task_id: str, attempt: int) -> str:
    identity = json_digest({"plan_id": plan_id, "task_id": task_id, "attempt": attempt})
    return f"workspace-{identity.removeprefix('sha256:')[:32]}"


def _expected_base_commit(state: AlphaV2RunState, task: AlphaV2TaskState) -> str:
    if task.head_commit is not None:
        return task.head_commit
    if not task.depends_on:
        return state.goal_base_commit
    dependency_heads = tuple(state.task(item).head_commit for item in task.depends_on)
    if any(item is None for item in dependency_heads) or len(set(dependency_heads)) > 1:
        raise AlphaV2RuntimeError("alpha-v2-base-commit-ambiguous")
    return cast("str", dependency_heads[0])


def _attempt_base_commit(
    state: AlphaV2RunState,
    task: AlphaTaskSpec,
    goal_base_commit: str,
) -> str:
    if state.goal_base_commit != goal_base_commit:
        raise AlphaV2RuntimeError("alpha-v2-run-binding-mismatch")
    return _expected_base_commit(state, state.task(task.task_id))


def _remaining_budget(budget: GatewayBudget, state: AlphaV2RunState) -> GatewayBudget:
    return GatewayBudget(
        max(0, budget.max_input_tokens - state.input_tokens),
        max(0, budget.max_output_tokens - state.output_tokens),
        max(0, budget.max_latency_ms - state.latency_ms),
        max(0, budget.max_cost_microusd - state.cost_microusd),
    )


def _provider_budget_exhausted(budget: GatewayBudget) -> bool:
    return (
        budget.max_input_tokens == 0 or budget.max_output_tokens == 0 or budget.max_latency_ms == 0
    )


def _budget_overdrawn(budget: GatewayBudget, state: AlphaV2RunState) -> bool:
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
        raise AlphaV2RuntimeError("invalid-alpha-v2-run-id")
    return f"alpha:run:{run_id}"


def _validate_stream(events: tuple[EventEnvelope, ...], stream_id: str) -> None:
    for sequence, event in enumerate(events, start=1):
        known_event = (
            event.source == ALPHA_EVENT_SOURCE and event.event_type in ALPHA_RUN_EVENT_TYPES
        ) or (event.source == ALPHA_V2_EVENT_SOURCE and event.event_type in ALPHA_V2_EVENT_TYPES)
        if (
            event.stream_id != stream_id
            or event.stream_sequence != sequence
            or event.schema_version != 1
            or not known_event
            or (sequence > 1 and event.causation_id != events[sequence - 2].event_id)
        ):
            raise AlphaV2RuntimeError("invalid-alpha-v2-event")


def _require_event_payload(event_type: str, payload: Mapping[str, object]) -> None:
    expected = _EVENT_PAYLOAD_FIELDS.get(event_type)
    if expected is None or set(payload) != expected:
        raise AlphaV2RuntimeError("invalid-alpha-v2-event")


def _require_fields(value: Mapping[str, object], expected: set[str], error_code: str) -> None:
    if set(value) != expected:
        raise AlphaV2RuntimeError(error_code)


def _task_state_from_checkpoint(value: object) -> AlphaV2TaskState:
    raw = _mapping(value)
    _require_fields(
        raw,
        {
            "task_id",
            "status",
            "depends_on",
            "allowed_paths",
            "max_attempts",
            "attempts",
            "last_error_signature",
            "same_error_count",
            "active_workspace_id",
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
        },
        "invalid-alpha-v2-checkpoint",
    )
    task = AlphaV2TaskState(
        task_id=_text(raw, "task_id"),
        status=TaskLifecycleStatus(_text(raw, "status")),
        depends_on=_text_tuple(raw, "depends_on"),
        allowed_paths=_text_tuple(raw, "allowed_paths"),
        max_attempts=_integer(raw, "max_attempts"),
        attempts=_integer(raw, "attempts"),
        last_error_signature=_optional_digest(raw, "last_error_signature"),
        same_error_count=_integer(raw, "same_error_count"),
        active_workspace_id=_optional_text(raw, "active_workspace_id"),
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
        raise AlphaV2RuntimeError("invalid-alpha-v2-checkpoint")
    if (
        not 1 <= task.max_attempts <= 3
        or task.attempts > task.max_attempts
        or task.task_id in task.depends_on
        or len(set(task.depends_on)) != len(task.depends_on)
        or len(set(task.allowed_paths)) != len(task.allowed_paths)
    ):
        raise AlphaV2RuntimeError("invalid-alpha-v2-checkpoint")
    return task


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise AlphaV2RuntimeError("invalid-alpha-v2-event")
    return cast("Mapping[str, object]", value)


def _text(value: Mapping[str, object], field: str) -> str:
    item = value.get(field)
    if not isinstance(item, str) or not item:
        raise AlphaV2RuntimeError("invalid-alpha-v2-event")
    return item


def _optional_text(value: Mapping[str, object], field: str) -> str | None:
    item = value.get(field)
    if item is None:
        return None
    return _text(value, field)


def _integer(value: Mapping[str, object], field: str) -> int:
    item = value.get(field)
    if isinstance(item, bool) or not isinstance(item, int) or item < 0:
        raise AlphaV2RuntimeError("invalid-alpha-v2-event")
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
        raise AlphaV2RuntimeError("invalid-alpha-v2-event")
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
        raise AlphaV2RuntimeError("invalid-alpha-v2-event")
    return item


def _optional_boolean(value: Mapping[str, object], field: str) -> bool | None:
    if value.get(field) is None:
        return None
    return _boolean(value, field)


def _digest(value: Mapping[str, object], field: str) -> str:
    item = _text(value, field)
    if not item.startswith("sha256:") or len(item) != 71:
        raise AlphaV2RuntimeError("invalid-alpha-v2-event")
    return item


def _optional_digest(value: Mapping[str, object], field: str) -> str | None:
    if value.get(field) is None:
        return None
    return _digest(value, field)


def _commit(value: Mapping[str, object], field: str) -> str:
    item = _text(value, field)
    if len(item) != 40 or any(character not in "0123456789abcdef" for character in item):
        raise AlphaV2RuntimeError("invalid-alpha-v2-event")
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
        raise AlphaV2RuntimeError("invalid-alpha-v2-event")
    digests = tuple(cast("list[str]", item))
    if digests != tuple(sorted(set(digests))):
        raise AlphaV2RuntimeError("invalid-alpha-v2-event")
    return digests


def _text_tuple(value: Mapping[str, object], field: str) -> tuple[str, ...]:
    item = value.get(field)
    if not isinstance(item, list) or any(not isinstance(child, str) for child in item):
        raise AlphaV2RuntimeError("invalid-alpha-v2-event")
    return tuple(cast("list[str]", item))


__all__ = [
    "ALPHA_V2_EVENT_SOURCE",
    "ALPHA_V2_GOAL_ADMITTED",
    "ALPHA_V2_PLAN_ADMITTED",
    "ALPHA_V2_PLAN_DRAFT_RECEIVED",
    "ALPHA_V2_POLICY_DECIDED",
    "ALPHA_V2_RUN_TERMINATED",
    "ALPHA_V2_TASK_BLOCKED",
    "ALPHA_V2_TASK_READY",
    "ALPHA_V2_TASK_STARTED",
    "ALPHA_V2_TASK_VERIFIED",
    "ALPHA_V2_TASK_VERIFYING",
    "AlphaV2Coordinator",
    "AlphaV2EventObserver",
    "AlphaV2RunProjection",
    "AlphaV2RunState",
    "AlphaV2RuntimeError",
    "AlphaV2TaskState",
    "EventBackedAlphaV2RunJournal",
]
