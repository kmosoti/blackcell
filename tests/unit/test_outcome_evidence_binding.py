from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from blackcell.features.evaluate_outcome import (
    EvaluateOutcome,
    EvaluationAuthorizationOutcome,
    EvaluationCriterion,
    EvaluationExecutionStatus,
    EvaluationObservationStatus,
    EvaluationSpec,
    EvaluationVerdict,
    OutcomeEvaluator,
)
from blackcell.features.execute_affordance import (
    ExecutionResult,
    ExecutionStatus,
    serialize_execution_result,
)
from blackcell.features.ingest_observation import IngestObservation
from blackcell.features.ingest_observation.events import observation_events
from blackcell.features.observe_outcome import (
    OutcomeArgument,
    OutcomeClaim,
    OutcomeEvidencePointer,
    OutcomeExecutionBinding,
    OutcomeObservation,
    OutcomeObservationStatus,
)
from blackcell.kernel import (
    ArtifactNotFoundError,
    ConcurrencyError,
    EventEnvelope,
    EventStore,
    IdempotencyConflict,
    JsonInput,
)
from blackcell.workflows.outcome_evidence import (
    OutcomeEvidenceBindingError,
    OutcomeEvidenceWriteError,
    OutcomeEvidenceWriter,
    WriteOutcomeEvidence,
    bind_evaluation_observation,
    inconclusive_outcome_event,
    outcome_observation_input,
)

NOW = datetime(2026, 7, 12, 20, tzinfo=UTC)
EXECUTION_EVENT_ID = "event:execution"
OUTCOME_EVENT_ID = "event:outcome"
STREAM_ID = "observations:daily"
_DEFAULT_ARTIFACT = object()


class History:
    def __init__(self, *events: EventEnvelope, aliases: Mapping[str, EventEnvelope] | None = None):
        self.events = {event.event_id: event for event in events}
        self.events.update(aliases or {})

    def get(self, event_id: str) -> EventEnvelope | None:
        return self.events.get(event_id)


class Artifacts:
    def __init__(self, values: Mapping[str, bytes]) -> None:
        self.values = dict(values)

    def get_bytes(self, digest: str, *, verify: bool = True) -> bytes:
        assert verify
        try:
            return self.values[digest]
        except KeyError as error:
            raise ArtifactNotFoundError(digest) from error


def test_verified_ledger_events_and_execution_artifact_drive_evaluation() -> None:
    spec = _spec()
    outcome = _outcome(spec=spec, value=True)
    event = _observed_event(outcome)

    bound = _bind(outcome, event)
    evaluation = OutcomeEvaluator(clock=lambda: NOW + timedelta(seconds=3)).handle(
        EvaluateOutcome(
            "run:1",
            spec,
            EvaluationAuthorizationOutcome.ALLOW,
            EvaluationExecutionStatus.SUCCEEDED,
            EXECUTION_EVENT_ID,
            outcome.binding.binding_id,
            bound,
            5,
        )
    )

    assert bound.observation_digest == outcome.observation_digest
    assert bound.sources[0].payload_hash == event.payload_hash
    assert bound.sources[0].causation_id == EXECUTION_EVENT_ID
    assert evaluation.outcome_evidence_binding_id == bound.evidence_binding_id
    assert evaluation.verdict is EvaluationVerdict.PASS


def test_same_claim_identity_with_different_value_changes_every_bound_identity() -> None:
    spec = _spec()
    truth = _outcome(spec=spec, value=True)
    falsehood = _outcome(spec=spec, value=False)
    true_bound = _bind(truth, _observed_event(truth))
    false_bound = _bind(falsehood, _observed_event(falsehood))

    assert truth.observation_digest != falsehood.observation_digest
    assert true_bound.evidence_binding_id != false_bound.evidence_binding_id
    with pytest.raises(OutcomeEvidenceBindingError, match="does not match"):
        _bind(falsehood, _observed_event(truth))


