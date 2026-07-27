from __future__ import annotations

from collections.abc import Iterable
from itertools import pairwise
from pathlib import PurePosixPath
from typing import Literal

import msgspec

from blackcell.orchestration.acceptance import (
    MAX_ACCEPTANCE_TIMEOUT_SECONDS,
    is_acceptance_check_id,
    is_acceptance_executable_alias,
)

MAX_REQUEST_BODY_BYTES = 1_048_576
MAX_RESPONSE_BODY_BYTES = 256 * MAX_REQUEST_BODY_BYTES
MAX_RUNTIME_EVENT_PAGE_SIZE = 200
MAX_RUN_QUERY_PAGE_SIZE = 100
MAX_RUN_QUERY_SCAN_EVENTS = 1_000
RUN_QUERY_MEDIA_TYPE = "application/vnd.blackcell.run-query+json"
RUN_QUERY_RESULT_MEDIA_TYPE = "application/vnd.blackcell.run-query-result+json"
_MAX_ID_CHARS = 120
_MAX_ROOT_CHARS = 4_096
_MAX_OBJECTIVE_CHARS = 8_000
_MAX_TEXT_CHARS = 2_000
_MAX_COLLECTION_ITEMS = 64
_MAX_PLAN_NODES = 64
_MAX_CHECK_ARGV = 32
_MAX_ARG_CHARS = 2_048

PlanEffect = Literal["repository-read", "repository-write", "process", "network"]
RuntimeEventType = Literal[
    "project.registered",
    "intent.accepted",
    "plan.accepted",
    "run.queued",
    "node.claimed",
    "node.worktree-prepared",
    "node.provider-dispatch-started",
    "run.cancel-requested",
    "node.succeeded",
    "node.failed",
    "node.requeued",
    "node.canceled",
    "node.reconciliation-required",
    "node.worktree-cleanup-requested",
    "node.worktree-cleaned",
    "node.worktree-cleanup-failed",
    "run.succeeded",
    "run.failed",
    "run.canceled",
    "run.reconciliation-required",
    "review.claimed",
    "review.lease-renewed",
    "review.provider-dispatch-started",
    "review.succeeded",
    "review.failed",
    "review.requeued",
    "review.reconciliation-required",
    "verification.claimed",
    "verification.completed",
    "verification.failed",
    "verification.requeued",
]
RunStatus = Literal[
    "queued",
    "running",
    "canceling",
    "canceled",
    "succeeded",
    "failed",
    "reconciliation-required",
]
_RUN_STATUSES = frozenset(
    {
        "queued",
        "running",
        "canceling",
        "canceled",
        "succeeded",
        "failed",
        "reconciliation-required",
    }
)
RunNodeStatus = Literal[
    "pending",
    "ready",
    "claimed",
    "running",
    "verifying",
    "succeeded",
    "repairable",
    "replan-required",
    "blocked",
    "escalated",
    "terminal-failure",
    "failed",
    "canceled",
    "reconciliation-required",
]
ReplayArtifactIntegrity = Literal[
    "not-applicable",
    "verified",
    "inconclusive",
    "failed",
]
ReplayArtifactRole = Literal[
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
ReplayFindingCode = Literal[
    "replay-artifact-store-unavailable",
    "replay-outcome-reference-absent",
    "replay-artifact-missing",
    "replay-artifact-integrity-failed",
    "replay-artifact-metadata-mismatch",
    "replay-artifact-read-unavailable",
    "replay-artifact-budget-exceeded",
    "replay-artifact-json-invalid",
    "replay-artifact-noncanonical",
    "replay-outcome-schema-unsupported",
    "replay-outcome-invalid",
    "replay-artifact-binding-mismatch",
]
VerificationReplayLifecycle = Literal[
    "not-started",
    "claimed",
    "requeued",
    "completed",
    "verifier-error",
]
VerificationVerdict = Literal["pass", "fail", "inconclusive"]
VerificationReplayFindingCode = Literal[
    "verification-replay-event-store-unavailable",
    "verification-replay-lifecycle-invalid",
    "verification-replay-source-binding-mismatch",
    "verification-replay-artifact-store-unavailable",
    "verification-replay-report-missing",
    "verification-replay-report-integrity-failed",
    "verification-replay-report-metadata-mismatch",
    "verification-replay-report-read-unavailable",
    "verification-replay-report-json-invalid",
    "verification-replay-report-noncanonical",
    "verification-replay-report-invalid",
    "verification-replay-report-binding-mismatch",
]


class WireContractError(ValueError):
    """A bounded public request-contract failure."""

    def __init__(self, code: str = "invalid-request") -> None:
        self.code = code
        super().__init__(code)


class StrictStruct(
    msgspec.Struct,
    frozen=True,
    forbid_unknown_fields=True,
    kw_only=True,
):
    pass


class HealthResponse(StrictStruct, frozen=True):
    status: Literal["live", "ready", "not-ready"]
    schema_version: Literal["health/v1"] = "health/v1"


class ErrorResponse(StrictStruct, frozen=True):
    error: str
    schema_version: Literal["error/v1"] = "error/v1"


class ProjectRequest(StrictStruct, frozen=True):
    schema_version: Literal["project-request/v1"]
    project_id: str
    root: str
    configuration_provider: Literal["kernform"]
    configuration_version: Literal["0.2.0"]
    configuration_digest: str
    idempotency_key: str

    def __post_init__(self) -> None:
        _identifier(self.project_id)
        _bounded_text(self.root, maximum=_MAX_ROOT_CHARS)
        _digest(self.configuration_digest)
        _identifier(self.idempotency_key)


class IntentRequest(StrictStruct, frozen=True):
    schema_version: Literal["intent-request/v1"]
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


class NodeBudget(StrictStruct, frozen=True):
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
            maximum=MAX_ACCEPTANCE_TIMEOUT_SECONDS,
        )
        _bounded_integer(self.max_cost_microusd, minimum=0, maximum=10_000_000_000)
        _bounded_integer(self.max_changed_files, minimum=0, maximum=10_000)


