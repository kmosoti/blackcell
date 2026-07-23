from __future__ import annotations

from collections.abc import Iterable
from itertools import pairwise
from pathlib import PurePosixPath
from typing import Literal

from blackcell.interfaces.http.contracts import StrictStruct, WireContractError
from blackcell.orchestration.alpha_acceptance import (
    MAX_ALPHA_ACCEPTANCE_TIMEOUT_SECONDS,
    is_alpha_acceptance_check_id,
    is_alpha_acceptance_executable_alias,
)

MAX_ALPHA_EVENT_PAGE_SIZE = 200
_MAX_ID_CHARS = 120
_MAX_ROOT_CHARS = 4_096
_MAX_OBJECTIVE_CHARS = 8_000
_MAX_TEXT_CHARS = 2_000
_MAX_COLLECTION_ITEMS = 64
_MAX_PLAN_NODES = 64
_MAX_CHECK_ARGV = 32
_MAX_ARG_CHARS = 2_048

AlphaEffect = Literal["repository-read", "repository-write", "process", "network"]
AlphaEventType = Literal[
    "alpha.project.registered",
    "alpha.intent.accepted",
    "alpha.plan.accepted",
    "alpha.run.queued",
    "alpha.node.claimed",
    "alpha.node.worktree-prepared",
    "alpha.node.provider-dispatch-started",
    "alpha.run.cancel-requested",
    "alpha.node.succeeded",
    "alpha.node.failed",
    "alpha.node.requeued",
    "alpha.node.canceled",
    "alpha.node.reconciliation-required",
    "alpha.node.worktree-cleanup-requested",
    "alpha.node.worktree-cleaned",
    "alpha.node.worktree-cleanup-failed",
    "alpha.run.succeeded",
    "alpha.run.failed",
    "alpha.run.canceled",
    "alpha.run.reconciliation-required",
    "alpha.review.claimed",
    "alpha.review.lease-renewed",
    "alpha.review.provider-dispatch-started",
    "alpha.review.succeeded",
    "alpha.review.failed",
    "alpha.review.requeued",
    "alpha.review.reconciliation-required",
    "alpha.verification.claimed",
    "alpha.verification.completed",
    "alpha.verification.failed",
    "alpha.verification.requeued",
]
AlphaRunStatus = Literal[
    "queued",
    "running",
    "canceling",
    "canceled",
    "succeeded",
    "failed",
    "reconciliation-required",
]
AlphaReplayArtifactIntegrity = Literal[
    "not-applicable",
    "verified",
    "inconclusive",
    "failed",
]
AlphaReplayArtifactRole = Literal[
    "outcome",
    "context",
    "proposal",
    "provider",
    "effect",
    "check-command",
    "check-result",
    "check-stdout",
    "check-stderr",
]
AlphaReplayFindingCode = Literal[
    "alpha-replay-artifact-store-unavailable",
    "alpha-replay-outcome-reference-absent",
    "alpha-replay-artifact-missing",
    "alpha-replay-artifact-integrity-failed",
    "alpha-replay-artifact-metadata-mismatch",
    "alpha-replay-artifact-read-unavailable",
    "alpha-replay-artifact-budget-exceeded",
    "alpha-replay-artifact-json-invalid",
    "alpha-replay-artifact-noncanonical",
    "alpha-replay-outcome-schema-unsupported",
    "alpha-replay-outcome-invalid",
    "alpha-replay-artifact-binding-mismatch",
]
AlphaVerificationReplayLifecycle = Literal[
    "not-started",
    "claimed",
    "requeued",
    "completed",
    "verifier-error",
]
AlphaVerificationVerdict = Literal["pass", "fail", "inconclusive"]
AlphaVerificationReplayFindingCode = Literal[
    "alpha-verification-replay-event-store-unavailable",
    "alpha-verification-replay-lifecycle-invalid",
    "alpha-verification-replay-source-binding-mismatch",
    "alpha-verification-replay-artifact-store-unavailable",
    "alpha-verification-replay-report-missing",
    "alpha-verification-replay-report-integrity-failed",
    "alpha-verification-replay-report-metadata-mismatch",
    "alpha-verification-replay-report-read-unavailable",
    "alpha-verification-replay-report-json-invalid",
    "alpha-verification-replay-report-noncanonical",
    "alpha-verification-replay-report-invalid",
    "alpha-verification-replay-report-binding-mismatch",
]


class AlphaProjectRequest(StrictStruct, frozen=True):
    schema_version: Literal["alpha-project-request/v1"]
    project_id: str
    root: str
    configuration_provider: Literal["kernform"]
    configuration_version: Literal["0.1.0"]
    configuration_digest: str
    idempotency_key: str

    def __post_init__(self) -> None:
        _identifier(self.project_id)
        _bounded_text(self.root, maximum=_MAX_ROOT_CHARS)
        _digest(self.configuration_digest)
        _identifier(self.idempotency_key)


