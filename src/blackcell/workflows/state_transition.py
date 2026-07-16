from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol, cast

from blackcell.features.accept_state_transition import (
    AcceptStateTransition,
    AuthorizationReference,
    EvaluationReference,
    ExecutionReference,
    ProposalReference,
    StateSnapshotReference,
    StateTransitionAcceptor,
    StateTransitionIntegrityError,
    TransitionAcceptance,
    TransitionActionArgument,
    TransitionAuthorizationOutcome,
    TransitionClaim,
    TransitionEpistemicStatus,
    TransitionEvaluationFinding,
    TransitionEvaluationVerdict,
    TransitionEventReference,
    TransitionExecutionStatus,
    TransitionStateView,
)
from blackcell.features.authorize_action import (
    ACTION_PROPOSAL_MEDIA_TYPE,
    AUTHORIZATION_DECISION_MEDIA_TYPE,
    ActionProposal,
    AuthorizationDecision,
    AuthorizeAction,
    authorize_action,
    decode_action_proposal,
    decode_authorization_decision,
)
from blackcell.features.build_context import (
    CONTEXT_FRAME_MEDIA_TYPE,
    ContextFrame,
    ContextFrameStorageError,
    decode_context_frame,
)
from blackcell.features.evaluate_outcome import (
    EVALUATION_SPEC_MEDIA_TYPE,
    OUTCOME_EVALUATION_MEDIA_TYPE,
    EvaluateOutcome,
    EvaluationAuthorizationOutcome,
    EvaluationExecutionStatus,
    EvaluationSpec,
    OutcomeEvaluation,
    OutcomeEvaluator,
    decode_evaluation_spec,
    decode_outcome_evaluation,
)
from blackcell.features.execute_affordance import (
    EXECUTION_PREPARATION_MEDIA_TYPE,
    EXECUTION_PREPARATION_SCHEMA_VERSION,
    EXECUTION_RESULT_MEDIA_TYPE,
    EXECUTION_RESULT_SCHEMA_VERSION,
    ExecutionPreparation,
    ExecutionResult,
    deserialize_execution_preparation,
    deserialize_execution_result,
)
from blackcell.features.observe_outcome import (
    OUTCOME_OBSERVATION_MEDIA_TYPE,
    OutcomeObservation,
    OutcomeObservationStatus,
    decode_outcome_observation,
)
from blackcell.features.project_operational_state import (
    OPERATIONAL_STATE_SNAPSHOT_MEDIA_TYPE,
    BeliefClaim,
    OperationalBeliefState,
    OperationalStateScope,
    ProjectOperationalState,
    ProjectOperationalStateHandler,
    decode_operational_state_snapshot,
)
from blackcell.features.request_decision import (
    DECISION_ATTEMPT_MEDIA_TYPE,
    DECISION_REQUEST_MEDIA_TYPE,
    DECISION_RESPONSE_MEDIA_TYPE,
    DECISION_ROUTE_MEDIA_TYPE,
    DECISION_USAGE_MEDIA_TYPE,
    DecisionAffordance,
    DecisionArgumentSpec,
    DecisionAttempt,
    DecisionResponse,
    DecisionRoute,
    DecisionUsage,
    RequestDecision,
    decode_decision_attempt,
    decode_decision_request,
    decode_decision_response,
    decode_decision_route,
    decode_decision_usage,
)
from blackcell.features.solve_constraints import (
    CONSTRAINT_EVALUATION_MEDIA_TYPE,
    ConstraintEvaluation,
    DeterministicConstraintSolver,
    decode_constraint_evaluation,
)
from blackcell.kernel import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactRef,
    EventEnvelope,
    ProjectionCheckpoint,
)
from blackcell.workflows.daily_operator_v2_evidence import (
    DailyOperatorV2EvidenceError,
    rebuild_requested_context,
    verify_requested_ingestion,
)
from blackcell.workflows.daily_operator_v2_identity import (
    DAILY_OPERATOR_REQUEST_V2_MEDIA_TYPE,
    DAILY_OPERATOR_REQUEST_V2_SCHEMA_VERSION,
    daily_operator_v2_request_digest,
    decode_daily_operator_v2_request,
)
from blackcell.workflows.daily_operator_v2_request import DailyOperatorV2Request
from blackcell.workflows.outcome_evidence import (
    OutcomeEvidenceBindingError,
    bind_evaluation_observation,
)
from blackcell.workflows.run_grammar import validate_run_grammar
from blackcell.workflows.run_protocol import (
    AUTHORIZATION_DECIDED,
    CONSTRAINTS_EVALUATED,
    CONTEXT_RECORDED,
    EVALUATION_RECORDED,
    EVALUATION_SPECIFIED,
    EXECUTION_RECORDED,
    INITIAL_STATE_RECORDED,
    MODEL_ATTEMPT_RECORDED,
    MODEL_FAILED,
    MODEL_REQUESTED,
    MODEL_RESPONDED,
    OUTCOME_OBSERVED,
    OUTCOME_STATE_RECORDED,
    PROPOSAL_RECORDED,
    RUN_STARTED,
    RUN_WORKFLOW,
    RUN_WORKFLOW_VERSION_V2,
    RunProtocolIntegrityError,
    RunProtocolVersion,
    run_stream_id,
)
from blackcell.workflows.run_protocol_v2 import V2_EVENT_PAYLOAD_FIELDS

from ._state_transition_errors import StateTransitionBindingError, StateTransitionNotReady
from ._state_transition_integrity import (
    _Artifact,
    _artifact,
    _artifact_from_mapping,
    _event,
    _matches,
    _named_artifact,
    _prove_occurrence,
    _required_occurrence,
    _strings,
    _text,
)
from ._state_transition_suffix import _verify_recorded_suffix

_SOURCE = "blackcell.workflows.daily_operator"


class StateTransitionHistory(Protocol):
    def read_stream(
        self,
        stream_id: str,
        *,
        after_sequence: int = 0,
        limit: int | None = None,
    ) -> Sequence[EventEnvelope]: ...

    def read_all(
        self,
        *,
        after_position: int = 0,
        limit: int | None = None,
    ) -> Sequence[EventEnvelope]: ...

    def get(self, event_id: str) -> EventEnvelope | None: ...


class StateTransitionArtifacts(Protocol):
    def stat(self, digest: str) -> ArtifactRef: ...

    def get_bytes(self, digest: str, *, verify: bool = True) -> bytes: ...


class StateTransitionAcceptancePort(Protocol):
    def handle(self, command: AcceptStateTransition) -> TransitionAcceptance: ...


