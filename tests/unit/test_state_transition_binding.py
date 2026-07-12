from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from blackcell.features.accept_state_transition import TransitionAcceptanceStatus
from blackcell.features.authorize_action import (
    ACTION_PROPOSAL_MEDIA_TYPE,
    AUTHORIZATION_DECISION_MEDIA_TYPE,
    ActionProposal,
    AuthorizationDecision,
    AuthorizationFinding,
    AuthorizationOutcome,
    decode_action_proposal,
    decode_authorization_decision,
    encode_action_proposal,
    encode_authorization_decision,
)
from blackcell.features.build_context import ContextFrame, serialize_context_frame
from blackcell.features.evaluate_outcome import (
    EVALUATION_SPEC_MEDIA_TYPE,
    OUTCOME_EVALUATION_MEDIA_TYPE,
    EvaluateOutcome,
    EvaluationAuthorizationOutcome,
    EvaluationCriterion,
    EvaluationExecutionStatus,
    EvaluationSpec,
    OutcomeEvaluation,
    OutcomeEvaluator,
    decode_outcome_evaluation,
    encode_evaluation_spec,
    encode_outcome_evaluation,
)
from blackcell.features.execute_affordance import (
    EXECUTION_PREPARATION_MEDIA_TYPE,
    AffordanceDefinition,
    AffordanceInvocation,
    ExecutionPreparation,
    ExecutionResult,
    ExecutionStatus,
    SideEffectClass,
    deserialize_execution_preparation,
    deserialize_execution_result,
    serialize_execution_preparation,
    serialize_execution_result,
)
from blackcell.features.ingest_observation import (
    EvidencePointer,
    IngestObservation,
    ObservationInput,
    ObservedClaim,
)
from blackcell.features.ingest_observation.events import observation_events
from blackcell.features.observe_outcome import (
    OUTCOME_OBSERVATION_MEDIA_TYPE,
    OutcomeArgument,
    OutcomeClaim,
    OutcomeEvidencePointer,
    OutcomeExecutionBinding,
    OutcomeObservation,
    OutcomeObservationStatus,
    encode_outcome_observation,
)
from blackcell.features.project_operational_state import (
    OPERATIONAL_STATE_SNAPSHOT_MEDIA_TYPE,
    OperationalBeliefState,
    OperationalStateScope,
    ProjectOperationalState,
    ProjectOperationalStateHandler,
    encode_operational_state_snapshot,
)
from blackcell.features.request_decision import (
    DECISION_ATTEMPT_MEDIA_TYPE,
    DECISION_REQUEST_MEDIA_TYPE,
    DECISION_RESPONSE_MEDIA_TYPE,
    DecisionAffordance,
    DecisionAttempt,
    DecisionBudget,
    DecisionCapability,
    DecisionClassification,
    DecisionLocality,
    DecisionProposal,
    DecisionRequirements,
    DecisionResponse,
    DecisionRoute,
    RequestDecision,
    decode_decision_request,
    encode_decision_attempt,
    encode_decision_request,
    encode_decision_response,
)
from blackcell.features.solve_constraints import (
    CONSTRAINT_EVALUATION_MEDIA_TYPE,
    ConstraintEvaluation,
    ConstraintOutcome,
    ConstraintProof,
    decode_constraint_evaluation,
    encode_constraint_evaluation,
)
from blackcell.kernel import (
    ArtifactRef,
    ArtifactStore,
    EventEnvelope,
    EventStore,
    JsonInput,
    ProjectionCheckpoint,
)
from blackcell.kernel._json import canonical_json_bytes, json_digest
from blackcell.workflows.outcome_evidence import (
    bind_evaluation_observation,
    inconclusive_outcome_event,
    outcome_observation_input,
)
from blackcell.workflows.run_protocol import (
    AUTHORIZATION_DECIDED,
    CONSTRAINTS_EVALUATED,
    CONTEXT_RECORDED,
    EVALUATION_RECORDED,
    EVALUATION_SPECIFIED,
    EXECUTION_RECORDED,
    INITIAL_STATE_RECORDED,
    MODEL_ATTEMPT_RECORDED,
    MODEL_REQUESTED,
    MODEL_RESPONDED,
    OUTCOME_OBSERVED,
    OUTCOME_STATE_RECORDED,
    PROPOSAL_RECORDED,
    RUN_STARTED,
    run_stream_id,
)
from blackcell.workflows.state_transition import (
    StateTransitionBindingError,
    StateTransitionNotReady,
    bind_and_accept_state_transition,
)

NOW = datetime(2026, 7, 12, 1, tzinfo=UTC)
EXECUTED_AT = NOW + timedelta(minutes=1)
OBSERVED_AT = NOW + timedelta(minutes=2)
EVALUATED_AT = NOW + timedelta(minutes=3)
RUN_ID = "run:transition:1"
RUN_STREAM = run_stream_id(RUN_ID)
DOMAIN = "repository"
OBSERVATION_STREAM = "observations:transition"
SOURCE = "blackcell.workflows.daily_operator"
ACTOR = "operator"
DIGEST = f"sha256:{'7' * 64}"


@dataclass(slots=True)
class Scenario:
    events: EventStore
    artifacts: ArtifactStore
    run_events: dict[str, EventEnvelope]
    initial_state: OperationalBeliefState
    outcome_state: OperationalBeliefState | None
    evaluation: OutcomeEvaluation
    owner_observation: OutcomeObservation | None


class NoCheckpoints:
    def load(
        self,
        projection_name: str,
        projection_version: int,
        *,
        stream_id: str | None = None,
    ) -> ProjectionCheckpoint | None:
        del projection_name, projection_version, stream_id
        return None

    def save(
        self,
        checkpoint: ProjectionCheckpoint,
        *,
        expected_position: int | None = None,
    ) -> ProjectionCheckpoint:
        del checkpoint, expected_position
        raise AssertionError("historical projection must not persist a checkpoint")


class HistoryView:
    def __init__(
        self,
        delegate: EventStore,
        replacements: Mapping[str, EventEnvelope] | None = None,
        *,
        stream_only: bool = False,
        prefix_length: int | None = None,
    ) -> None:
        self.delegate = delegate
        self.replacements = dict(replacements or {})
        self.stream_only = stream_only
        self.prefix_length = prefix_length

    def read_stream(
        self,
        stream_id: str,
        *,
        after_sequence: int = 0,
        limit: int | None = None,
    ) -> Sequence[EventEnvelope]:
        events = self.delegate.read_stream(
            stream_id,
            after_sequence=after_sequence,
            limit=limit,
        )
        if self.prefix_length is not None:
            events = events[: self.prefix_length]
        return tuple(self.replacements.get(item.event_id, item) for item in events)

    def read_all(
        self,
        *,
        after_position: int = 0,
        limit: int | None = None,
    ) -> Sequence[EventEnvelope]:
        events = self.delegate.read_all(after_position=after_position, limit=limit)
        if self.stream_only:
            return events
        return tuple(self.replacements.get(item.event_id, item) for item in events)

    def get(self, event_id: str) -> EventEnvelope | None:
        event = self.delegate.get(event_id)
        if event is None or self.stream_only:
            return event
        return self.replacements.get(event_id, event)


class EmptySlotHistory(HistoryView):
    def __init__(self, delegate: EventStore, empty_position: int) -> None:
        super().__init__(delegate)
        self.empty_position = empty_position

    def read_all(
        self,
        *,
        after_position: int = 0,
        limit: int | None = None,
    ) -> Sequence[EventEnvelope]:
        if limit == 1 and after_position == self.empty_position - 1:
            return ()
        return super().read_all(after_position=after_position, limit=limit)


class CorruptBytes:
    def __init__(self, delegate: ArtifactStore, digest: str) -> None:
        self.delegate = delegate
        self.digest = digest

    def stat(self, digest: str) -> ArtifactRef:
        return self.delegate.stat(digest)

    def get_bytes(self, digest: str, *, verify: bool = True) -> bytes:
        data = self.delegate.get_bytes(digest, verify=verify)
        return data + b"x" if digest == self.digest else data


