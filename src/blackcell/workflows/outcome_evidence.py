from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from blackcell.features.evaluate_outcome import (
    EvaluationExecutionStatus,
    EvaluationFact,
    EvaluationObservation,
    EvaluationObservationStatus,
    EvaluationSourceEvent,
)
from blackcell.features.execute_affordance import deserialize_execution_result
from blackcell.features.ingest_observation import (
    EvidencePointer,
    IngestObservation,
    IngestObservationHandler,
    ObservationInput,
    ObservedClaim,
)
from blackcell.features.ingest_observation.events import observation_events
from blackcell.features.observe_outcome import (
    OutcomeObservation,
    OutcomeObservationStatus,
)
from blackcell.kernel import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    EventEnvelope,
    JsonInput,
    utc_now,
)
from blackcell.workflows.run_protocol import RunProtocolVersion, run_stream_id

OUTCOME_INCONCLUSIVE_EVENT_TYPE = "outcome.observation-inconclusive"
OUTCOME_INCONCLUSIVE_SCHEMA_VERSION = "outcome-inconclusive/v1"


class OutcomeEvidenceBindingError(ValueError):
    """Stored domain evidence does not exactly match its owner outcome artifact."""


class OutcomeEvidenceWriteError(RuntimeError):
    """Outcome evidence could not be written as one canonical domain occurrence."""


class OutcomeEvidenceHistory(Protocol):
    def get(self, event_id: str) -> EventEnvelope | None: ...


class OutcomeEvidenceArtifacts(Protocol):
    def get_bytes(self, digest: str, *, verify: bool = True) -> bytes: ...


class OutcomeEvidenceLedger(Protocol):
    """Narrow append boundary shared by canonical and inconclusive evidence writes."""

    def append(self, event: EventEnvelope, *, expected_sequence: int) -> EventEnvelope: ...

    def append_many(
        self,
        events: Sequence[EventEnvelope],
        *,
        expected_sequences: Mapping[str, int],
    ) -> tuple[EventEnvelope, ...]: ...


@dataclass(frozen=True, slots=True)
class WriteOutcomeEvidence:
    """Commit one observer result to its operational-state domain stream."""

    outcome: OutcomeObservation
    expected_sequence: int
    actor: str
    execution_event_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, OutcomeObservation):
            raise TypeError("outcome must be an OutcomeObservation")
        if (
            isinstance(self.expected_sequence, bool)
            or not isinstance(self.expected_sequence, int)
            or self.expected_sequence < 0
        ):
            raise ValueError("expected_sequence must be a non-negative integer")
        if not isinstance(self.actor, str) or not self.actor.strip():
            raise ValueError("actor must not be empty")
        if not isinstance(self.execution_event_id, str) or not self.execution_event_id.strip():
            raise ValueError("execution_event_id must not be empty")