class AlphaIntentRequest(StrictStruct, frozen=True):
    schema_version: Literal["alpha-intent-request/v1"]
    intent_id: str
    project_id: str
    objective: str
    constraints: tuple[str, ...]
    assumptions: tuple[str, ...]
    unresolved_questions: tuple[str, ...]
    idempotency_key: str

    def __post_init__(self) -> None:
        _identifier(self.intent_id)
        _identifier(self.project_id)
        _bounded_text(self.objective, maximum=_MAX_OBJECTIVE_CHARS)
        _bounded_text_collection(self.constraints)
        _bounded_text_collection(self.assumptions)
        _bounded_text_collection(self.unresolved_questions)
        _identifier(self.idempotency_key)


class AlphaNodeBudget(StrictStruct, frozen=True):
    max_input_tokens: int
    max_output_tokens: int
    timeout_seconds: int
    max_cost_microusd: int
    max_changed_files: int

    def __post_init__(self) -> None:
        _bounded_integer(self.max_input_tokens, minimum=0, maximum=1_000_000)
        _bounded_integer(self.max_output_tokens, minimum=0, maximum=1_000_000)
        _bounded_integer(
            self.timeout_seconds,
            minimum=1,
            maximum=MAX_ALPHA_ACCEPTANCE_TIMEOUT_SECONDS,
        )
        _bounded_integer(self.max_cost_microusd, minimum=0, maximum=10_000_000_000)
        _bounded_integer(self.max_changed_files, minimum=0, maximum=10_000)


class AlphaAcceptanceCheck(StrictStruct, frozen=True):
    check_id: str
    argv: tuple[str, ...]
    expected_exit_code: int = 0

    def __post_init__(self) -> None:
        if not is_alpha_acceptance_check_id(self.check_id):
            raise WireContractError()
        if not self.argv or len(self.argv) > _MAX_CHECK_ARGV:
            raise WireContractError()
        for token in self.argv:
            _bounded_token(token)
        if not is_alpha_acceptance_executable_alias(self.argv[0]):
            raise WireContractError()
        _bounded_integer(self.expected_exit_code, minimum=0, maximum=255)


class AlphaPlanNode(StrictStruct, frozen=True):
    node_id: str
    objective: str
    depends_on: tuple[str, ...]
    budget: AlphaNodeBudget
    effects: tuple[AlphaEffect, ...]
    allowed_paths: tuple[str, ...]
    checks: tuple[AlphaAcceptanceCheck, ...]

    def __post_init__(self) -> None:
        _identifier(self.node_id)
        _bounded_text(self.objective, maximum=_MAX_TEXT_CHARS)
        _unique_identifiers(self.depends_on, maximum=_MAX_PLAN_NODES)
        if self.node_id in self.depends_on:
            raise WireContractError()
        _unique_values(self.effects, maximum=4)
        if not {"repository-read", "process"}.issubset(self.effects):
            raise WireContractError()
        _unique_repository_paths(self.allowed_paths)
        if "repository-write" in self.effects:
            if (
                not self.allowed_paths
                or self.budget.max_changed_files < 1
                or self.budget.max_input_tokens < 1
            ):
                raise WireContractError()
        elif self.allowed_paths or self.budget.max_changed_files != 0:
            raise WireContractError()
        if not self.checks or len(self.checks) > _MAX_COLLECTION_ITEMS:
            raise WireContractError()
        check_ids = tuple(check.check_id for check in self.checks)
        if len(check_ids) != len(set(check_ids)):
            raise WireContractError()


class AlphaPlanRequest(StrictStruct, frozen=True):
    schema_version: Literal["alpha-plan-request/v1"]
    plan_id: str
    project_id: str
    intent_id: str
    base_commit: str
    allowed_effects: tuple[AlphaEffect, ...]
    nodes: tuple[AlphaPlanNode, ...]
    idempotency_key: str

    def __post_init__(self) -> None:
        _identifier(self.plan_id)
        _identifier(self.project_id)
        _identifier(self.intent_id)
        _commit(self.base_commit)
        _identifier(self.idempotency_key)
        _unique_values(self.allowed_effects, maximum=4)
        if not self.nodes or len(self.nodes) > _MAX_PLAN_NODES:
            raise WireContractError()
        node_ids = tuple(node.node_id for node in self.nodes)
        if len(node_ids) != len(set(node_ids)):
            raise WireContractError()
        known = set(node_ids)
        allowed = set(self.allowed_effects)
        for node in self.nodes:
            if any(dependency not in known for dependency in node.depends_on):
                raise WireContractError()
            if not set(node.effects).issubset(allowed):
                raise WireContractError()
        order = alpha_plan_topological_order(self.nodes)
        by_id = {node.node_id: node for node in self.nodes}
        ancestors: dict[str, set[str]] = {}
        for node_id in order:
            dependencies = by_id[node_id].depends_on
            ancestors[node_id] = set(dependencies).union(
                *(ancestors[dependency] for dependency in dependencies)
            )
        writers = [node_id for node_id in order if "repository-write" in by_id[node_id].effects]
        if any(previous not in ancestors[current] for previous, current in pairwise(writers)):
            raise WireContractError()