class AcceptanceCheck(StrictStruct, frozen=True):
    check_id: str
    argv: tuple[str, ...]
    expected_exit_code: int = 0

    def __post_init__(self) -> None:
        if not is_acceptance_check_id(self.check_id):
            raise WireContractError()
        if not self.argv or len(self.argv) > _MAX_CHECK_ARGV:
            raise WireContractError()
        for token in self.argv:
            _bounded_token(token)
        if not is_acceptance_executable_alias(self.argv[0]):
            raise WireContractError()
        _bounded_integer(self.expected_exit_code, minimum=0, maximum=255)


class PlanNode(StrictStruct, frozen=True):
    node_id: str
    objective: str
    depends_on: tuple[str, ...]
    budget: NodeBudget
    effects: tuple[PlanEffect, ...]
    allowed_paths: tuple[str, ...]
    checks: tuple[AcceptanceCheck, ...]

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
                or self.budget.max_output_tokens < 1
            ):
                raise WireContractError()
        elif self.allowed_paths or self.budget.max_changed_files != 0:
            raise WireContractError()
        if not self.checks or len(self.checks) > _MAX_COLLECTION_ITEMS:
            raise WireContractError()
        check_ids = tuple(check.check_id for check in self.checks)
        if len(check_ids) != len(set(check_ids)):
            raise WireContractError()


class PlanRequest(StrictStruct, frozen=True):
    schema_version: Literal["plan-request/v1"]
    plan_id: str
    project_id: str
    intent_id: str
    base_commit: str
    allowed_effects: tuple[PlanEffect, ...]
    nodes: tuple[PlanNode, ...]
    idempotency_key: str
    planning_mode: Literal["declared", "generated"] = "declared"

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
        order = plan_topological_order(self.nodes)
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


class RunRequest(StrictStruct, frozen=True):
    schema_version: Literal["run-request/v1"]
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


class CancelRunRequest(StrictStruct, frozen=True):
    schema_version: Literal["execution-cancel-run-request/v1"]
    idempotency_key: str

    def __post_init__(self) -> None:
        _identifier(self.idempotency_key)


class ProjectResponse(StrictStruct, frozen=True):
    project_id: str
    root: str
    configuration_provider: Literal["kernform"]
    configuration_version: Literal["0.2.0"]
    configuration_digest: str
    principal_id: str
    event_id: str
    cursor: int
    event_digest: str
    schema_version: Literal["project/v1"] = "project/v1"


class IntentResponse(StrictStruct, frozen=True):
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
    schema_version: Literal["intent/v1"] = "intent/v1"


class PlanResponse(StrictStruct, frozen=True):
    plan_id: str
    project_id: str
    intent_id: str
    base_commit: str
    allowed_effects: tuple[PlanEffect, ...]
    nodes: tuple[PlanNode, ...]
    topological_order: tuple[str, ...]
    principal_id: str
    event_id: str
    cursor: int
    event_digest: str
    schema_version: Literal["plan/v1"] = "plan/v1"


class RunResponse(StrictStruct, frozen=True):
    run_id: str
    project_id: str
    intent_id: str
    plan_id: str
    status: RunStatus
    cancellation_requested: bool
    active_node_id: str | None
    attempt: int
    fencing_token: int
    retained_worktree: bool
    principal_id: str
    event_id: str
    cursor: int
    event_digest: str
    schema_version: Literal["run/v1"] = "run/v1"