class OutcomeEvidenceWriter:
    """Write observed or inconclusive outcome evidence through one typed seam."""

    def __init__(
        self,
        ledger: OutcomeEvidenceLedger,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._ledger = ledger
        self._clock = clock

    def handle(self, command: WriteOutcomeEvidence) -> EventEnvelope:
        recorded_at = self._recorded_at(command.outcome)
        if command.outcome.status is OutcomeObservationStatus.OBSERVED:
            stored = IngestObservationHandler(
                self._ledger,
                clock=lambda: recorded_at,
            ).handle(
                IngestObservation(
                    stream_id=command.outcome.stream_id,
                    expected_sequence=command.expected_sequence,
                    actor=command.actor,
                    source=command.outcome.observer_id,
                    correlation_id=command.outcome.binding.run_id,
                    observations=(outcome_observation_input(command.outcome),),
                    causation_id=command.execution_event_id,
                    domain=command.outcome.domain,
                )
            )
            if len(stored) != 1:
                raise OutcomeEvidenceWriteError(
                    "observed outcome ingestion must return exactly one stored occurrence"
                )
            return stored[0]

        candidate = inconclusive_outcome_event(
            command.outcome,
            stream_sequence=command.expected_sequence + 1,
            actor=command.actor,
            recorded_at=recorded_at,
            execution_event_id=command.execution_event_id,
        )
        return self._ledger.append(
            candidate,
            expected_sequence=command.expected_sequence,
        )

    def _recorded_at(self, outcome: OutcomeObservation) -> datetime:
        recorded_at = self._clock()
        if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
            raise OutcomeEvidenceWriteError("recorded clock must be timezone-aware")
        if recorded_at < outcome.observed_at:
            raise OutcomeEvidenceWriteError("recorded clock cannot precede the outcome observation")
        return recorded_at


def outcome_observation_input(outcome: OutcomeObservation) -> ObservationInput:
    """Translate an observed owner artifact into the canonical ingestion command value."""

    if outcome.status is not OutcomeObservationStatus.OBSERVED:
        raise OutcomeEvidenceBindingError(
            "only observed outcome claims can enter the operational belief state"
        )
    return ObservationInput(
        observation_id=outcome.observation_id,
        effective_at=outcome.observed_at,
        claims=tuple(
            ObservedClaim(
                claim_id=item.claim_id,
                subject=item.subject,
                predicate=item.predicate,
                value=item.value,
                confidence=item.confidence,
            )
            for item in outcome.claims
        ),
        evidence=tuple(
            EvidencePointer(
                locator=item.locator,
                artifact_id=item.artifact_id,
                digest=item.digest,
            )
            for item in outcome.evidence
        ),
        idempotency_key=outcome.observation_digest,
    )


def inconclusive_outcome_event(
    outcome: OutcomeObservation,
    *,
    stream_sequence: int,
    actor: str,
    recorded_at: datetime,
    execution_event_id: str,
) -> EventEnvelope:
    """Create the claim-free domain event for an inconclusive owner artifact."""

    if outcome.status is not OutcomeObservationStatus.INCONCLUSIVE:
        raise OutcomeEvidenceBindingError(
            "only an inconclusive outcome can create an inconclusive domain event"
        )
    return EventEnvelope.create(
        stream_id=outcome.stream_id,
        stream_sequence=stream_sequence,
        event_type=OUTCOME_INCONCLUSIVE_EVENT_TYPE,
        actor=actor,
        source=outcome.observer_id,
        payload=_inconclusive_payload(outcome),
        recorded_at=recorded_at,
        effective_at=outcome.observed_at,
        correlation_id=outcome.binding.run_id,
        causation_id=execution_event_id,
        idempotency_key=outcome.observation_digest,
    )


def bind_evaluation_observation(
    outcome: OutcomeObservation,
    history: OutcomeEvidenceHistory,
    artifacts: OutcomeEvidenceArtifacts,
    *,
    execution_event_id: str,
    outcome_event_ids: tuple[str, ...],
) -> EvaluationObservation:
    """Verify owner artifact, stored event, and execution causation before evaluation."""

    if not execution_event_id.strip():
        raise OutcomeEvidenceBindingError("execution_event_id must not be empty")
    if len(outcome_event_ids) != 1 or not outcome_event_ids[0].strip():
        raise OutcomeEvidenceBindingError(
            "one outcome artifact must identify exactly one stored domain event"
        )
    execution_event = _stored_event(
        history,
        execution_event_id,
        label="execution",
    )
    event = _stored_event(history, outcome_event_ids[0], label="outcome")
    _verify_execution_event(outcome, execution_event, artifacts)
    outcome_position = event.global_position
    execution_position = execution_event.global_position
    if outcome_position is None or execution_position is None:  # pragma: no cover - loaded above
        raise OutcomeEvidenceBindingError("stored evidence requires ledger positions")
    if outcome_position <= execution_position:
        raise OutcomeEvidenceBindingError(
            "outcome evidence must be recorded after its execution event"
        )
    _verify_common(outcome, event, execution_event_id=execution_event_id)
    if outcome.status is OutcomeObservationStatus.OBSERVED:
        _verify_observed_event(outcome, event, execution_event_id=execution_event_id)
        facts = tuple(
            EvaluationFact(
                claim_id=item.claim_id,
                subject=item.subject,
                predicate=item.predicate,
                value=item.value,
                confidence=item.confidence,
                source_event_id=event.event_id,
            )
            for item in outcome.claims
        )
        status = EvaluationObservationStatus.OBSERVED
    else:
        expected = inconclusive_outcome_event(
            outcome,
            stream_sequence=event.stream_sequence,
            actor=event.actor,
            recorded_at=event.recorded_at,
            execution_event_id=execution_event_id,
        )
        _verify_event_content(event, expected)
        facts = ()
        status = EvaluationObservationStatus.INCONCLUSIVE
    return EvaluationObservation(
        observation_id=outcome.observation_id,
        observation_digest=outcome.observation_digest,
        evaluation_spec_id=outcome.evaluation_spec_id,
        execution_binding_id=outcome.binding.binding_id,
        execution_status=EvaluationExecutionStatus(outcome.binding.execution_status),
        status=status,
        observed_at=outcome.observed_at,
        sources=(
            EvaluationSourceEvent(
                event_id=event.event_id,
                global_position=outcome_position,
                event_type=event.event_type,
                stream_id=event.stream_id,
                correlation_id=event.correlation_id,
                causation_id=execution_event_id,
                payload_hash=event.payload_hash,
            ),
        ),
        facts=facts,
    )


def _stored_event(
    history: OutcomeEvidenceHistory,
    event_id: str,
    *,
    label: str,
) -> EventEnvelope:
    event = history.get(event_id)
    if event is None or event.global_position is None:
        raise OutcomeEvidenceBindingError(
            f"{label} evidence event is not present in the immutable ledger"
        )
    if event.event_id != event_id:
        raise OutcomeEvidenceBindingError(
            f"{label} history lookup returned a different event identity"
        )
    return event


def _verify_execution_event(
    outcome: OutcomeObservation,
    event: EventEnvelope,
    artifacts: OutcomeEvidenceArtifacts,
) -> None:
    binding = outcome.binding
    if event.event_type != "run.execution-recorded":
        raise OutcomeEvidenceBindingError("execution evidence is not run.execution-recorded")
    if event.schema_version != RunProtocolVersion.V2.event_schema_version:
        raise OutcomeEvidenceBindingError("execution event is not a version-two run event")
    if event.source != "blackcell.workflows.daily_operator":
        raise OutcomeEvidenceBindingError("execution event source is not canonical")
    if event.stream_id != run_stream_id(binding.run_id):
        raise OutcomeEvidenceBindingError("execution event belongs to a different run stream")
    if event.correlation_id != binding.run_id:
        raise OutcomeEvidenceBindingError("execution event belongs to a different run")
    if event.recorded_at < binding.completed_at:
        raise OutcomeEvidenceBindingError("execution event precedes execution completion")
    expected = {
        "run_id": binding.run_id,
        "result_id": binding.execution_result_id,
        "invocation_id": binding.invocation_id,
        "proposal_id": binding.proposal_id,
        "proposal_digest": binding.proposal_digest,
        "authorization_decision_id": binding.authorization_decision_id,
        "authorized_action_digest": binding.authorized_action_digest,
        "execution_identity_digest": binding.execution_identity_digest,
        "status": binding.execution_status,
        "affordance": binding.affordance,
        "adapter_id": binding.execution_adapter_id,
        "adapter_contract_version": binding.execution_adapter_contract_version,
        "completed_at": binding.completed_at.isoformat(),
        "arguments": tuple({"name": item.name, "value": item.value} for item in binding.arguments),
    }
    if any(event.payload.get(key) != value for key, value in expected.items()):
        raise OutcomeEvidenceBindingError(
            "execution event payload does not match OutcomeExecutionBinding"
        )
    artifact = event.payload.get("artifact")
    if not isinstance(artifact, Mapping) or any(
        artifact.get(key) != binding.execution_result_id for key in ("digest", "logical_id")
    ):
        raise OutcomeEvidenceBindingError(
            "execution event artifact does not match its execution result"
        )
    if (
        artifact.get("media_type") != "application/vnd.blackcell.execution-result+json"
        or artifact.get("schema_version") != "execution-result/v3"
        or artifact.get("encoding") != "utf-8"
    ):
        raise OutcomeEvidenceBindingError("execution event artifact metadata is incompatible")
    try:
        data = artifacts.get_bytes(binding.execution_result_id, verify=True)
        size_bytes = artifact.get("size_bytes")
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int):
            raise ValueError("execution artifact size is invalid")
        if size_bytes != len(data):
            raise ValueError("execution artifact size does not match its bytes")
        result = deserialize_execution_result(
            data,
            expected_result_id=binding.execution_result_id,
        )
    except (
        ArtifactIntegrityError,
        ArtifactNotFoundError,
        TypeError,
        ValueError,
    ) as error:
        raise OutcomeEvidenceBindingError(
            "execution result artifact is missing or invalid"
        ) from error
    exact = (
        result.invocation_id == binding.invocation_id
        and result.proposal_id == binding.proposal_id
        and result.authorization_decision_id == binding.authorization_decision_id
        and result.affordance == binding.affordance
        and result.adapter_id == binding.execution_adapter_id
        and result.authorized_action_digest == binding.authorized_action_digest
        and result.execution_identity_digest == binding.execution_identity_digest
        and result.status.value == binding.execution_status
        and result.completed_at == binding.completed_at
    )
    if not exact:
        raise OutcomeEvidenceBindingError(
            "execution result artifact does not match OutcomeExecutionBinding"
        )


