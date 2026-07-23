from __future__ import annotations

import math
from datetime import datetime
from typing import Literal

import msgspec

MAX_REQUEST_BODY_BYTES = 1_048_576
MAX_EVENT_PAGE_SIZE = 200
MAX_REPLAY_EVENTS = 256
_MAX_ID_CHARS = 200
_MAX_TEXT_CHARS = 1_000
_MAX_OBJECTIVE_CHARS = 4_000
_MAX_CONTEXT_CHARS = 65_536
_MAX_OBSERVATIONS = 64
_MAX_CLAIMS = 128
_MAX_EVIDENCE = 64

JsonScalar = None | bool | int | float | str


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


class RunSubmissionRequest(StrictStruct, frozen=True):
    schema_version: Literal["run-submission-request/v1"]
    objective: str
    approval_granted: bool = False
    token_budget: int = 2_000
    character_budget: int = 8_000

    def __post_init__(self) -> None:
        _bounded_text(self.objective, "objective", maximum=_MAX_OBJECTIVE_CHARS)
        _bounded_integer(self.token_budget, "token_budget", minimum=1, maximum=100_000)
        _bounded_integer(
            self.character_budget,
            "character_budget",
            minimum=1,
            maximum=_MAX_CONTEXT_CHARS,
        )


class EvidenceRequest(StrictStruct, frozen=True):
    locator: str | None = None
    artifact_id: str | None = None
    digest: str | None = None

    def __post_init__(self) -> None:
        values = (self.locator, self.artifact_id, self.digest)
        if not any(value is not None for value in values):
            raise WireContractError()
        for value in values:
            if value is not None:
                _bounded_text(value, "evidence", maximum=_MAX_TEXT_CHARS)


class ClaimRequest(StrictStruct, frozen=True):
    claim_id: str
    subject: str
    predicate: str
    value: JsonScalar
    confidence: float = 1.0
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        _identifier(self.claim_id, "claim_id")
        _bounded_text(self.subject, "subject", maximum=_MAX_TEXT_CHARS)
        _bounded_text(self.predicate, "predicate", maximum=_MAX_ID_CHARS)
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise WireContractError()
        if isinstance(self.confidence, bool) or not math.isfinite(self.confidence):
            raise WireContractError()
        if not 0.0 <= self.confidence <= 1.0:
            raise WireContractError()
        if self.expires_at is not None:
            _aware(self.expires_at)


class ObservationRequest(StrictStruct, frozen=True):
    observation_id: str
    effective_at: datetime
    claims: tuple[ClaimRequest, ...]
    evidence: tuple[EvidenceRequest, ...]
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.observation_id, "observation_id")
        _aware(self.effective_at)
        _bounded_collection(self.claims, maximum=_MAX_CLAIMS)
        _bounded_collection(self.evidence, maximum=_MAX_EVIDENCE)
        identifiers = tuple(item.claim_id for item in self.claims)
        if len(identifiers) != len(set(identifiers)):
            raise WireContractError()
        if any(
            claim.expires_at is not None and claim.expires_at < self.effective_at
            for claim in self.claims
        ):
            raise WireContractError()
        if self.idempotency_key is not None:
            _identifier(self.idempotency_key, "idempotency_key")


class ObservationIngestRequest(StrictStruct, frozen=True):
    schema_version: Literal["observation-ingest-request/v1"]
    stream_id: str
    expected_sequence: int
    source: str
    correlation_id: str
    observations: tuple[ObservationRequest, ...]
    causation_id: str | None = None
    domain: str = "repository"

    def __post_init__(self) -> None:
        _identifier(self.stream_id, "stream_id")
        if not self.stream_id.startswith(("repository:", "observation:")):
            raise WireContractError()
        _bounded_integer(
            self.expected_sequence,
            "expected_sequence",
            minimum=0,
            maximum=2**63 - 1,
        )
        _bounded_text(self.source, "source", maximum=_MAX_ID_CHARS)
        _identifier(self.correlation_id, "correlation_id")
        _bounded_collection(self.observations, maximum=_MAX_OBSERVATIONS)
        keys = tuple(item.idempotency_key or item.observation_id for item in self.observations)
        if len(keys) != len(set(keys)):
            raise WireContractError()
        if self.causation_id is not None:
            _identifier(self.causation_id, "causation_id")
        _bounded_text(self.domain, "domain", maximum=_MAX_ID_CHARS)


class ApprovalRequest(StrictStruct, frozen=True):
    schema_version: Literal["orchestration-approval-request/v1"]
    role: Literal["reviewer", "verifier"]
    approved: bool


class HealthResponse(StrictStruct, frozen=True):
    status: Literal["live", "ready", "not-ready"]
    schema_version: Literal["health/v1"] = "health/v1"


class RunResponse(StrictStruct, frozen=True):
    run_id: str
    status: str
    outcome: str | None
    workflow_version: str | None
    repository_stream_id: str
    run_stream_id: str
    context_frame_id: str | None
    authorization_outcome: str | None
    execution_status: str | None
    evaluation_verdict: str | None
    transition_recorded: bool
    run_event_count: int
    artifact_count: int
    schema_version: Literal["runtime-run/v1"] = "runtime-run/v1"