@pytest.mark.parametrize(
    ("mutate", "message"),
    (
        (lambda event: replace(event, causation_id="event:unrelated"), "not caused"),
        (lambda event: replace(event, correlation_id="run:other"), "different run"),
        (lambda event: replace(event, stream_id="observations:other"), "domain stream"),
        (lambda event: replace(event, source="other-observer"), "source does not match"),
        (
            lambda event: replace(event, effective_at=event.effective_at + timedelta(seconds=1)),
            "effective time",
        ),
        (lambda event: replace(event, recorded_at=NOW), "recorded before observation"),
        (lambda event: _event_with(event, event_type="unrelated.event"), "does not match"),
        (
            lambda event: _event_with(
                event,
                payload={**event.payload, "observation_id": "outcome:forged"},
            ),
            "does not match",
        ),
    ),
)
def test_unrelated_or_tampered_outcome_event_cannot_satisfy_evaluation(
    mutate,
    message: str,
) -> None:
    outcome = _outcome(spec=_spec())

    with pytest.raises(OutcomeEvidenceBindingError, match=message):
        _bind(outcome, mutate(_observed_event(outcome)))


@pytest.mark.parametrize(
    ("mutate", "message"),
    (
        (lambda event: _event_with(event, event_type="other"), "run.execution-recorded"),
        (lambda event: replace(event, schema_version=1), "version-two"),
        (lambda event: replace(event, source="other"), "source is not canonical"),
        (lambda event: replace(event, stream_id="other"), "run stream"),
        (lambda event: replace(event, correlation_id="run:other"), "different run"),
        (
            lambda event: _event_with(
                event,
                payload={**event.payload, "proposal_digest": f"sha256:{'9' * 64}"},
            ),
            "payload does not match",
        ),
        (
            lambda event: _event_with(
                event,
                payload={**event.payload, "arguments": ()},
            ),
            "payload does not match",
        ),
        (
            lambda event: _event_with(
                event,
                payload={
                    **event.payload,
                    "artifact": {
                        **event.payload["artifact"],
                        "logical_id": f"sha256:{'9' * 64}",
                    },
                },
            ),
            "artifact does not match",
        ),
    ),
)
def test_execution_envelope_must_exactly_bind_the_observed_execution(
    mutate,
    message: str,
) -> None:
    outcome = _outcome(spec=_spec())
    event = _observed_event(outcome)
    execution = mutate(_execution_event(outcome))

    with pytest.raises(OutcomeEvidenceBindingError, match=message):
        _bind(outcome, event, execution_event=execution)


def test_execution_result_artifact_is_verified_against_every_available_binding_field() -> None:
    outcome = _outcome(spec=_spec())
    event = _observed_event(outcome)
    genuine = _result()
    altered = replace(genuine, proposal_id="proposal:other")

    with pytest.raises(OutcomeEvidenceBindingError, match="missing or invalid"):
        _bind(
            outcome,
            event,
            artifact_data=serialize_execution_result(altered).encode(),
        )
    with pytest.raises(OutcomeEvidenceBindingError, match="missing or invalid"):
        _bind(outcome, event, artifact_data=b"not-json")
    with pytest.raises(OutcomeEvidenceBindingError, match="missing or invalid"):
        _bind(outcome, event, artifact_data=None)


def test_history_lookup_identity_position_and_order_fail_closed() -> None:
    outcome = _outcome(spec=_spec())
    execution = _execution_event(outcome)
    event = _observed_event(outcome)
    data = serialize_execution_result(_result()).encode()
    artifacts = Artifacts({outcome.binding.execution_result_id: data})

    with pytest.raises(OutcomeEvidenceBindingError, match="different event identity"):
        bind_evaluation_observation(
            outcome,
            History(event, aliases={"invented": execution}),
            artifacts,
            execution_event_id="invented",
            outcome_event_ids=(event.event_id,),
        )
    with pytest.raises(OutcomeEvidenceBindingError, match="not present"):
        bind_evaluation_observation(
            outcome,
            History(execution),
            artifacts,
            execution_event_id=execution.event_id,
            outcome_event_ids=(event.event_id,),
        )
    with pytest.raises(OutcomeEvidenceBindingError, match="recorded after"):
        _bind(outcome, replace(event, global_position=5))