def bind_and_accept_state_transition(
    run_id: str,
    history: StateTransitionHistory,
    artifacts: StateTransitionArtifacts,
    acceptor: StateTransitionAcceptancePort | None = None,
) -> TransitionAcceptance:
    """Reconstruct one v2 run from immutable evidence and invoke the pure acceptor.

    Event IDs, artifact links, snapshots, and feature DTOs are deliberately absent from
    this public API.  A caller can select a run, but cannot inject a preconstructed view.
    """

    if not run_id.strip():
        raise ValueError("run_id must not be empty")
    selected_acceptor = acceptor or StateTransitionAcceptor()
    try:
        events = tuple(history.read_stream(run_stream_id(run_id)))
        command = _bind_command(run_id, history, artifacts, events=events)
        acceptance = selected_acceptor.handle(command)
        _verify_recorded_suffix(
            run_id,
            events,
            command=command,
            acceptance=acceptance,
            artifacts=artifacts,
        )
        return acceptance
    except StateTransitionNotReady:
        raise
    except StateTransitionBindingError:
        raise
    except (
        ArtifactIntegrityError,
        ArtifactNotFoundError,
        ContextFrameStorageError,
        OutcomeEvidenceBindingError,
        RunProtocolIntegrityError,
        StateTransitionIntegrityError,
        TypeError,
        ValueError,
    ) as error:
        raise StateTransitionBindingError(
            f"run {run_id!r} transition evidence is corrupt: {error}"
        ) from error


def _bind_command(
    run_id: str,
    history: StateTransitionHistory,
    artifacts: StateTransitionArtifacts,
    *,
    events: tuple[EventEnvelope, ...],
) -> AcceptStateTransition:
    if not events:
        raise StateTransitionNotReady(f"run {run_id!r} has not started")
    grammar = validate_run_grammar(events, run_id=run_id)
    if grammar.protocol_version is not RunProtocolVersion.V2:
        raise StateTransitionBindingError("state transitions require daily-operator/v2 evidence")
    _verify_run_occurrences(events, run_id=run_id, history=history, artifacts=artifacts)
    by_type = {event.event_type: event for event in events}
    if EVALUATION_RECORDED not in by_type:
        raise StateTransitionNotReady(
            f"run {run_id!r} has not recorded its deterministic outcome evaluation"
        )

    start = _event(by_type, RUN_STARTED)
    workflow_request = _workflow_request(start, artifacts)
    if workflow_request.run_id != run_id or workflow_request.ingestion.actor != start.actor:
        raise StateTransitionBindingError("DailyOperatorV2Request differs from its run identity")
    spec_event = _event(by_type, EVALUATION_SPECIFIED)
    spec, spec_artifact = _evaluation_spec(
        start,
        spec_event,
        workflow_request=workflow_request,
        artifacts=artifacts,
    )
    initial_event = _event(by_type, INITIAL_STATE_RECORDED)
    initial_state, initial_artifact = _state_snapshot(
        initial_event,
        history=history,
        artifacts=artifacts,
        label="initial state",
    )
    _verify_initial_scope(workflow_request, initial_state)
    try:
        verify_requested_ingestion(workflow_request, start, initial_state, history)
    except DailyOperatorV2EvidenceError as error:
        raise StateTransitionBindingError(str(error)) from error

    context_event = _event(by_type, CONTEXT_RECORDED)
    frame = _context_frame(
        context_event,
        workflow_request=workflow_request,
        initial_state=initial_state,
        artifacts=artifacts,
    )
    _request, _attempts, response = _gateway_prefix(
        events,
        workflow_request=workflow_request,
        context_event=context_event,
        frame=frame,
        artifacts=artifacts,
    )
    proposal_event = _event(by_type, PROPOSAL_RECORDED)
    proposal, proposal_artifact = _proposal(
        proposal_event,
        response=response,
        artifacts=artifacts,
    )
    constraints_event = _event(by_type, CONSTRAINTS_EVALUATED)
    constraints = _constraints(
        constraints_event,
        workflow_request=workflow_request,
        proposal=proposal,
        frame=frame,
        artifacts=artifacts,
    )
    authorization_event = _event(by_type, AUTHORIZATION_DECIDED)
    authorization, authorization_artifact = _authorization(
        authorization_event,
        workflow_request=workflow_request,
        proposal=proposal,
        constraints=constraints,
        frame=frame,
        artifacts=artifacts,
    )

    execution_event = by_type.get(EXECUTION_RECORDED)
    execution: ExecutionResult | None = None
    execution_reference: ExecutionReference | None = None
    if execution_event is not None:
        execution, execution_reference = _execution(
            execution_event,
            run_id=run_id,
            workflow_request=workflow_request,
            proposal=proposal,
            proposal_digest=proposal.proposal_digest,
            authorization=authorization,
            artifacts=artifacts,
        )

    outcome_observation: OutcomeObservation | None = None
    outcome_observation_artifact: _Artifact | None = None
    evaluation_observation = None
    outcome_state: OperationalBeliefState | None = None
    outcome_artifact: _Artifact | None = None
    outcome_event = by_type.get(OUTCOME_OBSERVED)
    outcome_state_event = by_type.get(OUTCOME_STATE_RECORDED)
    if outcome_event is not None or outcome_state_event is not None:
        if outcome_event is None or outcome_state_event is None or execution_event is None:
            raise StateTransitionBindingError("terminal outcome evidence is incomplete")
        outcome_observation, outcome_observation_artifact, outcome_event_ids = _outcome(
            outcome_event,
            workflow_request=workflow_request,
            spec=spec,
            execution_reference=execution_reference,
            history=history,
            artifacts=artifacts,
        )
        evaluation_observation = bind_evaluation_observation(
            outcome_observation,
            history,
            artifacts,
            execution_event_id=execution_event.event_id,
            outcome_event_ids=outcome_event_ids,
        )
        _verify_outcome_sources(
            outcome_event,
            outcome_event_ids=outcome_event_ids,
            history=history,
            actor=start.actor,
        )
        outcome_state, outcome_artifact = _state_snapshot(
            outcome_state_event,
            history=history,
            artifacts=artifacts,
            label="outcome state",
        )
        _verify_outcome_state(
            initial_state,
            outcome_state,
            outcome_observation,
            outcome_event_ids=outcome_event_ids,
            history=history,
        )

    evaluation_event = _event(by_type, EVALUATION_RECORDED)
    evaluation, evaluation_artifact = _evaluation(
        evaluation_event,
        spec=spec,
        authorization=authorization,
        execution=execution,
        execution_reference=execution_reference,
        execution_event=execution_event,
        observation=evaluation_observation,
        initial_state=initial_state,
        artifacts=artifacts,
    )
    _verify_branch_shape(
        authorization=authorization,
        execution=execution,
        outcome_observation=outcome_observation,
        outcome_state=outcome_state,
        evaluation=evaluation,
    )

    triggering_events = _triggering_events(evaluation, history=history)
    command = AcceptStateTransition(
        run_id=run_id,
        initial_state=_state_view(initial_state, initial_artifact.digest),
        outcome_state=(
            None
            if outcome_state is None or outcome_artifact is None
            else _state_view(outcome_state, outcome_artifact.digest)
        ),
        proposal=_proposal_reference(proposal, proposal_artifact.digest),
        authorization=_authorization_reference(
            authorization,
            authorization_artifact.digest,
        ),
        execution=execution_reference,
        evaluation=_evaluation_reference(
            evaluation,
            evaluation_artifact_digest=evaluation_artifact.digest,
            evaluation_spec_digest=spec_artifact.digest,
            owner_observation=outcome_observation,
            owner_observation_artifact=outcome_observation_artifact,
        ),
        triggering_events=triggering_events,
    )
    return command