class AlphaRunRequest(StrictStruct, frozen=True):
    schema_version: Literal["alpha-run-request/v1"]
    run_id: str
    project_id: str
    intent_id: str
    plan_id: str
    idempotency_key: str

    def __post_init__(self) -> None:
        _identifier(self.run_id)
        _identifier(self.project_id)
        _identifier(self.intent_id)
        _identifier(self.plan_id)
        _identifier(self.idempotency_key)


class AlphaCancelRunRequest(StrictStruct, frozen=True):
    schema_version: Literal["alpha-cancel-run-request/v1"]
    idempotency_key: str

    def __post_init__(self) -> None:
        _identifier(self.idempotency_key)


class AlphaProjectResponse(StrictStruct, frozen=True):
    project_id: str
    root: str
    configuration_provider: Literal["kernform"]
    configuration_version: Literal["0.1.0"]
    configuration_digest: str
    principal_id: str
    event_id: str
    cursor: int
    event_digest: str
    schema_version: Literal["alpha-project/v1"] = "alpha-project/v1"


class AlphaIntentResponse(StrictStruct, frozen=True):
    intent_id: str
    project_id: str
    objective: str
    constraints: tuple[str, ...]
    assumptions: tuple[str, ...]
    unresolved_questions: tuple[str, ...]
    principal_id: str
    event_id: str
    cursor: int
    event_digest: str
    schema_version: Literal["alpha-intent/v1"] = "alpha-intent/v1"


class AlphaPlanResponse(StrictStruct, frozen=True):
    plan_id: str
    project_id: str
    intent_id: str
    base_commit: str
    allowed_effects: tuple[AlphaEffect, ...]
    nodes: tuple[AlphaPlanNode, ...]
    topological_order: tuple[str, ...]
    principal_id: str
    event_id: str
    cursor: int
    event_digest: str
    schema_version: Literal["alpha-plan/v1"] = "alpha-plan/v1"


class AlphaRunResponse(StrictStruct, frozen=True):
    run_id: str
    project_id: str
    intent_id: str
    plan_id: str
    status: AlphaRunStatus
    cancellation_requested: bool
    active_node_id: str | None
    attempt: int
    fencing_token: int
    retained_worktree: bool
    principal_id: str
    event_id: str
    cursor: int
    event_digest: str
    schema_version: Literal["alpha-run/v1"] = "alpha-run/v1"


class AlphaEventResponse(StrictStruct, frozen=True):
    event_id: str
    cursor: int
    stream_id: str
    stream_sequence: int
    event_type: AlphaEventType
    event_schema_version: Literal[1]
    recorded_at: str
    correlation_id: str
    causation_id: str | None
    actor: str
    payload_digest: str
    payload: dict[str, object]
    schema_version: Literal["alpha-event/v1"] = "alpha-event/v1"


class AlphaEventPageResponse(StrictStruct, frozen=True):
    after_cursor: int
    limit: int
    scanned_events: int
    events: tuple[AlphaEventResponse, ...]
    next_cursor: int
    has_more: bool
    schema_version: Literal["alpha-event-page/v1"] = "alpha-event-page/v1"


class AlphaReplayArtifactResponse(StrictStruct, frozen=True):
    node_id: str
    role: AlphaReplayArtifactRole
    check_id: str | None
    digest: str
    size_bytes: int
    media_type: str
    encoding: str | None
    verified: bool


class AlphaReplayFindingResponse(StrictStruct, frozen=True):
    code: AlphaReplayFindingCode
    node_id: str | None
    role: AlphaReplayArtifactRole | None
    check_id: str | None
    artifact_digest: str | None


class AlphaVerificationReplayResponse(StrictStruct, frozen=True):
    lifecycle_status: AlphaVerificationReplayLifecycle
    verification_id: str | None
    review_id: str | None
    attempt: int | None
    fencing_token: int | None
    verdict: AlphaVerificationVerdict | None
    failure_code: str | None
    report_artifact_digest: str | None
    report_size_bytes: int | None
    report_media_type: str | None
    report_encoding: str | None
    matrix_digest: str | None
    artifact_integrity: AlphaReplayArtifactIntegrity
    finding_code: AlphaVerificationReplayFindingCode | None
    processed_events: int
    evidence_digest: str
    schema_version: Literal["alpha-verification-replay/v1"] = "alpha-verification-replay/v1"