class ObservationIngestResponse(StrictStruct, frozen=True):
    stream_id: str
    event_ids: tuple[str, ...]
    first_sequence: int
    last_sequence: int
    schema_version: Literal["observation-ingest/v1"] = "observation-ingest/v1"


class ContextResponse(StrictStruct, frozen=True):
    run_id: str
    frame_id: str
    artifact_digest: str
    payload: dict[str, object]
    schema_version: Literal["runtime-context/v1"] = "runtime-context/v1"


class EventResponse(StrictStruct, frozen=True):
    event_id: str
    global_position: int
    stream_id: str
    stream_sequence: int
    event_type: str
    schema_version: int
    recorded_at: str
    effective_at: str
    actor: str
    source: str
    correlation_id: str | None
    causation_id: str | None
    idempotency_key: str | None
    payload_hash: str
    payload: dict[str, object]


class EventPageResponse(StrictStruct, frozen=True):
    after_position: int
    limit: int
    events: tuple[EventResponse, ...]
    next_after_position: int
    schema_version: Literal["event-page/v1"] = "event-page/v1"


class ReplayArtifactResponse(StrictStruct, frozen=True):
    event_id: str
    event_type: str
    stream_sequence: int
    field: str
    digest: str
    verified: bool


class ReplayProjectionResponse(StrictStruct, frozen=True):
    stage: str
    status: str
    snapshot_digest: str | None
    cutoff_global_position: int | None
    effective_time_cutoff: str | None


class ReplayFindingResponse(StrictStruct, frozen=True):
    stage: str
    code: str
    message: str


class ReplayResponse(StrictStruct, frozen=True):
    run_id: str
    run_stream_id: str
    protocol_version: str | None
    classification: str
    outcome: str | None
    events: tuple[EventResponse, ...]
    artifacts: tuple[ReplayArtifactResponse, ...]
    projections: tuple[ReplayProjectionResponse, ...]
    finding: ReplayFindingResponse | None
    schema_version: Literal["runtime-replay/v1"] = "runtime-replay/v1"

    def __post_init__(self) -> None:
        if len(self.events) > MAX_REPLAY_EVENTS:
            raise ValueError("runtime replay response exceeds its event bound")


class EvaluationResponse(StrictStruct, frozen=True):
    run_id: str
    evaluation_id: str
    evaluation_spec_id: str
    verdict: str
    artifact_digest: str
    schema_version: Literal["runtime-evaluation/v1"] = "runtime-evaluation/v1"


class OrchestrationNodeResponse(StrictStruct, frozen=True):
    node_id: str
    status: str
    attempts: int
    fencing_token: int
    available_at: str
    lease_worker_id: str | None
    lease_expires_at: str | None
    result_digest: str | None
    failure_code: str | None
    input_tokens: int
    output_tokens: int
    latency_ms: int
    cost_microusd: int


class OrchestrationApprovalResponse(StrictStruct, frozen=True):
    node_id: str
    role: str
    principal_id: str
    approved: bool
    decided_at: str
    decision_digest: str
    schema_version: Literal["orchestration-approval/v1"] = "orchestration-approval/v1"


class OrchestrationRunResponse(StrictStruct, frozen=True):
    run_id: str
    dag_id: str
    dag_digest: str
    status: str
    submitted_by: str
    submitted_at: str
    updated_at: str
    nodes: tuple[OrchestrationNodeResponse, ...]
    approvals: tuple[OrchestrationApprovalResponse, ...]
    schema_version: Literal["orchestration-run/v1"] = "orchestration-run/v1"


class ErrorResponse(StrictStruct, frozen=True):
    error: str
    schema_version: Literal["error/v1"] = "error/v1"


def decode_contract[ContractT](data: bytes, contract_type: type[ContractT]) -> ContractT:
    if not data or len(data) > MAX_REQUEST_BODY_BYTES:
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


def _identifier(value: str, field_name: str) -> None:
    _bounded_text(value, field_name, maximum=_MAX_ID_CHARS)
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise WireContractError()


def _bounded_text(value: str, field_name: str, *, maximum: int) -> None:
    del field_name
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise WireContractError()


def _bounded_integer(
    value: int,
    field_name: str,
    *,
    minimum: int,
    maximum: int,
) -> None:
    del field_name
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise WireContractError()


def _bounded_collection(value: tuple[object, ...], *, maximum: int) -> None:
    if not value or len(value) > maximum:
        raise WireContractError()


def _aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise WireContractError()


__all__ = [
    "MAX_EVENT_PAGE_SIZE",
    "MAX_REQUEST_BODY_BYTES",
    "ApprovalRequest",
    "ClaimRequest",
    "ContextResponse",
    "ErrorResponse",
    "EvaluationResponse",
    "EventPageResponse",
    "EventResponse",
    "EvidenceRequest",
    "HealthResponse",
    "ObservationIngestRequest",
    "ObservationIngestResponse",
    "ObservationRequest",
    "OrchestrationApprovalResponse",
    "OrchestrationNodeResponse",
    "OrchestrationRunResponse",
    "ReplayArtifactResponse",
    "ReplayFindingResponse",
    "ReplayProjectionResponse",
    "ReplayResponse",
    "RunResponse",
    "RunSubmissionRequest",
    "StrictStruct",
    "WireContractError",
    "contract_to_builtins",
    "contract_to_json_builtins",
    "convert_contract",
    "decode_contract",
    "encode_contract",
]