def _verify_run_occurrences(
    events: tuple[EventEnvelope, ...],
    *,
    run_id: str,
    history: StateTransitionHistory,
    artifacts: StateTransitionArtifacts,
) -> None:
    positions: list[int] = []
    actor = events[0].actor
    for event in events:
        if event.source != _SOURCE or event.actor != actor:
            raise StateTransitionBindingError("run source or actor is not canonical")
        if event.correlation_id != run_id:
            raise StateTransitionBindingError("run correlation is inconsistent")
        _prove_occurrence(event, history)
        expected_fields = V2_EVENT_PAYLOAD_FIELDS.get(event.event_type)
        if expected_fields is None or frozenset(event.payload) != expected_fields:
            raise StateTransitionBindingError(f"{event.event_type} payload fields are not exact")
        position = cast("int", event.global_position)
        positions.append(position)
        _verify_nested_artifact_links(event, artifacts)
    if positions != sorted(positions) or len(positions) != len(set(positions)):
        raise StateTransitionBindingError("run events do not advance in global ledger order")


def _verify_nested_artifact_links(
    event: EventEnvelope,
    artifacts: StateTransitionArtifacts,
) -> None:
    for name, value in event.payload.items():
        if name == "artifact" or name.endswith("_artifact"):
            if (
                event.event_type == MODEL_FAILED
                and name in {"route_artifact", "usage_artifact"}
                and value is None
            ):
                continue
            if not isinstance(value, Mapping):
                raise StateTransitionBindingError(f"{event.event_type} {name} is not an object")
            _artifact_from_mapping(
                cast("Mapping[str, object]", value),
                artifacts=artifacts,
                label=f"{event.event_type}.{name}",
            )


def _workflow_request(
    event: EventEnvelope,
    artifacts: StateTransitionArtifacts,
) -> DailyOperatorV2Request:
    link = _artifact(
        event,
        artifacts=artifacts,
        media_type=DAILY_OPERATOR_REQUEST_V2_MEDIA_TYPE,
        schema_version=DAILY_OPERATOR_REQUEST_V2_SCHEMA_VERSION,
    )
    request = decode_daily_operator_v2_request(link.data)
    request_digest = daily_operator_v2_request_digest(request)
    if link.logical_id != request_digest:
        raise StateTransitionBindingError("DailyOperatorV2Request logical identity is inconsistent")
    _matches(
        event,
        {
            "run_id": request.run_id,
            "request_digest": request_digest,
            "workflow": RUN_WORKFLOW,
            "workflow_version": RUN_WORKFLOW_VERSION_V2,
            "task_id": request.context.task_id,
            "objective": request.context.objective,
            "domain": request.ingestion.domain,
            "observation_stream_id": request.ingestion.stream_id,
        },
    )
    return request


def _evaluation_spec(
    start: EventEnvelope,
    event: EventEnvelope,
    *,
    workflow_request: DailyOperatorV2Request,
    artifacts: StateTransitionArtifacts,
) -> tuple[EvaluationSpec, _Artifact]:
    link = _artifact(
        event,
        artifacts=artifacts,
        media_type=EVALUATION_SPEC_MEDIA_TYPE,
        schema_version="evaluation-spec/v1",
    )
    spec = decode_evaluation_spec(link.data)
    _matches(
        event,
        {
            "evaluation_spec_id": spec.spec_id,
            "evaluation_spec_digest": link.digest,
            "request_digest": daily_operator_v2_request_digest(workflow_request),
        },
    )
    if link.logical_id != spec.spec_id:
        raise StateTransitionBindingError("EvaluationSpec logical identity is inconsistent")
    if spec != workflow_request.evaluation_spec or _text(
        start.payload, "request_digest"
    ) != daily_operator_v2_request_digest(workflow_request):
        raise StateTransitionBindingError("EvaluationSpec differs from the immutable request")
    return spec, link


def _state_snapshot(
    event: EventEnvelope,
    *,
    history: StateTransitionHistory,
    artifacts: StateTransitionArtifacts,
    label: str,
) -> tuple[OperationalBeliefState, _Artifact]:
    link = _artifact(
        event,
        artifacts=artifacts,
        media_type=OPERATIONAL_STATE_SNAPSHOT_MEDIA_TYPE,
        schema_version="operational-state-snapshot/v1",
    )
    if link.logical_id != link.digest:
        raise StateTransitionBindingError(f"{label} logical ID must equal its snapshot digest")
    state = decode_operational_state_snapshot(
        link.data,
        expected_snapshot_digest=link.digest,
    )
    _matches(
        event,
        {
            "snapshot_digest": link.digest,
            "domain": state.scope.domain,
            "stream_id": state.scope.stream_id,
            "cutoff_global_position": state.cutoff_global_position,
            "last_source_stream_sequence": state.last_source_stream_sequence,
            "effective_time_cutoff": (
                None
                if state.effective_time_cutoff is None
                else state.effective_time_cutoff.isoformat()
            ),
        },
    )
    if event.global_position is None or state.cutoff_global_position >= event.global_position:
        raise StateTransitionBindingError(f"{label} cutoff must precede its run record")
    replayed = ProjectOperationalStateHandler(history, _NoCheckpoints()).handle(
        ProjectOperationalState(
            state.scope,
            as_of_time=state.effective_time_cutoff,
            as_of_position=state.cutoff_global_position,
        )
    )
    if replayed != state:
        raise StateTransitionBindingError(f"{label} does not equal exact ledger replay")
    return state, link


def _verify_initial_scope(
    workflow_request: DailyOperatorV2Request,
    state: OperationalBeliefState,
) -> None:
    if state.scope != OperationalStateScope(
        workflow_request.ingestion.domain,
        workflow_request.ingestion.stream_id,
    ):
        raise StateTransitionBindingError("initial state scope differs from the run request")
    if state.effective_time_cutoff != workflow_request.initial_effective_time_cutoff:
        raise StateTransitionBindingError("initial state cutoff differs from the immutable request")