class AlphaReplayResponse(StrictStruct, frozen=True):
    run_id: str
    project: AlphaProjectResponse
    intent: AlphaIntentResponse
    plan: AlphaPlanResponse
    run: AlphaRunResponse
    processed_events: int
    state_digest: str
    artifact_integrity: AlphaReplayArtifactIntegrity
    artifacts: tuple[AlphaReplayArtifactResponse, ...]
    findings: tuple[AlphaReplayFindingResponse, ...]
    artifact_evidence_digest: str
    verification: AlphaVerificationReplayResponse
    schema_version: Literal["alpha-replay/v2"] = "alpha-replay/v2"


def alpha_plan_topological_order(nodes: tuple[AlphaPlanNode, ...]) -> tuple[str, ...]:
    dependents: dict[str, list[str]] = {node.node_id: [] for node in nodes}
    remaining = {node.node_id: len(node.depends_on) for node in nodes}
    for node in nodes:
        for dependency in node.depends_on:
            if dependency not in dependents:
                raise WireContractError()
            dependents[dependency].append(node.node_id)
    ready = sorted(node_id for node_id, count in remaining.items() if count == 0)
    ordered: list[str] = []
    while ready:
        node_id = ready.pop(0)
        ordered.append(node_id)
        for dependent in sorted(dependents[node_id]):
            remaining[dependent] -= 1
            if remaining[dependent] == 0:
                ready.append(dependent)
                ready.sort()
    if len(ordered) != len(nodes):
        raise WireContractError()
    return tuple(ordered)


def _identifier(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_ID_CHARS
        or any(
            not (character.isascii() and (character.isalnum() or character in "-._"))
            for character in value
        )
    ):
        raise WireContractError()


def _bounded_text(value: str, *, maximum: int) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or any(ord(character) == 0 or ord(character) == 0x7F for character in value)
    ):
        raise WireContractError()


def _bounded_token(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_ARG_CHARS
        or any(ord(character) == 0 or ord(character) == 0x7F for character in value)
    ):
        raise WireContractError()


def _bounded_text_collection(values: tuple[str, ...]) -> None:
    if len(values) > _MAX_COLLECTION_ITEMS:
        raise WireContractError()
    for value in values:
        _bounded_text(value, maximum=_MAX_TEXT_CHARS)
    if len(values) != len(set(values)):
        raise WireContractError()


def _unique_identifiers(values: tuple[str, ...], *, maximum: int) -> None:
    if len(values) > maximum:
        raise WireContractError()
    for value in values:
        _identifier(value)
    if len(values) != len(set(values)):
        raise WireContractError()


def _unique_values(values: Iterable[object], *, maximum: int) -> None:
    items = tuple(values)
    if len(items) > maximum or len(items) != len(set(items)):
        raise WireContractError()


def _unique_repository_paths(values: tuple[str, ...]) -> None:
    if len(values) > _MAX_COLLECTION_ITEMS or len(values) != len(set(values)):
        raise WireContractError()
    for value in values:
        if (
            not isinstance(value, str)
            or not value
            or len(value) > _MAX_ROOT_CHARS
            or "\x00" in value
            or "\\" in value
        ):
            raise WireContractError()
        if value == ".":
            continue
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or path.as_posix() != value
            or any(part in {"", ".", ".."} for part in path.parts)
            or ".git" in path.parts
        ):
            raise WireContractError()


def _bounded_integer(value: int, *, minimum: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise WireContractError()


def _commit(value: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 40
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise WireContractError()


def _digest(value: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 71
        or not value.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise WireContractError()


__all__ = [
    "MAX_ALPHA_EVENT_PAGE_SIZE",
    "AlphaAcceptanceCheck",
    "AlphaEffect",
    "AlphaEventPageResponse",
    "AlphaEventResponse",
    "AlphaEventType",
    "AlphaIntentRequest",
    "AlphaIntentResponse",
    "AlphaNodeBudget",
    "AlphaPlanNode",
    "AlphaPlanRequest",
    "AlphaPlanResponse",
    "AlphaProjectRequest",
    "AlphaProjectResponse",
    "AlphaReplayResponse",
    "AlphaRunRequest",
    "AlphaRunResponse",
    "AlphaRunStatus",
    "AlphaVerificationReplayFindingCode",
    "AlphaVerificationReplayLifecycle",
    "AlphaVerificationReplayResponse",
    "AlphaVerificationVerdict",
    "alpha_plan_topological_order",
]