def test_pre_execution_observation_and_binding_status_mismatch_fail_closed() -> None:
    spec = _spec()
    outcome = replace(_outcome(spec=spec), observed_at=NOW - timedelta(seconds=1))
    with pytest.raises(OutcomeEvidenceBindingError, match="precede execution"):
        _bind(outcome, _observed_event(outcome))

    verified = _bind(_outcome(spec=spec), _observed_event(_outcome(spec=spec)))
    with pytest.raises(ValueError, match="different execution status"):
        EvaluateOutcome(
            "run:1",
            spec,
            EvaluationAuthorizationOutcome.ALLOW,
            EvaluationExecutionStatus.FAILED,
            EXECUTION_EVENT_ID,
            verified.execution_binding_id,
            verified,
            5,
        )


def test_binding_requires_one_named_outcome_event_and_exact_execution_identity() -> None:
    outcome = _outcome(spec=_spec())
    event = _observed_event(outcome)
    history, artifacts = _ports(outcome, event)
    with pytest.raises(OutcomeEvidenceBindingError, match="execution_event_id"):
        bind_evaluation_observation(
            outcome,
            history,
            artifacts,
            execution_event_id=" ",
            outcome_event_ids=(event.event_id,),
        )
    with pytest.raises(OutcomeEvidenceBindingError, match="exactly one"):
        bind_evaluation_observation(
            outcome,
            history,
            artifacts,
            execution_event_id=EXECUTION_EVENT_ID,
            outcome_event_ids=(),
        )
    with pytest.raises(OutcomeEvidenceBindingError, match="only an inconclusive"):
        inconclusive_outcome_event(
            outcome,
            stream_sequence=1,
            actor="operator",
            recorded_at=NOW + timedelta(seconds=2),
            execution_event_id=EXECUTION_EVENT_ID,
        )


def test_inconclusive_owner_artifact_has_a_claim_free_bound_event() -> None:
    spec = _spec()
    outcome = replace(
        _outcome(spec=spec),
        observation_id="outcome:inconclusive",
        status=OutcomeObservationStatus.INCONCLUSIVE,
        claims=(),
    )
    event = replace(
        inconclusive_outcome_event(
            outcome,
            stream_sequence=1,
            actor="operator",
            recorded_at=NOW + timedelta(seconds=2),
            execution_event_id=EXECUTION_EVENT_ID,
        ),
        event_id=OUTCOME_EVENT_ID,
        global_position=11,
    )

    bound = _bind(outcome, event)

    assert bound.status is EvaluationObservationStatus.INCONCLUSIVE
    assert bound.facts == ()
    assert bound.sources[0].event_type == "outcome.observation-inconclusive"
    with pytest.raises(OutcomeEvidenceBindingError, match="only observed"):
        outcome_observation_input(outcome)


@pytest.mark.parametrize(
    ("status", "event_type"),
    (
        (OutcomeObservationStatus.OBSERVED, "observation.recorded"),
        (OutcomeObservationStatus.INCONCLUSIVE, "outcome.observation-inconclusive"),
    ),
)
def test_writer_commits_one_causal_domain_occurrence(
    tmp_path: Path,
    status: OutcomeObservationStatus,
    event_type: str,
) -> None:
    store = _store_with_execution(tmp_path / f"{status}.sqlite3")
    outcome = _outcome_with_status(status)
    clock_calls = 0

    def clock() -> datetime:
        nonlocal clock_calls
        clock_calls += 1
        return NOW + timedelta(seconds=2)

    stored = OutcomeEvidenceWriter(store, clock=clock).handle(_write_command(outcome))

    assert store.read_stream(STREAM_ID) == (stored,)
    assert stored.event_type == event_type
    assert stored.stream_sequence == 1
    assert stored.actor == "operator"
    assert stored.source == outcome.observer_id
    assert stored.correlation_id == outcome.binding.run_id
    assert stored.causation_id == EXECUTION_EVENT_ID
    assert stored.recorded_at == NOW + timedelta(seconds=2)
    assert stored.effective_at == outcome.observed_at
    assert stored.idempotency_key == outcome.observation_digest
    assert clock_calls == 1