def _context_frame(
    event: EventEnvelope,
    *,
    workflow_request: DailyOperatorV2Request,
    initial_state: OperationalBeliefState,
    artifacts: StateTransitionArtifacts,
) -> ContextFrame:
    frame_id = _text(event.payload, "frame_id")
    link = _artifact(
        event,
        artifacts=artifacts,
        media_type=CONTEXT_FRAME_MEDIA_TYPE,
        schema_version=None,
    )
    if link.digest != frame_id or link.logical_id != frame_id:
        raise StateTransitionBindingError("ContextFrame link does not match frame_id")
    frame = decode_context_frame(link.data, expected_frame_id=frame_id)
    expected_frame = rebuild_requested_context(workflow_request, initial_state)
    if frame.schema_version != link.schema_version:
        raise StateTransitionBindingError("ContextFrame schema differs from its artifact link")
    if frame.schema_version == "context-frame/v4" and frame.state_effective_time != (
        initial_state.effective_time_cutoff
    ):
        raise StateTransitionBindingError("ContextFrame effective-time identity differs from state")
    if (
        frame != expected_frame
        or frame.task_id != workflow_request.context.task_id
        or frame.objective != workflow_request.context.objective
        or frame.generated_at != workflow_request.context.generated_at
        or frame.model_payload_characters > workflow_request.context.max_characters
        or frame.task_id != _text(event.payload, "task_id")
        or frame.state_domain != initial_state.scope.domain
        or frame.state_stream_id != initial_state.scope.stream_id
        or frame.state_global_position != initial_state.cutoff_global_position
        or frame.state_stream_position != initial_state.last_source_stream_sequence
    ):
        raise StateTransitionBindingError("ContextFrame content differs from its run/state link")
    _matches(
        event,
        {
            "state_domain": initial_state.scope.domain,
            "state_stream_id": initial_state.scope.stream_id,
            "state_global_position": initial_state.cutoff_global_position,
            "state_stream_position": initial_state.last_source_stream_sequence,
            "source_packet_id": frame.source_packet_id,
            "source_selection_id": frame.source_selection_id,
        },
    )
    return frame


def _gateway_prefix(
    events: tuple[EventEnvelope, ...],
    *,
    workflow_request: DailyOperatorV2Request,
    context_event: EventEnvelope,
    frame: ContextFrame,
    artifacts: StateTransitionArtifacts,
) -> tuple[RequestDecision, tuple[DecisionAttempt, ...], DecisionResponse]:
    request_event = _event({item.event_type: item for item in events}, MODEL_REQUESTED)
    request_link = _artifact(
        request_event,
        artifacts=artifacts,
        media_type=DECISION_REQUEST_MEDIA_TYPE,
        schema_version="decision-request/v1",
    )
    if request_link.logical_id != request_link.digest:
        raise StateTransitionBindingError("decision request link identity is inconsistent")
    request = decode_decision_request(
        request_link.data,
        expected_request_digest=request_link.digest,
    )
    _matches(
        request_event,
        {
            "request_id": request.request_id,
            "request_digest": request.request_digest,
            "context_frame_id": frame.frame_id,
        },
    )
    if (
        request.run_id != request_event.correlation_id
        or request.correlation_id != request_event.correlation_id
        or request.causation_id != context_event.event_id
        or request.context_frame_id != frame.frame_id
        or request.objective != frame.objective
        or request.context_payload != frame.model_payload
        or request.evidence_event_ids != frame.provenance_event_ids
        or request.requirements != workflow_request.gateway_requirements
        or request.affordances != (_decision_affordance(workflow_request),)
    ):
        raise StateTransitionBindingError(
            "decision request differs from its immutable policy or causal ContextFrame"
        )

    attempt_events = tuple(item for item in events if item.event_type == MODEL_ATTEMPT_RECORDED)
    attempts: list[DecisionAttempt] = []
    routes: list[DecisionRoute] = []
    for number, attempt_event in enumerate(attempt_events, start=1):
        attempt_link = _artifact(
            attempt_event,
            artifacts=artifacts,
            media_type=DECISION_ATTEMPT_MEDIA_TYPE,
            schema_version="decision-attempt/v1",
        )
        if attempt_link.logical_id != attempt_link.digest:
            raise StateTransitionBindingError("decision attempt link identity is inconsistent")
        attempt = decode_decision_attempt(
            attempt_link.data,
            expected_attempt_id=attempt_link.digest,
        )
        route_link = _named_artifact(
            attempt_event,
            "route_artifact",
            artifacts=artifacts,
            media_type=DECISION_ROUTE_MEDIA_TYPE,
            schema_version="decision-route/v1",
        )
        route = decode_decision_route(
            route_link.data,
            expected_route_id=route_link.digest,
        )
        if route_link.logical_id != route.route_id:
            raise StateTransitionBindingError("decision route link identity is inconsistent")
        _matches(
            attempt_event,
            {
                "attempt_id": attempt.attempt_id,
                "request_id": attempt.request_id,
                "request_digest": attempt.request_digest,
                "route_id": attempt.route_id,
                "attempt_number": attempt.attempt_number,
            },
        )
        if (
            attempt.request_id != request.request_id
            or attempt.request_digest != request.request_digest
            or attempt.attempt_number != number
            or attempt.route_id != route.route_id
            or route.capability != request.capability
            or (request.locality.value == "local-only" and not route.local)
            or (request.deterministic_required and not route.deterministic)
            or route.selected_at > attempt.started_at
        ):
            raise StateTransitionBindingError(
                "decision attempt or route differs from its request policy"
            )
        attempts.append(attempt)
        routes.append(route)
    if not attempts:
        raise StateTransitionBindingError("a successful model response requires an attempt")

    response_event = _event({item.event_type: item for item in events}, MODEL_RESPONDED)
    response_link = _artifact(
        response_event,
        artifacts=artifacts,
        media_type=DECISION_RESPONSE_MEDIA_TYPE,
        schema_version="decision-response/v1",
    )
    if response_link.logical_id != response_link.digest:
        raise StateTransitionBindingError("decision response link identity is inconsistent")
    response = decode_decision_response(
        response_link.data,
        expected_response_id=response_link.digest,
        request=request,
    )
    usage_link = _named_artifact(
        response_event,
        "usage_artifact",
        artifacts=artifacts,
        media_type=DECISION_USAGE_MEDIA_TYPE,
        schema_version="decision-usage/v1",
    )
    usage = decode_decision_usage(
        usage_link.data,
        expected_usage_id=usage_link.digest,
    )
    if usage_link.logical_id != usage.usage_id:
        raise StateTransitionBindingError("decision usage link identity is inconsistent")
    _matches(
        response_event,
        {
            "response_id": response.response_id,
            "request_id": response.request_id,
            "request_digest": response.request_digest,
            "attempt_id": response.attempt_id,
            "route_id": response.route_id,
            "proposal_id": response.proposal.proposal_id,
            "usage_id": usage.usage_id,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "latency_ms": usage.latency_ms,
            "cost_microusd": usage.cost_microusd,
            "deterministic": usage.deterministic,
        },
    )
    final_attempt = attempts[-1]
    final_route = routes[-1]
    if (
        response.request_id != request.request_id
        or response.request_digest != request.request_digest
        or response.attempt_id != final_attempt.attempt_id
        or response.route_id != final_attempt.route_id
        or response.route_id != final_route.route_id
        or response.completed_at < final_attempt.started_at
        or response_event.recorded_at < response.completed_at
        or usage.request_id != request.request_id
        or usage.attempt_id != final_attempt.attempt_id
        or not _usage_within_budget(usage, request, final_route)
    ):
        raise StateTransitionBindingError(
            "decision response or usage differs from its final attempt"
        )
    return request, tuple(attempts), response