def _verify_observed_event(
    outcome: OutcomeObservation,
    event: EventEnvelope,
    *,
    execution_event_id: str,
) -> None:
    command = IngestObservation(
        stream_id=outcome.stream_id,
        expected_sequence=event.stream_sequence - 1,
        actor=event.actor,
        source=outcome.observer_id,
        correlation_id=outcome.binding.run_id,
        observations=(outcome_observation_input(outcome),),
        causation_id=execution_event_id,
        domain=outcome.domain,
    )
    expected = observation_events(command, recorded_at=event.recorded_at)[0]
    _verify_event_content(event, expected)


def _verify_common(
    outcome: OutcomeObservation,
    event: EventEnvelope,
    *,
    execution_event_id: str,
) -> None:
    if outcome.observed_at < outcome.binding.completed_at:
        raise OutcomeEvidenceBindingError("outcome observation cannot precede execution completion")
    if event.stream_id != outcome.stream_id:
        raise OutcomeEvidenceBindingError("outcome event belongs to a different domain stream")
    if event.source != outcome.observer_id:
        raise OutcomeEvidenceBindingError("outcome event source does not match its observer")
    if event.correlation_id != outcome.binding.run_id:
        raise OutcomeEvidenceBindingError("outcome event belongs to a different run")
    if event.causation_id != execution_event_id:
        raise OutcomeEvidenceBindingError("outcome event is not caused by the execution event")
    if event.effective_at != outcome.observed_at:
        raise OutcomeEvidenceBindingError("outcome event effective time does not match observation")
    if event.recorded_at < outcome.observed_at:
        raise OutcomeEvidenceBindingError("outcome event cannot be recorded before observation")