@pytest.mark.parametrize(
    "status",
    (OutcomeObservationStatus.OBSERVED, OutcomeObservationStatus.INCONCLUSIVE),
)
def test_writer_exact_redelivery_returns_original_and_collision_propagates(
    tmp_path: Path,
    status: OutcomeObservationStatus,
) -> None:
    store = _store_with_execution(tmp_path / f"{status}.sqlite3")
    outcome = _outcome_with_status(status)
    ticks = iter(
        (
            NOW + timedelta(seconds=2),
            NOW + timedelta(seconds=3),
            NOW + timedelta(seconds=4),
        )
    )
    writer = OutcomeEvidenceWriter(store, clock=lambda: next(ticks))
    command = _write_command(outcome)

    original = writer.handle(command)

    assert writer.handle(command) == original
    assert original.recorded_at == NOW + timedelta(seconds=2)
    assert store.read_stream(STREAM_ID) == (original,)
    with pytest.raises(IdempotencyConflict):
        writer.handle(replace(command, actor="different-actor"))
    assert store.read_stream(STREAM_ID) == (original,)


@pytest.mark.parametrize(
    "status",
    (OutcomeObservationStatus.OBSERVED, OutcomeObservationStatus.INCONCLUSIVE),
)
def test_writer_preserves_stale_sequence_errors_without_partial_evidence(
    tmp_path: Path,
    status: OutcomeObservationStatus,
) -> None:
    store = _store_with_execution(tmp_path / f"{status}.sqlite3")
    unrelated = EventEnvelope.create(
        stream_id=STREAM_ID,
        stream_sequence=1,
        event_type="fixture.unrelated",
        actor="fixture",
        source="fixture",
        payload={"value": "already-present"},
        recorded_at=NOW,
        correlation_id="run:unrelated",
        idempotency_key="fixture:unrelated",
    )
    unrelated = store.append(unrelated, expected_sequence=0)

    with pytest.raises(ConcurrencyError):
        OutcomeEvidenceWriter(store, clock=lambda: NOW + timedelta(seconds=2)).handle(
            _write_command(_outcome_with_status(status))
        )

    assert store.read_stream(STREAM_ID) == (unrelated,)


@pytest.mark.parametrize(
    "clock",
    (
        lambda: datetime(2026, 7, 12, 20, 0),
        lambda: NOW,
    ),
)
def test_writer_rejects_invalid_recorded_clocks_before_append(clock) -> None:
    ledger = _NeverAppendLedger()
    writer = OutcomeEvidenceWriter(ledger, clock=clock)

    with pytest.raises(OutcomeEvidenceWriteError, match="recorded clock"):
        writer.handle(_write_command(_outcome_with_status(OutcomeObservationStatus.OBSERVED)))

    assert ledger.calls == 0