def _proposal(
    event: EventEnvelope,
    *,
    response: DecisionResponse,
    artifacts: StateTransitionArtifacts,
) -> tuple[ActionProposal, _Artifact]:
    link = _artifact(
        event,
        artifacts=artifacts,
        media_type=ACTION_PROPOSAL_MEDIA_TYPE,
        schema_version="action-proposal/v2",
    )
    proposal = decode_action_proposal(link.data)
    _matches(
        event,
        {
            "proposal_id": proposal.proposal_id,
            "proposal_digest": proposal.proposal_digest,
            "action_digest": proposal.action_digest,
            "context_frame_id": proposal.context_frame_id,
        },
    )
    if link.logical_id != proposal.proposal_digest:
        raise StateTransitionBindingError("ActionProposal logical ID is inconsistent")
    model = response.proposal
    if (
        proposal.proposal_id != model.proposal_id
        or proposal.context_frame_id != model.context_frame_id
        or proposal.affordance != model.affordance
        or tuple((item.name, item.value) for item in proposal.arguments)
        != tuple((item.name, item.value) for item in model.arguments)
        or proposal.rationale != model.rationale
        or proposal.evidence_event_ids != model.evidence_event_ids
    ):
        raise StateTransitionBindingError("ActionProposal differs from verified model response")
    return proposal, link


def _constraints(
    event: EventEnvelope,
    *,
    workflow_request: DailyOperatorV2Request,
    proposal: ActionProposal,
    frame: ContextFrame,
    artifacts: StateTransitionArtifacts,
) -> ConstraintEvaluation:
    link = _artifact(
        event,
        artifacts=artifacts,
        media_type=CONSTRAINT_EVALUATION_MEDIA_TYPE,
        schema_version="constraint-evaluation/v1",
    )
    evaluation = decode_constraint_evaluation(link.data)
    _matches(
        event,
        {
            "evaluation_id": evaluation.evaluation_id,
            "context_frame_id": evaluation.context_frame_id,
            "proof_ids": tuple(item.proof_id for item in evaluation.proofs),
            "safe": evaluation.safe,
        },
    )
    if link.logical_id != evaluation.evaluation_id:
        raise StateTransitionBindingError("ConstraintEvaluation logical ID is inconsistent")
    if evaluation.context_frame_id != proposal.context_frame_id:
        raise StateTransitionBindingError("ConstraintEvaluation belongs to another ContextFrame")
    provenance = set(frame.provenance_event_ids)
    cited = {event_id for proof in evaluation.proofs for event_id in proof.evidence_event_ids}
    if not cited <= provenance:
        raise StateTransitionBindingError("constraint proof cites evidence outside ContextFrame")
    replayed = DeterministicConstraintSolver().handle(workflow_request.constraints, frame)
    if evaluation != replayed:
        raise StateTransitionBindingError(
            "ConstraintEvaluation differs from immutable request replay"
        )
    return evaluation


def _authorization(
    event: EventEnvelope,
    *,
    workflow_request: DailyOperatorV2Request,
    proposal: ActionProposal,
    constraints: ConstraintEvaluation,
    frame: ContextFrame,
    artifacts: StateTransitionArtifacts,
) -> tuple[AuthorizationDecision, _Artifact]:
    link = _artifact(
        event,
        artifacts=artifacts,
        media_type=AUTHORIZATION_DECISION_MEDIA_TYPE,
        schema_version="authorization-decision/v2",
    )
    decision = decode_authorization_decision(link.data)
    _matches(
        event,
        {
            "decision_id": decision.decision_id,
            "proposal_id": decision.proposal_id,
            "constraint_evaluation_id": decision.constraint_evaluation_id,
            "outcome": decision.outcome.value,
        },
    )
    if link.logical_id != decision.decision_id:
        raise StateTransitionBindingError("AuthorizationDecision logical ID is inconsistent")
    if (
        decision.proposal_id != proposal.proposal_id
        or decision.proposal_digest != proposal.proposal_digest
        or decision.context_frame_id != proposal.context_frame_id
        or decision.authorized_action_digest != proposal.action_digest
        or decision.constraint_evaluation_id != constraints.evaluation_id
    ):
        raise StateTransitionBindingError("AuthorizationDecision cross-identity is inconsistent")
    proof_ids = {item.proof_id for item in constraints.proofs}
    if any(not set(item.proof_ids) <= proof_ids for item in decision.findings):
        raise StateTransitionBindingError("authorization finding cites an unknown proof")
    if event.recorded_at < decision.evaluated_at:
        raise StateTransitionBindingError(
            "authorization record cannot precede authorization evaluation"
        )
    replayed = authorize_action(
        AuthorizeAction(
            proposal,
            workflow_request.authorization_affordance,
            workflow_request.constraints.evaluated_at,
            frame.provenance_event_ids,
            workflow_request.approval_granted,
        ),
        constraints,
    )
    if decision != replayed:
        raise StateTransitionBindingError(
            "AuthorizationDecision differs from immutable policy replay"
        )
    return decision, link