def _verify_event_content(actual: EventEnvelope, expected: EventEnvelope) -> None:
    fields = (
        "stream_id",
        "stream_sequence",
        "event_type",
        "schema_version",
        "recorded_at",
        "effective_at",
        "correlation_id",
        "causation_id",
        "actor",
        "source",
        "payload",
        "payload_hash",
        "idempotency_key",
    )
    if any(getattr(actual, field) != getattr(expected, field) for field in fields):
        raise OutcomeEvidenceBindingError(
            "stored outcome event content does not match its owner observation artifact"
        )


def _inconclusive_payload(outcome: OutcomeObservation) -> dict[str, JsonInput]:
    return {
        "domain": outcome.domain,
        "observation_id": outcome.observation_id,
        "outcome_schema_version": OUTCOME_INCONCLUSIVE_SCHEMA_VERSION,
        "observation_digest": outcome.observation_digest,
        "evaluation_spec_id": outcome.evaluation_spec_id,
        "execution_binding_id": outcome.binding.binding_id,
        "status": outcome.status.value,
        "evidence": [
            {
                "locator": item.locator,
                "artifact_id": item.artifact_id,
                "digest": item.digest,
            }
            for item in outcome.evidence
        ],
    }


__all__ = [
    "OUTCOME_INCONCLUSIVE_EVENT_TYPE",
    "OUTCOME_INCONCLUSIVE_SCHEMA_VERSION",
    "OutcomeEvidenceArtifacts",
    "OutcomeEvidenceBindingError",
    "OutcomeEvidenceHistory",
    "OutcomeEvidenceLedger",
    "OutcomeEvidenceWriteError",
    "OutcomeEvidenceWriter",
    "WriteOutcomeEvidence",
    "bind_evaluation_observation",
    "inconclusive_outcome_event",
    "outcome_observation_input",
]