class RunWriter:
    def __init__(self, events: EventStore) -> None:
        self.events = events
        self.sequence = 0
        self.last: EventEnvelope | None = None

    def append(
        self,
        event_type: str,
        payload: Mapping[str, JsonInput],
        *,
        recorded_at: datetime = NOW,
    ) -> EventEnvelope:
        self.sequence += 1
        event = EventEnvelope.create(
            stream_id=RUN_STREAM,
            stream_sequence=self.sequence,
            event_type=event_type,
            schema_version=2,
            actor=ACTOR,
            source=SOURCE,
            payload={"run_id": RUN_ID, **payload},
            recorded_at=recorded_at,
            effective_at=recorded_at,
            correlation_id=RUN_ID,
            causation_id=None if self.last is None else self.last.event_id,
        )
        stored = self.events.append(event, expected_sequence=self.sequence - 1)
        self.last = stored
        return stored


@pytest.mark.parametrize(
    ("branch", "status", "code"),
    (
        ("pass", TransitionAcceptanceStatus.ACCEPTED, "definitive-outcome-evidence-accepted"),
        ("fail", TransitionAcceptanceStatus.ACCEPTED, "definitive-outcome-evidence-accepted"),
        ("low-confidence", TransitionAcceptanceStatus.NOT_ACCEPTED, "evaluation-inconclusive"),
        (
            "observer-inconclusive",
            TransitionAcceptanceStatus.NOT_ACCEPTED,
            "evaluation-inconclusive",
        ),
        ("unknown", TransitionAcceptanceStatus.NOT_ACCEPTED, "execution-unknown"),
        ("deny", TransitionAcceptanceStatus.NOT_ACCEPTED, "evaluation-not-evaluated"),
        ("approval", TransitionAcceptanceStatus.NOT_ACCEPTED, "evaluation-not-evaluated"),
    ),
)
def test_binder_reconstructs_every_material_evaluation_branch(
    tmp_path: Path,
    branch: str,
    status: TransitionAcceptanceStatus,
    code: str,
) -> None:
    scenario = _scenario(tmp_path, branch=branch)

    result = bind_and_accept_state_transition(
        RUN_ID,
        scenario.events,
        scenario.artifacts,
    )

    assert result.status is status
    assert result.code == code
    assert (result.transition is not None) is (status is TransitionAcceptanceStatus.ACCEPTED)
    if result.transition is not None:
        assert result.transition.evaluation.verdict.value == branch
        assert result.transition.accepted_claim_ids == ("claim:outcome",)
    if branch == "low-confidence":
        assert scenario.owner_observation is not None
        assert scenario.owner_observation.status is OutcomeObservationStatus.OBSERVED
        assert scenario.outcome_state is not None
        assert scenario.outcome_state.last_source_stream_sequence == 2
    if branch == "observer-inconclusive":
        assert scenario.owner_observation is not None
        assert scenario.owner_observation.status is OutcomeObservationStatus.INCONCLUSIVE
        assert scenario.outcome_state is not None
        assert scenario.outcome_state.last_source_stream_sequence == 1


def test_concurrent_unrelated_evidence_is_replayed_but_never_cited(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path, branch="pass", unrelated=True)

    result = bind_and_accept_state_transition(RUN_ID, scenario.events, scenario.artifacts)

    assert result.transition is not None
    assert result.transition.accepted_source_event_ids == (
        scenario.run_events["outcome-source"].event_id,
    )
    assert all(
        item.event_id != scenario.run_events["unrelated"].event_id
        for item in result.transition.triggering_events
    )


def test_incomplete_valid_prefix_is_not_ready_and_empty_run_is_not_ready(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path, branch="pass")
    prefix = HistoryView(
        scenario.events,
        prefix_length=scenario.run_events[AUTHORIZATION_DECIDED].stream_sequence,
    )

    with pytest.raises(StateTransitionNotReady, match="not recorded"):
        bind_and_accept_state_transition(RUN_ID, prefix, scenario.artifacts)

    other = EventStore(tmp_path / "empty.sqlite3")
    with pytest.raises(StateTransitionNotReady, match="not started"):
        bind_and_accept_state_transition("run:absent", other, scenario.artifacts)


def test_stream_history_must_prove_the_same_global_occurrence(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path, branch="pass")
    original = scenario.run_events[PROPOSAL_RECORDED]
    forged = _rehash(original, actor="other-actor")
    history = HistoryView(
        scenario.events,
        {original.event_id: forged},
        stream_only=True,
    )

    with pytest.raises(StateTransitionBindingError, match=r"source or actor|selected occurrence"):
        bind_and_accept_state_transition(RUN_ID, history, scenario.artifacts)


def test_occurrence_lookup_and_global_slot_are_both_required(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path, branch="pass")
    original = scenario.run_events[PROPOSAL_RECORDED]
    forged = _rehash(original, payload={**original.payload, "extra": True})
    with pytest.raises(StateTransitionBindingError, match="selected occurrence"):
        bind_and_accept_state_transition(
            RUN_ID,
            HistoryView(
                scenario.events,
                {original.event_id: forged},
                stream_only=True,
            ),
            scenario.artifacts,
        )

    position = cast("int", original.global_position)
    with pytest.raises(StateTransitionBindingError, match="claimed global position"):
        bind_and_accept_state_transition(
            RUN_ID,
            EmptySlotHistory(scenario.events, position),
            scenario.artifacts,
        )


def test_run_correlation_and_stored_position_are_required(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path, branch="pass")
    original = scenario.run_events[CONTEXT_RECORDED]
    wrong_correlation = _rehash(original, correlation_id="run:other")
    with pytest.raises(StateTransitionBindingError, match="correlation"):
        bind_and_accept_state_transition(
            RUN_ID,
            HistoryView(scenario.events, {original.event_id: wrong_correlation}),
            scenario.artifacts,
        )

    unstored = replace(original, global_position=None)
    with pytest.raises(StateTransitionBindingError, match="stored ledger occurrence"):
        bind_and_accept_state_transition(
            RUN_ID,
            HistoryView(scenario.events, {original.event_id: unstored}),
            scenario.artifacts,
        )