class RunQueryRequest(StrictStruct, frozen=True):
    """Closed RFC 10008 content for bounded, read-only run discovery."""

    schema_version: Literal["run-query-request/v1"]
    statuses: tuple[RunStatus, ...] = ()
    project_ids: tuple[str, ...] = ()
    intent_ids: tuple[str, ...] = ()
    plan_ids: tuple[str, ...] = ()
    run_ids: tuple[str, ...] = ()
    after_cursor: int = 0
    limit: int = 50

    def __post_init__(self) -> None:
        statuses = tuple(sorted(self.statuses))
        if len(statuses) > len(_RUN_STATUSES) or len(statuses) != len(set(statuses)):
            raise WireContractError()
        for value in statuses:
            if value not in _RUN_STATUSES:
                raise WireContractError()
        object.__setattr__(self, "statuses", statuses)
        for field_name in ("project_ids", "intent_ids", "plan_ids", "run_ids"):
            values = tuple(sorted(getattr(self, field_name)))
            _unique_identifiers(values, maximum=_MAX_COLLECTION_ITEMS)
            object.__setattr__(self, field_name, values)
        _bounded_integer(self.after_cursor, minimum=0, maximum=2**63 - 1)
        _bounded_integer(self.limit, minimum=1, maximum=MAX_RUN_QUERY_PAGE_SIZE)


class RunNodeQueryResponse(StrictStruct, frozen=True):
    node_id: str
    status: RunNodeStatus
    attempts: int
    fencing_token: int
    failure_code: str | None
    retained_worktree: bool
    head_commit: str | None
    depends_on: tuple[str, ...] = ()
    max_attempts: int | None = None

    def __post_init__(self) -> None:
        _identifier(self.node_id)
        _unique_identifiers(self.depends_on, maximum=_MAX_PLAN_NODES)
        if self.max_attempts is not None:
            _bounded_integer(self.max_attempts, minimum=1, maximum=3)


class RunBudgetUsageResponse(StrictStruct, frozen=True):
    input_tokens: int
    input_tokens_complete: bool
    max_input_tokens: int
    output_tokens: int
    output_tokens_complete: bool
    max_output_tokens: int
    latency_ms: int
    max_latency_ms: int
    cost_microusd: int
    cost_microusd_complete: bool
    max_cost_microusd: int

    def __post_init__(self) -> None:
        for value in (
            self.input_tokens,
            self.max_input_tokens,
            self.output_tokens,
            self.max_output_tokens,
            self.latency_ms,
            self.max_latency_ms,
            self.cost_microusd,
            self.max_cost_microusd,
        ):
            _bounded_integer(value, minimum=0, maximum=2**63 - 1)
        if not all(
            isinstance(value, bool)
            for value in (
                self.input_tokens_complete,
                self.output_tokens_complete,
                self.cost_microusd_complete,
            )
        ):
            raise WireContractError()


class RunQueryItem(StrictStruct, frozen=True):
    queued_cursor: int
    run: RunResponse
    nodes: tuple[RunNodeQueryResponse, ...]
    usage: RunBudgetUsageResponse | None = None


class RunQueryResponse(StrictStruct, frozen=True):
    query: RunQueryRequest
    scanned_events: int
    runs: tuple[RunQueryItem, ...]
    next_cursor: int
    has_more: bool
    schema_version: Literal["run-query/v1"] = "run-query/v1"


class RuntimeEventResponse(StrictStruct, frozen=True):
    event_id: str
    cursor: int
    stream_id: str
    stream_sequence: int
    event_type: RuntimeEventType
    event_schema_version: Literal[1]
    recorded_at: str
    correlation_id: str
    causation_id: str | None
    actor: str
    payload_digest: str
    payload: dict[str, object]
    schema_version: Literal["event/v1"] = "event/v1"


class RuntimeEventPageResponse(StrictStruct, frozen=True):
    after_cursor: int
    limit: int
    scanned_events: int
    events: tuple[RuntimeEventResponse, ...]
    next_cursor: int
    has_more: bool
    schema_version: Literal["event-page/v1"] = "event-page/v1"


class ReplayArtifactResponse(StrictStruct, frozen=True):
    node_id: str
    role: ReplayArtifactRole
    check_id: str | None
    digest: str
    size_bytes: int
    media_type: str
    encoding: str | None
    verified: bool


class ReplayFindingResponse(StrictStruct, frozen=True):
    code: ReplayFindingCode
    node_id: str | None
    role: ReplayArtifactRole | None
    check_id: str | None
    artifact_digest: str | None