def _execution(
    event: EventEnvelope,
    *,
    run_id: str,
    workflow_request: DailyOperatorV2Request,
    proposal: ActionProposal,
    proposal_digest: str,
    authorization: AuthorizationDecision,
    artifacts: StateTransitionArtifacts,
) -> tuple[ExecutionResult, ExecutionReference]:
    link = _artifact(
        event,
        artifacts=artifacts,
        media_type=EXECUTION_RESULT_MEDIA_TYPE,
        schema_version=EXECUTION_RESULT_SCHEMA_VERSION,
    )
    result = deserialize_execution_result(link.data, expected_result_id=link.digest)
    if link.logical_id != result.result_id:
        raise StateTransitionBindingError("ExecutionResult logical ID is inconsistent")
    preparation_link = _named_artifact(
        event,
        "preparation_artifact",
        artifacts=artifacts,
        media_type=EXECUTION_PREPARATION_MEDIA_TYPE,
        schema_version=EXECUTION_PREPARATION_SCHEMA_VERSION,
    )
    preparation = deserialize_execution_preparation(
        preparation_link.data,
        expected_preparation_id=preparation_link.digest,
    )
    if preparation_link.logical_id != preparation.preparation_id:
        raise StateTransitionBindingError("ExecutionPreparation logical ID is inconsistent")
    arguments = tuple(
        TransitionActionArgument(item.name, item.value) for item in proposal.arguments
    )
    contract_version = preparation.adapter_contract_version
    _matches(
        event,
        {
            "run_id": run_id,
            "preparation_id": preparation.preparation_id,
            "result_id": result.result_id,
            "invocation_id": result.invocation_id,
            "proposal_id": result.proposal_id,
            "proposal_digest": proposal_digest,
            "authorization_decision_id": result.authorization_decision_id,
            "authorized_action_digest": result.authorized_action_digest,
            "execution_identity_digest": result.execution_identity_digest,
            "status": result.status.value,
            "affordance": result.affordance,
            "adapter_id": result.adapter_id,
            "adapter_contract_version": contract_version,
            "completed_at": result.completed_at.isoformat(),
            "arguments": tuple(
                {"name": item.name, "value": item.value} for item in proposal.arguments
            ),
        },
    )
    _verify_execution_preparation(
        preparation,
        run_id=run_id,
        workflow_request=workflow_request,
        proposal=proposal,
        authorization=authorization,
        result=result,
    )
    journal_position = event.payload.get("journal_position")
    if (
        result.proposal_id != proposal.proposal_id
        or result.authorization_decision_id != authorization.decision_id
        or result.authorized_action_digest != authorization.authorized_action_digest
        or result.affordance != proposal.affordance
        or result.started_at < authorization.evaluated_at
        or result.started_at < preparation.invocation.requested_at
        or event.recorded_at < result.completed_at
        or isinstance(journal_position, bool)
        or not isinstance(journal_position, int)
        or journal_position < 1
    ):
        raise StateTransitionBindingError("ExecutionResult differs from its authorized action")
    reference = ExecutionReference(
        run_id=run_id,
        execution_event_id=event.event_id,
        execution_result_id=result.result_id,
        execution_result_digest=link.digest,
        invocation_id=result.invocation_id,
        proposal_id=result.proposal_id,
        proposal_digest=proposal.proposal_digest,
        authorization_decision_id=result.authorization_decision_id,
        execution_binding_id=_execution_binding_id(
            run_id=run_id,
            result=result,
            proposal=proposal,
            adapter_contract_version=contract_version,
        ),
        execution_identity_digest=result.execution_identity_digest,
        authorized_action_digest=result.authorized_action_digest,
        idempotency_key=result.idempotency_key,
        affordance=result.affordance,
        arguments=arguments,
        adapter_id=result.adapter_id,
        adapter_contract_version=contract_version,
        status=TransitionExecutionStatus(result.status.value),
        completed_at=result.completed_at,
    )
    return result, reference


def _verify_execution_preparation(
    preparation: ExecutionPreparation,
    *,
    run_id: str,
    workflow_request: DailyOperatorV2Request,
    proposal: ActionProposal,
    authorization: AuthorizationDecision,
    result: ExecutionResult,
) -> None:
    invocation = preparation.invocation
    definition = preparation.definition
    proposal_arguments = tuple((item.name, item.value) for item in proposal.arguments)
    invocation_arguments = tuple((item.name, item.value) for item in invocation.arguments)
    declared = {item.name: item for item in definition.arguments}
    provided = {item.name for item in invocation.arguments}
    if (
        preparation.run_id != run_id
        or preparation.definition != workflow_request.execution_affordance
        or invocation.invocation_id != workflow_request.invocation_id
        or invocation.idempotency_key != workflow_request.idempotency_key
        or invocation.proposal_id != proposal.proposal_id
        or invocation.affordance != proposal.affordance
        or invocation_arguments != proposal_arguments
        or definition.name != proposal.affordance
        or any(name not in declared for name in provided)
        or any(item.required and item.name not in provided for item in definition.arguments)
        or preparation.authorization_decision_id != authorization.decision_id
        or preparation.authorized_action_digest != authorization.authorized_action_digest
        or invocation.requested_at < authorization.evaluated_at
        or result.invocation_id != invocation.invocation_id
        or result.idempotency_key != invocation.idempotency_key
        or result.adapter_id != definition.adapter_id
        or result.execution_identity_digest != preparation.binding.execution_identity_digest
    ):
        raise StateTransitionBindingError(
            "ExecutionPreparation differs from its run, action, authorization, or result"
        )


def _execution_binding_id(
    *,
    run_id: str,
    result: ExecutionResult,
    proposal: ActionProposal,
    adapter_contract_version: str,
) -> str:
    # Keep construction beside the outcome-owned identity payload.
    from blackcell.features.observe_outcome import (
        OutcomeArgument,
        OutcomeExecutionBinding,
    )

    return OutcomeExecutionBinding(
        run_id=run_id,
        invocation_id=result.invocation_id,
        proposal_id=result.proposal_id,
        proposal_digest=proposal.proposal_digest,
        authorization_decision_id=result.authorization_decision_id,
        authorized_action_digest=result.authorized_action_digest,
        execution_result_id=result.result_id,
        execution_identity_digest=result.execution_identity_digest,
        execution_status=result.status.value,
        affordance=result.affordance,
        arguments=tuple(OutcomeArgument(item.name, item.value) for item in proposal.arguments),
        execution_adapter_id=result.adapter_id,
        execution_adapter_contract_version=adapter_contract_version,
        completed_at=result.completed_at,
    ).binding_id


