"""Versioned plan compilation and bounded task-attempt policy for the alpha runtime.

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

ALPHA_V2_GOAL_SCHEMA = "blackcell.alpha-goal/v2"
ALPHA_V2_PLAN_DRAFT_SCHEMA = "blackcell.alpha-plan-draft/v1"
ALPHA_V2_PLAN_SCHEMA = "blackcell.alpha-plan/v2"
ALPHA_V2_ATTEMPT_EVIDENCE_SCHEMA = "blackcell.alpha-attempt-evidence/v1"
ALPHA_V2_PROMOTION_CANDIDATE_SCHEMA = "praxis-promotion-candidate/v1"
ALPHA_V2_EVENT_SOURCE = "blackcell.alpha.v2"
ALPHA_V2_GOAL_ADMITTED = "alpha.v2.goal.admitted"
ALPHA_V2_PLAN_DRAFT_RECEIVED = "alpha.v2.plan-draft.received"
ALPHA_V2_PLAN_ADMITTED = "alpha.v2.plan.admitted"
ALPHA_V2_REPLAN_STARTED = "alpha.v2.replan.started"
ALPHA_V2_TASK_READY = "alpha.v2.task.ready"
ALPHA_V2_POLICY_DECIDED = "alpha.v2.policy.decided"
ALPHA_V2_TASK_STARTED = "alpha.v2.task.started"
ALPHA_V2_TASK_VERIFYING = "alpha.v2.task.verifying"
ALPHA_V2_TASK_VERIFIED = "alpha.v2.task.verified"
ALPHA_V2_TASK_BLOCKED = "alpha.v2.task.blocked"
ALPHA_V2_RUN_TERMINATED = "alpha.v2.run.terminated"
ALPHA_V2_EVENT_TYPES = frozenset(
    {
        ALPHA_V2_GOAL_ADMITTED,
        ALPHA_V2_PLAN_DRAFT_RECEIVED,
        ALPHA_V2_PLAN_ADMITTED,
        ALPHA_V2_REPLAN_STARTED,
        ALPHA_V2_TASK_READY,
        ALPHA_V2_POLICY_DECIDED,
        ALPHA_V2_TASK_STARTED,
        ALPHA_V2_TASK_VERIFYING,
        ALPHA_V2_TASK_VERIFIED,
        ALPHA_V2_TASK_BLOCKED,
        ALPHA_V2_RUN_TERMINATED,
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


class AlphaV2ContractError(ValueError):
    """A content-free rejection at the deterministic alpha-v2 boundary."""

    def __init__(self, code: str = "invalid-alpha-v2-contract") -> None:
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
class AlphaGoalSpec:
    goal_id: str
    project_id: str
    intent_id: str
    objective: str
    base_commit: str
    constraints: tuple[str, ...]
    allowed_paths: tuple[str, ...]
    verification_checks: tuple[AlphaVerificationCheck, ...]
    max_attempts: int = 3
    same_error_limit: int = 2
    schema_version: str = ALPHA_V2_GOAL_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_version != ALPHA_V2_GOAL_SCHEMA:
            raise AlphaV2ContractError()
        for value in (self.goal_id, self.project_id, self.intent_id):
            _identifier(value)
        if (
            not isinstance(self.objective, str)
            or not self.objective.strip()
            or len(self.objective.encode("utf-8")) > 16 * 1024
        ):
            raise AlphaV2ContractError()
        if not isinstance(self.base_commit, str) or _COMMIT.fullmatch(self.base_commit) is None:
            raise AlphaV2ContractError()
        constraints = _bounded_text_tuple(self.constraints, maximum=64)
        paths = _path_tuple(self.allowed_paths)
        if (
            not isinstance(self.verification_checks, tuple)
            or not 1 <= len(self.verification_checks) <= _MAX_CHECKS
            or not all(
                isinstance(item, AlphaVerificationCheck) for item in self.verification_checks
            )
        ):
            raise AlphaV2ContractError()
        checks = tuple(sorted(self.verification_checks))
        if len({item.check_id for item in checks}) != len(checks):
            raise AlphaV2ContractError()
        if not 1 <= self.max_attempts <= 3 or not 1 <= self.same_error_limit <= 2:
            raise AlphaV2ContractError()
        object.__setattr__(self, "constraints", constraints)
        object.__setattr__(self, "allowed_paths", paths)
        object.__setattr__(self, "verification_checks", checks)

    @property
    def digest(self) -> str:
        return json_digest(alpha_goal_payload(self))


@dataclass(frozen=True, slots=True, order=True)
class AlphaVerificationCheck:
    check_id: str
    argv: tuple[str, ...]
    expected_exit_code: int = 0

    def __post_init__(self) -> None:
        _identifier(self.check_id)
        if not 1 <= len(self.argv) <= _MAX_ARGV:
            raise AlphaV2ContractError()
        for token in self.argv:
            if (
                not isinstance(token, str)
                or not token
                or "\x00" in token
                or len(token.encode("utf-8")) > _MAX_TOKEN_BYTES
            ):
                raise AlphaV2ContractError()
        if _SAFE_EXECUTABLE.fullmatch(self.argv[0]) is None:
            raise AlphaV2ContractError()
        if not 0 <= self.expected_exit_code <= 255:
            raise AlphaV2ContractError()


@dataclass(frozen=True, slots=True)
class AlphaTaskSpec:
    task_id: str
    objective: str
    depends_on: tuple[str, ...]
    allowed_paths: tuple[str, ...]
    checks: tuple[AlphaVerificationCheck, ...]
    max_attempts: int
    task_digest: str = field(init=False)

    def __post_init__(self) -> None:
        _identifier(self.task_id)
        if (
            not isinstance(self.objective, str)
            or not self.objective.strip()
            or len(self.objective.encode("utf-8")) > 16 * 1024
        ):
            raise AlphaV2ContractError()
        dependencies = tuple(sorted(set(self.depends_on)))
        if self.task_id in dependencies or any(
            not isinstance(item, str) or _IDENTIFIER.fullmatch(item) is None
            for item in dependencies
        ):
            raise AlphaV2ContractError()
        paths = _path_tuple(self.allowed_paths)
        checks = tuple(sorted(self.checks))
        if not checks or len(checks) > _MAX_CHECKS:
            raise AlphaV2ContractError()
        if len({item.check_id for item in checks}) != len(checks):
            raise AlphaV2ContractError()
        if not 1 <= self.max_attempts <= 3:
            raise AlphaV2ContractError()
        object.__setattr__(self, "depends_on", dependencies)
        object.__setattr__(self, "allowed_paths", paths)
        object.__setattr__(self, "checks", checks)
        object.__setattr__(self, "task_digest", json_digest(alpha_task_payload(self)))


@dataclass(frozen=True, slots=True)
class AlphaPlanVersion:
    plan_id: str
    goal_id: str
    project_id: str
    intent_id: str
    plan_version: int
    supersedes_plan_id: str | None
    base_commit: str
    draft_digest: str
    tasks: tuple[AlphaTaskSpec, ...]
    schema_version: str = ALPHA_V2_PLAN_SCHEMA
    plan_digest: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != ALPHA_V2_PLAN_SCHEMA:
            raise AlphaV2ContractError()
        for value in (self.plan_id, self.goal_id, self.project_id, self.intent_id):
            _identifier(value)
        if self.plan_version < 1:
            raise AlphaV2ContractError()
        if (self.plan_version == 1) != (self.supersedes_plan_id is None):
            raise AlphaV2ContractError()
        if self.supersedes_plan_id is not None:
            _identifier(self.supersedes_plan_id)
            if self.supersedes_plan_id == self.plan_id:
                raise AlphaV2ContractError()
        if (
            _COMMIT.fullmatch(self.base_commit) is None
            or _DIGEST.fullmatch(self.draft_digest) is None
        ):
            raise AlphaV2ContractError()
        tasks = tuple(sorted(self.tasks, key=lambda item: item.task_id))
        if not 1 <= len(tasks) <= _MAX_TASKS:
            raise AlphaV2ContractError()
        identifiers = tuple(item.task_id for item in tasks)
        if len(set(identifiers)) != len(identifiers):
            raise AlphaV2ContractError()
        known = set(identifiers)
        if any(not set(item.depends_on).issubset(known) for item in tasks):
            raise AlphaV2ContractError()
        _topological_order(tasks)
        _validate_writer_order(tasks)
        object.__setattr__(self, "tasks", tasks)
        object.__setattr__(self, "plan_digest", json_digest(alpha_plan_payload(self)))

    @property
    def topological_order(self) -> tuple[str, ...]:
        return _topological_order(self.tasks)


@dataclass(frozen=True, slots=True)
class AlphaPlanningRequest:
    goal: AlphaGoalSpec
    classification: DataClassification
    locality: LocalityPolicy
    budget: GatewayBudget
    estimated_input_tokens: int
    correlation_id: str
    run_id: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.goal, AlphaGoalSpec)
            or not isinstance(self.classification, DataClassification)
            or not isinstance(self.locality, LocalityPolicy)
            or not isinstance(self.budget, GatewayBudget)
            or isinstance(self.estimated_input_tokens, bool)
            or not isinstance(self.estimated_input_tokens, int)
            or self.estimated_input_tokens < 0
        ):
            raise AlphaV2ContractError()
        _identifier(self.correlation_id)
        _identifier(self.run_id)


@dataclass(frozen=True, slots=True)
class AlphaPlanningResult:
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
            raise AlphaV2ContractError()
        for value in (self.profile_id, self.adapter_id, self.model_id):
            _identifier(value)
        for value in (self.input_tokens, self.output_tokens, self.cost_microusd):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise AlphaV2ContractError()
        if (
            isinstance(self.latency_ms, bool)
            or not isinstance(self.latency_ms, int)
            or self.latency_ms < 0
        ):
            raise AlphaV2ContractError()
        frozen = freeze_json(self.draft, path="$.draft")
        if not isinstance(frozen, Mapping):
            raise AlphaV2ContractError()
        object.__setattr__(self, "draft", cast("Mapping[str, JsonValue]", frozen))


class AlphaPlanningProvider(Protocol):
    def propose_plan(self, request: AlphaPlanningRequest) -> AlphaPlanningResult: ...


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
            raise AlphaV2ContractError()
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
            raise AlphaV2ContractError()
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
    schema_version: str = ALPHA_V2_ATTEMPT_EVIDENCE_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_version != ALPHA_V2_ATTEMPT_EVIDENCE_SCHEMA:
            raise AlphaV2ContractError()
        if not isinstance(self.workspace_clean, bool) or not isinstance(
            self.required_checks_passed, bool
        ):
            raise AlphaV2ContractError()
        if (
            isinstance(self.verifier_exit_code, bool)
            or not isinstance(self.verifier_exit_code, int)
            or not 0 <= self.verifier_exit_code <= 255
            or _COMMIT.fullmatch(self.head_commit) is None
        ):
            raise AlphaV2ContractError()
        succeeded = self.verifier_exit_code == 0 and self.required_checks_passed
        if succeeded:
            if self.failure_class is not None or self.failure_summary is not None:
                raise AlphaV2ContractError()
        elif (
            not isinstance(self.failure_class, FailureClass)
            or not isinstance(self.failure_summary, str)
            or not self.failure_summary.strip()
            or len(self.failure_summary.encode("utf-8")) > 4_096
        ):
            raise AlphaV2ContractError()
        if not self.artifact_digests or not self.progress_digests:
            raise AlphaV2ContractError()
        artifact_digests = tuple(sorted(set(self.artifact_digests)))
        progress_digests = tuple(sorted(set(self.progress_digests)))
        if any(_DIGEST.fullmatch(item) is None for item in (*artifact_digests, *progress_digests)):
            raise AlphaV2ContractError()
        if not set(progress_digests).issubset(artifact_digests):
            raise AlphaV2ContractError()
        for value in (self.input_tokens, self.output_tokens, self.cost_microusd):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise AlphaV2ContractError()
        if (
            isinstance(self.latency_ms, bool)
            or not isinstance(self.latency_ms, int)
            or self.latency_ms < 0
        ):
            raise AlphaV2ContractError()
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
    ) -> AttemptEvidence: ...


class AlphaV2PolicyKernel:
    """Deterministic authorization and repair routing; model output has no authority here."""

    def authorize(
        self,
        goal: AlphaGoalSpec,
        plan: AlphaPlanVersion,
        task: AlphaTaskSpec,
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
        goal: AlphaGoalSpec,
        task: AlphaTaskSpec,
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
    schema_version: str = ALPHA_V2_PROMOTION_CANDIDATE_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_version != ALPHA_V2_PROMOTION_CANDIDATE_SCHEMA:
            raise AlphaV2ContractError()
        for value in (self.candidate_id, self.run_id, self.plan_id, self.task_id, self.kind):
            _identifier(value)
        if not self.evidence_event_ids or any(
            not isinstance(item, str) or not item for item in self.evidence_event_ids
        ):
            raise AlphaV2ContractError()
        if any(_DIGEST.fullmatch(item) is None for item in self.evidence_digests):
            raise AlphaV2ContractError()


ALPHA_PLAN_DRAFT_OUTPUT_SCHEMA: Mapping[str, JsonValue] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ("schema_version", "tasks"),
    "properties": {
        "schema_version": {"type": "string", "const": ALPHA_V2_PLAN_DRAFT_SCHEMA},
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


def compile_alpha_plan(
    goal: AlphaGoalSpec,
    draft: Mapping[str, object],
    *,
    previous: AlphaPlanVersion | None = None,
) -> AlphaPlanVersion:
    """Compile one untrusted provider draft into an immutable, host-owned plan version."""

    if not isinstance(goal, AlphaGoalSpec) or not isinstance(draft, Mapping):
        raise AlphaV2ContractError()
    if (
        set(draft) != {"schema_version", "tasks"}
        or draft.get("schema_version") != ALPHA_V2_PLAN_DRAFT_SCHEMA
    ):
        raise AlphaV2ContractError()
    raw_tasks = _sequence(draft.get("tasks"))
    if not 1 <= len(raw_tasks) <= _MAX_TASKS:
        raise AlphaV2ContractError()
    tasks = tuple(_compile_task(goal, raw) for raw in raw_tasks)
    draft_digest = json_digest(cast("Mapping[str, JsonInput]", draft))
    if previous is None:
        version = 1
        supersedes = None
    else:
        if (
            previous.goal_id != goal.goal_id
            or previous.project_id != goal.project_id
            or previous.intent_id != goal.intent_id
            or previous.base_commit != goal.base_commit
        ):
            raise AlphaV2ContractError("invalid-plan-lineage")
        version = previous.plan_version + 1
        supersedes = previous.plan_id
    identity = json_digest(
        {
            "goal_digest": goal.digest,
            "plan_version": version,
            "supersedes_plan_id": supersedes,
            "draft_digest": draft_digest,
            "task_digests": [item.task_digest for item in tasks],
        }
    )
    return AlphaPlanVersion(
        plan_id=f"plan-{identity.removeprefix('sha256:')[:32]}",
        goal_id=goal.goal_id,
        project_id=goal.project_id,
        intent_id=goal.intent_id,
        plan_version=version,
        supersedes_plan_id=supersedes,
        base_commit=goal.base_commit,
        draft_digest=draft_digest,
        tasks=tasks,
    )


def alpha_goal_payload(goal: AlphaGoalSpec) -> dict[str, JsonInput]:
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


def alpha_goal_from_payload(value: object) -> AlphaGoalSpec:
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
        raise AlphaV2ContractError()
    checks = tuple(
        AlphaVerificationCheck(
            check_id=_mapping_text(check, "check_id"),
            argv=tuple(_text(token) for token in _sequence(check.get("argv"))),
            expected_exit_code=_mapping_integer(check, "expected_exit_code"),
        )
        for item in _sequence(raw.get("verification_checks"))
        for check in (_mapping(item),)
    )
    return AlphaGoalSpec(
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


def alpha_planning_payload(goal: AlphaGoalSpec) -> dict[str, JsonInput]:
    """Return only semantic planning data suitable for an untrusted provider.

    Verification commands and expected process outcomes remain host-owned.  A planner may
    select admitted check identifiers, but it never receives or proposes their argv.
    """

    if not isinstance(goal, AlphaGoalSpec):
        raise AlphaV2ContractError()
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


def alpha_task_payload(task: AlphaTaskSpec) -> dict[str, JsonInput]:
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


def alpha_plan_payload(plan: AlphaPlanVersion) -> dict[str, JsonInput]:
    return {
        "schema_version": plan.schema_version,
        "plan_id": plan.plan_id,
        "goal_id": plan.goal_id,
        "project_id": plan.project_id,
        "intent_id": plan.intent_id,
        "plan_version": plan.plan_version,
        "supersedes_plan_id": plan.supersedes_plan_id,
        "base_commit": plan.base_commit,
        "draft_digest": plan.draft_digest,
        "tasks": [alpha_task_payload(item) for item in plan.tasks],
        "topological_order": list(plan.topological_order),
    }


def alpha_plan_from_payload(value: object) -> AlphaPlanVersion:
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
        raise AlphaV2ContractError()
    tasks = tuple(_task_from_payload(item) for item in _sequence(raw.get("tasks")))
    supersedes = raw.get("supersedes_plan_id")
    if supersedes is not None and not isinstance(supersedes, str):
        raise AlphaV2ContractError()
    plan = AlphaPlanVersion(
        schema_version=_mapping_text(raw, "schema_version"),
        plan_id=_mapping_text(raw, "plan_id"),
        goal_id=_mapping_text(raw, "goal_id"),
        project_id=_mapping_text(raw, "project_id"),
        intent_id=_mapping_text(raw, "intent_id"),
        plan_version=_mapping_integer(raw, "plan_version"),
        supersedes_plan_id=supersedes,
        base_commit=_mapping_text(raw, "base_commit"),
        draft_digest=_mapping_text(raw, "draft_digest"),
        tasks=tasks,
    )
    order = tuple(_text(item) for item in _sequence(raw.get("topological_order")))
    if order != plan.topological_order:
        raise AlphaV2ContractError()
    return plan


def _task_from_payload(value: object) -> AlphaTaskSpec:
    raw = _mapping(value)
    if set(raw) != {
        "task_id",
        "objective",
        "depends_on",
        "allowed_paths",
        "checks",
        "max_attempts",
    }:
        raise AlphaV2ContractError()
    checks = tuple(
        AlphaVerificationCheck(
            check_id=_mapping_text(check, "check_id"),
            argv=tuple(_text(token) for token in _sequence(check.get("argv"))),
            expected_exit_code=_mapping_integer(check, "expected_exit_code"),
        )
        for item in _sequence(raw.get("checks"))
        for check in (_mapping(item),)
    )
    return AlphaTaskSpec(
        task_id=_mapping_text(raw, "task_id"),
        objective=_mapping_text(raw, "objective"),
        depends_on=tuple(_text(item) for item in _sequence(raw.get("depends_on"))),
        allowed_paths=tuple(_text(item) for item in _sequence(raw.get("allowed_paths"))),
        checks=checks,
        max_attempts=_mapping_integer(raw, "max_attempts"),
    )


def _compile_task(goal: AlphaGoalSpec, value: object) -> AlphaTaskSpec:
    raw = _mapping(value)
    if set(raw) != {"task_id", "objective", "depends_on", "allowed_paths", "checks"}:
        raise AlphaV2ContractError()
    raw_dependencies = _sequence(raw.get("depends_on"))
    raw_paths = _sequence(raw.get("allowed_paths"))
    raw_checks = _sequence(raw.get("checks"))
    if len(raw_paths) > _MAX_PATHS or not 1 <= len(raw_checks) <= _MAX_CHECKS:
        raise AlphaV2ContractError()
    paths = tuple(_text(item) for item in raw_paths)
    if not set(paths).issubset(goal.allowed_paths):
        raise AlphaV2ContractError("plan-path-outside-goal")
    check_ids = tuple(_text(item) for item in raw_checks)
    if len(check_ids) != len(set(check_ids)):
        raise AlphaV2ContractError()
    catalog = {item.check_id: item for item in goal.verification_checks}
    try:
        checks = tuple(catalog[check_id] for check_id in check_ids)
    except KeyError as error:
        raise AlphaV2ContractError("plan-check-outside-goal") from error
    return AlphaTaskSpec(
        task_id=_text(raw.get("task_id")),
        objective=_text(raw.get("objective")),
        depends_on=tuple(_text(item) for item in raw_dependencies),
        allowed_paths=paths,
        checks=checks,
        max_attempts=goal.max_attempts,
    )


def _topological_order(tasks: tuple[AlphaTaskSpec, ...]) -> tuple[str, ...]:
    dependencies = {item.task_id: set(item.depends_on) for item in tasks}
    resolved: list[str] = []
    pending = set(dependencies)
    while pending:
        ready = sorted(item for item in pending if dependencies[item].issubset(resolved))
        if not ready:
            raise AlphaV2ContractError("cyclic-alpha-plan")
        resolved.extend(ready)
        pending.difference_update(ready)
    return tuple(resolved)


def _validate_writer_order(tasks: tuple[AlphaTaskSpec, ...]) -> None:
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
        raise AlphaV2ContractError("parallel-writer-plan")


def _path_tuple(value: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(value, tuple) or len(value) > _MAX_PATHS:
        raise AlphaV2ContractError()
    paths = tuple(sorted(_repository_path(item) for item in value))
    if len(paths) != len(set(paths)):
        raise AlphaV2ContractError()
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
        raise AlphaV2ContractError()
    return text.rstrip("/")


def _bounded_text_tuple(value: tuple[str, ...], *, maximum: int) -> tuple[str, ...]:
    if not isinstance(value, tuple) or len(value) > maximum:
        raise AlphaV2ContractError()
    result = tuple(sorted(_text(item) for item in value))
    if len(result) != len(set(result)):
        raise AlphaV2ContractError()
    return result


def _identifier(value: object) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise AlphaV2ContractError()
    return value


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AlphaV2ContractError()
    return value


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise AlphaV2ContractError()
    return cast("Mapping[str, object]", value)


def _mapping_text(value: Mapping[str, object], field: str) -> str:
    return _text(value.get(field))


def _mapping_integer(value: Mapping[str, object], field: str) -> int:
    item = value.get(field)
    if isinstance(item, bool) or not isinstance(item, int) or item < 0:
        raise AlphaV2ContractError()
    return item


def _sequence(value: object) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        raise AlphaV2ContractError()
    return cast("Sequence[object]", value)


__all__ = [
    "ALPHA_PLAN_DRAFT_OUTPUT_SCHEMA",
    "ALPHA_V2_ATTEMPT_EVIDENCE_SCHEMA",
    "ALPHA_V2_EVENT_SOURCE",
    "ALPHA_V2_EVENT_TYPES",
    "ALPHA_V2_GOAL_ADMITTED",
    "ALPHA_V2_GOAL_SCHEMA",
    "ALPHA_V2_PLAN_ADMITTED",
    "ALPHA_V2_PLAN_DRAFT_RECEIVED",
    "ALPHA_V2_PLAN_DRAFT_SCHEMA",
    "ALPHA_V2_PLAN_SCHEMA",
    "ALPHA_V2_POLICY_DECIDED",
    "ALPHA_V2_PROMOTION_CANDIDATE_SCHEMA",
    "ALPHA_V2_REPLAN_STARTED",
    "ALPHA_V2_RUN_TERMINATED",
    "ALPHA_V2_TASK_BLOCKED",
    "ALPHA_V2_TASK_READY",
    "ALPHA_V2_TASK_STARTED",
    "ALPHA_V2_TASK_VERIFIED",
    "ALPHA_V2_TASK_VERIFYING",
    "AlphaGoalSpec",
    "AlphaPlanVersion",
    "AlphaPlanningProvider",
    "AlphaPlanningRequest",
    "AlphaPlanningResult",
    "AlphaTaskSpec",
    "AlphaV2ContractError",
    "AlphaV2PolicyKernel",
    "AlphaVerificationCheck",
    "AttemptEvidence",
    "AttemptRoute",
    "FailureClass",
    "PolicyDecision",
    "PraxisPromotionCandidate",
    "RunLifecycleStatus",
    "TaskAttemptExecutor",
    "TaskLifecycleStatus",
    "ToolActionRequest",
    "alpha_goal_from_payload",
    "alpha_goal_payload",
    "alpha_plan_from_payload",
    "alpha_plan_payload",
    "alpha_planning_payload",
    "alpha_task_payload",
    "compile_alpha_plan",
]