def test_exact_artifact_metadata_rejects_rehashed_logical_identity(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path, branch="pass")
    original = scenario.run_events[PROPOSAL_RECORDED]
    artifact = cast("Mapping[str, JsonInput]", original.payload["artifact"])
    forged = _rehash(
        original,
        payload={
            **original.payload,
            "artifact": {**artifact, "logical_id": DIGEST},
        },
    )

    with pytest.raises(StateTransitionBindingError, match="logical ID"):
        bind_and_accept_state_transition(
            RUN_ID,
            HistoryView(scenario.events, {original.event_id: forged}),
            scenario.artifacts,
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("missing", "lacks its owner artifact"),
        ("scalar", "artifact is not an object"),
        ("extra-field", "fields are not exact"),
        ("encoding", "encoding is invalid"),
        ("size", "size is invalid"),
        ("media", "persisted artifact metadata"),
        ("schema", "artifact schema is incompatible"),
    ),
)
def test_artifact_links_fail_closed_on_shape_type_and_contract(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    scenario = _scenario(tmp_path, branch="pass")
    original = scenario.run_events[PROPOSAL_RECORDED]
    artifact = dict(cast("Mapping[str, JsonInput]", original.payload["artifact"]))
    payload = cast("dict[str, JsonInput]", dict(original.payload))
    if mutation == "missing":
        del payload["artifact"]
    elif mutation == "scalar":
        payload["artifact"] = "not-an-object"
    elif mutation == "extra-field":
        payload["artifact"] = {**artifact, "extra": True}
    elif mutation == "encoding":
        payload["artifact"] = {**artifact, "encoding": ""}
    elif mutation == "size":
        payload["artifact"] = {**artifact, "size_bytes": -1}
    elif mutation == "media":
        payload["artifact"] = {**artifact, "media_type": "application/json"}
    else:
        payload["artifact"] = {**artifact, "schema_version": "action-proposal/v999"}
    forged = _rehash(original, payload=cast("Mapping[str, JsonInput]", payload))

    with pytest.raises(StateTransitionBindingError, match=message):
        bind_and_accept_state_transition(
            RUN_ID,
            HistoryView(scenario.events, {original.event_id: forged}),
            scenario.artifacts,
        )


def test_artifact_bytes_are_rechecked_against_link_size_and_digest(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path, branch="pass")
    artifact = cast(
        "Mapping[str, object]",
        scenario.run_events[PROPOSAL_RECORDED].payload["artifact"],
    )
    digest = cast("str", artifact["digest"])

    with pytest.raises(StateTransitionBindingError, match="content address"):
        bind_and_accept_state_transition(
            RUN_ID,
            scenario.events,
            CorruptBytes(scenario.artifacts, digest),
        )


@pytest.mark.parametrize(
    ("column", "value"),
    (
        ("media_type", "application/x-forged"),
        ("encoding", "utf-16"),
    ),
)
def test_event_link_must_equal_persisted_artifact_metadata(
    tmp_path: Path,
    column: str,
    value: str,
) -> None:
    scenario = _scenario(tmp_path, branch="pass")
    artifact = cast(
        "Mapping[str, object]",
        scenario.run_events[PROPOSAL_RECORDED].payload["artifact"],
    )
    digest = cast("str", artifact["digest"])
    with closing(sqlite3.connect(scenario.artifacts.database_path)) as connection, connection:
        connection.execute(
            f"update kernel_artifacts set {column} = ? where digest = ?",
            (value, digest),
        )

    with pytest.raises(StateTransitionBindingError, match="persisted artifact metadata"):
        bind_and_accept_state_transition(RUN_ID, scenario.events, scenario.artifacts)


def test_snapshot_must_equal_exact_ledger_replay_not_only_decode(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path, branch="pass")
    state = scenario.initial_state
    claim = state.claims[0]
    forged_state = replace(state, claims=(replace(claim, value="forged"),))
    data = encode_operational_state_snapshot(forged_state)
    reference = scenario.artifacts.put_bytes(
        data,
        media_type=OPERATIONAL_STATE_SNAPSHOT_MEDIA_TYPE,
        encoding="utf-8",
    )
    original = scenario.run_events[INITIAL_STATE_RECORDED]
    forged = _rehash(
        original,
        payload={
            **original.payload,
            "snapshot_digest": reference.digest,
            "artifact": _link(
                reference,
                "operational-state-snapshot/v1",
                reference.digest,
            ),
        },
    )

    with pytest.raises(StateTransitionBindingError, match="exact ledger replay"):
        bind_and_accept_state_transition(
            RUN_ID,
            HistoryView(scenario.events, {original.event_id: forged}),
            scenario.artifacts,
        )


def test_spec_and_state_owner_logical_ids_are_not_interchangeable(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path, branch="pass")
    for event_type, message in (
        (EVALUATION_SPECIFIED, "EvaluationSpec logical identity"),
        (INITIAL_STATE_RECORDED, "initial state logical ID"),
    ):
        original = scenario.run_events[event_type]
        artifact = cast("Mapping[str, JsonInput]", original.payload["artifact"])
        forged = _rehash(
            original,
            payload={
                **original.payload,
                "artifact": {**artifact, "logical_id": DIGEST},
            },
        )
        with pytest.raises(StateTransitionBindingError, match=message):
            bind_and_accept_state_transition(
                RUN_ID,
                HistoryView(scenario.events, {original.event_id: forged}),
                scenario.artifacts,
            )


def test_evaluation_spec_objective_is_bound_to_run_start(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path, branch="pass")
    spec = EvaluationSpec(
        "repository-ready",
        "different objective",
        _spec().criteria,
    )
    reference = _put(
        scenario.artifacts,
        encode_evaluation_spec(spec),
        EVALUATION_SPEC_MEDIA_TYPE,
    )
    original = scenario.run_events[EVALUATION_SPECIFIED]
    forged = _rehash(
        original,
        payload={
            **original.payload,
            "evaluation_spec_id": spec.spec_id,
            "evaluation_spec_digest": reference.digest,
            "artifact": _link(reference, spec.schema_version, spec.spec_id),
        },
    )

    with pytest.raises(StateTransitionBindingError, match="objective differs"):
        bind_and_accept_state_transition(
            RUN_ID,
            HistoryView(scenario.events, {original.event_id: forged}),
            scenario.artifacts,
        )


@pytest.mark.parametrize("field", ("context_payload", "evidence_event_ids"))
def test_model_request_uses_exact_owner_context_projection(
    tmp_path: Path,
    field: str,
) -> None:
    scenario = _scenario(tmp_path, branch="pass")
    original = scenario.run_events[MODEL_REQUESTED]
    artifact = cast("Mapping[str, object]", original.payload["artifact"])
    request = decode_decision_request(
        scenario.artifacts.get_bytes(cast("str", artifact["digest"])),
        expected_request_digest=cast("str", artifact["digest"]),
    )
    forged_request = (
        replace(request, context_payload='{"forged":true}')
        if field == "context_payload"
        else replace(request, evidence_event_ids=(scenario.run_events[RUN_STARTED].event_id,))
    )
    reference = _put(
        scenario.artifacts,
        encode_decision_request(forged_request),
        DECISION_REQUEST_MEDIA_TYPE,
    )
    forged = _rehash(
        original,
        payload={
            **original.payload,
            "request_digest": forged_request.request_digest,
            "artifact": _link(
                reference,
                forged_request.schema_version,
                forged_request.request_digest,
            ),
        },
    )

    with pytest.raises(StateTransitionBindingError, match="causal ContextFrame"):
        bind_and_accept_state_transition(
            RUN_ID,
            HistoryView(scenario.events, {original.event_id: forged}),
            scenario.artifacts,
        )


def test_initial_snapshot_scope_and_cutoff_are_bound_to_run(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path, branch="pass")
    start = scenario.run_events[RUN_STARTED]
    wrong_domain = _rehash(start, payload={**start.payload, "domain": "other-domain"})
    with pytest.raises(StateTransitionBindingError, match="scope differs"):
        bind_and_accept_state_transition(
            RUN_ID,
            HistoryView(scenario.events, {start.event_id: wrong_domain}),
            scenario.artifacts,
        )

    initial = scenario.run_events[INITIAL_STATE_RECORDED]
    outcome = scenario.run_events[OUTCOME_STATE_RECORDED]
    artifact = cast("Mapping[str, JsonInput]", outcome.payload["artifact"])
    late_state = _rehash(
        initial,
        payload={
            **initial.payload,
            **{
                key: outcome.payload[key]
                for key in (
                    "snapshot_digest",
                    "domain",
                    "stream_id",
                    "cutoff_global_position",
                    "last_source_stream_sequence",
                    "effective_time_cutoff",
                )
            },
            "artifact": artifact,
        },
    )
    with pytest.raises(StateTransitionBindingError, match="cutoff must precede"):
        bind_and_accept_state_transition(
            RUN_ID,
            HistoryView(scenario.events, {initial.event_id: late_state}),
            scenario.artifacts,
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("link", "ContextFrame link"),
        ("schema", "unsupported ContextFrame schema"),
        ("content", "content differs"),
        ("effective", "effective-time identity"),
    ),
)
def test_context_frame_identity_is_bound_to_initial_state(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    scenario = _scenario(tmp_path, branch="pass")
    original = scenario.run_events[CONTEXT_RECORDED]
    link = dict(cast("Mapping[str, JsonInput]", original.payload["artifact"]))
    if mutation == "link":
        forged = _rehash(
            original,
            payload={**original.payload, "frame_id": DIGEST},
        )
    else:
        data = scenario.artifacts.get_bytes(cast("str", link["digest"]))
        payload = cast("dict[str, object]", json.loads(data))
        if mutation == "schema":
            payload["schema_version"] = "context-frame/v999"
            schema = "context-frame/v999"
        elif mutation == "content":
            payload["state_stream_position"] = 0
            schema = cast("str", link["schema_version"])
        else:
            payload["state_effective_time"] = (NOW + timedelta(days=1)).isoformat()
            schema = cast("str", link["schema_version"])
        reference = _put(
            scenario.artifacts,
            canonical_json_bytes(payload),
            "application/vnd.blackcell.context-frame+json",
        )
        forged = _rehash(
            original,
            payload={
                **original.payload,
                "frame_id": reference.digest,
                "artifact": _link(reference, schema, reference.digest),
            },
        )
    with pytest.raises(StateTransitionBindingError, match=message):
        bind_and_accept_state_transition(
            RUN_ID,
            HistoryView(scenario.events, {original.event_id: forged}),
            scenario.artifacts,
        )


@pytest.mark.parametrize(
    ("event_type", "message"),
    (
        (MODEL_REQUESTED, "decision request link identity"),
        (MODEL_ATTEMPT_RECORDED, "decision attempt link identity"),
        (MODEL_RESPONDED, "decision response link identity"),
        (CONSTRAINTS_EVALUATED, "ConstraintEvaluation logical ID"),
        (AUTHORIZATION_DECIDED, "AuthorizationDecision logical ID"),
        (EXECUTION_RECORDED, "ExecutionResult logical ID"),
        (OUTCOME_OBSERVED, "OutcomeObservation logical ID"),
        (EVALUATION_RECORDED, "OutcomeEvaluation logical ID"),
    ),
)
def test_every_owner_artifact_has_an_exact_logical_identity(
    tmp_path: Path,
    event_type: str,
    message: str,
) -> None:
    scenario = _scenario(tmp_path, branch="pass")
    original = scenario.run_events[event_type]
    artifact = cast("Mapping[str, JsonInput]", original.payload["artifact"])
    forged = _rehash(
        original,
        payload={
            **original.payload,
            "artifact": {**artifact, "logical_id": DIGEST},
        },
    )

    with pytest.raises(StateTransitionBindingError, match=message):
        bind_and_accept_state_transition(
            RUN_ID,
            HistoryView(scenario.events, {original.event_id: forged}),
            scenario.artifacts,
        )


@pytest.mark.parametrize(
    ("data", "message"),
    (
        (b"{", "canonical JSON"),
        (b'{ "schema_version": "context-frame/v4" }', "canonical JSON"),
    ),
)
def test_context_frame_requires_canonical_utf8_json(
    tmp_path: Path,
    data: bytes,
    message: str,
) -> None:
    scenario = _scenario(tmp_path, branch="pass")
    reference = _put(
        scenario.artifacts,
        data,
        "application/vnd.blackcell.context-frame+json",
    )
    original = scenario.run_events[CONTEXT_RECORDED]
    forged = _rehash(
        original,
        payload={
            **original.payload,
            "frame_id": reference.digest,
            "artifact": _link(reference, "context-frame/v4", reference.digest),
        },
    )

    with pytest.raises(StateTransitionBindingError, match=message):
        bind_and_accept_state_transition(
            RUN_ID,
            HistoryView(scenario.events, {original.event_id: forged}),
            scenario.artifacts,
        )


@pytest.mark.parametrize(
    ("value", "message"),
    (
        ("event:not-an-array", "must be an array"),
        (("",), "non-empty strings"),
        (("event:one", "event:one"), "must be unique"),
        (("event:one", "event:two"), "exactly one domain event"),
        (("event:absent",), "absent from the ledger"),
    ),
)
def test_outcome_event_identifiers_are_exact_unique_ledger_references(
    tmp_path: Path,
    value: JsonInput,
    message: str,
) -> None:
    scenario = _scenario(tmp_path, branch="pass")
    original = scenario.run_events[OUTCOME_OBSERVED]
    forged = _rehash(
        original,
        payload={**original.payload, "outcome_event_ids": value},
    )

    with pytest.raises(StateTransitionBindingError, match=message):
        bind_and_accept_state_transition(
            RUN_ID,
            HistoryView(scenario.events, {original.event_id: forged}),
            scenario.artifacts,
        )


def test_model_response_and_action_proposal_must_have_the_same_semantics(
    tmp_path: Path,
) -> None:
    scenario = _scenario(tmp_path, branch="pass")
    original = scenario.run_events[PROPOSAL_RECORDED]
    artifact = cast("Mapping[str, object]", original.payload["artifact"])
    proposal = decode_action_proposal(scenario.artifacts.get_bytes(cast("str", artifact["digest"])))
    forged_proposal = replace(proposal, rationale="different verified rationale")
    reference = _put(
        scenario.artifacts,
        encode_action_proposal(forged_proposal),
        ACTION_PROPOSAL_MEDIA_TYPE,
    )
    forged = _rehash(
        original,
        payload={
            **original.payload,
            "proposal_digest": forged_proposal.proposal_digest,
            "action_digest": forged_proposal.action_digest,
            "artifact": _link(
                reference,
                forged_proposal.schema_version,
                forged_proposal.proposal_digest,
            ),
        },
    )

    with pytest.raises(StateTransitionBindingError, match="differs from verified model response"):
        bind_and_accept_state_transition(
            RUN_ID,
            HistoryView(scenario.events, {original.event_id: forged}),
            scenario.artifacts,
        )


def test_constraint_proof_cannot_escape_context_provenance(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path, branch="pass")
    original = scenario.run_events[CONSTRAINTS_EVALUATED]
    artifact = cast("Mapping[str, object]", original.payload["artifact"])
    evaluation = decode_constraint_evaluation(
        scenario.artifacts.get_bytes(cast("str", artifact["digest"]))
    )
    proof = replace(
        evaluation.proofs[0],
        evidence_event_ids=(scenario.run_events[RUN_STARTED].event_id,),
    )
    forged_evaluation = ConstraintEvaluation(
        evaluation.context_frame_id,
        (proof,),
        evaluation.evaluated_at,
    )
    reference = _put(
        scenario.artifacts,
        encode_constraint_evaluation(forged_evaluation),
        CONSTRAINT_EVALUATION_MEDIA_TYPE,
    )
    forged = _rehash(
        original,
        payload={
            **original.payload,
            "evaluation_id": forged_evaluation.evaluation_id,
            "proof_ids": (proof.proof_id,),
            "artifact": _link(
                reference,
                forged_evaluation.schema_version,
                forged_evaluation.evaluation_id,
            ),
        },
    )

    with pytest.raises(StateTransitionBindingError, match="outside ContextFrame"):
        bind_and_accept_state_transition(
            RUN_ID,
            HistoryView(scenario.events, {original.event_id: forged}),
            scenario.artifacts,
        )


def test_authorization_cannot_change_proposal_digest_after_recording(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path, branch="pass")
    original = scenario.run_events[AUTHORIZATION_DECIDED]
    artifact = cast("Mapping[str, object]", original.payload["artifact"])
    decision = decode_authorization_decision(
        scenario.artifacts.get_bytes(cast("str", artifact["digest"]))
    )
    forged_decision = replace(decision, proposal_digest=DIGEST)
    reference = _put(
        scenario.artifacts,
        encode_authorization_decision(forged_decision),
        AUTHORIZATION_DECISION_MEDIA_TYPE,
    )
    forged = _rehash(
        original,
        payload={
            **original.payload,
            "decision_id": forged_decision.decision_id,
            "artifact": _link(
                reference,
                forged_decision.schema_version,
                forged_decision.decision_id,
            ),
        },
    )

    with pytest.raises(StateTransitionBindingError, match="cross-identity"):
        bind_and_accept_state_transition(
            RUN_ID,
            HistoryView(scenario.events, {original.event_id: forged}),
            scenario.artifacts,
        )


def test_authorization_artifact_cannot_be_recorded_post_hoc(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path, branch="pass")
    original = scenario.run_events[AUTHORIZATION_DECIDED]
    artifact = cast("Mapping[str, object]", original.payload["artifact"])
    decision = decode_authorization_decision(
        scenario.artifacts.get_bytes(cast("str", artifact["digest"]))
    )
    post_hoc = replace(decision, evaluated_at=NOW + timedelta(seconds=30))
    reference = _put(
        scenario.artifacts,
        encode_authorization_decision(post_hoc),
        AUTHORIZATION_DECISION_MEDIA_TYPE,
    )
    forged = _rehash(
        original,
        payload={
            **original.payload,
            "decision_id": post_hoc.decision_id,
            "artifact": _link(reference, post_hoc.schema_version, post_hoc.decision_id),
        },
    )

    with pytest.raises(StateTransitionBindingError, match="precede authorization evaluation"):
        bind_and_accept_state_transition(
            RUN_ID,
            HistoryView(scenario.events, {original.event_id: forged}),
            scenario.artifacts,
        )


@pytest.mark.parametrize("mutation", ("swapped-invocation", "forged-run"))
def test_execution_preparation_cannot_be_swapped_or_forged(
    tmp_path: Path,
    mutation: str,
) -> None:
    scenario = _scenario(tmp_path, branch="pass")
    original = scenario.run_events[EXECUTION_RECORDED]
    link = cast("Mapping[str, object]", original.payload["preparation_artifact"])
    preparation = deserialize_execution_preparation(
        scenario.artifacts.get_bytes(cast("str", link["digest"])),
        expected_preparation_id=cast("str", link["digest"]),
    )
    forged_preparation = (
        replace(
            preparation,
            invocation=replace(
                preparation.invocation,
                invocation_id="invocation:swapped",
                idempotency_key="execute:swapped",
            ),
        )
        if mutation == "swapped-invocation"
        else replace(preparation, run_id="run:forged")
    )
    reference = _put(
        scenario.artifacts,
        serialize_execution_preparation(forged_preparation).encode(),
        EXECUTION_PREPARATION_MEDIA_TYPE,
    )
    forged = _rehash(
        original,
        payload={
            **original.payload,
            "preparation_id": forged_preparation.preparation_id,
            "preparation_artifact": _link(
                reference,
                forged_preparation.schema_version,
                forged_preparation.preparation_id,
            ),
        },
    )

    with pytest.raises(StateTransitionBindingError, match="ExecutionPreparation differs"):
        bind_and_accept_state_transition(
            RUN_ID,
            HistoryView(scenario.events, {original.event_id: forged}),
            scenario.artifacts,
        )


@pytest.mark.parametrize("field", ("preparation_id", "preparation_artifact"))
def test_execution_event_requires_both_preparation_owner_fields(
    tmp_path: Path,
    field: str,
) -> None:
    scenario = _scenario(tmp_path, branch="pass")
    original = scenario.run_events[EXECUTION_RECORDED]
    payload = cast("dict[str, JsonInput]", dict(original.payload))
    del payload[field]
    forged = _rehash(original, payload=payload)

    with pytest.raises(StateTransitionBindingError, match="preparation"):
        bind_and_accept_state_transition(
            RUN_ID,
            HistoryView(scenario.events, {original.event_id: forged}),
            scenario.artifacts,
        )


def test_execution_cannot_start_before_authorization(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path, branch="pass")
    original = scenario.run_events[EXECUTION_RECORDED]
    artifact = cast("Mapping[str, object]", original.payload["artifact"])
    result = deserialize_execution_result(
        scenario.artifacts.get_bytes(cast("str", artifact["digest"])),
        expected_result_id=cast("str", artifact["digest"]),
    )
    preauthorized = replace(result, started_at=NOW - timedelta(seconds=1))
    reference = _put(
        scenario.artifacts,
        serialize_execution_result(preauthorized).encode(),
        "application/vnd.blackcell.execution-result+json",
    )
    forged = _rehash(
        original,
        payload={
            **original.payload,
            "result_id": preauthorized.result_id,
            "artifact": _link(
                reference,
                preauthorized.schema_version,
                preauthorized.result_id,
            ),
        },
    )

    with pytest.raises(StateTransitionBindingError, match="authorized action"):
        bind_and_accept_state_transition(
            RUN_ID,
            HistoryView(scenario.events, {original.event_id: forged}),
            scenario.artifacts,
        )


def test_evaluation_cannot_be_recorded_before_it_was_computed(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path, branch="pass")
    original = scenario.run_events[EVALUATION_RECORDED]
    forged = _rehash(original, recorded_at=NOW)

    with pytest.raises(StateTransitionBindingError, match="precedes evaluation time"):
        bind_and_accept_state_transition(
            RUN_ID,
            HistoryView(scenario.events, {original.event_id: forged}),
            scenario.artifacts,
        )


def test_required_text_fields_fail_closed_when_blank(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path, branch="pass")
    original = scenario.run_events[CONTEXT_RECORDED]
    forged = _rehash(original, payload={**original.payload, "task_id": ""})

    with pytest.raises(StateTransitionBindingError, match="non-empty string"):
        bind_and_accept_state_transition(
            RUN_ID,
            HistoryView(scenario.events, {original.event_id: forged}),
            scenario.artifacts,
        )


def test_rehashed_evaluation_cannot_change_the_initial_cutoff(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path, branch="pass")
    forged_evaluation = replace(
        scenario.evaluation,
        initial_state_position=scenario.evaluation.initial_state_position + 1,
    )
    data = encode_outcome_evaluation(forged_evaluation)
    reference = scenario.artifacts.put_bytes(
        data,
        media_type=OUTCOME_EVALUATION_MEDIA_TYPE,
        encoding="utf-8",
    )
    original = scenario.run_events[EVALUATION_RECORDED]
    forged = _rehash(
        original,
        payload={
            **original.payload,
            "evaluation_id": forged_evaluation.evaluation_id,
            "artifact": _link(
                reference,
                forged_evaluation.schema_version,
                forged_evaluation.evaluation_id,
            ),
        },
    )

    with pytest.raises(StateTransitionBindingError, match="deterministic replay"):
        bind_and_accept_state_transition(
            RUN_ID,
            HistoryView(scenario.events, {original.event_id: forged}),
            scenario.artifacts,
        )


def test_execution_event_cannot_relabel_verified_result_arguments(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path, branch="pass")
    original = scenario.run_events[EXECUTION_RECORDED]
    forged = _rehash(
        original,
        payload={**original.payload, "adapter_contract_version": "adapter/v999"},
    )

    with pytest.raises(StateTransitionBindingError, match=r"cross-identity|differs"):
        bind_and_accept_state_transition(
            RUN_ID,
            HistoryView(scenario.events, {original.event_id: forged}),
            scenario.artifacts,
        )


def test_owner_observation_artifact_is_bound_to_run_event_identity(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path, branch="pass")
    original = scenario.run_events[OUTCOME_OBSERVED]
    forged = _rehash(
        original,
        payload={**original.payload, "observation_digest": DIGEST},
    )

    with pytest.raises(StateTransitionBindingError, match="owner evidence"):
        bind_and_accept_state_transition(
            RUN_ID,
            HistoryView(scenario.events, {original.event_id: forged}),
            scenario.artifacts,
        )


def test_noncanonical_source_and_v1_history_fail_closed(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path, branch="pass")
    original = scenario.run_events[CONTEXT_RECORDED]
    forged = _rehash(original, source="fixture.forged")
    with pytest.raises(StateTransitionBindingError, match="source or actor"):
        bind_and_accept_state_transition(
            RUN_ID,
            HistoryView(scenario.events, {original.event_id: forged}),
            scenario.artifacts,
        )

    start = scenario.run_events[RUN_STARTED]
    v1 = _rehash(
        start,
        schema_version=1,
        payload={**start.payload, "workflow_version": "daily-operator/v1"},
    )
    with pytest.raises(StateTransitionBindingError, match="daily-operator/v2"):
        bind_and_accept_state_transition(
            RUN_ID,
            HistoryView(scenario.events, {start.event_id: v1}, prefix_length=1),
            scenario.artifacts,
        )


def test_decoder_sees_stored_evaluation_before_binder_recomputes_it(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path, branch="pass")
    event = scenario.run_events[EVALUATION_RECORDED]
    artifact = cast("Mapping[str, object]", event.payload["artifact"])
    stored = decode_outcome_evaluation(
        scenario.artifacts.get_bytes(cast("str", artifact["digest"])),
        spec=_spec(),
    )
    assert stored == scenario.evaluation


def _scenario(
    tmp_path: Path,
    *,
    branch: str,
    unrelated: bool = False,
) -> Scenario:
    database = tmp_path / "kernel.sqlite3"
    events = EventStore(database)
    artifacts = ArtifactStore(tmp_path / "artifacts", database_path=database)
    writer = RunWriter(events)
    recorded: dict[str, EventEnvelope] = {}
    request_digest = json_digest({"run_id": RUN_ID, "request": "daily"})
    recorded[RUN_STARTED] = writer.append(
        RUN_STARTED,
        {
            "request_digest": request_digest,
            "workflow": "daily-operator",
            "workflow_version": "daily-operator/v2",
            "task_id": "task:daily",
            "objective": "verify repository readiness",
            "domain": DOMAIN,
            "observation_stream_id": OBSERVATION_STREAM,
        },
    )
    spec = _spec(low_confidence=branch == "low-confidence")
    spec_ref = _put(
        artifacts,
        encode_evaluation_spec(spec),
        EVALUATION_SPEC_MEDIA_TYPE,
    )
    recorded[EVALUATION_SPECIFIED] = writer.append(
        EVALUATION_SPECIFIED,
        {
            "evaluation_spec_id": spec.spec_id,
            "evaluation_spec_digest": spec_ref.digest,
            "request_digest": request_digest,
            "artifact": _link(spec_ref, spec.schema_version, spec.spec_id),
        },
    )

    initial_event = observation_events(
        IngestObservation(
            OBSERVATION_STREAM,
            0,
            ACTOR,
            "fixture.initial",
            RUN_ID,
            (
                ObservationInput(
                    "observation:initial",
                    NOW,
                    (ObservedClaim("claim:initial", "project:blackcell", "ready", False),),
                    (EvidencePointer(locator="fixture://initial"),),
                ),
            ),
            recorded[RUN_STARTED].event_id,
            DOMAIN,
        ),
        recorded_at=NOW,
    )[0]
    initial_event = events.append(initial_event, expected_sequence=0)
    initial_state = _project(events, len(events), NOW)
    initial_ref = _put(
        artifacts,
        encode_operational_state_snapshot(initial_state),
        OPERATIONAL_STATE_SNAPSHOT_MEDIA_TYPE,
    )
    recorded[INITIAL_STATE_RECORDED] = writer.append(
        INITIAL_STATE_RECORDED,
        {
            **_state_payload(initial_state),
            "snapshot_digest": initial_ref.digest,
            "artifact": _link(initial_ref, "operational-state-snapshot/v1", initial_ref.digest),
        },
    )

    frame = ContextFrame(
        task_id="task:daily",
        objective="verify repository readiness",
        generated_at=NOW,
        source_packet_id="packet:transition",
        source_packet_purpose="daily-operator",
        source_selection_id="selection:transition",
        state_domain=DOMAIN,
        state_stream_id=OBSERVATION_STREAM,
        state_global_position=initial_state.cutoff_global_position,
        state_stream_position=initial_state.last_source_stream_sequence,
        source_claim_identities=(),
        evidence=(),
        provenance_event_ids=(),
        omissions=(),
        model_payload_characters=0,
        schema_version="context-frame/v4",
        state_effective_time=NOW,
    )
    frame_ref = _put(
        artifacts,
        serialize_context_frame(frame).encode(),
        "application/vnd.blackcell.context-frame+json",
    )
    recorded[CONTEXT_RECORDED] = writer.append(
        CONTEXT_RECORDED,
        {
            "frame_id": frame.frame_id,
            "task_id": frame.task_id,
            "state_domain": DOMAIN,
            "state_stream_id": OBSERVATION_STREAM,
            "state_global_position": initial_state.cutoff_global_position,
            "state_stream_position": initial_state.last_source_stream_sequence,
            "source_packet_id": frame.source_packet_id,
            "source_selection_id": frame.source_selection_id,
            "artifact": _link(frame_ref, frame.schema_version, frame.frame_id),
        },
    )

    request = RequestDecision(
        DecisionRequirements(
            "decision:transition",
            "node:planner",
            DecisionCapability.REASON,
            DecisionClassification.PRIVATE,
            DecisionLocality.LOCAL_ONLY,
            DecisionBudget(100, 20, 1_000, 100),
            1,
            True,
            NOW,
        ),
        RUN_ID,
        RUN_ID,
        recorded[CONTEXT_RECORDED].event_id,
        frame.frame_id,
        frame.objective,
        frame.model_payload,
        (),
        (DecisionAffordance("inspect"),),
    )
    request_ref = _put(artifacts, encode_decision_request(request), DECISION_REQUEST_MEDIA_TYPE)
    recorded[MODEL_REQUESTED] = writer.append(
        MODEL_REQUESTED,
        {
            "request_id": request.request_id,
            "request_digest": request.request_digest,
            "context_frame_id": frame.frame_id,
            "artifact": _link(request_ref, request.schema_version, request.request_digest),
        },
    )
    route = DecisionRoute(
        "profile:local",
        "adapter:recorded",
        "model:fixture",
        DecisionCapability.REASON,
        True,
        True,
        NOW,
    )
    attempt = DecisionAttempt(
        request.request_id,
        request.request_digest,
        route.route_id,
        1,
        NOW,
    )
    attempt_ref = _put(artifacts, encode_decision_attempt(attempt), DECISION_ATTEMPT_MEDIA_TYPE)
    recorded[MODEL_ATTEMPT_RECORDED] = writer.append(
        MODEL_ATTEMPT_RECORDED,
        {
            "attempt_id": attempt.attempt_id,
            "request_id": attempt.request_id,
            "request_digest": attempt.request_digest,
            "route_id": attempt.route_id,
            "attempt_number": attempt.attempt_number,
            "artifact": _link(attempt_ref, attempt.schema_version, attempt.attempt_id),
        },
    )
    model_proposal = DecisionProposal(
        "proposal:transition",
        frame.frame_id,
        "inspect",
        (),
        "inspect the bounded repository state",
        (),
    )
    response = DecisionResponse(
        request.request_id,
        request.request_digest,
        route.route_id,
        attempt.attempt_id,
        model_proposal,
        NOW,
    )
    response_ref = _put(
        artifacts,
        encode_decision_response(response),
        DECISION_RESPONSE_MEDIA_TYPE,
    )
    recorded[MODEL_RESPONDED] = writer.append(
        MODEL_RESPONDED,
        {
            "response_id": response.response_id,
            "request_id": response.request_id,
            "request_digest": response.request_digest,
            "attempt_id": response.attempt_id,
            "proposal_id": response.proposal.proposal_id,
            "artifact": _link(response_ref, response.schema_version, response.response_id),
        },
    )
    proposal = ActionProposal(
        model_proposal.proposal_id,
        model_proposal.context_frame_id,
        model_proposal.affordance,
        (),
        model_proposal.rationale,
        model_proposal.evidence_event_ids,
    )
    proposal_ref = _put(artifacts, encode_action_proposal(proposal), ACTION_PROPOSAL_MEDIA_TYPE)
    recorded[PROPOSAL_RECORDED] = writer.append(
        PROPOSAL_RECORDED,
        {
            "proposal_id": proposal.proposal_id,
            "proposal_digest": proposal.proposal_digest,
            "action_digest": proposal.action_digest,
            "context_frame_id": proposal.context_frame_id,
            "artifact": _link(proposal_ref, proposal.schema_version, proposal.proposal_digest),
        },
    )
    proof = ConstraintProof(
        "constraint:bounded",
        DIGEST,
        ConstraintOutcome.SATISFIED,
        "satisfied",
        "bounded fixture is safe",
        (),
        NOW,
    )
    constraints = ConstraintEvaluation(frame.frame_id, (proof,), NOW)
    constraints_ref = _put(
        artifacts,
        encode_constraint_evaluation(constraints),
        CONSTRAINT_EVALUATION_MEDIA_TYPE,
    )
    recorded[CONSTRAINTS_EVALUATED] = writer.append(
        CONSTRAINTS_EVALUATED,
        {
            "evaluation_id": constraints.evaluation_id,
            "context_frame_id": frame.frame_id,
            "proof_ids": (proof.proof_id,),
            "safe": True,
            "artifact": _link(
                constraints_ref,
                constraints.schema_version,
                constraints.evaluation_id,
            ),
        },
    )
    authorization_outcome = {
        "deny": AuthorizationOutcome.DENY,
        "approval": AuthorizationOutcome.REQUIRE_APPROVAL,
    }.get(branch, AuthorizationOutcome.ALLOW)
    authorization = AuthorizationDecision(
        proposal_id=proposal.proposal_id,
        proposal_digest=proposal.proposal_digest,
        context_frame_id=frame.frame_id,
        constraint_evaluation_id=constraints.evaluation_id,
        authorized_action_digest=proposal.action_digest,
        affordance_policy_digest=DIGEST,
        authorized_read_only=True,
        authorized_external=False,
        authorized_mutates_state=False,
        outcome=authorization_outcome,
        findings=(
            AuthorizationFinding(
                authorization_outcome,
                authorization_outcome.value,
                "fixture authorization",
                (proof.proof_id,),
            ),
        ),
        evaluated_at=NOW,
        approval_granted=False,
    )
    authorization_ref = _put(
        artifacts,
        encode_authorization_decision(authorization),
        AUTHORIZATION_DECISION_MEDIA_TYPE,
    )
    recorded[AUTHORIZATION_DECIDED] = writer.append(
        AUTHORIZATION_DECIDED,
        {
            "decision_id": authorization.decision_id,
            "proposal_id": proposal.proposal_id,
            "constraint_evaluation_id": constraints.evaluation_id,
            "outcome": authorization.outcome.value,
            "artifact": _link(
                authorization_ref,
                authorization.schema_version,
                authorization.decision_id,
            ),
        },
    )

    if authorization.outcome is not AuthorizationOutcome.ALLOW:
        evaluation = OutcomeEvaluator(clock=lambda: EVALUATED_AT).handle(
            EvaluateOutcome(
                RUN_ID,
                spec,
                EvaluationAuthorizationOutcome(authorization.outcome.value),
                None,
                None,
                None,
                None,
                initial_state.cutoff_global_position,
            )
        )
        recorded[EVALUATION_RECORDED] = _record_evaluation(
            writer,
            artifacts,
            evaluation,
        )
        return Scenario(events, artifacts, recorded, initial_state, None, evaluation, None)

    execution_status = ExecutionStatus.UNKNOWN if branch == "unknown" else ExecutionStatus.SUCCEEDED
    invocation = AffordanceInvocation(
        "invocation:transition",
        proposal.proposal_id,
        proposal.affordance,
        (),
        "execute:transition",
        NOW,
    )
    definition = AffordanceDefinition(
        proposal.affordance,
        "adapter:fixture",
        SideEffectClass.READ_ONLY,
        5.0,
    )
    preparation = ExecutionPreparation(
        RUN_ID,
        invocation,
        definition,
        authorization.decision_id,
        proposal.action_digest,
        "adapter/v1",
    )
    preparation_ref = _put(
        artifacts,
        serialize_execution_preparation(preparation).encode(),
        EXECUTION_PREPARATION_MEDIA_TYPE,
    )
    result = ExecutionResult(
        invocation_id=invocation.invocation_id,
        proposal_id=proposal.proposal_id,
        authorization_decision_id=authorization.decision_id,
        affordance=proposal.affordance,
        adapter_id=definition.adapter_id,
        idempotency_key=invocation.idempotency_key,
        authorized_action_digest=proposal.action_digest,
        execution_identity_digest=preparation.binding.execution_identity_digest,
        status=execution_status,
        started_at=NOW,
        completed_at=EXECUTED_AT,
        output_digest=None if execution_status is ExecutionStatus.UNKNOWN else DIGEST,
        observed_effects=(),
        error_code=None,
        reconciled=False,
    )
    result_ref = _put(
        artifacts,
        serialize_execution_result(result).encode(),
        "application/vnd.blackcell.execution-result+json",
    )
    recorded[EXECUTION_RECORDED] = writer.append(
        EXECUTION_RECORDED,
        {
            "preparation_id": preparation.preparation_id,
            "result_id": result.result_id,
            "invocation_id": result.invocation_id,
            "proposal_id": proposal.proposal_id,
            "proposal_digest": proposal.proposal_digest,
            "authorization_decision_id": authorization.decision_id,
            "authorized_action_digest": authorization.authorized_action_digest,
            "execution_identity_digest": result.execution_identity_digest,
            "status": result.status.value,
            "affordance": proposal.affordance,
            "adapter_id": result.adapter_id,
            "adapter_contract_version": "adapter/v1",
            "completed_at": result.completed_at.isoformat(),
            "arguments": (),
            "preparation_artifact": _link(
                preparation_ref,
                preparation.schema_version,
                preparation.preparation_id,
            ),
            "artifact": _link(result_ref, result.schema_version, result.result_id),
        },
        recorded_at=EXECUTED_AT,
    )
    binding = OutcomeExecutionBinding(
        run_id=RUN_ID,
        invocation_id=result.invocation_id,
        proposal_id=proposal.proposal_id,
        proposal_digest=proposal.proposal_digest,
        authorization_decision_id=authorization.decision_id,
        authorized_action_digest=authorization.authorized_action_digest,
        execution_result_id=result.result_id,
        execution_identity_digest=result.execution_identity_digest,
        execution_status=result.status.value,
        affordance=proposal.affordance,
        arguments=tuple(OutcomeArgument(item.name, item.value) for item in proposal.arguments),
        execution_adapter_id=result.adapter_id,
        execution_adapter_contract_version="adapter/v1",
        completed_at=result.completed_at,
    )
    if execution_status is ExecutionStatus.UNKNOWN:
        evaluation = OutcomeEvaluator(clock=lambda: EVALUATED_AT).handle(
            EvaluateOutcome(
                RUN_ID,
                spec,
                EvaluationAuthorizationOutcome.ALLOW,
                EvaluationExecutionStatus.UNKNOWN,
                recorded[EXECUTION_RECORDED].event_id,
                binding.binding_id,
                None,
                initial_state.cutoff_global_position,
            )
        )
        recorded[EVALUATION_RECORDED] = _record_evaluation(
            writer,
            artifacts,
            evaluation,
        )
        return Scenario(events, artifacts, recorded, initial_state, None, evaluation, None)

    owner_status = (
        OutcomeObservationStatus.INCONCLUSIVE
        if branch == "observer-inconclusive"
        else OutcomeObservationStatus.OBSERVED
    )
    claim_value = branch != "fail"
    confidence = 0.4 if branch == "low-confidence" else 1.0
    owner = OutcomeObservation(
        observation_id="observation:outcome",
        binding=binding,
        evaluation_spec_id=spec.spec_id,
        domain=DOMAIN,
        stream_id=OBSERVATION_STREAM,
        observer_id="observer:fixture",
        observer_contract_version="observer/v1",
        status=owner_status,
        observed_at=OBSERVED_AT,
        claims=(
            ()
            if owner_status is OutcomeObservationStatus.INCONCLUSIVE
            else (
                OutcomeClaim(
                    "claim:outcome",
                    "project:blackcell",
                    "ready",
                    claim_value,
                    confidence,
                ),
            )
        ),
        evidence=(OutcomeEvidencePointer(locator="fixture://outcome"),),
    )
    owner_ref = _put(
        artifacts,
        encode_outcome_observation(owner),
        OUTCOME_OBSERVATION_MEDIA_TYPE,
    )
    if owner_status is OutcomeObservationStatus.OBSERVED:
        domain_event = observation_events(
            IngestObservation(
                OBSERVATION_STREAM,
                1,
                ACTOR,
                owner.observer_id,
                RUN_ID,
                (outcome_observation_input(owner),),
                recorded[EXECUTION_RECORDED].event_id,
                DOMAIN,
            ),
            recorded_at=OBSERVED_AT,
        )[0]
    else:
        domain_event = inconclusive_outcome_event(
            owner,
            stream_sequence=2,
            actor=ACTOR,
            recorded_at=OBSERVED_AT,
            execution_event_id=recorded[EXECUTION_RECORDED].event_id,
        )
    domain_event = events.append(domain_event, expected_sequence=1)
    recorded["outcome-source"] = domain_event
    if unrelated:
        unrelated_event = observation_events(
            IngestObservation(
                OBSERVATION_STREAM,
                2,
                "other",
                "fixture.concurrent",
                "run:other",
                (
                    ObservationInput(
                        "observation:unrelated",
                        OBSERVED_AT,
                        (ObservedClaim("claim:unrelated", "other", "status", "ok"),),
                        (EvidencePointer(locator="fixture://unrelated"),),
                    ),
                ),
                None,
                DOMAIN,
            ),
            recorded_at=OBSERVED_AT,
        )[0]
        recorded["unrelated"] = events.append(unrelated_event, expected_sequence=2)
    recorded[OUTCOME_OBSERVED] = writer.append(
        OUTCOME_OBSERVED,
        {
            "observation_id": owner.observation_id,
            "observation_digest": owner.observation_digest,
            "evaluation_spec_id": owner.evaluation_spec_id,
            "execution_binding_id": owner.binding.binding_id,
            "status": owner.status.value,
            "outcome_event_ids": (domain_event.event_id,),
            "artifact": _link(owner_ref, owner.schema_version, owner.observation_digest),
        },
        recorded_at=OBSERVED_AT,
    )
    outcome_state = _project(events, len(events), OBSERVED_AT)
    outcome_ref = _put(
        artifacts,
        encode_operational_state_snapshot(outcome_state),
        OPERATIONAL_STATE_SNAPSHOT_MEDIA_TYPE,
    )
    recorded[OUTCOME_STATE_RECORDED] = writer.append(
        OUTCOME_STATE_RECORDED,
        {
            **_state_payload(outcome_state),
            "snapshot_digest": outcome_ref.digest,
            "artifact": _link(
                outcome_ref,
                "operational-state-snapshot/v1",
                outcome_ref.digest,
            ),
        },
        recorded_at=OBSERVED_AT,
    )
    evaluation_observation = bind_evaluation_observation(
        owner,
        events,
        artifacts,
        execution_event_id=recorded[EXECUTION_RECORDED].event_id,
        outcome_event_ids=(domain_event.event_id,),
    )
    evaluation = OutcomeEvaluator(clock=lambda: EVALUATED_AT).handle(
        EvaluateOutcome(
            RUN_ID,
            spec,
            EvaluationAuthorizationOutcome.ALLOW,
            EvaluationExecutionStatus.SUCCEEDED,
            recorded[EXECUTION_RECORDED].event_id,
            binding.binding_id,
            evaluation_observation,
            initial_state.cutoff_global_position,
        )
    )
    recorded[EVALUATION_RECORDED] = _record_evaluation(writer, artifacts, evaluation)
    return Scenario(events, artifacts, recorded, initial_state, outcome_state, evaluation, owner)


def _record_evaluation(
    writer: RunWriter,
    artifacts: ArtifactStore,
    evaluation: OutcomeEvaluation,
) -> EventEnvelope:
    reference = _put(
        artifacts,
        encode_outcome_evaluation(evaluation),
        OUTCOME_EVALUATION_MEDIA_TYPE,
    )
    return writer.append(
        EVALUATION_RECORDED,
        {
            "evaluation_id": evaluation.evaluation_id,
            "evaluation_spec_id": evaluation.evaluation_spec_id,
            "verdict": evaluation.verdict.value,
            "artifact": _link(
                reference,
                evaluation.schema_version,
                evaluation.evaluation_id,
            ),
        },
        recorded_at=EVALUATED_AT,
    )


def _project(
    events: EventStore,
    position: int,
    effective_time: datetime,
) -> OperationalBeliefState:
    return ProjectOperationalStateHandler(events, NoCheckpoints()).handle(
        ProjectOperationalState(
            OperationalStateScope(DOMAIN, OBSERVATION_STREAM),
            as_of_time=effective_time,
            as_of_position=position,
        )
    )


def _spec(*, low_confidence: bool = False) -> EvaluationSpec:
    return EvaluationSpec(
        "repository-ready",
        "verify repository readiness",
        (
            EvaluationCriterion(
                "criterion:ready",
                "project:blackcell",
                "ready",
                True,
                0.8 if low_confidence else 0.0,
            ),
        ),
    )


def _put(artifacts: ArtifactStore, data: bytes, media_type: str) -> ArtifactRef:
    return artifacts.put_bytes(data, media_type=media_type, encoding="utf-8")


def _link(
    reference: ArtifactRef,
    schema_version: str,
    logical_id: str,
) -> dict[str, JsonInput]:
    return {
        "digest": reference.digest,
        "media_type": reference.media_type,
        "encoding": reference.encoding,
        "size_bytes": reference.size_bytes,
        "schema_version": schema_version,
        "logical_id": logical_id,
    }


def _state_payload(state: OperationalBeliefState) -> dict[str, JsonInput]:
    return {
        "domain": state.scope.domain,
        "stream_id": state.scope.stream_id,
        "cutoff_global_position": state.cutoff_global_position,
        "last_source_stream_sequence": state.last_source_stream_sequence,
        "effective_time_cutoff": (
            None if state.effective_time_cutoff is None else state.effective_time_cutoff.isoformat()
        ),
    }


def _rehash(
    event: EventEnvelope,
    *,
    payload: Mapping[str, JsonInput] | None = None,
    actor: str | None = None,
    source: str | None = None,
    correlation_id: str | None = None,
    recorded_at: datetime | None = None,
    schema_version: int | None = None,
) -> EventEnvelope:
    rebuilt = EventEnvelope.create(
        stream_id=event.stream_id,
        stream_sequence=event.stream_sequence,
        event_type=event.event_type,
        schema_version=event.schema_version if schema_version is None else schema_version,
        actor=event.actor if actor is None else actor,
        source=event.source if source is None else source,
        payload=event.payload if payload is None else payload,
        recorded_at=event.recorded_at if recorded_at is None else recorded_at,
        effective_at=event.effective_at,
        correlation_id=event.correlation_id if correlation_id is None else correlation_id,
        causation_id=event.causation_id,
        idempotency_key=event.idempotency_key,
        event_id=event.event_id,
    )
    return replace(rebuilt, global_position=event.global_position)


def test_artifact_link_helper_is_canonical() -> None:
    value = canonical_json_bytes({"status": "ok"})
    assert value == b'{"status":"ok"}'
