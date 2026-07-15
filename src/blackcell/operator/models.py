from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from blackcell.control import ActionProposal, PolicyDecision
from blackcell.kernel import EventEnvelope
from blackcell.models import ModelInvocation


@dataclass(frozen=True, slots=True)
class CanonicalOperatorRunResult:
    """Compact product view of one replay-verified Daily Operator v2 run."""

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
    schema_version: str = "canonical-operator-run-result/v1"

    def __post_init__(self) -> None:
        for name in ("run_id", "status", "repository_stream_id", "run_stream_id"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        if self.run_event_count < 1 or self.artifact_count < 0:
            raise ValueError("operator run counts must be non-negative")


class OperatorRunStatus(StrEnum):
    COMPLETED = "completed"
    DENIED = "denied"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class OperatorEvaluation:
    evaluation_id: str
    proposal_id: str
    policy_outcome: str
    policy_enforced: bool
    action_attempted: bool
    execution_success: bool | None
    effect_match: bool | None
    task_success: bool | None
    expected_effect_count: int
    matched_effect_count: int
    residuals: tuple[str, ...]
    passed: bool
    schema_version: str = "operator-evaluation/v1"


@dataclass(frozen=True, slots=True)
class ExecutionSummary:
    attempt_id: str
    affordance: str
    status: str
    success: bool
    output_digest: str
    truncated: bool
    error: str | None


@dataclass(frozen=True, slots=True)
class RunArtifacts:
    initial_state: str
    signal_packet: str
    context_frame: str
    model_decision: str
    policy_decision: str
    execution_result: str | None
    final_state: str | None
    evaluation: str
    trace: str

    def digests(self) -> tuple[str, ...]:
        return tuple(
            digest
            for digest in (
                self.initial_state,
                self.signal_packet,
                self.context_frame,
                self.model_decision,
                self.policy_decision,
                self.execution_result,
                self.final_state,
                self.evaluation,
                self.trace,
            )
            if digest is not None
        )


@dataclass(frozen=True, slots=True)
class OperatorRunResult:
    run_id: str
    status: OperatorRunStatus
    repository_stream_id: str
    run_stream_id: str
    initial_state_id: str
    signal_packet_id: str
    final_state_id: str | None
    context_frame_id: str
    proposal: ActionProposal
    invocation: ModelInvocation
    policy: PolicyDecision
    execution: ExecutionSummary | None
    evaluation: OperatorEvaluation
    artifacts: RunArtifacts
    run_event_count: int
    trace_span_count: int
    schema_version: str = "operator-run-result/v1"


@dataclass(frozen=True, slots=True)
class ReplayArtifact:
    digest: str
    media_type: str
    size_bytes: int
    verified: bool
    reproduced: bool | None = None


@dataclass(frozen=True, slots=True)
class HistoricalReplay:
    run_id: str
    status: str
    run_stream_id: str
    events: tuple[EventEnvelope, ...]
    artifacts: tuple[ReplayArtifact, ...]
    projection_hash_match: bool
    schema_version: str = "historical-replay/v1"

    @property
    def event_count(self) -> int:
        return len(self.events)


@dataclass(frozen=True, slots=True)
class StoredContextFrame:
    run_id: str
    frame_id: str
    artifact_digest: str
    payload: Mapping[str, Any]
    schema_version: str = "stored-context-frame/v1"