def _outcome(
    event: EventEnvelope,
    *,
    workflow_request: DailyOperatorV2Request,
    spec: EvaluationSpec,
    execution_reference: ExecutionReference | None,
    history: StateTransitionHistory,
    artifacts: StateTransitionArtifacts,
) -> tuple[OutcomeObservation, _Artifact, tuple[str, ...]]:
    if execution_reference is None:
        raise StateTransitionBindingError("outcome observation requires verified execution")
    link = _artifact(
        event,
        artifacts=artifacts,
        media_type=OUTCOME_OBSERVATION_MEDIA_TYPE,
        schema_version="outcome-observation/v1",
    )
    outcome = decode_outcome_observation(link.data)
    outcome_event_ids = _strings(event.payload.get("outcome_event_ids"), "outcome_event_ids")
    _matches(
        event,
        {
            "observation_id": outcome.observation_id,
            "observation_digest": outcome.observation_digest,
            "evaluation_spec_id": outcome.evaluation_spec_id,
            "execution_binding_id": outcome.binding.binding_id,
            "status": outcome.status.value,
            "outcome_event_ids": outcome_event_ids,
        },
    )
    if link.logical_id != outcome.observation_digest:
        raise StateTransitionBindingError("OutcomeObservation logical ID is inconsistent")
    targets = {(item.subject, item.predicate) for item in spec.criteria}
    if (
        outcome.evaluation_spec_id != spec.spec_id
        or outcome.binding.binding_id != execution_reference.execution_binding_id
        or outcome.binding.run_id != execution_reference.run_id
        or outcome.binding.execution_result_id != execution_reference.execution_result_id
        or outcome.domain != workflow_request.ingestion.domain
        or outcome.stream_id != workflow_request.ingestion.stream_id
        or outcome.observer_id != workflow_request.expected_observer_id
        or outcome.observer_contract_version != workflow_request.expected_observer_contract_version
        or any(item.key not in targets for item in outcome.claims)
    ):
        raise StateTransitionBindingError("OutcomeObservation cross-identity is inconsistent")
    if len(outcome_event_ids) != 1:
        raise StateTransitionBindingError("one owner observation requires exactly one domain event")
    source = history.get(outcome_event_ids[0])
    if source is None or source.global_position is None:
        raise StateTransitionBindingError("outcome source event is absent from the ledger")
    if event.global_position is None or source.global_position >= event.global_position:
        raise StateTransitionBindingError("outcome source must precede its run record")
    return outcome, link, outcome_event_ids


def _verify_outcome_sources(
    outcome_event: EventEnvelope,
    *,
    outcome_event_ids: tuple[str, ...],
    history: StateTransitionHistory,
    actor: str,
) -> None:
    for event_id in outcome_event_ids:
        event = history.get(event_id)
        if event is None:
            raise StateTransitionBindingError("outcome source event is missing")
        _prove_occurrence(event, history)
        if event.actor != actor:
            raise StateTransitionBindingError("outcome source actor differs from its run")
        if event.global_position is None or outcome_event.global_position is None:
            raise StateTransitionBindingError("outcome evidence requires stored positions")
        if event.global_position >= outcome_event.global_position:
            raise StateTransitionBindingError("outcome source event was recorded too late")


def _verify_outcome_state(
    initial: OperationalBeliefState,
    outcome: OperationalBeliefState,
    observation: OutcomeObservation,
    *,
    outcome_event_ids: tuple[str, ...],
    history: StateTransitionHistory,
) -> None:
    if initial.scope != outcome.scope:
        raise StateTransitionBindingError("outcome state scope differs from initial state")
    source_events = tuple(_required_occurrence(history, event_id) for event_id in outcome_event_ids)
    max_position = max(cast("int", item.global_position) for item in source_events)
    if outcome.cutoff_global_position < max_position:
        raise StateTransitionBindingError("outcome state cutoff excludes its owner observation")
    if outcome.cutoff_global_position <= initial.cutoff_global_position:
        raise StateTransitionBindingError("outcome state must advance the global ledger cutoff")
    if observation.status is OutcomeObservationStatus.OBSERVED:
        if outcome.last_source_stream_sequence <= initial.last_source_stream_sequence:
            raise StateTransitionBindingError(
                "observed outcome state must advance its source stream"
            )
    elif outcome.last_source_stream_sequence < initial.last_source_stream_sequence:
        raise StateTransitionBindingError("claim-free outcome state cannot regress its stream")


def _evaluation(
    event: EventEnvelope,
    *,
    spec: EvaluationSpec,
    authorization: AuthorizationDecision,
    execution: ExecutionResult | None,
    execution_reference: ExecutionReference | None,
    execution_event: EventEnvelope | None,
    observation,
    initial_state: OperationalBeliefState,
    artifacts: StateTransitionArtifacts,
) -> tuple[OutcomeEvaluation, _Artifact]:
    link = _artifact(
        event,
        artifacts=artifacts,
        media_type=OUTCOME_EVALUATION_MEDIA_TYPE,
        schema_version="outcome-evaluation/v1",
    )
    evaluation = decode_outcome_evaluation(link.data, spec=spec)
    _matches(
        event,
        {
            "evaluation_id": evaluation.evaluation_id,
            "evaluation_spec_id": evaluation.evaluation_spec_id,
            "verdict": evaluation.verdict.value,
        },
    )
    if link.logical_id != evaluation.evaluation_id:
        raise StateTransitionBindingError("OutcomeEvaluation logical ID is inconsistent")
    execution_status = (
        None if execution is None else EvaluationExecutionStatus(execution.status.value)
    )
    command = EvaluateOutcome(
        run_id=_text(event.payload, "run_id"),
        spec=spec,
        authorization_outcome=EvaluationAuthorizationOutcome(authorization.outcome.value),
        execution_status=execution_status,
        execution_event_id=None if execution_event is None else execution_event.event_id,
        execution_binding_id=(
            None if execution_reference is None else execution_reference.execution_binding_id
        ),
        observation=observation,
        initial_state_position=initial_state.cutoff_global_position,
    )
    recomputed = OutcomeEvaluator(clock=lambda: evaluation.evaluated_at).handle(command)
    if recomputed != evaluation:
        raise StateTransitionBindingError("OutcomeEvaluation differs from deterministic replay")
    if event.recorded_at < evaluation.evaluated_at:
        raise StateTransitionBindingError("evaluation record precedes evaluation time")
    return evaluation, link


def _verify_branch_shape(
    *,
    authorization: AuthorizationDecision,
    execution: ExecutionResult | None,
    outcome_observation: OutcomeObservation | None,
    outcome_state: OperationalBeliefState | None,
    evaluation: OutcomeEvaluation,
) -> None:
    if authorization.outcome.value != "allow":
        if any(item is not None for item in (execution, outcome_observation, outcome_state)):
            raise StateTransitionBindingError("blocked authorization carries outcome evidence")
        return
    if execution is None:
        raise StateTransitionBindingError("allowed authorization lacks execution evidence")
    if execution.status.value == "unknown":
        if outcome_observation is not None or outcome_state is not None:
            raise StateTransitionBindingError("UNKNOWN execution carries outcome observation")
        return
    if outcome_observation is None or outcome_state is None:
        raise StateTransitionBindingError("terminal execution lacks outcome evidence")
    if evaluation.outcome_observation_digest != outcome_observation.observation_digest:
        raise StateTransitionBindingError("evaluation cites a different owner observation")


