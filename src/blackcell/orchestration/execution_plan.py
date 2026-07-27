"""Host-owned plan compilation and bounded task-attempt policy for the execution runtime.

Provider output enters this module only as an untrusted mapping. The compiler owns every
executable field: ordering, attempt bounds, allowed repository paths, and verification argv.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from itertools import pairwise
from typing import Protocol, cast

from blackcell.gateway import DataClassification, GatewayBudget, LocalityPolicy
from blackcell.kernel import JsonInput, JsonValue
from blackcell.kernel._json import freeze_json, json_digest

EXECUTION_GOAL_SCHEMA = "blackcell.goal/v2"
EXECUTION_PLAN_DRAFT_SCHEMA = "blackcell.plan-draft/v1"
EXECUTION_PLAN_SCHEMA = "blackcell.plan/v2"
EXECUTION_ATTEMPT_EVIDENCE_SCHEMA = "blackcell.attempt-evidence/v1"
EXECUTION_PROMOTION_CANDIDATE_SCHEMA = "praxis-promotion-candidate/v1"
EXECUTION_EVENT_SOURCE = "blackcell.execution"
EXECUTION_GOAL_ADMITTED = "execution.goal.admitted"
EXECUTION_PLAN_DRAFT_RECEIVED = "execution.plan-draft.received"
EXECUTION_PLAN_ADMITTED = "execution.plan.admitted"
EXECUTION_REPLAN_STARTED = "execution.replan.started"
EXECUTION_TASK_READY = "execution.task.ready"
EXECUTION_POLICY_DECIDED = "execution.policy.decided"
EXECUTION_TASK_STARTED = "execution.task.started"
EXECUTION_TASK_VERIFYING = "execution.task.verifying"
EXECUTION_TASK_VERIFIED = "execution.task.verified"
EXECUTION_TASK_BLOCKED = "execution.task.blocked"
EXECUTION_RUN_TERMINATED = "execution.run.terminated"
EXECUTION_EVENT_TYPES = frozenset(
    {
        EXECUTION_GOAL_ADMITTED,
        EXECUTION_PLAN_DRAFT_RECEIVED,
        EXECUTION_PLAN_ADMITTED,
        EXECUTION_REPLAN_STARTED,
        EXECUTION_TASK_READY,
        EXECUTION_POLICY_DECIDED,
        EXECUTION_TASK_STARTED,
        EXECUTION_TASK_VERIFYING,
        EXECUTION_TASK_VERIFIED,
        EXECUTION_TASK_BLOCKED,
        EXECUTION_RUN_TERMINATED,
    }
)

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SAFE_EXECUTABLE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}\Z")
_MAX_TASKS = 64
_MAX_PATHS = 256
_MAX_CHECKS = 32
_MAX_ARGV = 64
_MAX_TOKEN_BYTES = 4_096


class PlanContractError(ValueError):
    """A content-free rejection at the deterministic execution boundary."""

    def __init__(self, code: str = "invalid-execution-contract") -> None:
        self.code = code
        super().__init__(code)


class FailureClass(StrEnum):
    TRANSIENT = "transient"
    LOGIC_BUG = "logic-bug"
    CONTRACT_MISMATCH = "contract-mismatch"
    MISSING_DEPENDENCY = "missing-dependency"
    INVALID_ASSUMPTION = "invalid-assumption"
    POLICY = "policy"
    UNCLASSIFIED = "unclassified"


class AttemptRoute(StrEnum):
    SUCCEEDED = "succeeded"
    REPAIRABLE = "repairable"
    REPLAN_REQUIRED = "replan-required"
    BLOCKED = "blocked"
    ESCALATED = "escalated"
    TERMINAL_FAILURE = "terminal-failure"


class TaskLifecycleStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    REPAIRABLE = "repairable"
    REPLAN_REQUIRED = "replan-required"
    BLOCKED = "blocked"
    CANCELED = "canceled"
    ESCALATED = "escalated"
    TERMINAL_FAILURE = "terminal-failure"


class RunLifecycleStatus(StrEnum):
    ADMITTED = "admitted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    REPLAN_REQUIRED = "replan-required"
    REPLANNING = "replanning"
    BLOCKED = "blocked"
    CANCELED = "canceled"
    ESCALATED = "escalated"
    TERMINAL_FAILURE = "terminal-failure"


@dataclass(frozen=True, slots=True)
class ExecutionAuthority:
    budget: GatewayBudget
    check_timeout_seconds: int
    max_changed_paths: int
    consumed_budget: GatewayBudget = field(
        default_factory=lambda: GatewayBudget(0, 0, 0, 0),
    )
    input_tokens_complete: bool = True
    output_tokens_complete: bool = True
    cost_microusd_complete: bool = True

    def __post_init__(self) -> None:
        if (
            not isinstance(self.budget, GatewayBudget)
            or not isinstance(self.consumed_budget, GatewayBudget)
            or self.consumed_budget.max_input_tokens > self.budget.max_input_tokens
            or self.consumed_budget.max_output_tokens > self.budget.max_output_tokens
            or self.consumed_budget.max_latency_ms > self.budget.max_latency_ms
            or self.consumed_budget.max_cost_microusd > self.budget.max_cost_microusd
            or not isinstance(self.input_tokens_complete, bool)
            or not isinstance(self.output_tokens_complete, bool)
            or not isinstance(self.cost_microusd_complete, bool)
            or isinstance(self.check_timeout_seconds, bool)
            or not isinstance(self.check_timeout_seconds, int)
            or not 1 <= self.check_timeout_seconds <= 600
            or isinstance(self.max_changed_paths, bool)
            or not isinstance(self.max_changed_paths, int)
            or not 0 <= self.max_changed_paths <= 10_000
        ):
            raise PlanContractError()


@dataclass(frozen=True, slots=True)
class GoalSpec:
    goal_id: str
    project_id: str
    intent_id: str
    objective: str
    base_commit: str
    constraints: tuple[str, ...]
    allowed_paths: tuple[str, ...]
    verification_checks: tuple[VerificationCheck, ...]
    max_attempts: int = 3
    same_error_limit: int = 2
    schema_version: str = EXECUTION_GOAL_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_version != EXECUTION_GOAL_SCHEMA:
            raise PlanContractError()
        for value in (self.goal_id, self.project_id, self.intent_id):
            _identifier(value)
        if (
            not isinstance(self.objective, str)
            or not self.objective.strip()
            or len(self.objective.encode("utf-8")) > 16 * 1024
        ):
            raise PlanContractError()
        if not isinstance(self.base_commit, str) or _COMMIT.fullmatch(self.base_commit) is None:
            raise PlanContractError()
        constraints = _bounded_text_tuple(self.constraints, maximum=64)
        paths = _path_tuple(self.allowed_paths)
        if (
            not isinstance(self.verification_checks, tuple)
            or not 1 <= len(self.verification_checks) <= _MAX_CHECKS
            or not all(isinstance(item, VerificationCheck) for item in self.verification_checks)
        ):
            raise PlanContractError()
        checks = tuple(sorted(self.verification_checks))
        if len({item.check_id for item in checks}) != len(checks):
            raise PlanContractError()
        if not 1 <= self.max_attempts <= 3 or not 1 <= self.same_error_limit <= 2:
            raise PlanContractError()
        object.__setattr__(self, "constraints", constraints)
        object.__setattr__(self, "allowed_paths", paths)
        object.__setattr__(self, "verification_checks", checks)

    @property
    def digest(self) -> str:
        return json_digest(goal_payload(self))


@dataclass(frozen=True, slots=True, order=True)
class VerificationCheck:
    check_id: str
    argv: tuple[str, ...]
    expected_exit_code: int = 0

    def __post_init__(self) -> None:
        _identifier(self.check_id)
        if not 1 <= len(self.argv) <= _MAX_ARGV:
            raise PlanContractError()
        for token in self.argv:
            if (
                not isinstance(token, str)
                or not token
                or "\x00" in token
                or len(token.encode("utf-8")) > _MAX_TOKEN_BYTES
            ):
                raise PlanContractError()
        if _SAFE_EXECUTABLE.fullmatch(self.argv[0]) is None:
            raise PlanContractError()
        if not 0 <= self.expected_exit_code <= 255:
            raise PlanContractError()


@dataclass(frozen=True, slots=True)
class TaskSpec:
    task_id: str
    objective: str
    depends_on: tuple[str, ...]
    allowed_paths: tuple[str, ...]
    checks: tuple[VerificationCheck, ...]
    max_attempts: int
    task_digest: str = field(init=False)

    def __post_init__(self) -> None:
        _identifier(self.task_id)
        if (
            not isinstance(self.objective, str)
            or not self.objective.strip()
            or len(self.objective.encode("utf-8")) > 16 * 1024
        ):
            raise PlanContractError()
        dependencies = tuple(sorted(set(self.depends_on)))
        if self.task_id in dependencies or any(
            not isinstance(item, str) or _IDENTIFIER.fullmatch(item) is None
            for item in dependencies
        ):
            raise PlanContractError()
        paths = _path_tuple(self.allowed_paths)
        checks = tuple(sorted(self.checks))
        if not checks or len(checks) > _MAX_CHECKS:
            raise PlanContractError()
        if len({item.check_id for item in checks}) != len(checks):
            raise PlanContractError()
        if not 1 <= self.max_attempts <= 3:
            raise PlanContractError()
        object.__setattr__(self, "depends_on", dependencies)
        object.__setattr__(self, "allowed_paths", paths)
        object.__setattr__(self, "checks", checks)
        object.__setattr__(self, "task_digest", json_digest(task_payload(self)))


@dataclass(frozen=True, slots=True)
class Plan:
    plan_id: str
    goal_id: str
    project_id: str
    intent_id: str
    plan_revision: int
    supersedes_plan_id: str | None
    base_commit: str
    draft_digest: str
    tasks: tuple[TaskSpec, ...]
    schema_version: str = EXECUTION_PLAN_SCHEMA
    plan_digest: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != EXECUTION_PLAN_SCHEMA:
            raise PlanContractError()
        for value in (self.plan_id, self.goal_id, self.project_id, self.intent_id):
            _identifier(value)
        if self.plan_revision < 1:
            raise PlanContractError()
        if (self.plan_revision == 1) != (self.supersedes_plan_id is None):
            raise PlanContractError()
        if self.supersedes_plan_id is not None:
            _identifier(self.supersedes_plan_id)
            if self.supersedes_plan_id == self.plan_id:
                raise PlanContractError()
        if (
            _COMMIT.fullmatch(self.base_commit) is None
            or _DIGEST.fullmatch(self.draft_digest) is None
        ):
            raise PlanContractError()
        tasks = tuple(sorted(self.tasks, key=lambda item: item.task_id))
        if not 1 <= len(tasks) <= _MAX_TASKS:
            raise PlanContractError()
        identifiers = tuple(item.task_id for item in tasks)
        if len(set(identifiers)) != len(identifiers):
            raise PlanContractError()
        known = set(identifiers)
        if any(not set(item.depends_on).issubset(known) for item in tasks):
            raise PlanContractError()
        _topological_order(tasks)
        _validate_writer_order(tasks)
        object.__setattr__(self, "tasks", tasks)
        object.__setattr__(self, "plan_digest", json_digest(plan_payload(self)))

    @property
    def topological_order(self) -> tuple[str, ...]:
        return _topological_order(self.tasks)


@dataclass(frozen=True, slots=True)
class PlanningRequest:
    goal: GoalSpec
    classification: DataClassification
    locality: LocalityPolicy
    budget: GatewayBudget
    estimated_input_tokens: int
    correlation_id: str
    run_id: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.goal, GoalSpec)
            or not isinstance(self.classification, DataClassification)
            or not isinstance(self.locality, LocalityPolicy)
            or not isinstance(self.budget, GatewayBudget)
            or isinstance(self.estimated_input_tokens, bool)
            or not isinstance(self.estimated_input_tokens, int)
            or self.estimated_input_tokens < 0
        ):
            raise PlanContractError()
        _identifier(self.correlation_id)
        _identifier(self.run_id)


@dataclass(frozen=True, slots=True)
class PlanningResult:
    draft: Mapping[str, JsonValue]
    provider_output_digest: str
    profile_id: str
    adapter_id: str
    model_id: str
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int
    cost_microusd: int | None

    def __post_init__(self) -> None:
        if _DIGEST.fullmatch(self.provider_output_digest) is None:
            raise PlanContractError()
        for value in (self.profile_id, self.adapter_id, self.model_id):
            _identifier(value)
        for value in (self.input_tokens, self.output_tokens, self.cost_microusd):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise PlanContractError()
        if (
            isinstance(self.latency_ms, bool)
            or not isinstance(self.latency_ms, int)
            or self.latency_ms < 0
        ):
            raise PlanContractError()
        frozen = freeze_json(self.draft, path="$.draft")
        if not isinstance(frozen, Mapping):
            raise PlanContractError()
        object.__setattr__(self, "draft", cast("Mapping[str, JsonValue]", frozen))


class PlanningProvider(Protocol):
    def propose_plan(self, request: PlanningRequest) -> PlanningResult: ...


@dataclass(frozen=True, slots=True)
class ToolActionRequest:
    run_id: str
    plan_id: str
    task_id: str
    attempt: int
    capability: str
    allowed_paths: tuple[str, ...]
    destructive: bool = False

    def __post_init__(self) -> None:
        for value in (self.run_id, self.plan_id, self.task_id, self.capability):
            _identifier(value)
        if (
            isinstance(self.attempt, bool)
            or not isinstance(self.attempt, int)
            or self.attempt < 1
            or not isinstance(self.destructive, bool)
        ):
            raise PlanContractError()
        object.__setattr__(self, "allowed_paths", _path_tuple(self.allowed_paths))

    @property
    def digest(self) -> str:
        """Bind one authorization decision to the exact proposed tool action."""

        return json_digest(
            {
                "run_id": self.run_id,
                "plan_id": self.plan_id,
                "task_id": self.task_id,
                "attempt": self.attempt,
                "capability": self.capability,
                "allowed_paths": list(self.allowed_paths),
                "destructive": self.destructive,
            }
        )


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    allowed: bool
    reason: str
    action_digest: str
    decision_id: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.allowed, bool)
            or _IDENTIFIER.fullmatch(self.reason) is None
            or _DIGEST.fullmatch(self.action_digest) is None
        ):
            raise PlanContractError()
        object.__setattr__(
            self,
            "decision_id",
            json_digest(
                {
                    "allowed": self.allowed,
                    "reason": self.reason,
                    "action_digest": self.action_digest,
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class AttemptEvidence:
    workspace_clean: bool
    verifier_exit_code: int
    required_checks_passed: bool
    failure_class: FailureClass | None
    failure_summary: str | None
    artifact_digests: tuple[str, ...]
    progress_digests: tuple[str, ...]
    head_commit: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int = 0
    cost_microusd: int | None = None
    schema_version: str = EXECUTION_ATTEMPT_EVIDENCE_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_version != EXECUTION_ATTEMPT_EVIDENCE_SCHEMA:
            raise PlanContractError()
        if not isinstance(self.workspace_clean, bool) or not isinstance(
            self.required_checks_passed, bool
        ):
            raise PlanContractError()
        if (
            isinstance(self.verifier_exit_code, bool)
            or not isinstance(self.verifier_exit_code, int)
            or not 0 <= self.verifier_exit_code <= 255
            or _COMMIT.fullmatch(self.head_commit) is None
        ):
            raise PlanContractError()
        succeeded = self.verifier_exit_code == 0 and self.required_checks_passed
        if succeeded:
            if self.failure_class is not None or self.failure_summary is not None:
                raise PlanContractError()
        elif (
            not isinstance(self.failure_class, FailureClass)
            or not isinstance(self.failure_summary, str)
            or not self.failure_summary.strip()
            or len(self.failure_summary.encode("utf-8")) > 4_096
        ):
            raise PlanContractError()
        if not self.artifact_digests or not self.progress_digests:
            raise PlanContractError()
        artifact_digests = tuple(sorted(set(self.artifact_digests)))
        progress_digests = tuple(sorted(set(self.progress_digests)))
        if any(_DIGEST.fullmatch(item) is None for item in (*artifact_digests, *progress_digests)):
            raise PlanContractError()
        if not set(progress_digests).issubset(artifact_digests):
            raise PlanContractError()
        for value in (self.input_tokens, self.output_tokens, self.cost_microusd):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise PlanContractError()
        if (
            isinstance(self.latency_ms, bool)
            or not isinstance(self.latency_ms, int)
            or self.latency_ms < 0
        ):
            raise PlanContractError()
        object.__setattr__(self, "artifact_digests", artifact_digests)
        object.__setattr__(self, "progress_digests", progress_digests)

    @property
    def error_signature(self) -> str | None:
        if self.failure_summary is None:
            return None
        normalized = re.sub(r"\b\d+\b", "#", self.failure_summary.casefold())
        normalized = re.sub(r"\s+", " ", normalized).strip()
        return json_digest({"failure_class": self.failure_class, "summary": normalized})


class TaskAttemptExecutor(Protocol):
    def execute(
        self,
        *,
        run_id: str,
        goal: GoalSpec,
        plan: Plan,
        task: TaskSpec,
        attempt: int,
        workspace_id: str,
        base_commit: str,
        prior_failure_class: FailureClass | None,
        prior_failure_summary: str | None,
        policy_decision: PolicyDecision,
        remaining_budget: GatewayBudget,
    ) -> AttemptEvidence: ...


class ExecutionPolicyKernel:
    """Deterministic authorization and repair routing; model output has no authority here."""

    def authorize(
        self,
        goal: GoalSpec,
        plan: Plan,
        task: TaskSpec,
        request: ToolActionRequest,
    ) -> PolicyDecision:
        if (
            request.run_id == ""
            or request.plan_id != plan.plan_id
            or request.task_id != task.task_id
            or request.capability != "repository-task"
            or request.destructive
            or not 1 <= request.attempt <= min(goal.max_attempts, task.max_attempts)
            or not set(request.allowed_paths).issubset(goal.allowed_paths)
            or request.allowed_paths != task.allowed_paths
        ):
            return PolicyDecision(False, "intent-scope-violation", request.digest)
        return PolicyDecision(True, "bounded-task-authorized", request.digest)

    def route(
        self,
        goal: GoalSpec,
        task: TaskSpec,
        evidence: AttemptEvidence,
        *,
        attempt: int,
        same_error_count: int,
        new_evidence: bool,
    ) -> AttemptRoute:
        if not evidence.workspace_clean:
            return AttemptRoute.BLOCKED
        if evidence.verifier_exit_code == 0 and evidence.required_checks_passed:
            return AttemptRoute.SUCCEEDED
        if evidence.failure_class in {
            FailureClass.MISSING_DEPENDENCY,
            FailureClass.INVALID_ASSUMPTION,
        }:
            return AttemptRoute.REPLAN_REQUIRED
        if same_error_count >= goal.same_error_limit and not new_evidence:
            return AttemptRoute.ESCALATED
        if attempt >= min(goal.max_attempts, task.max_attempts):
            return AttemptRoute.ESCALATED
        if evidence.failure_class in {
            FailureClass.TRANSIENT,
            FailureClass.LOGIC_BUG,
            FailureClass.CONTRACT_MISMATCH,
        }:
            return AttemptRoute.REPAIRABLE
        if evidence.failure_class is FailureClass.POLICY:
            return AttemptRoute.BLOCKED
        return AttemptRoute.TERMINAL_FAILURE


@dataclass(frozen=True, slots=True)
class PraxisPromotionCandidate:
    candidate_id: str
    run_id: str
    plan_id: str
    task_id: str
    kind: str
    evidence_event_ids: tuple[str, ...]
    evidence_digests: tuple[str, ...]
    schema_version: str = EXECUTION_PROMOTION_CANDIDATE_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_version != EXECUTION_PROMOTION_CANDIDATE_SCHEMA:
            raise PlanContractError()
        for value in (self.candidate_id, self.run_id, self.plan_id, self.task_id, self.kind):
            _identifier(value)
        if not self.evidence_event_ids or any(
            not isinstance(item, str) or not item for item in self.evidence_event_ids
        ):
            raise PlanContractError()
        if any(_DIGEST.fullmatch(item) is None for item in self.evidence_digests):
            raise PlanContractError()


PLAN_DRAFT_OUTPUT_SCHEMA: Mapping[str, JsonValue] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ("schema_version", "tasks"),
    "properties": {
        "schema_version": {"type": "string", "const": EXECUTION_PLAN_DRAFT_SCHEMA},
        "tasks": {
            "type": "array",
            "minItems": 1,
            "maxItems": _MAX_TASKS,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ("task_id", "objective", "depends_on", "allowed_paths", "checks"),
                "properties": {
                    "task_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "objective": {"type": "string", "minLength": 1, "maxLength": 16_384},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                    "allowed_paths": {"type": "array", "items": {"type": "string"}},
                    "checks": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": _MAX_CHECKS,
                        "items": {"type": "string", "minLength": 1, "maxLength": 128},
                    },
                },
            },
        },
    },
}


def compile_plan(
    goal: GoalSpec,
    draft: Mapping[str, object],
    *,
    previous: Plan | None = None,
) -> Plan:
    """Compile one untrusted provider draft into an immutable, host-owned plan revision."""

    if not isinstance(goal, GoalSpec) or not isinstance(draft, Mapping):
        raise PlanContractError()
    if (
        set(draft) != {"schema_version", "tasks"}
        or draft.get("schema_version") != EXECUTION_PLAN_DRAFT_SCHEMA
    ):
        raise PlanContractError()
    raw_tasks = _sequence(draft.get("tasks"))
    if not 1 <= len(raw_tasks) <= _MAX_TASKS:
        raise PlanContractError()
    tasks = tuple(_compile_task(goal, raw) for raw in raw_tasks)
    covered_checks = {check.check_id for task in tasks for check in task.checks}
    required_checks = {check.check_id for check in goal.verification_checks}
    if covered_checks != required_checks:
        raise PlanContractError("plan-check-coverage-incomplete")
    draft_digest = json_digest(cast("Mapping[str, JsonInput]", draft))
    if previous is None:
        revision = 1
        supersedes = None
    else:
        if (
            previous.goal_id != goal.goal_id
            or previous.project_id != goal.project_id
            or previous.intent_id != goal.intent_id
            or previous.base_commit != goal.base_commit
        ):
            raise PlanContractError("invalid-plan-lineage")
        revision = previous.plan_revision + 1
        supersedes = previous.plan_id
    identity = json_digest(
        {
            "goal_digest": goal.digest,
            "plan_version": revision,
            "supersedes_plan_id": supersedes,
            "draft_digest": draft_digest,
            "task_digests": [item.task_digest for item in tasks],
        }
    )
    return Plan(
        plan_id=f"plan-{identity.removeprefix('sha256:')[:32]}",
        goal_id=goal.goal_id,
        project_id=goal.project_id,
        intent_id=goal.intent_id,
        plan_revision=revision,
        supersedes_plan_id=supersedes,
        base_commit=goal.base_commit,
        draft_digest=draft_digest,
        tasks=tasks,
    )


def goal_payload(goal: GoalSpec) -> dict[str, JsonInput]:
    return {
        "schema_version": goal.schema_version,
        "goal_id": goal.goal_id,
        "project_id": goal.project_id,
        "intent_id": goal.intent_id,
        "objective": goal.objective,
        "base_commit": goal.base_commit,
        "constraints": list(goal.constraints),
        "allowed_paths": list(goal.allowed_paths),
        "verification_checks": [
            {
                "check_id": item.check_id,
                "argv": list(item.argv),
                "expected_exit_code": item.expected_exit_code,
            }
            for item in goal.verification_checks
        ],
        "max_attempts": goal.max_attempts,
        "same_error_limit": goal.same_error_limit,
    }


def goal_from_payload(value: object) -> GoalSpec:
    """Reconstruct and revalidate one host-owned goal from durable event content."""

    raw = _mapping(value)
    if set(raw) != {
        "schema_version",
        "goal_id",
        "project_id",
        "intent_id",
        "objective",
        "base_commit",
        "constraints",
        "allowed_paths",
        "verification_checks",
        "max_attempts",
        "same_error_limit",
    }:
        raise PlanContractError()
    checks = tuple(
        VerificationCheck(
            check_id=_mapping_text(check, "check_id"),
            argv=tuple(_text(token) for token in _sequence(check.get("argv"))),
            expected_exit_code=_mapping_integer(check, "expected_exit_code"),
        )
        for item in _sequence(raw.get("verification_checks"))
        for check in (_mapping(item),)
    )
    return GoalSpec(
        schema_version=_mapping_text(raw, "schema_version"),
        goal_id=_mapping_text(raw, "goal_id"),
        project_id=_mapping_text(raw, "project_id"),
        intent_id=_mapping_text(raw, "intent_id"),
        objective=_mapping_text(raw, "objective"),
        base_commit=_mapping_text(raw, "base_commit"),
        constraints=tuple(_text(item) for item in _sequence(raw.get("constraints"))),
        allowed_paths=tuple(_text(item) for item in _sequence(raw.get("allowed_paths"))),
        verification_checks=checks,
        max_attempts=_mapping_integer(raw, "max_attempts"),
        same_error_limit=_mapping_integer(raw, "same_error_limit"),
    )


def planning_payload(goal: GoalSpec) -> dict[str, JsonInput]:
    """Return only semantic planning data suitable for an untrusted provider.

    Verification commands and expected process outcomes remain host-owned.  A planner may
    select admitted check identifiers, but it never receives or proposes their argv.
    """

    if not isinstance(goal, GoalSpec):
        raise PlanContractError()
    return {
        "schema_version": goal.schema_version,
        "goal_id": goal.goal_id,
        "project_id": goal.project_id,
        "intent_id": goal.intent_id,
        "objective": goal.objective,
        "constraints": list(goal.constraints),
        "allowed_paths": list(goal.allowed_paths),
        "verification_check_ids": [item.check_id for item in goal.verification_checks],
        "max_attempts": goal.max_attempts,
        "same_error_limit": goal.same_error_limit,
    }


def task_payload(task: TaskSpec) -> dict[str, JsonInput]:
    return {
        "task_id": task.task_id,
        "objective": task.objective,
        "depends_on": list(task.depends_on),
        "allowed_paths": list(task.allowed_paths),
        "checks": [
            {
                "check_id": item.check_id,
                "argv": list(item.argv),
                "expected_exit_code": item.expected_exit_code,
            }
            for item in task.checks
        ],
        "max_attempts": task.max_attempts,
    }


def plan_payload(plan: Plan) -> dict[str, JsonInput]:
    return {
        "schema_version": plan.schema_version,
        "plan_id": plan.plan_id,
        "goal_id": plan.goal_id,
        "project_id": plan.project_id,
        "intent_id": plan.intent_id,
        "plan_version": plan.plan_revision,
        "supersedes_plan_id": plan.supersedes_plan_id,
        "base_commit": plan.base_commit,
        "draft_digest": plan.draft_digest,
        "tasks": [task_payload(item) for item in plan.tasks],
        "topological_order": list(plan.topological_order),
    }


def plan_from_payload(value: object) -> Plan:
    """Reconstruct and revalidate one immutable plan from durable event content."""

    raw = _mapping(value)
    if set(raw) != {
        "schema_version",
        "plan_id",
        "goal_id",
        "project_id",
        "intent_id",
        "plan_version",
        "supersedes_plan_id",
        "base_commit",
        "draft_digest",
        "tasks",
        "topological_order",
    }:
        raise PlanContractError()
    tasks = tuple(_task_from_payload(item) for item in _sequence(raw.get("tasks")))
    supersedes = raw.get("supersedes_plan_id")
    if supersedes is not None and not isinstance(supersedes, str):
        raise PlanContractError()
    plan = Plan(
        schema_version=_mapping_text(raw, "schema_version"),
        plan_id=_mapping_text(raw, "plan_id"),
        goal_id=_mapping_text(raw, "goal_id"),
        project_id=_mapping_text(raw, "project_id"),
        intent_id=_mapping_text(raw, "intent_id"),
        plan_revision=_mapping_integer(raw, "plan_version"),
        supersedes_plan_id=supersedes,
        base_commit=_mapping_text(raw, "base_commit"),
        draft_digest=_mapping_text(raw, "draft_digest"),
        tasks=tasks,
    )
    order = tuple(_text(item) for item in _sequence(raw.get("topological_order")))
    if order != plan.topological_order:
        raise PlanContractError()
    return plan


def _task_from_payload(value: object) -> TaskSpec:
    raw = _mapping(value)
    if set(raw) != {
        "task_id",
        "objective",
        "depends_on",
        "allowed_paths",
        "checks",
        "max_attempts",
    }:
        raise PlanContractError()
    checks = tuple(
        VerificationCheck(
            check_id=_mapping_text(check, "check_id"),
            argv=tuple(_text(token) for token in _sequence(check.get("argv"))),
            expected_exit_code=_mapping_integer(check, "expected_exit_code"),
        )
        for item in _sequence(raw.get("checks"))
        for check in (_mapping(item),)
    )
    return TaskSpec(
        task_id=_mapping_text(raw, "task_id"),
        objective=_mapping_text(raw, "objective"),
        depends_on=tuple(_text(item) for item in _sequence(raw.get("depends_on"))),
        allowed_paths=tuple(_text(item) for item in _sequence(raw.get("allowed_paths"))),
        checks=checks,
        max_attempts=_mapping_integer(raw, "max_attempts"),
    )


def _compile_task(goal: GoalSpec, value: object) -> TaskSpec:
    raw = _mapping(value)
    if set(raw) != {"task_id", "objective", "depends_on", "allowed_paths", "checks"}:
        raise PlanContractError()
    raw_dependencies = _sequence(raw.get("depends_on"))
    raw_paths = _sequence(raw.get("allowed_paths"))
    raw_checks = _sequence(raw.get("checks"))
    if len(raw_paths) > _MAX_PATHS or not 1 <= len(raw_checks) <= _MAX_CHECKS:
        raise PlanContractError()
    paths = tuple(_text(item) for item in raw_paths)
    if not set(paths).issubset(goal.allowed_paths):
        raise PlanContractError("plan-path-outside-goal")
    check_ids = tuple(_text(item) for item in raw_checks)
    if len(check_ids) != len(set(check_ids)):
        raise PlanContractError()
    catalog = {item.check_id: item for item in goal.verification_checks}
    try:
        checks = tuple(catalog[check_id] for check_id in check_ids)
    except KeyError as error:
        raise PlanContractError("plan-check-outside-goal") from error
    return TaskSpec(
        task_id=_text(raw.get("task_id")),
        objective=_text(raw.get("objective")),
        depends_on=tuple(_text(item) for item in raw_dependencies),
        allowed_paths=paths,
        checks=checks,
        max_attempts=goal.max_attempts,
    )


def _topological_order(tasks: tuple[TaskSpec, ...]) -> tuple[str, ...]:
    dependencies = {item.task_id: set(item.depends_on) for item in tasks}
    resolved: list[str] = []
    pending = set(dependencies)
    while pending:
        ready = sorted(item for item in pending if dependencies[item].issubset(resolved))
        if not ready:
            raise PlanContractError("cyclic-plan")
        resolved.extend(ready)
        pending.difference_update(ready)
    return tuple(resolved)


def _validate_writer_order(tasks: tuple[TaskSpec, ...]) -> None:
    order = _topological_order(tasks)
    by_id = {item.task_id: item for item in tasks}
    ancestors: dict[str, set[str]] = {}
    for task_id in order:
        dependencies = by_id[task_id].depends_on
        ancestors[task_id] = set(dependencies).union(
            *(ancestors[dependency] for dependency in dependencies)
        )
    writers = [task_id for task_id in order if by_id[task_id].allowed_paths]
    if any(previous not in ancestors[current] for previous, current in pairwise(writers)):
        raise PlanContractError("parallel-writer-plan")


def _path_tuple(value: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(value, tuple) or len(value) > _MAX_PATHS:
        raise PlanContractError()
    paths = tuple(sorted(_repository_path(item) for item in value))
    if len(paths) != len(set(paths)):
        raise PlanContractError()
    return paths


def _repository_path(value: object) -> str:
    text = _text(value)
    if (
        text.startswith(("/", "../"))
        or "/../" in f"/{text}/"
        or text in {".", ".."}
        or "\\" in text
        or "\x00" in text
    ):
        raise PlanContractError()
    return text.rstrip("/")


def _bounded_text_tuple(value: tuple[str, ...], *, maximum: int) -> tuple[str, ...]:
    if not isinstance(value, tuple) or len(value) > maximum:
        raise PlanContractError()
    result = tuple(sorted(_text(item) for item in value))
    if len(result) != len(set(result)):
        raise PlanContractError()
    return result


def _identifier(value: object) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise PlanContractError()
    return value


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlanContractError()
    return value


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise PlanContractError()
    return cast("Mapping[str, object]", value)


def _mapping_text(value: Mapping[str, object], field: str) -> str:
    return _text(value.get(field))


def _mapping_integer(value: Mapping[str, object], field: str) -> int:
    item = value.get(field)
    if isinstance(item, bool) or not isinstance(item, int) or item < 0:
        raise PlanContractError()
    return item


def _sequence(value: object) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        raise PlanContractError()
    return cast("Sequence[object]", value)


__all__ = [
    "EXECUTION_ATTEMPT_EVIDENCE_SCHEMA",
    "EXECUTION_EVENT_SOURCE",
    "EXECUTION_EVENT_TYPES",
    "EXECUTION_GOAL_ADMITTED",
    "EXECUTION_GOAL_SCHEMA",
    "EXECUTION_PLAN_ADMITTED",
    "EXECUTION_PLAN_DRAFT_RECEIVED",
    "EXECUTION_PLAN_DRAFT_SCHEMA",
    "EXECUTION_PLAN_SCHEMA",
    "EXECUTION_POLICY_DECIDED",
    "EXECUTION_PROMOTION_CANDIDATE_SCHEMA",
    "EXECUTION_REPLAN_STARTED",
    "EXECUTION_RUN_TERMINATED",
    "EXECUTION_TASK_BLOCKED",
    "EXECUTION_TASK_READY",
    "EXECUTION_TASK_STARTED",
    "EXECUTION_TASK_VERIFIED",
    "EXECUTION_TASK_VERIFYING",
    "PLAN_DRAFT_OUTPUT_SCHEMA",
    "AttemptEvidence",
    "AttemptRoute",
    "ExecutionAuthority",
    "ExecutionPolicyKernel",
    "FailureClass",
    "GoalSpec",
    "Plan",
    "PlanContractError",
    "PlanningProvider",
    "PlanningRequest",
    "PlanningResult",
    "PolicyDecision",
    "PraxisPromotionCandidate",
    "RunLifecycleStatus",
    "TaskAttemptExecutor",
    "TaskLifecycleStatus",
    "TaskSpec",
    "ToolActionRequest",
    "VerificationCheck",
    "compile_plan",
    "goal_from_payload",
    "goal_payload",
    "plan_from_payload",
    "plan_payload",
    "planning_payload",
    "task_payload",
]