@pytest.mark.parametrize(
    ("expected_sequence", "actor", "execution_event_id", "message"),
    (
        (-1, "operator", EXECUTION_EVENT_ID, "expected_sequence"),
        (True, "operator", EXECUTION_EVENT_ID, "expected_sequence"),
        ("0", "operator", EXECUTION_EVENT_ID, "expected_sequence"),
        (0, " ", EXECUTION_EVENT_ID, "actor"),
        (0, "operator", " ", "execution_event_id"),
    ),
)
def test_write_command_rejects_invalid_coordination_values(
    expected_sequence: Any,
    actor: str,
    execution_event_id: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        WriteOutcomeEvidence(
            _outcome(spec=_spec()),
            expected_sequence,
            actor,
            execution_event_id,
        )


def test_write_command_rejects_non_observation_owner() -> None:
    invalid: Any = object()

    with pytest.raises(TypeError, match="OutcomeObservation"):
        WriteOutcomeEvidence(invalid, 0, "operator", EXECUTION_EVENT_ID)


class _NeverAppendLedger:
    def __init__(self) -> None:
        self.calls = 0

    def append(self, event: EventEnvelope, *, expected_sequence: int) -> EventEnvelope:
        self.calls += 1
        raise AssertionError((event, expected_sequence))

    def append_many(self, events, *, expected_sequences):
        self.calls += 1
        raise AssertionError((events, expected_sequences))


def _store_with_execution(path: Path) -> EventStore:
    store = EventStore(path)
    execution = EventEnvelope.create(
        stream_id="daily-operator-run:run:1",
        stream_sequence=1,
        event_type="fixture.execution",
        actor="operator",
        source="fixture",
        payload={"run_id": "run:1"},
        recorded_at=NOW,
        correlation_id="run:1",
        event_id=EXECUTION_EVENT_ID,
    )
    store.append(execution, expected_sequence=0)
    return store


def _outcome_with_status(status: OutcomeObservationStatus) -> OutcomeObservation:
    outcome = _outcome(spec=_spec())
    if status is OutcomeObservationStatus.OBSERVED:
        return outcome
    return replace(
        outcome,
        observation_id="outcome:inconclusive",
        status=status,
        claims=(),
    )


def _write_command(outcome: OutcomeObservation) -> WriteOutcomeEvidence:
    return WriteOutcomeEvidence(
        outcome=outcome,
        expected_sequence=0,
        actor="operator",
        execution_event_id=EXECUTION_EVENT_ID,
    )


def _spec() -> EvaluationSpec:
    return EvaluationSpec(
        "daily-ready",
        "repository is clean",
        (EvaluationCriterion("clean", "repository", "git.clean", True),),
    )


def _result() -> ExecutionResult:
    return ExecutionResult(
        invocation_id="invocation:1",
        proposal_id="proposal:1",
        authorization_decision_id="authorization:1",
        affordance="inspect",
        adapter_id="fixture",
        idempotency_key="execution:1",
        authorized_action_digest=f"sha256:{'5' * 64}",
        execution_identity_digest=f"sha256:{'7' * 64}",
        status=ExecutionStatus.SUCCEEDED,
        started_at=NOW - timedelta(seconds=1),
        completed_at=NOW,
        output_digest=f"sha256:{'3' * 64}",
        observed_effects=(),
        error_code=None,
        reconciled=False,
    )


def _outcome(*, spec: EvaluationSpec, value=True) -> OutcomeObservation:
    result = _result()
    return OutcomeObservation(
        observation_id="outcome:1",
        binding=OutcomeExecutionBinding(
            run_id="run:1",
            invocation_id=result.invocation_id,
            proposal_id=result.proposal_id,
            proposal_digest=f"sha256:{'4' * 64}",
            authorization_decision_id=result.authorization_decision_id,
            authorized_action_digest=result.authorized_action_digest,
            execution_result_id=result.result_id,
            execution_identity_digest=result.execution_identity_digest,
            execution_status=result.status.value,
            affordance=result.affordance,
            arguments=(OutcomeArgument("path", "README.md"),),
            execution_adapter_id=result.adapter_id,
            execution_adapter_contract_version="fixture/v1",
            completed_at=result.completed_at,
        ),
        evaluation_spec_id=spec.spec_id,
        domain="repository",
        stream_id=STREAM_ID,
        observer_id="fixture-observer",
        observer_contract_version="fixture-observer/v1",
        status=OutcomeObservationStatus.OBSERVED,
        observed_at=NOW + timedelta(seconds=1),
        claims=(OutcomeClaim("claim:clean", "repository", "git.clean", value, 0.95),),
        evidence=(
            OutcomeEvidencePointer(
                locator="fixture://repository/status",
                digest=f"sha256:{'8' * 64}",
            ),
        ),
    )


def _execution_event(outcome: OutcomeObservation) -> EventEnvelope:
    binding = outcome.binding
    data = serialize_execution_result(_result()).encode()
    return replace(
        EventEnvelope.create(
            stream_id="daily-operator-run:run:1",
            stream_sequence=9,
            event_type="run.execution-recorded",
            schema_version=2,
            actor="operator",
            source="blackcell.workflows.daily_operator",
            payload={
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
                "arguments": [
                    {"name": item.name, "value": item.value} for item in binding.arguments
                ],
                "adapter_id": binding.execution_adapter_id,
                "adapter_contract_version": binding.execution_adapter_contract_version,
                "completed_at": binding.completed_at.isoformat(),
                "artifact": {
                    "digest": binding.execution_result_id,
                    "media_type": "application/vnd.blackcell.execution-result+json",
                    "encoding": "utf-8",
                    "size_bytes": len(data),
                    "schema_version": "execution-result/v3",
                    "logical_id": binding.execution_result_id,
                },
            },
            recorded_at=NOW,
            effective_at=NOW,
            correlation_id=binding.run_id,
            causation_id="event:authorization",
            event_id=EXECUTION_EVENT_ID,
        ),
        global_position=10,
    )


def _observed_event(outcome: OutcomeObservation) -> EventEnvelope:
    command = IngestObservation(
        STREAM_ID,
        0,
        "operator",
        outcome.observer_id,
        outcome.binding.run_id,
        (outcome_observation_input(outcome),),
        EXECUTION_EVENT_ID,
        outcome.domain,
    )
    return replace(
        observation_events(command, recorded_at=NOW + timedelta(seconds=2))[0],
        event_id=OUTCOME_EVENT_ID,
        global_position=11,
    )


def _ports(
    outcome: OutcomeObservation,
    event: EventEnvelope,
    *,
    execution_event: EventEnvelope | None = None,
    artifact_data: bytes | None | object = _DEFAULT_ARTIFACT,
) -> tuple[History, Artifacts]:
    execution = execution_event or _execution_event(outcome)
    if artifact_data is _DEFAULT_ARTIFACT:
        data = serialize_execution_result(_result()).encode()
    elif artifact_data is None:
        data = None
    elif isinstance(artifact_data, bytes):
        data = artifact_data
    else:  # pragma: no cover - test helper contract
        raise TypeError("artifact_data must be bytes or None")
    values: dict[str, bytes] = {} if data is None else {outcome.binding.execution_result_id: data}
    return History(execution, event), Artifacts(values)


def _bind(
    outcome: OutcomeObservation,
    event: EventEnvelope,
    *,
    execution_event: EventEnvelope | None = None,
    artifact_data: bytes | None | object = _DEFAULT_ARTIFACT,
):
    history, artifacts = _ports(
        outcome,
        event,
        execution_event=execution_event,
        artifact_data=artifact_data,
    )
    return bind_evaluation_observation(
        outcome,
        history,
        artifacts,
        execution_event_id=EXECUTION_EVENT_ID,
        outcome_event_ids=(event.event_id,),
    )


def _event_with(
    event: EventEnvelope,
    *,
    event_type: str | None = None,
    payload: Mapping[str, JsonInput] | None = None,
) -> EventEnvelope:
    return replace(
        EventEnvelope.create(
            stream_id=event.stream_id,
            stream_sequence=event.stream_sequence,
            event_type=event.event_type if event_type is None else event_type,
            actor=event.actor,
            source=event.source,
            payload=event.payload if payload is None else payload,
            schema_version=event.schema_version,
            recorded_at=event.recorded_at,
            effective_at=event.effective_at,
            correlation_id=event.correlation_id,
            causation_id=event.causation_id,
            idempotency_key=event.idempotency_key,
            event_id=event.event_id,
        ),
        global_position=event.global_position,
    )