def _triggering_events(
    evaluation: OutcomeEvaluation,
    *,
    history: StateTransitionHistory,
) -> tuple[TransitionEventReference, ...]:
    event_ids = tuple(
        sorted(
            {event_id for finding in evaluation.findings for event_id in finding.source_event_ids}
        )
    )
    references: list[TransitionEventReference] = []
    for event_id in event_ids:
        event = _required_occurrence(history, event_id)
        if event.global_position is None or event.causation_id is None:
            raise StateTransitionBindingError("triggering evidence lacks stored causal identity")
        references.append(
            TransitionEventReference(
                event_id=event.event_id,
                global_position=event.global_position,
                stream_sequence=event.stream_sequence,
                event_type=event.event_type,
                stream_id=event.stream_id,
                correlation_id=event.correlation_id,
                causation_id=event.causation_id,
                payload_hash=event.payload_hash,
            )
        )
    return tuple(references)


def _state_view(state: OperationalBeliefState, digest: str) -> TransitionStateView:
    stream_id = state.scope.stream_id
    if stream_id is None:  # pragma: no cover - snapshots are scope-bound above
        raise StateTransitionBindingError("transition state must have a source stream")
    return TransitionStateView(
        StateSnapshotReference(
            snapshot_digest=digest,
            domain=state.scope.domain,
            stream_id=stream_id,
            cutoff_global_position=state.cutoff_global_position,
            last_source_stream_sequence=state.last_source_stream_sequence,
            effective_time_cutoff=state.effective_time_cutoff,
        ),
        tuple(_transition_claim(item) for item in state.claims),
    )


def _transition_claim(claim: BeliefClaim) -> TransitionClaim:
    return TransitionClaim(
        claim_id=claim.claim_id,
        subject=claim.subject,
        predicate=claim.predicate,
        value=claim.value,
        confidence=claim.confidence,
        effective_at=claim.effective_at,
        recorded_at=claim.recorded_at,
        source_event_id=claim.source_event_id,
        source=claim.source,
        actor=claim.actor,
        correlation_id=claim.correlation_id,
        domain=claim.domain,
        stream_id=claim.stream_id,
        stream_sequence=claim.stream_sequence,
        global_position=claim.global_position,
        correction_id=claim.correction_id,
        supersedes_claim_ids=claim.supersedes_claim_ids,
        expires_at=claim.expires_at,
        epistemic_status=TransitionEpistemicStatus(claim.epistemic_status.value),
        unknown_reason=(None if claim.unknown_reason is None else claim.unknown_reason.value),
    )


def _proposal_reference(proposal: ActionProposal, digest: str) -> ProposalReference:
    return ProposalReference(
        proposal_id=proposal.proposal_id,
        proposal_digest=proposal.proposal_digest,
        proposal_artifact_digest=digest,
        context_frame_id=proposal.context_frame_id,
        affordance=proposal.affordance,
        arguments=tuple(
            TransitionActionArgument(item.name, item.value) for item in proposal.arguments
        ),
        action_digest=proposal.action_digest,
    )


def _authorization_reference(
    decision: AuthorizationDecision,
    digest: str,
) -> AuthorizationReference:
    return AuthorizationReference(
        decision_id=decision.decision_id,
        decision_artifact_digest=digest,
        proposal_id=decision.proposal_id,
        proposal_digest=decision.proposal_digest,
        constraint_evaluation_id=decision.constraint_evaluation_id,
        authorized_action_digest=decision.authorized_action_digest,
        affordance_policy_digest=decision.affordance_policy_digest,
        outcome=TransitionAuthorizationOutcome(decision.outcome.value),
        approval_granted=decision.approval_granted,
    )


def _evaluation_reference(
    evaluation: OutcomeEvaluation,
    *,
    evaluation_artifact_digest: str,
    evaluation_spec_digest: str,
    owner_observation: OutcomeObservation | None,
    owner_observation_artifact: _Artifact | None,
) -> EvaluationReference:
    return EvaluationReference(
        evaluation_id=evaluation.evaluation_id,
        evaluation_artifact_digest=evaluation_artifact_digest,
        evaluation_spec_id=evaluation.evaluation_spec_id,
        evaluation_spec_digest=evaluation_spec_digest,
        run_id=evaluation.run_id,
        authorization_outcome=TransitionAuthorizationOutcome(
            evaluation.authorization_outcome.value
        ),
        execution_status=(
            None
            if evaluation.execution_status is None
            else TransitionExecutionStatus(evaluation.execution_status.value)
        ),
        verdict=TransitionEvaluationVerdict(evaluation.verdict.value),
        execution_event_id=evaluation.execution_event_id,
        execution_binding_id=evaluation.execution_binding_id,
        evidence_binding_id=evaluation.outcome_evidence_binding_id,
        owner_observation_id=evaluation.outcome_observation_id,
        owner_observation_digest=evaluation.outcome_observation_digest,
        owner_observation_artifact_digest=(
            None
            if owner_observation is None or owner_observation_artifact is None
            else owner_observation_artifact.digest
        ),
        initial_state_position=evaluation.initial_state_position,
        findings=tuple(
            TransitionEvaluationFinding(
                criterion_id=item.criterion_id,
                required=item.required,
                verdict=TransitionEvaluationVerdict(item.verdict.value),
                code=item.code,
                expected_value=item.expected_value,
                actual_present=item.actual_present,
                actual_value=item.actual_value,
                actual_confidence=item.actual_confidence,
                observed_claim_ids=item.observed_claim_ids,
                source_event_ids=item.source_event_ids,
            )
            for item in evaluation.findings
        ),
        evaluated_at=evaluation.evaluated_at,
    )


def _decision_affordance(request: DailyOperatorV2Request) -> DecisionAffordance:
    definition = request.execution_affordance
    return DecisionAffordance(
        definition.name,
        tuple(DecisionArgumentSpec(item.name, item.required) for item in definition.arguments),
    )


def _usage_within_budget(
    usage: DecisionUsage,
    request: RequestDecision,
    route: DecisionRoute,
) -> bool:
    budget = request.budget
    return (
        usage.request_id == request.request_id
        and usage.input_tokens <= budget.max_input_tokens
        and usage.output_tokens <= budget.max_output_tokens
        and usage.latency_ms <= budget.max_latency_ms
        and usage.cost_microusd <= budget.max_cost_microusd
        and (not request.deterministic_required or usage.deterministic)
        and (not route.deterministic or usage.deterministic)
    )


class _NoCheckpoints:
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
        raise AssertionError("historical transition verification must not write checkpoints")


__all__ = [
    "StateTransitionAcceptancePort",
    "StateTransitionArtifacts",
    "StateTransitionBindingError",
    "StateTransitionHistory",
    "StateTransitionNotReady",
    "bind_and_accept_state_transition",
]