class VerificationReplayResponse(StrictStruct, frozen=True):
    lifecycle_status: VerificationReplayLifecycle
    verification_id: str | None
    review_id: str | None
    attempt: int | None
    fencing_token: int | None
    verdict: VerificationVerdict | None
    failure_code: str | None
    report_artifact_digest: str | None
    report_size_bytes: int | None
    report_media_type: str | None
    report_encoding: str | None
    matrix_digest: str | None
    artifact_integrity: ReplayArtifactIntegrity
    finding_code: VerificationReplayFindingCode | None
    processed_events: int
    evidence_digest: str
    schema_version: Literal["verification-replay/v1"] = "verification-replay/v1"


class ReplayResponse(StrictStruct, frozen=True):
    run_id: str
    project: ProjectResponse
    intent: IntentResponse
    plan: PlanResponse
    run: RunResponse
    processed_events: int
    state_digest: str
    artifact_integrity: ReplayArtifactIntegrity
    artifacts: tuple[ReplayArtifactResponse, ...]
    findings: tuple[ReplayFindingResponse, ...]
    artifact_evidence_digest: str
    verification: VerificationReplayResponse
    schema_version: Literal["replay/v2"] = "replay/v2"


def decode_contract[ContractT](data: bytes, contract_type: type[ContractT]) -> ContractT:
    return _decode_bounded_contract(
        data,
        contract_type,
        maximum_bytes=MAX_REQUEST_BODY_BYTES,
    )


def decode_response_contract[ContractT](data: bytes, contract_type: type[ContractT]) -> ContractT:
    return _decode_bounded_contract(
        data,
        contract_type,
        maximum_bytes=MAX_RESPONSE_BODY_BYTES,
    )


def _decode_bounded_contract[ContractT](
    data: bytes,
    contract_type: type[ContractT],
    *,
    maximum_bytes: int,
) -> ContractT:
    if not data or len(data) > maximum_bytes:
        raise WireContractError()
    try:
        return msgspec.json.decode(data, type=contract_type, strict=True)
    except (msgspec.DecodeError, TypeError, ValueError) as error:
        raise WireContractError() from error


def convert_contract[ContractT](value: object, contract_type: type[ContractT]) -> ContractT:
    """Strictly convert built-in values at the interface contract boundary."""

    try:
        return msgspec.convert(value, type=contract_type, strict=True)
    except (msgspec.ValidationError, TypeError, ValueError) as error:
        raise WireContractError() from error


def encode_contract(value: msgspec.Struct) -> bytes:
    return msgspec.json.encode(value)


def contract_to_builtins(value: StrictStruct) -> object:
    """Project a wire contract into JSON-compatible built-in values."""

    return msgspec.to_builtins(value)


def contract_to_json_builtins(value: StrictStruct) -> object:
    """Project a wire contract through its exact JSON representation."""

    return msgspec.json.decode(msgspec.json.encode(value))


def plan_topological_order(nodes: tuple[PlanNode, ...]) -> tuple[str, ...]:
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
    "MAX_REQUEST_BODY_BYTES",
    "MAX_RESPONSE_BODY_BYTES",
    "MAX_RUNTIME_EVENT_PAGE_SIZE",
    "MAX_RUN_QUERY_PAGE_SIZE",
    "MAX_RUN_QUERY_SCAN_EVENTS",
    "RUN_QUERY_MEDIA_TYPE",
    "RUN_QUERY_RESULT_MEDIA_TYPE",
    "AcceptanceCheck",
    "CancelRunRequest",
    "ErrorResponse",
    "HealthResponse",
    "IntentRequest",
    "IntentResponse",
    "NodeBudget",
    "PlanEffect",
    "PlanNode",
    "PlanRequest",
    "PlanResponse",
    "ProjectRequest",
    "ProjectResponse",
    "ReplayResponse",
    "RunBudgetUsageResponse",
    "RunNodeQueryResponse",
    "RunNodeStatus",
    "RunQueryItem",
    "RunQueryRequest",
    "RunQueryResponse",
    "RunRequest",
    "RunResponse",
    "RunStatus",
    "RuntimeEventPageResponse",
    "RuntimeEventResponse",
    "RuntimeEventType",
    "StrictStruct",
    "VerificationReplayFindingCode",
    "VerificationReplayLifecycle",
    "VerificationReplayResponse",
    "VerificationVerdict",
    "WireContractError",
    "contract_to_builtins",
    "contract_to_json_builtins",
    "convert_contract",
    "decode_contract",
    "decode_response_contract",
    "encode_contract",
    "plan_topological_order",
]
