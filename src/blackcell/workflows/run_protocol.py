from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from blackcell.features.authorize_action import ActionProposal, AuthorizationDecision
from blackcell.features.build_context import ContextFrame
from blackcell.features.execute_affordance import ExecutionResult
from blackcell.features.solve_constraints import ConstraintEvaluation
from blackcell.kernel import EventEnvelope, JsonInput

RUN_STARTED = "run.started"
CONTEXT_RECORDED = "run.context-recorded"
PROPOSAL_RECORDED = "run.proposal-recorded"
CONSTRAINTS_EVALUATED = "run.constraints-evaluated"
AUTHORIZATION_DECIDED = "run.authorization-decided"
EXECUTION_RECORDED = "run.execution-recorded"
TRACE_RECORDED = "run.trace-recorded"
RUN_COMPLETED = "run.completed"
RUN_FAILED = "run.failed"

RUN_EVENT_SCHEMA_VERSION = 1
RUN_WORKFLOW = "daily-operator"
RUN_WORKFLOW_VERSION = "daily-operator/v1"
RUN_TRACE_SCHEMA_VERSION = "run-trace/v1"
RUN_FAILURE_SCHEMA_VERSION = "run-failure/v1"
RUN_TRACE_MEDIA_TYPE = "application/vnd.blackcell.run-trace+json"
RUN_FAILURE_MEDIA_TYPE = "application/vnd.blackcell.run-failure+json"

_SHA256_PREFIX = "sha256:"


class RunOutcome(StrEnum):
    EXECUTED = "executed"
    DENIED = "denied"
    APPROVAL_REQUIRED = "approval-required"
    EXECUTION_FAILED = "execution-failed"
    REQUIRES_RECONCILIATION = "requires-reconciliation"
    FAILED = "failed"


class RunProtocolError(RuntimeError):
    """Base error for the durable Daily Operator run aggregate."""


class RunIdentityConflict(RunProtocolError):
    """A run ID is already bound to a different request digest."""


class RunAlreadyExists(RunProtocolError):
    """An exact delivery targets an already-terminal run."""


class RunInterrupted(RunProtocolError):
    """An exact delivery targets a nonterminal run requiring explicit recovery."""


class RunProtocolIntegrityError(RunProtocolError):
    """Stored run events or artifacts violate the v1 aggregate grammar."""


@dataclass(frozen=True, slots=True)
class RunStart:
    run_id: str
    request_digest: str
    actor: str
    task_id: str
    objective: str
    domain: str
    observation_stream_id: str

    def __post_init__(self) -> None:
        for name in (
            "run_id",
            "actor",
            "task_id",
            "objective",
            "domain",
            "observation_stream_id",
        ):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        _validate_digest(self.request_digest, label="request_digest")


@dataclass(frozen=True, slots=True)
class RunArtifactLink:
    digest: str
    media_type: str
    encoding: str | None
    size_bytes: int
    schema_version: str
    logical_id: str

    def __post_init__(self) -> None:
        _validate_digest(self.digest, label="artifact digest")
        for name in ("media_type", "schema_version", "logical_id"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        if self.encoding is not None and not self.encoding.strip():
            raise ValueError("encoding must not be blank")
        if self.size_bytes < 0:
            raise ValueError("size_bytes must be non-negative")

    def as_payload(self) -> dict[str, JsonInput]:
        return {
            "digest": self.digest,
            "media_type": self.media_type,
            "encoding": self.encoding,
            "size_bytes": self.size_bytes,
            "schema_version": self.schema_version,
            "logical_id": self.logical_id,
        }


@dataclass(frozen=True, slots=True)
class RunTerminal:
    trace_event: EventEnvelope
    terminal_event: EventEnvelope


def run_stream_id(run_id: str) -> str:
    if not run_id.strip():
        raise ValueError("run_id must not be empty")
    return f"daily-operator-run:{run_id}"


class RunRecorder(Protocol):
    """Durable application port for one create-only Daily Operator run."""

    def start(self, command: RunStart) -> EventEnvelope: ...

    def record_context(self, run_id: str, frame: ContextFrame) -> EventEnvelope: ...

    def record_proposal(self, run_id: str, proposal: ActionProposal) -> EventEnvelope: ...

    def record_constraints(
        self, run_id: str, evaluation: ConstraintEvaluation
    ) -> EventEnvelope: ...

    def record_authorization(
        self, run_id: str, decision: AuthorizationDecision
    ) -> EventEnvelope: ...

    def record_execution(self, run_id: str, result: ExecutionResult) -> EventEnvelope: ...

    def complete(self, run_id: str, outcome: RunOutcome) -> RunTerminal: ...

    def fail(self, run_id: str, *, phase: str, error_type: str) -> RunTerminal: ...


def _validate_digest(value: str, *, label: str) -> None:
    hexadecimal = value.removeprefix(_SHA256_PREFIX)
    if not value.startswith(_SHA256_PREFIX) or len(hexadecimal) != 64:
        raise ValueError(f"{label} must be a SHA-256 digest")
    try:
        int(hexadecimal, 16)
    except ValueError as error:
        raise ValueError(f"{label} must be a SHA-256 digest") from error
