from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import cast

from blackcell.features.accept_state_transition import (
    ACCEPTED_STATE_TRANSITION_MEDIA_TYPE,
    AcceptedStateTransition,
    decode_accepted_state_transition,
    encode_accepted_state_transition,
)
from blackcell.features.authorize_action import (
    ACTION_PROPOSAL_MEDIA_TYPE,
    AUTHORIZATION_DECISION_MEDIA_TYPE,
    ActionAuthorizer,
    ActionProposal,
    AuthorizationDecision,
    AuthorizationOutcome,
    AuthorizeAction,
    decode_action_proposal,
    decode_authorization_decision,
    encode_action_proposal,
    encode_authorization_decision,
)
from blackcell.features.build_context import (
    CONTEXT_FRAME_MEDIA_TYPE,
    ContextFrame,
    decode_context_frame,
    encode_context_frame,
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
    encode_evaluation_spec,
    encode_outcome_evaluation,
)
from blackcell.features.execute_affordance import (
    EXECUTION_PREPARATION_MEDIA_TYPE,
    EXECUTION_RESULT_MEDIA_TYPE,
    ExecutionJournalEntry,
    ExecutionJournalError,
    ExecutionJournalStatus,
    ExecutionPreparation,
    ExecutionResult,
    SideEffectClass,
    deserialize_execution_preparation,
    deserialize_execution_result,
)
from blackcell.features.execute_affordance.ports import ExecutionJournal
from blackcell.features.observe_outcome import (
    OUTCOME_OBSERVATION_MEDIA_TYPE,
    OutcomeArgument,
    OutcomeExecutionBinding,
    OutcomeObservation,
    decode_outcome_observation,
    encode_outcome_observation,
)
from blackcell.features.project_operational_state import (
    OPERATIONAL_STATE_SNAPSHOT_MEDIA_TYPE,
    OPERATIONAL_STATE_SNAPSHOT_SCHEMA_VERSION,
    OperationalBeliefState,
    ProjectOperationalState,
    ProjectOperationalStateHandler,
    decode_operational_state_snapshot,
    encode_operational_state_snapshot,
)
from blackcell.features.request_decision import (
    DECISION_ATTEMPT_MEDIA_TYPE,
    DECISION_FAILURE_MEDIA_TYPE,
    DECISION_REQUEST_MEDIA_TYPE,
    DECISION_RESPONSE_MEDIA_TYPE,
    DECISION_ROUTE_MEDIA_TYPE,
    DECISION_USAGE_MEDIA_TYPE,
    DecisionAffordance,
    DecisionArgumentSpec,
    DecisionAttempt,
    DecisionAttemptRecord,
    DecisionEvidenceJournal,
    DecisionFailure,
    DecisionFailureRecord,
    DecisionJournalError,
    DecisionPreparation,
    DecisionRequestRecord,
    DecisionResponse,
    DecisionRoute,
    DecisionSuccessRecord,
    DecisionTerminalRecord,
    DecisionUsage,
    RequestDecision,
    decode_decision_attempt,
    decode_decision_failure,
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
    encode_constraint_evaluation,
)
from blackcell.kernel import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactRef,
    ArtifactStore,
    ConcurrencyError,
    EventEnvelope,
    EventStore,
    JsonInput,
    JsonScalar,
    ProjectionCheckpoint,
    utc_now,
)
from blackcell.kernel._json import canonical_json_bytes
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
    encode_daily_operator_v2_request,
)
from blackcell.workflows.daily_operator_v2_request import DailyOperatorV2Request
from blackcell.workflows.outcome_evidence import bind_evaluation_observation
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
    RUN_COMPLETED,
    RUN_EVENT_SCHEMA_VERSION_V2,
    RUN_FAILED,
    RUN_FAILURE_MEDIA_TYPE,
    RUN_FAILURE_SCHEMA_VERSION,
    RUN_STARTED,
    RUN_TRACE_MEDIA_TYPE,
    RUN_TRACE_SCHEMA_VERSION_V2,
    RUN_WORKFLOW,
    RUN_WORKFLOW_VERSION_V2,
    STATE_TRANSITION_RECORDED,
    TRACE_RECORDED,
    RunAlreadyExists,
    RunArtifactLink,
    RunIdentityConflict,
    RunInterrupted,
    RunOutcome,
    RunProtocolIntegrityError,
    RunTerminal,
    run_stream_id,
)
from blackcell.workflows.run_protocol_v2 import (
    V2_EVENT_PAYLOAD_FIELDS,
    FeedbackRunOpening,
)
from blackcell.workflows.state_transition import bind_and_accept_state_transition

_SOURCE = "blackcell.workflows.daily_operator"
_TERMINALS = frozenset({RUN_COMPLETED, RUN_FAILED})
_ARTIFACT_KEYS = frozenset(
    {"digest", "media_type", "encoding", "size_bytes", "schema_version", "logical_id"}
)
_USAGE_PAYLOAD_FIELDS = frozenset(
    {
        "usage_id",
        "input_tokens",
        "output_tokens",
        "latency_ms",
        "cost_microusd",
        "deterministic",
    }
)
Clock = Callable[[], datetime]


class KernelFeedbackRunRecorder:
    """Artifact-first SQLite writer for one strict v2 feedback-run aggregate."""

    def __init__(
        self,
        events: EventStore,
        artifacts: ArtifactStore,
        decision_journal: DecisionEvidenceJournal,
        execution_journal: ExecutionJournal,
        *,
        clock: Clock = utc_now,
    ) -> None:
        if events.path.resolve() != artifacts.database_path.resolve():
            raise ValueError("run events and artifacts must use the same kernel database")
        self._events = events
        self._artifacts = artifacts
        self._decision_journal = decision_journal
        self._execution_journal = execution_journal
        self._clock = clock

    def open(self, request: DailyOperatorV2Request) -> FeedbackRunOpening:
        """Atomically commit the complete request and mandatory EvaluationSpec."""

        run_id = request.run_id
        request_digest = daily_operator_v2_request_digest(request)
        stream = run_stream_id(run_id)
        existing = self._events.read_stream(stream)
        if existing:
            self._raise_existing_open(request_digest, run_id, existing)
        request_link = self._put_link(
            encode_daily_operator_v2_request(request),
            media_type=DAILY_OPERATOR_REQUEST_V2_MEDIA_TYPE,
            schema_version=DAILY_OPERATOR_REQUEST_V2_SCHEMA_VERSION,
            logical_id=request_digest,
        )
        spec = request.evaluation_spec
        spec_link = self._put_link(
            encode_evaluation_spec(spec),
            media_type=EVALUATION_SPEC_MEDIA_TYPE,
            schema_version=spec.schema_version,
            logical_id=spec.spec_id,
        )
        timestamp = self._clock()
        started = EventEnvelope.create(
            stream_id=stream,
            stream_sequence=1,
            event_type=RUN_STARTED,
            schema_version=RUN_EVENT_SCHEMA_VERSION_V2,
            actor=request.ingestion.actor,
            source=_SOURCE,
            payload={
                "run_id": run_id,
                "request_digest": request_digest,
                "workflow": RUN_WORKFLOW,
                "workflow_version": RUN_WORKFLOW_VERSION_V2,
                "task_id": request.context.task_id,
                "objective": request.context.objective,
                "domain": request.ingestion.domain,
                "observation_stream_id": request.ingestion.stream_id,
                "artifact": request_link.as_payload(),
            },
            recorded_at=timestamp,
            effective_at=timestamp,
            correlation_id=run_id,
        )
        evaluation_specified = EventEnvelope.create(
            stream_id=stream,
            stream_sequence=2,
            event_type=EVALUATION_SPECIFIED,
            schema_version=RUN_EVENT_SCHEMA_VERSION_V2,
            actor=request.ingestion.actor,
            source=_SOURCE,
            payload={
                "run_id": run_id,
                "evaluation_spec_id": spec.spec_id,
                "evaluation_spec_digest": spec_link.digest,
                "request_digest": request_digest,
                "artifact": spec_link.as_payload(),
            },
            recorded_at=timestamp,
            effective_at=timestamp,
            correlation_id=run_id,
            causation_id=started.event_id,
        )
        candidate = (started, evaluation_specified)
        self._validate_prefix(run_id, candidate, stored=False)
        try:
            stored = self._events.append_many(candidate, expected_sequences={stream: 0})
        except ConcurrencyError:
            self._raise_existing_open(
                request_digest,
                run_id,
                self._events.read_stream(stream),
            )
            raise AssertionError("existing opening classification must raise") from None
        self._validate_prefix(run_id, stored, stored=True)
        return FeedbackRunOpening(stored[0], stored[1])

    def record_initial_state(
        self,
        run_id: str,
        state: OperationalBeliefState,
    ) -> EventEnvelope:
        events = self._events_for(run_id)
        start = events[0]
        request = self._owner_workflow_request(start)
        self._require_state_scope(start, state)
        if state.effective_time_cutoff != request.initial_effective_time_cutoff:
            raise RunProtocolIntegrityError(
                "initial state effective cutoff differs from its request"
            )
        replayed = ProjectOperationalStateHandler(self._events, _NoCheckpoints()).handle(
            ProjectOperationalState(
                state.scope,
                as_of_time=state.effective_time_cutoff,
                as_of_position=state.cutoff_global_position,
            )
        )
        if replayed != state:
            raise RunProtocolIntegrityError("initial state differs from exact ledger replay")
        try:
            verify_requested_ingestion(request, start, state, self._events)
        except DailyOperatorV2EvidenceError as error:
            raise RunProtocolIntegrityError(str(error)) from error
        data = encode_operational_state_snapshot(state)
        link = self._put_link(
            data,
            media_type=OPERATIONAL_STATE_SNAPSHOT_MEDIA_TYPE,
            schema_version=OPERATIONAL_STATE_SNAPSHOT_SCHEMA_VERSION,
            logical_id=_snapshot_digest(data),
        )
        return self._append(
            run_id,
            INITIAL_STATE_RECORDED,
            {"run_id": run_id, **_state_payload(state, link), "artifact": link.as_payload()},
            events,
        )

    def record_context(self, run_id: str, frame: ContextFrame) -> EventEnvelope:
        events = self._events_for(run_id)
        start = events[0]
        request = self._owner_workflow_request(start)
        initial = self._owner_state(_event(events, INITIAL_STATE_RECORDED))
        expected = rebuild_requested_context(request, initial)
        if (
            frame != expected
            or frame.task_id != request.context.task_id
            or frame.state_domain != initial.scope.domain
            or frame.state_stream_id != initial.scope.stream_id
            or frame.state_global_position != initial.cutoff_global_position
            or frame.state_stream_position != initial.last_source_stream_sequence
            or frame.state_effective_time != initial.effective_time_cutoff
        ):
            raise RunProtocolIntegrityError("ContextFrame differs from its run initial state")
        link = self._put_link(
            encode_context_frame(frame),
            media_type=CONTEXT_FRAME_MEDIA_TYPE,
            schema_version=frame.schema_version,
            logical_id=frame.frame_id,
        )
        return self._append(
            run_id,
            CONTEXT_RECORDED,
            {
                "run_id": run_id,
                "frame_id": frame.frame_id,
                "task_id": frame.task_id,
                "state_domain": frame.state_domain,
                "state_stream_id": frame.state_stream_id,
                "state_global_position": frame.state_global_position,
                "state_stream_position": frame.state_stream_position,
                "source_packet_id": frame.source_packet_id,
                "source_selection_id": frame.source_selection_id,
                "artifact": link.as_payload(),
            },
            events,
        )

    def record_model_request(
        self,
        run_id: str,
        record: DecisionRequestRecord,
    ) -> EventEnvelope:
        events = self._events_for(run_id)
        request = record.request
        workflow_request = self._owner_workflow_request(events[0])
        context_event = _event(events, CONTEXT_RECORDED)
        frame = self._owner_context(context_event)
        if (
            self._journal_request(request.request_id) != record
            or request.run_id != run_id
            or request.correlation_id != run_id
            or request.causation_id != context_event.event_id
            or request.context_frame_id != frame.frame_id
            or request.objective != frame.objective
            or request.context_payload != frame.model_payload
            or request.evidence_event_ids != frame.provenance_event_ids
            or request.requirements != workflow_request.gateway_requirements
            or request.affordances != (_decision_affordance(workflow_request),)
        ):
            raise RunProtocolIntegrityError(
                "decision request differs from its request policy or causal ContextFrame"
            )
        self._clock_not_before(record.registered_at, "decision request")
        link = self._existing_link(
            record.request_artifact_digest,
            media_type=DECISION_REQUEST_MEDIA_TYPE,
            schema_version=request.schema_version,
            logical_id=request.request_digest,
        )
        return self._append(
            run_id,
            MODEL_REQUESTED,
            {
                "run_id": run_id,
                "request_id": request.request_id,
                "request_digest": request.request_digest,
                "context_frame_id": request.context_frame_id,
                "artifact": link.as_payload(),
            },
            events,
        )

    def record_model_attempt(
        self,
        run_id: str,
        preparation: DecisionPreparation,
        attempt_record: DecisionAttemptRecord,
    ) -> EventEnvelope:
        events = self._events_for(run_id)
        request = self._owner_request(_event(events, MODEL_REQUESTED))
        attempt = attempt_record.attempt
        previous = tuple(item for item in events if item.event_type == MODEL_ATTEMPT_RECORDED)
        if (
            self._journal_preparation(request.request_id) != preparation
            or self._journal_attempt(request.request_id) != attempt_record
            or preparation.request_record.request != request
            or attempt.request_id != request.request_id
            or attempt.request_digest != request.request_digest
            or preparation.route.route_id != attempt.route_id
            or preparation.route.capability != request.capability
            or attempt.attempt_number != 1
            or attempt.started_at < request.requested_at
        ):
            raise RunProtocolIntegrityError("decision attempt differs from its durable preparation")
        if previous and _text(previous[0].payload, "attempt_id") != attempt.attempt_id:
            raise RunIdentityConflict("v2 supports only one fenced decision attempt")
        self._clock_not_before(attempt.started_at, "decision attempt")
        route_link = self._existing_link(
            preparation.route_artifact_digest,
            media_type=DECISION_ROUTE_MEDIA_TYPE,
            schema_version=preparation.route.schema_version,
            logical_id=preparation.route.route_id,
        )
        link = self._existing_link(
            attempt_record.attempt_artifact_digest,
            media_type=DECISION_ATTEMPT_MEDIA_TYPE,
            schema_version=attempt.schema_version,
            logical_id=attempt.attempt_id,
        )
        return self._append(
            run_id,
            MODEL_ATTEMPT_RECORDED,
            {
                "run_id": run_id,
                "attempt_id": attempt.attempt_id,
                "request_id": attempt.request_id,
                "request_digest": attempt.request_digest,
                "route_id": attempt.route_id,
                "attempt_number": attempt.attempt_number,
                "route_artifact": route_link.as_payload(),
                "artifact": link.as_payload(),
            },
            events,
        )

    def record_model_terminal(
        self,
        run_id: str,
        record: DecisionTerminalRecord,
    ) -> EventEnvelope:
        events = self._events_for(run_id)
        request = self._owner_request(_event(events, MODEL_REQUESTED))
        attempts = tuple(item for item in events if item.event_type == MODEL_ATTEMPT_RECORDED)
        if self._journal_terminal(request.request_id) != record:
            raise RunProtocolIntegrityError("decision terminal is not owned by its durable journal")
        if isinstance(record, DecisionSuccessRecord):
            response = record.response
            attempt = record.attempt_record.attempt
            decoded_response = decode_decision_response(
                self._artifacts.get_bytes(record.response_artifact_digest, verify=True),
                expected_response_id=response.response_id,
                request=request,
            )
            if (
                record.preparation.request_record.request != request
                or len(attempts) != 1
                or self._owner_attempt(attempts[0]) != attempt
                or decoded_response != response
                or response.completed_at < attempt.started_at
            ):
                raise RunProtocolIntegrityError(
                    "decision success differs from its durable request or attempt"
                )
            self._clock_not_before(response.completed_at, "decision response")
            response_link = self._existing_link(
                record.response_artifact_digest,
                media_type=DECISION_RESPONSE_MEDIA_TYPE,
                schema_version=response.schema_version,
                logical_id=response.response_id,
            )
            usage_link = self._existing_link(
                record.usage_artifact_digest,
                media_type=DECISION_USAGE_MEDIA_TYPE,
                schema_version=record.usage.schema_version,
                logical_id=record.usage.usage_id,
            )
            return self._append(
                run_id,
                MODEL_RESPONDED,
                {
                    "run_id": run_id,
                    "response_id": response.response_id,
                    "request_id": response.request_id,
                    "request_digest": response.request_digest,
                    "attempt_id": response.attempt_id,
                    "route_id": response.route_id,
                    "proposal_id": response.proposal.proposal_id,
                    **_usage_payload(record.usage),
                    "artifact": response_link.as_payload(),
                    "usage_artifact": usage_link.as_payload(),
                },
                events,
            )

        if not isinstance(record, DecisionFailureRecord):
            raise TypeError("record must be a DecisionSuccessRecord or DecisionFailureRecord")
        failure = record.failure
        if record.request_record.request != request:
            raise RunProtocolIntegrityError("decision failure belongs to a different request")
        attempt_record = record.attempt_record
        if attempt_record is None:
            if attempts:
                raise RunProtocolIntegrityError("decision failure omits its recorded attempt")
        elif len(attempts) != 1 or self._owner_attempt(attempts[0]) != attempt_record.attempt:
            raise RunProtocolIntegrityError("decision failure differs from its recorded attempt")
        if failure.failed_at < request.requested_at:
            raise RunProtocolIntegrityError("decision failure predates its request")
        self._clock_not_before(failure.failed_at, "decision failure")
        failure_link = self._existing_link(
            record.failure_artifact_digest,
            media_type=DECISION_FAILURE_MEDIA_TYPE,
            schema_version=failure.schema_version,
            logical_id=failure.failure_id,
        )
        route_link = None
        if record.preparation is not None:
            route = record.preparation.route
            route_link = self._existing_link(
                record.preparation.route_artifact_digest,
                media_type=DECISION_ROUTE_MEDIA_TYPE,
                schema_version=route.schema_version,
                logical_id=route.route_id,
            )
        usage_link = None
        if record.usage is not None and record.usage_artifact_digest is not None:
            usage_link = self._existing_link(
                record.usage_artifact_digest,
                media_type=DECISION_USAGE_MEDIA_TYPE,
                schema_version=record.usage.schema_version,
                logical_id=record.usage.usage_id,
            )
        return self._append(
            run_id,
            MODEL_FAILED,
            {
                "run_id": run_id,
                "failure_id": failure.failure_id,
                "request_id": failure.request_id,
                "request_digest": failure.request_digest,
                "kind": failure.kind.value,
                "code": failure.code,
                "retryable": failure.retryable,
                "route_id": failure.route_id,
                "attempt_id": failure.attempt_id,
                **_usage_payload(record.usage),
                "route_artifact": None if route_link is None else route_link.as_payload(),
                "artifact": failure_link.as_payload(),
                "usage_artifact": None if usage_link is None else usage_link.as_payload(),
            },
            events,
        )

    def record_proposal(self, run_id: str, proposal: ActionProposal) -> EventEnvelope:
        events = self._events_for(run_id)
        response = self._owner_response(_event(events, MODEL_RESPONDED))
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
            raise RunProtocolIntegrityError("ActionProposal differs from model response")
        link = self._put_link(
            encode_action_proposal(proposal),
            media_type=ACTION_PROPOSAL_MEDIA_TYPE,
            schema_version=proposal.schema_version,
            logical_id=proposal.proposal_digest,
        )
        return self._append(
            run_id,
            PROPOSAL_RECORDED,
            {
                "run_id": run_id,
                "proposal_id": proposal.proposal_id,
                "proposal_digest": proposal.proposal_digest,
                "action_digest": proposal.action_digest,
                "context_frame_id": proposal.context_frame_id,
                "artifact": link.as_payload(),
            },
            events,
        )

    def record_constraints(
        self,
        run_id: str,
        evaluation: ConstraintEvaluation,
    ) -> EventEnvelope:
        events = self._events_for(run_id)
        request = self._owner_workflow_request(events[0])
        proposal = self._owner_proposal(_event(events, PROPOSAL_RECORDED))
        frame = self._owner_context(_event(events, CONTEXT_RECORDED))
        recomputed = DeterministicConstraintSolver().handle(request.constraints, frame)
        if recomputed != evaluation or evaluation.context_frame_id != proposal.context_frame_id:
            raise RunProtocolIntegrityError(
                "constraint evaluation differs from the declared deterministic policy"
            )
        self._clock_not_before(evaluation.evaluated_at, "constraint evaluation")
        link = self._put_link(
            encode_constraint_evaluation(evaluation),
            media_type=CONSTRAINT_EVALUATION_MEDIA_TYPE,
            schema_version=evaluation.schema_version,
            logical_id=evaluation.evaluation_id,
        )
        return self._append(
            run_id,
            CONSTRAINTS_EVALUATED,
            {
                "run_id": run_id,
                "evaluation_id": evaluation.evaluation_id,
                "context_frame_id": evaluation.context_frame_id,
                "proof_ids": tuple(item.proof_id for item in evaluation.proofs),
                "safe": evaluation.safe,
                "artifact": link.as_payload(),
            },
            events,
        )

    def record_authorization(
        self,
        run_id: str,
        decision: AuthorizationDecision,
    ) -> EventEnvelope:
        events = self._events_for(run_id)
        request = self._owner_workflow_request(events[0])
        proposal = self._owner_proposal(_event(events, PROPOSAL_RECORDED))
        constraints = self._owner_constraints(_event(events, CONSTRAINTS_EVALUATED))
        expected = ActionAuthorizer().handle(
            AuthorizeAction(
                proposal=proposal,
                affordance=request.authorization_affordance,
                evaluated_at=request.constraints.evaluated_at,
                context_evidence_event_ids=self._owner_context(
                    _event(events, CONTEXT_RECORDED)
                ).provenance_event_ids,
                approval_granted=request.approval_granted,
            ),
            constraints,
        )
        if decision != expected:
            raise RunProtocolIntegrityError(
                "AuthorizationDecision differs from the declared deterministic policy"
            )
        self._clock_not_before(decision.evaluated_at, "authorization decision")
        link = self._put_link(
            encode_authorization_decision(decision),
            media_type=AUTHORIZATION_DECISION_MEDIA_TYPE,
            schema_version=decision.schema_version,
            logical_id=decision.decision_id,
        )
        return self._append(
            run_id,
            AUTHORIZATION_DECIDED,
            {
                "run_id": run_id,
                "decision_id": decision.decision_id,
                "proposal_id": decision.proposal_id,
                "constraint_evaluation_id": decision.constraint_evaluation_id,
                "outcome": decision.outcome.value,
                "artifact": link.as_payload(),
            },
            events,
        )

    def record_execution(
        self,
        run_id: str,
        entry: ExecutionJournalEntry,
    ) -> EventEnvelope:
        events = self._events_for(run_id)
        request = self._owner_workflow_request(events[0])
        proposal = self._owner_proposal(_event(events, PROPOSAL_RECORDED))
        authorization = self._owner_authorization(_event(events, AUTHORIZATION_DECIDED))
        if (
            self._execution_entry(entry.journal_position) != entry
            or entry.status is ExecutionJournalStatus.PREPARED
            or entry.current_result is None
            or entry.active_claim is not None
        ):
            raise RunProtocolIntegrityError("execution journal entry is not terminal")
        result = entry.current_result
        preparation_link = self._existing_link(
            entry.binding.preparation_id,
            media_type=EXECUTION_PREPARATION_MEDIA_TYPE,
            schema_version="execution-preparation/v1",
            logical_id=entry.binding.preparation_id,
        )
        preparation = deserialize_execution_preparation(
            self._artifacts.get_bytes(preparation_link.digest, verify=True),
            expected_preparation_id=preparation_link.digest,
        )
        result_link = self._existing_link(
            result.result_id,
            media_type=EXECUTION_RESULT_MEDIA_TYPE,
            schema_version=result.schema_version,
            logical_id=result.result_id,
        )
        binding = preparation.binding
        exact = (
            preparation.run_id == run_id
            and entry.binding == binding
            and authorization.outcome is AuthorizationOutcome.ALLOW
            and preparation.authorization_decision_id == authorization.decision_id
            and preparation.authorized_action_digest == authorization.authorized_action_digest
            and preparation.invocation.proposal_id == proposal.proposal_id
            and preparation.invocation.affordance == proposal.affordance
            and tuple((item.name, item.value) for item in preparation.invocation.arguments)
            == tuple((item.name, item.value) for item in proposal.arguments)
            and result.invocation_id == binding.invocation_id
            and result.proposal_id == binding.proposal_id
            and result.authorization_decision_id == binding.authorization_decision_id
            and result.affordance == binding.affordance
            and result.adapter_id == binding.adapter_id
            and result.idempotency_key == binding.idempotency_key
            and result.authorized_action_digest == binding.authorized_action_digest
            and result.execution_identity_digest == binding.execution_identity_digest
            and result.started_at >= authorization.evaluated_at
            and preparation.invocation.invocation_id == request.invocation_id
            and preparation.invocation.idempotency_key == request.idempotency_key
            and preparation.definition == request.execution_affordance
        )
        definition_read_only = preparation.definition.side_effect_class is SideEffectClass.READ_ONLY
        if not exact or authorization.authorized_read_only != definition_read_only:
            raise RunProtocolIntegrityError("execution differs from its prepared authorized action")
        self._clock_not_before(result.completed_at, "execution result")
        return self._append(
            run_id,
            EXECUTION_RECORDED,
            {
                "run_id": run_id,
                "preparation_id": preparation.preparation_id,
                "result_id": result.result_id,
                "invocation_id": result.invocation_id,
                "proposal_id": proposal.proposal_id,
                "proposal_digest": proposal.proposal_digest,
                "authorization_decision_id": result.authorization_decision_id,
                "authorized_action_digest": result.authorized_action_digest,
                "execution_identity_digest": result.execution_identity_digest,
                "status": result.status.value,
                "affordance": result.affordance,
                "adapter_id": result.adapter_id,
                "adapter_contract_version": preparation.adapter_contract_version,
                "journal_position": entry.journal_position,
                "completed_at": result.completed_at.isoformat(),
                "arguments": tuple(
                    {"name": item.name, "value": item.value} for item in proposal.arguments
                ),
                "preparation_artifact": preparation_link.as_payload(),
                "artifact": result_link.as_payload(),
            },
            events,
        )

    def record_outcome(
        self,
        run_id: str,
        observation: OutcomeObservation,
        *,
        outcome_event_ids: tuple[str, ...],
    ) -> EventEnvelope:
        events = self._events_for(run_id)
        request = self._owner_workflow_request(events[0])
        execution_event = _event(events, EXECUTION_RECORDED)
        execution = self._owner_execution(execution_event)
        spec = self._owner_spec(_event(events, EVALUATION_SPECIFIED))
        targets = {(item.subject, item.predicate) for item in spec.criteria}
        if (
            observation.binding.run_id != run_id
            or observation.binding.execution_result_id != execution.result_id
            or observation.binding.execution_identity_digest != execution.execution_identity_digest
            or observation.evaluation_spec_id != spec.spec_id
            or observation.domain != request.ingestion.domain
            or observation.stream_id != request.ingestion.stream_id
            or observation.observer_id != request.expected_observer_id
            or observation.observer_contract_version != request.expected_observer_contract_version
            or any(item.key not in targets for item in observation.claims)
        ):
            raise RunProtocolIntegrityError("OutcomeObservation cross-identity is inconsistent")
        bind_evaluation_observation(
            observation,
            self._events,
            self._artifacts,
            execution_event_id=execution_event.event_id,
            outcome_event_ids=outcome_event_ids,
        )
        self._clock_not_before(observation.observed_at, "outcome observation")
        link = self._put_link(
            encode_outcome_observation(observation),
            media_type=OUTCOME_OBSERVATION_MEDIA_TYPE,
            schema_version=observation.schema_version,
            logical_id=observation.observation_digest,
        )
        return self._append(
            run_id,
            OUTCOME_OBSERVED,
            {
                "run_id": run_id,
                "observation_id": observation.observation_id,
                "observation_digest": observation.observation_digest,
                "evaluation_spec_id": observation.evaluation_spec_id,
                "execution_binding_id": observation.binding.binding_id,
                "status": observation.status.value,
                "outcome_event_ids": outcome_event_ids,
                "artifact": link.as_payload(),
            },
            events,
        )

    def record_outcome_state(
        self,
        run_id: str,
        state: OperationalBeliefState,
    ) -> EventEnvelope:
        events = self._events_for(run_id)
        initial = self._owner_state(_event(events, INITIAL_STATE_RECORDED))
        outcome = self._owner_outcome(_event(events, OUTCOME_OBSERVED))
        source_ids = _strings(_event(events, OUTCOME_OBSERVED).payload, "outcome_event_ids")
        source_positions = tuple(
            cast("int", _required_occurrence(self._events, event_id).global_position)
            for event_id in source_ids
        )
        if (
            state.scope != initial.scope
            or state.cutoff_global_position <= initial.cutoff_global_position
            or state.cutoff_global_position < max(source_positions)
            or state.effective_time_cutoff is None
            or state.effective_time_cutoff < outcome.observed_at
        ):
            raise RunProtocolIntegrityError("outcome state does not include its observed evidence")
        replayed = ProjectOperationalStateHandler(self._events, _NoCheckpoints()).handle(
            ProjectOperationalState(
                state.scope,
                as_of_time=state.effective_time_cutoff,
                as_of_position=state.cutoff_global_position,
            )
        )
        if replayed != state:
            raise RunProtocolIntegrityError("outcome state differs from exact ledger replay")
        data = encode_operational_state_snapshot(state)
        link = self._put_link(
            data,
            media_type=OPERATIONAL_STATE_SNAPSHOT_MEDIA_TYPE,
            schema_version=OPERATIONAL_STATE_SNAPSHOT_SCHEMA_VERSION,
            logical_id=_snapshot_digest(data),
        )
        return self._append(
            run_id,
            OUTCOME_STATE_RECORDED,
            {"run_id": run_id, **_state_payload(state, link), "artifact": link.as_payload()},
            events,
        )

    def record_evaluation(
        self,
        run_id: str,
        evaluation: OutcomeEvaluation,
    ) -> EventEnvelope:
        events = self._events_for(run_id)
        spec = self._owner_spec(_event(events, EVALUATION_SPECIFIED))
        authorization = self._owner_authorization(_event(events, AUTHORIZATION_DECIDED))
        initial = self._owner_state(_event(events, INITIAL_STATE_RECORDED))
        execution_event = next(
            (item for item in events if item.event_type == EXECUTION_RECORDED), None
        )
        execution = None if execution_event is None else self._owner_execution(execution_event)
        outcome_event = next((item for item in events if item.event_type == OUTCOME_OBSERVED), None)
        observation = None
        if outcome_event is not None:
            owner = self._owner_outcome(outcome_event)
            observation = bind_evaluation_observation(
                owner,
                self._events,
                self._artifacts,
                execution_event_id=cast("EventEnvelope", execution_event).event_id,
                outcome_event_ids=_strings(outcome_event.payload, "outcome_event_ids"),
            )
        command = EvaluateOutcome(
            run_id=run_id,
            spec=spec,
            authorization_outcome=EvaluationAuthorizationOutcome(authorization.outcome.value),
            execution_status=(
                None if execution is None else EvaluationExecutionStatus(execution.status.value)
            ),
            execution_event_id=None if execution_event is None else execution_event.event_id,
            execution_binding_id=(
                None if observation is None and execution is None else _execution_binding_id(events)
            ),
            observation=observation,
            initial_state_position=initial.cutoff_global_position,
        )
        recomputed = OutcomeEvaluator(clock=lambda: evaluation.evaluated_at).handle(command)
        if recomputed != evaluation:
            raise RunProtocolIntegrityError("OutcomeEvaluation differs from deterministic replay")
        self._clock_not_before(evaluation.evaluated_at, "outcome evaluation")
        link = self._put_link(
            encode_outcome_evaluation(evaluation),
            media_type=OUTCOME_EVALUATION_MEDIA_TYPE,
            schema_version=evaluation.schema_version,
            logical_id=evaluation.evaluation_id,
        )
        return self._append(
            run_id,
            EVALUATION_RECORDED,
            {
                "run_id": run_id,
                "evaluation_id": evaluation.evaluation_id,
                "evaluation_spec_id": evaluation.evaluation_spec_id,
                "verdict": evaluation.verdict.value,
                "artifact": link.as_payload(),
            },
            events,
        )

    def record_accepted_transition(
        self,
        run_id: str,
        transition: AcceptedStateTransition,
    ) -> EventEnvelope:
        events = self._events_for(run_id)
        expected = bind_and_accept_state_transition(
            run_id,
            self._events,
            self._artifacts,
        ).transition
        if expected is None or expected != transition:
            raise RunProtocolIntegrityError(
                "AcceptedStateTransition differs from the verified run evidence"
            )
        initial_event = _event(events, INITIAL_STATE_RECORDED)
        outcome_event = _event(events, OUTCOME_STATE_RECORDED)
        evaluation_event = _event(events, EVALUATION_RECORDED)
        if (
            transition.run_id != run_id
            or transition.initial_state.snapshot_digest
            != _text(initial_event.payload, "snapshot_digest")
            or transition.outcome_state.snapshot_digest
            != _text(outcome_event.payload, "snapshot_digest")
            or transition.evaluation.evaluation_id
            != _text(evaluation_event.payload, "evaluation_id")
        ):
            raise RunProtocolIntegrityError("AcceptedStateTransition differs from its run")
        link = self._put_link(
            encode_accepted_state_transition(transition),
            media_type=ACCEPTED_STATE_TRANSITION_MEDIA_TYPE,
            schema_version=transition.schema_version,
            logical_id=transition.transition_id,
        )
        return self._append(
            run_id,
            STATE_TRANSITION_RECORDED,
            {
                "run_id": run_id,
                "transition_id": transition.transition_id,
                "initial_snapshot_digest": transition.initial_state.snapshot_digest,
                "outcome_snapshot_digest": transition.outcome_state.snapshot_digest,
                "evaluation_id": transition.evaluation.evaluation_id,
                "accepted_claim_ids": transition.accepted_claim_ids,
                "accepted_source_event_ids": transition.accepted_source_event_ids,
                "artifact": link.as_payload(),
            },
            events,
        )

    def _record_trace(self, run_id: str, outcome: RunOutcome) -> EventEnvelope:
        events = self._events_for(run_id)
        if events[-1].event_type in _TERMINALS:
            raise RunAlreadyExists(f"run {run_id!r} is already terminal")
        existing = next((item for item in events if item.event_type == TRACE_RECORDED), None)
        if existing is not None:
            if _text(existing.payload, "outcome") != outcome.value:
                raise RunIdentityConflict("recorded trace has a different outcome")
            return existing
        if outcome is not RunOutcome.FAILED:
            actual = _material_outcome(events)
            if actual is not outcome:
                raise RunProtocolIntegrityError("trace outcome differs from its material run")
        entries = _trace_entries(events)
        link = self._put_link(
            canonical_json_bytes(
                {
                    "schema_version": RUN_TRACE_SCHEMA_VERSION_V2,
                    "run_id": run_id,
                    "run_stream_id": run_stream_id(run_id),
                    "outcome": outcome.value,
                    "entries": entries,
                }
            ),
            media_type=RUN_TRACE_MEDIA_TYPE,
            schema_version=RUN_TRACE_SCHEMA_VERSION_V2,
            logical_id=f"trace:{run_id}",
        )
        return self._append(
            run_id,
            TRACE_RECORDED,
            {
                "run_id": run_id,
                "outcome": outcome.value,
                "entry_count": len(entries),
                "artifact": link.as_payload(),
            },
            events,
        )

    def complete(self, run_id: str, outcome: RunOutcome) -> RunTerminal:
        if outcome is RunOutcome.FAILED:
            raise ValueError("failed runs must use fail()")
        events = self._events_for(run_id)
        if events[-1].event_type in _TERMINALS:
            raise RunAlreadyExists(f"run {run_id!r} is already terminal")
        self._require_no_unrecorded_decision(run_id, events)
        self._require_no_unrecorded_execution(run_id, events)
        acceptance = bind_and_accept_state_transition(
            run_id,
            self._events,
            self._artifacts,
        )
        if acceptance.transition is not None and not any(
            event.event_type == STATE_TRANSITION_RECORDED for event in events
        ):
            raise RunProtocolIntegrityError(
                "completed run must record its derived accepted transition"
            )
        trace = self._record_trace(run_id, outcome)
        events = self._events_for(run_id)
        authorization = _event(events, AUTHORIZATION_DECIDED)
        execution = next((item for item in events if item.event_type == EXECUTION_RECORDED), None)
        terminal = self._append(
            run_id,
            RUN_COMPLETED,
            {
                "run_id": run_id,
                "outcome": outcome.value,
                "authorization_outcome": _text(authorization.payload, "outcome"),
                "execution_status": (
                    None if execution is None else _text(execution.payload, "status")
                ),
                "trace_artifact_digest": _artifact_digest(trace),
            },
            events,
        )
        return RunTerminal(trace, terminal)

    def fail(self, run_id: str, *, phase: str, error_type: str) -> RunTerminal:
        if not phase.strip() or not error_type.strip():
            raise ValueError("failure phase and error type must not be empty")
        events = self._events_for(run_id)
        if events[-1].event_type in _TERMINALS:
            raise RunAlreadyExists(f"run {run_id!r} is already terminal")
        self._require_no_unrecorded_decision(run_id, events)
        self._require_no_unrecorded_execution(run_id, events)
        failure_link = self._put_link(
            canonical_json_bytes(
                {
                    "schema_version": RUN_FAILURE_SCHEMA_VERSION,
                    "run_id": run_id,
                    "phase": phase,
                    "error_type": error_type,
                }
            ),
            media_type=RUN_FAILURE_MEDIA_TYPE,
            schema_version=RUN_FAILURE_SCHEMA_VERSION,
            logical_id=f"failure:{run_id}",
        )
        trace = self._record_trace(run_id, RunOutcome.FAILED)
        events = self._events_for(run_id)
        terminal = self._append(
            run_id,
            RUN_FAILED,
            {
                "run_id": run_id,
                "outcome": RunOutcome.FAILED.value,
                "phase": phase,
                "error_type": error_type,
                "trace_artifact_digest": _artifact_digest(trace),
                "artifact": failure_link.as_payload(),
            },
            events,
        )
        return RunTerminal(trace, terminal)

    def _append(
        self,
        run_id: str,
        event_type: str,
        payload: Mapping[str, JsonInput],
        events: tuple[EventEnvelope, ...],
    ) -> EventEnvelope:
        existing = self._same_delivery(events, event_type, payload)
        if existing is not None:
            return existing
        if events[-1].event_type in _TERMINALS:
            raise RunAlreadyExists(f"run {run_id!r} is already terminal")
        timestamp = self._clock()
        event = EventEnvelope.create(
            stream_id=run_stream_id(run_id),
            stream_sequence=len(events) + 1,
            event_type=event_type,
            schema_version=RUN_EVENT_SCHEMA_VERSION_V2,
            actor=events[0].actor,
            source=_SOURCE,
            payload=payload,
            recorded_at=timestamp,
            effective_at=timestamp,
            correlation_id=run_id,
            causation_id=events[-1].event_id,
        )
        self._validate_prefix(run_id, (*events, event), stored=False)
        try:
            stored = self._events.append(event, expected_sequence=len(events))
        except ConcurrencyError as error:
            current = self._events_for(run_id)
            recovered = self._same_delivery(current, event_type, payload)
            if recovered is not None:
                return recovered
            raise RunInterrupted(
                f"run {run_id!r} advanced concurrently while recording {event_type!r}"
            ) from error
        self._validate_prefix(run_id, (*events, stored), stored=True)
        return stored

    def _same_delivery(
        self,
        events: Sequence[EventEnvelope],
        event_type: str,
        payload: Mapping[str, JsonInput],
    ) -> EventEnvelope | None:
        matches = tuple(item for item in events if item.event_type == event_type)
        for event in matches:
            if event.payload == payload:
                return event
        if not matches:
            return None
        raise RunIdentityConflict(f"run event {event_type!r} already has different evidence")

    def _events_for(self, run_id: str) -> tuple[EventEnvelope, ...]:
        events = self._events.read_stream(run_stream_id(run_id))
        if not events:
            raise RunProtocolIntegrityError(f"run {run_id!r} has not started")
        self._validate_prefix(run_id, events, stored=True)
        return events

    def _validate_prefix(
        self,
        run_id: str,
        events: Sequence[EventEnvelope],
        *,
        stored: bool,
    ) -> None:
        validate_run_grammar(events, run_id=run_id)
        actor = events[0].actor
        previous_time: datetime | None = None
        for event in events:
            if event.source != _SOURCE or event.actor != actor:
                raise RunProtocolIntegrityError("v2 run source or actor is not canonical")
            if stored and event.global_position is None:
                raise RunProtocolIntegrityError("v2 run event is not a stored occurrence")
            if previous_time is not None and event.recorded_at < previous_time:
                raise RunProtocolIntegrityError("v2 run record time regresses")
            previous_time = event.recorded_at
            expected_keys = V2_EVENT_PAYLOAD_FIELDS.get(event.event_type)
            if expected_keys is None or frozenset(event.payload) != expected_keys:
                raise RunProtocolIntegrityError(
                    f"v2 event {event.event_type!r} payload fields are not exact"
                )
            self._verify_event_artifacts(event, events)
        self._verify_cross_bindings(run_id, events, stored=stored)
        traces = tuple(item for item in events if item.event_type == TRACE_RECORDED)
        if traces:
            trace = traces[0]
            prior = tuple(item for item in events if item.stream_sequence < trace.stream_sequence)
            expected = {
                "schema_version": RUN_TRACE_SCHEMA_VERSION_V2,
                "run_id": run_id,
                "run_stream_id": run_stream_id(run_id),
                "outcome": _text(trace.payload, "outcome"),
                "entries": _trace_entries(prior),
            }
            if self._artifacts.get_json(_artifact_digest(trace)) != expected:
                raise RunProtocolIntegrityError("v2 trace artifact differs from its exact prefix")

    def _verify_cross_bindings(
        self,
        run_id: str,
        events: Sequence[EventEnvelope],
        *,
        stored: bool,
    ) -> None:
        by_type = {event.event_type: event for event in events}
        start = events[0]
        request = self._owner_workflow_request(start)
        if request.run_id != run_id or request.ingestion.actor != start.actor:
            raise RunProtocolIntegrityError("v2 request differs from its run identity")

        spec_event = by_type.get(EVALUATION_SPECIFIED)
        if spec_event is None:
            return
        spec = self._owner_spec(spec_event)
        if spec != request.evaluation_spec or _text(
            spec_event.payload, "request_digest"
        ) != daily_operator_v2_request_digest(request):
            raise RunProtocolIntegrityError("EvaluationSpec differs from the immutable request")

        initial_event = by_type.get(INITIAL_STATE_RECORDED)
        if initial_event is None:
            self._verify_terminal_summary(events)
            return
        initial = self._owner_state(initial_event)
        self._require_state_scope(start, initial)
        if initial.effective_time_cutoff != request.initial_effective_time_cutoff:
            raise RunProtocolIntegrityError(
                "initial state cutoff differs from the immutable request"
            )
        if (
            stored
            and initial_event.global_position is not None
            and initial.cutoff_global_position >= initial_event.global_position
        ):
            raise RunProtocolIntegrityError("initial state cutoff must precede its run event")
        self._verify_state_replay(initial, label="initial state")
        try:
            verify_requested_ingestion(request, start, initial, self._events)
        except DailyOperatorV2EvidenceError as error:
            raise RunProtocolIntegrityError(str(error)) from error

        context_event = by_type.get(CONTEXT_RECORDED)
        if context_event is None:
            self._verify_terminal_summary(events)
            return
        frame = self._owner_context(context_event)
        expected_frame = rebuild_requested_context(request, initial)
        if (
            frame != expected_frame
            or frame.task_id != request.context.task_id
            or frame.objective != request.context.objective
            or frame.generated_at != request.context.generated_at
            or frame.state_domain != initial.scope.domain
            or frame.state_stream_id != initial.scope.stream_id
            or frame.state_global_position != initial.cutoff_global_position
            or frame.state_stream_position != initial.last_source_stream_sequence
            or frame.state_effective_time != initial.effective_time_cutoff
            or context_event.recorded_at < frame.generated_at
        ):
            raise RunProtocolIntegrityError(
                "ContextFrame differs from its request or initial state"
            )

        decision_event = by_type.get(MODEL_REQUESTED)
        if decision_event is None:
            self._verify_terminal_summary(events)
            return
        decision = self._owner_request(decision_event)
        owned_request = self._journal_request(decision.request_id)
        if (
            owned_request is None
            or owned_request.request != decision
            or owned_request.request_artifact_digest != _artifact_link(decision_event).digest
            or decision.run_id != run_id
            or decision.correlation_id != run_id
            or decision.causation_id != context_event.event_id
            or decision.context_frame_id != frame.frame_id
            or decision.objective != frame.objective
            or decision.context_payload != frame.model_payload
            or decision.evidence_event_ids != frame.provenance_event_ids
            or decision.requirements != request.gateway_requirements
            or decision.affordances != (_decision_affordance(request),)
            or decision_event.recorded_at < decision.requested_at
        ):
            raise RunProtocolIntegrityError("decision request escapes its immutable run policy")

        attempt_event = by_type.get(MODEL_ATTEMPT_RECORDED)
        attempt: DecisionAttempt | None = None
        route: DecisionRoute | None = None
        if attempt_event is not None:
            attempt = self._owner_attempt(attempt_event)
            route = self._owner_route(attempt_event)
            owned_preparation = self._journal_preparation(decision.request_id)
            owned_attempt = self._journal_attempt(decision.request_id)
            if (
                owned_preparation is None
                or owned_attempt is None
                or owned_preparation.route != route
                or owned_preparation.route_artifact_digest
                != _artifact_link(attempt_event, "route_artifact").digest
                or owned_attempt.attempt != attempt
                or owned_attempt.attempt_artifact_digest != _artifact_link(attempt_event).digest
                or attempt.request_id != decision.request_id
                or attempt.request_digest != decision.request_digest
                or attempt.route_id != route.route_id
                or attempt.attempt_number != 1
                or attempt.started_at < decision.requested_at
                or attempt.started_at < route.selected_at
                or attempt_event.recorded_at < attempt.started_at
                or route.capability != decision.capability
                or (decision.locality.value == "local-only" and not route.local)
                or (decision.deterministic_required and not route.deterministic)
            ):
                raise RunProtocolIntegrityError("decision attempt or route violates its request")

        response_event = by_type.get(MODEL_RESPONDED)
        response: DecisionResponse | None = None
        if response_event is not None:
            if attempt is None or route is None:
                raise RunProtocolIntegrityError("decision response lacks its attempt and route")
            response = decode_decision_response(
                self._owner_data(response_event),
                expected_response_id=_artifact_link(response_event).digest,
                request=decision,
            )
            usage = self._owner_usage(response_event)
            owned_terminal = self._journal_terminal(decision.request_id)
            if (
                not isinstance(owned_terminal, DecisionSuccessRecord)
                or owned_terminal.response != response
                or owned_terminal.response_artifact_digest != _artifact_link(response_event).digest
                or owned_terminal.usage != usage
                or owned_terminal.usage_artifact_digest
                != _artifact_link(response_event, "usage_artifact").digest
                or response.request_id != decision.request_id
                or response.request_digest != decision.request_digest
                or response.attempt_id != attempt.attempt_id
                or response.route_id != route.route_id
                or response.completed_at < attempt.started_at
                or usage.request_id != decision.request_id
                or usage.attempt_id != attempt.attempt_id
                or not _usage_within_budget(usage, decision, route)
                or response_event.recorded_at < response.completed_at
            ):
                raise RunProtocolIntegrityError("decision response or usage violates its attempt")

        failure_event = by_type.get(MODEL_FAILED)
        if failure_event is not None:
            failure = self._owner_failure(failure_event)
            owned_terminal = self._journal_terminal(decision.request_id)
            route_link = failure_event.payload.get("route_artifact")
            usage_link = failure_event.payload.get("usage_artifact")
            failure_route = (
                None
                if route_link is None
                else decode_decision_route(
                    self._owner_data(failure_event, "route_artifact"),
                    expected_route_id=_artifact_link(failure_event, "route_artifact").digest,
                )
            )
            failure_usage = (
                None
                if usage_link is None
                else decode_decision_usage(
                    self._owner_data(failure_event, "usage_artifact"),
                    expected_usage_id=_artifact_link(failure_event, "usage_artifact").digest,
                )
            )
            if (
                not isinstance(owned_terminal, DecisionFailureRecord)
                or owned_terminal.failure != failure
                or owned_terminal.failure_artifact_digest != _artifact_link(failure_event).digest
                or owned_terminal.usage != failure_usage
                or owned_terminal.usage_artifact_digest
                != (
                    None
                    if usage_link is None
                    else _artifact_link(failure_event, "usage_artifact").digest
                )
                or (
                    failure_route is not None
                    and (
                        owned_terminal.preparation is None
                        or owned_terminal.preparation.route != failure_route
                    )
                )
                or failure.request_id != decision.request_id
                or failure.request_digest != decision.request_digest
                or failure.failed_at < decision.requested_at
                or (failure.route_id is None) != (failure_route is None)
                or (failure.attempt_id is None) != (attempt is None)
                or (failure_route is not None and failure_route.route_id != failure.route_id)
                or (failure_route is not None and failure_route.capability != decision.capability)
                or (
                    failure_route is not None
                    and decision.locality.value == "local-only"
                    and not failure_route.local
                )
                or (attempt is not None and failure.attempt_id != attempt.attempt_id)
                or (attempt is not None and failure.route_id != attempt.route_id)
                or (attempt is not None and failure.failed_at < attempt.started_at)
                or (failure_route is not None and failure.failed_at < failure_route.selected_at)
                or (failure_usage is not None and failure_usage.request_id != decision.request_id)
                or (failure_usage is not None and failure_usage.attempt_id != failure.attempt_id)
                or failure_event.recorded_at < failure.failed_at
            ):
                raise RunProtocolIntegrityError(
                    "decision failure evidence is causally inconsistent"
                )
            if failure_usage is None and any(
                failure_event.payload.get(field) is not None for field in _USAGE_PAYLOAD_FIELDS
            ):
                raise RunProtocolIntegrityError("decision failure claims usage without an artifact")

        proposal_event = by_type.get(PROPOSAL_RECORDED)
        if proposal_event is None:
            self._verify_terminal_summary(events)
            return
        if response is None:
            raise RunProtocolIntegrityError("ActionProposal lacks a successful model response")
        proposal = self._owner_proposal(proposal_event)
        if not _proposal_matches_response(proposal, response):
            raise RunProtocolIntegrityError("ActionProposal differs from the model response")

        constraints_event = by_type.get(CONSTRAINTS_EVALUATED)
        if constraints_event is None:
            self._verify_terminal_summary(events)
            return
        constraints = self._owner_constraints(constraints_event)
        if (
            constraints != DeterministicConstraintSolver().handle(request.constraints, frame)
            or constraints_event.recorded_at < constraints.evaluated_at
        ):
            raise RunProtocolIntegrityError("constraint evidence differs from deterministic replay")

        authorization_event = by_type.get(AUTHORIZATION_DECIDED)
        if authorization_event is None:
            self._verify_terminal_summary(events)
            return
        authorization = self._owner_authorization(authorization_event)
        recomputed_authorization = ActionAuthorizer().handle(
            AuthorizeAction(
                proposal,
                request.authorization_affordance,
                request.constraints.evaluated_at,
                frame.provenance_event_ids,
                request.approval_granted,
            ),
            constraints,
        )
        if (
            authorization != recomputed_authorization
            or authorization_event.recorded_at < authorization.evaluated_at
        ):
            raise RunProtocolIntegrityError("authorization differs from deterministic replay")

        execution_event = by_type.get(EXECUTION_RECORDED)
        execution: ExecutionResult | None = None
        preparation: ExecutionPreparation | None = None
        if execution_event is not None:
            execution = self._owner_execution(execution_event)
            preparation = deserialize_execution_preparation(
                self._owner_data(execution_event, "preparation_artifact"),
                expected_preparation_id=_artifact_link(
                    execution_event,
                    "preparation_artifact",
                ).digest,
            )
            if (
                not _execution_matches_run(
                    run_id,
                    request,
                    proposal,
                    authorization,
                    preparation,
                    execution,
                )
                or execution_event.recorded_at < execution.completed_at
            ):
                raise RunProtocolIntegrityError("execution differs from its immutable authority")
            journal_position = execution_event.payload.get("journal_position")
            if (
                isinstance(journal_position, bool)
                or not isinstance(journal_position, int)
                or journal_position < 1
            ):
                raise RunProtocolIntegrityError("execution journal position is invalid")
            owned_execution = self._execution_entry(journal_position)
            if (
                owned_execution is None
                or owned_execution.binding != preparation.binding
                or owned_execution.current_result != execution
                or owned_execution.active_claim is not None
                or owned_execution.status.value != execution.status.value
            ):
                raise RunProtocolIntegrityError(
                    "execution evidence is not owned by its durable journal"
                )

        outcome_event = by_type.get(OUTCOME_OBSERVED)
        outcome: OutcomeObservation | None = None
        evaluation_observation = None
        if outcome_event is not None:
            if execution_event is None or execution is None:
                raise RunProtocolIntegrityError("outcome observation lacks execution evidence")
            outcome = self._owner_outcome(outcome_event)
            outcome_event_ids = _strings(outcome_event.payload, "outcome_event_ids")
            targets = {(item.subject, item.predicate) for item in spec.criteria}
            if (
                outcome.binding.run_id != run_id
                or outcome.binding.execution_result_id != execution.result_id
                or outcome.binding.binding_id != _execution_binding_id(events)
                or outcome.evaluation_spec_id != spec.spec_id
                or outcome.domain != request.ingestion.domain
                or outcome.stream_id != request.ingestion.stream_id
                or outcome.observer_id != request.expected_observer_id
                or outcome.observer_contract_version != request.expected_observer_contract_version
                or any(item.key not in targets for item in outcome.claims)
                or outcome_event.recorded_at < outcome.observed_at
            ):
                raise RunProtocolIntegrityError("outcome observation violates its request")
            evaluation_observation = bind_evaluation_observation(
                outcome,
                self._events,
                self._artifacts,
                execution_event_id=execution_event.event_id,
                outcome_event_ids=outcome_event_ids,
            )

        outcome_state_event = by_type.get(OUTCOME_STATE_RECORDED)
        outcome_state: OperationalBeliefState | None = None
        if outcome_state_event is not None:
            if outcome_event is None or outcome is None:
                raise RunProtocolIntegrityError("outcome state lacks owner observation")
            outcome_state = self._owner_state(outcome_state_event)
            source_ids = _strings(outcome_event.payload, "outcome_event_ids")
            source_positions = tuple(
                cast("int", _required_occurrence(self._events, event_id).global_position)
                for event_id in source_ids
            )
            if (
                outcome_state.scope != initial.scope
                or outcome_state.cutoff_global_position <= initial.cutoff_global_position
                or outcome_state.cutoff_global_position < max(source_positions)
                or outcome_state.effective_time_cutoff is None
                or outcome_state.effective_time_cutoff < outcome.observed_at
            ):
                raise RunProtocolIntegrityError("outcome state excludes its owner observation")
            self._verify_state_replay(outcome_state, label="outcome state")

        evaluation_event = by_type.get(EVALUATION_RECORDED)
        if evaluation_event is not None:
            evaluation = self._owner_evaluation(evaluation_event, spec)
            command = EvaluateOutcome(
                run_id=run_id,
                spec=spec,
                authorization_outcome=EvaluationAuthorizationOutcome(authorization.outcome.value),
                execution_status=(
                    None if execution is None else EvaluationExecutionStatus(execution.status.value)
                ),
                execution_event_id=(None if execution_event is None else execution_event.event_id),
                execution_binding_id=(None if execution is None else _execution_binding_id(events)),
                observation=evaluation_observation,
                initial_state_position=initial.cutoff_global_position,
            )
            replayed = OutcomeEvaluator(clock=lambda: evaluation.evaluated_at).handle(command)
            if replayed != evaluation or evaluation_event.recorded_at < evaluation.evaluated_at:
                raise RunProtocolIntegrityError("OutcomeEvaluation differs from replay")

        transition_event = by_type.get(STATE_TRANSITION_RECORDED)
        trace_event = by_type.get(TRACE_RECORDED)
        if transition_event is not None or trace_event is not None:
            acceptance = bind_and_accept_state_transition(
                run_id,
                self._events,
                self._artifacts,
            )
            if transition_event is not None and (
                acceptance.transition is None
                or self._owner_transition(transition_event) != acceptance.transition
            ):
                raise RunProtocolIntegrityError("state transition differs from verified evidence")

        self._verify_terminal_summary(events)

    def _verify_state_replay(
        self,
        state: OperationalBeliefState,
        *,
        label: str,
    ) -> None:
        replayed = ProjectOperationalStateHandler(self._events, _NoCheckpoints()).handle(
            ProjectOperationalState(
                state.scope,
                as_of_time=state.effective_time_cutoff,
                as_of_position=state.cutoff_global_position,
            )
        )
        if replayed != state:
            raise RunProtocolIntegrityError(f"{label} differs from exact ledger replay")

    def _journal_request(self, request_id: str) -> DecisionRequestRecord | None:
        try:
            return self._decision_journal.get_request(request_id)
        except DecisionJournalError as error:
            raise RunProtocolIntegrityError(
                "decision journal evidence violates its request contract or is corrupt"
            ) from error

    def _journal_preparation(self, request_id: str) -> DecisionPreparation | None:
        try:
            return self._decision_journal.get_preparation(request_id)
        except DecisionJournalError as error:
            raise RunProtocolIntegrityError(
                "decision journal preparation evidence is corrupt"
            ) from error

    def _journal_attempt(self, request_id: str) -> DecisionAttemptRecord | None:
        try:
            return self._decision_journal.get_attempt(request_id)
        except DecisionJournalError as error:
            raise RunProtocolIntegrityError(
                "decision journal attempt evidence is corrupt"
            ) from error

    def _journal_terminal(self, request_id: str) -> DecisionTerminalRecord | None:
        try:
            return self._decision_journal.get_terminal(request_id)
        except DecisionJournalError as error:
            raise RunProtocolIntegrityError(
                "decision journal terminal evidence violates its request or is corrupt"
            ) from error

    def _execution_entry(self, journal_position: int) -> ExecutionJournalEntry | None:
        if isinstance(journal_position, bool) or journal_position < 1:
            return None
        try:
            entries = self._execution_journal.list_entries(
                after_position=journal_position - 1,
                limit=1,
            )
        except ExecutionJournalError as error:
            raise RunProtocolIntegrityError("execution journal evidence is corrupt") from error
        if len(entries) != 1 or entries[0].journal_position != journal_position:
            return None
        return entries[0]

    def _require_no_unrecorded_decision(
        self,
        run_id: str,
        events: Sequence[EventEnvelope],
    ) -> None:
        decision_event = next(
            (event for event in events if event.event_type == MODEL_REQUESTED),
            None,
        )
        request_id = (
            self._owner_workflow_request(events[0]).gateway_requirements.request_id
            if decision_event is None
            else _text(decision_event.payload, "request_id")
        )
        attempt_recorded = any(event.event_type == MODEL_ATTEMPT_RECORDED for event in events)
        terminal_recorded = any(
            event.event_type in {MODEL_RESPONDED, MODEL_FAILED} for event in events
        )
        if (self._journal_attempt(request_id) is not None and not attempt_recorded) or (
            self._journal_terminal(request_id) is not None and not terminal_recorded
        ):
            raise RunInterrupted(
                f"run {run_id!r} has decision-journal evidence requiring reconciliation"
            )

    def _require_no_unrecorded_execution(
        self,
        run_id: str,
        events: Sequence[EventEnvelope],
    ) -> None:
        authorization_event = next(
            (event for event in events if event.event_type == AUTHORIZATION_DECIDED),
            None,
        )
        if any(event.event_type == EXECUTION_RECORDED for event in events):
            return
        authorization_id = (
            None
            if authorization_event is None
            else _text(authorization_event.payload, "decision_id")
        )
        try:
            entries = self._execution_journal.list_entries()
        except ExecutionJournalError as error:
            raise RunProtocolIntegrityError("execution journal evidence is corrupt") from error
        unrecorded = tuple(
            entry
            for entry in entries
            if entry.binding.run_id == run_id
            or (
                authorization_id is not None
                and entry.binding.authorization_decision_id == authorization_id
            )
        )
        if unrecorded:
            raise RunInterrupted(
                f"run {run_id!r} has execution-journal evidence requiring reconciliation"
            )

    def _verify_terminal_summary(self, events: Sequence[EventEnvelope]) -> None:
        terminal = next((event for event in events if event.event_type in _TERMINALS), None)
        if terminal is None:
            return
        trace = _event(events, TRACE_RECORDED)
        if _text(terminal.payload, "trace_artifact_digest") != _artifact_digest(trace):
            raise RunProtocolIntegrityError("terminal trace digest differs from its trace")
        if terminal.event_type == RUN_COMPLETED:
            authorization = _event(events, AUTHORIZATION_DECIDED)
            execution = next(
                (event for event in events if event.event_type == EXECUTION_RECORDED),
                None,
            )
            expected_status = None if execution is None else _text(execution.payload, "status")
            if (
                terminal.payload.get("authorization_outcome")
                != authorization.payload.get("outcome")
                or terminal.payload.get("execution_status") != expected_status
                or _text(terminal.payload, "outcome") != _material_outcome(events).value
            ):
                raise RunProtocolIntegrityError("completed run summary differs from its evidence")

    def _verify_event_artifacts(
        self,
        event: EventEnvelope,
        events: Sequence[EventEnvelope],
    ) -> None:
        for field, value in event.payload.items():
            if field != "artifact" and not field.endswith("_artifact"):
                continue
            if value is None and field in {"route_artifact", "usage_artifact"}:
                continue
            if not isinstance(value, Mapping) or frozenset(value) != _ARTIFACT_KEYS:
                raise RunProtocolIntegrityError(
                    f"{event.event_type}.{field} artifact link fields are not exact"
                )
            link = _run_link(cast("Mapping[str, object]", value))
            try:
                reference = self._artifacts.stat(link.digest)
                data = self._artifacts.get_bytes(reference, verify=True)
            except (ArtifactIntegrityError, ArtifactNotFoundError, ValueError) as error:
                raise RunProtocolIntegrityError(
                    f"artifact {link.digest!r} is missing or corrupt"
                ) from error
            if (
                reference.digest != link.digest
                or reference.size_bytes != link.size_bytes
                or reference.media_type != link.media_type
                or reference.encoding != link.encoding
            ):
                raise RunProtocolIntegrityError(
                    f"artifact {link.digest!r} metadata differs from its event link"
                )
            self._verify_owner(event, field, link, data, events)
        if event.event_type == EXECUTION_RECORDED:
            preparation_link = _artifact_link(event, "preparation_artifact")
            result_link = _artifact_link(event)
            preparation = deserialize_execution_preparation(
                self._artifacts.get_bytes(preparation_link.digest, verify=True),
                expected_preparation_id=preparation_link.digest,
            )
            result = deserialize_execution_result(
                self._artifacts.get_bytes(result_link.digest, verify=True),
                expected_result_id=result_link.digest,
            )
            if not _result_matches_preparation(result, preparation):
                raise RunProtocolIntegrityError(
                    "execution result differs from its recorded preparation"
                )

    def _verify_owner(
        self,
        event: EventEnvelope,
        field: str,
        link: RunArtifactLink,
        data: bytes,
        events: Sequence[EventEnvelope],
    ) -> None:
        expected_media = _media_type(event.event_type, field)
        if link.media_type != expected_media or link.encoding != "utf-8":
            raise RunProtocolIntegrityError(f"{event.event_type}.{field} artifact type is invalid")
        try:
            if event.event_type == EVALUATION_RECORDED:
                spec = self._owner_spec(_event(events, EVALUATION_SPECIFIED))
                owner = decode_outcome_evaluation(data, spec=spec)
            else:
                owner = _decode_owner(event.event_type, field, data, link)
        except (TypeError, ValueError) as error:
            raise RunProtocolIntegrityError(
                f"{event.event_type}.{field} owner artifact is invalid"
            ) from error
        _verify_owner_identity(event, field, link, owner)
        _verify_owner_event_fields(event, field, link, owner)

    def _put_link(
        self,
        data: bytes,
        *,
        media_type: str,
        schema_version: str,
        logical_id: str,
    ) -> RunArtifactLink:
        try:
            reference = self._artifacts.put_bytes(
                data,
                media_type=media_type,
                encoding="utf-8",
            )
            stored = self._artifacts.stat(reference.digest)
            verified = self._artifacts.get_bytes(stored, verify=True)
        except (ArtifactIntegrityError, ArtifactNotFoundError, ValueError) as error:
            raise RunProtocolIntegrityError("artifact could not be durably verified") from error
        if (
            stored.digest != reference.digest
            or stored.media_type != media_type
            or stored.encoding != "utf-8"
            or stored.size_bytes != len(data)
            or verified != data
        ):
            raise RunProtocolIntegrityError(
                "artifact content address is already bound to incompatible metadata"
            )
        return _link(stored, schema_version=schema_version, logical_id=logical_id)

    def _existing_link(
        self,
        digest: str,
        *,
        media_type: str,
        schema_version: str,
        logical_id: str,
    ) -> RunArtifactLink:
        """Verify and link an artifact durably owned by another journal."""

        try:
            stored = self._artifacts.stat(digest)
            data = self._artifacts.get_bytes(stored, verify=True)
        except (ArtifactIntegrityError, ArtifactNotFoundError, ValueError) as error:
            raise RunProtocolIntegrityError(
                "journal-owned artifact is missing or corrupt"
            ) from error
        if (
            stored.digest != digest
            or stored.media_type != media_type
            or stored.encoding != "utf-8"
            or stored.size_bytes != len(data)
        ):
            raise RunProtocolIntegrityError("journal-owned artifact metadata is incompatible")
        return _link(stored, schema_version=schema_version, logical_id=logical_id)

    def _raise_existing_open(
        self,
        request_digest: str,
        run_id: str,
        events: Sequence[EventEnvelope],
    ) -> None:
        if not events:
            raise RunInterrupted(f"run {run_id!r} changed concurrently but is unreadable")
        self._validate_prefix(run_id, events, stored=True)
        if _text(events[0].payload, "request_digest") != request_digest:
            raise RunIdentityConflict(f"run {run_id!r} is bound to a different request digest")
        if events[-1].event_type in _TERMINALS:
            raise RunAlreadyExists(f"run {run_id!r} is already terminal")
        raise RunInterrupted(f"run {run_id!r} is nonterminal and requires explicit recovery")

    def _clock_not_before(self, material_time: datetime, label: str) -> None:
        if self._clock() < material_time:
            raise RunProtocolIntegrityError(f"{label} cannot be recorded before it occurred")

    @staticmethod
    def _require_state_scope(start: EventEnvelope, state: OperationalBeliefState) -> None:
        if state.scope.domain != _text(start.payload, "domain") or state.scope.stream_id != _text(
            start.payload, "observation_stream_id"
        ):
            raise RunProtocolIntegrityError("operational state scope differs from its run")

    def _owner_data(self, event: EventEnvelope, field: str = "artifact") -> bytes:
        return self._artifacts.get_bytes(_artifact_link(event, field).digest, verify=True)

    def _owner_workflow_request(self, event: EventEnvelope) -> DailyOperatorV2Request:
        return decode_daily_operator_v2_request(self._owner_data(event))

    def _owner_spec(self, event: EventEnvelope) -> EvaluationSpec:
        return decode_evaluation_spec(self._owner_data(event))

    def _owner_state(self, event: EventEnvelope) -> OperationalBeliefState:
        link = _artifact_link(event)
        return decode_operational_state_snapshot(
            self._owner_data(event),
            expected_snapshot_digest=link.digest,
        )

    def _owner_context(self, event: EventEnvelope) -> ContextFrame:
        link = _artifact_link(event)
        return decode_context_frame(self._owner_data(event), expected_frame_id=link.digest)

    def _owner_request(self, event: EventEnvelope) -> RequestDecision:
        link = _artifact_link(event)
        return decode_decision_request(self._owner_data(event), expected_request_digest=link.digest)

    def _owner_attempt(self, event: EventEnvelope) -> DecisionAttempt:
        link = _artifact_link(event)
        return decode_decision_attempt(self._owner_data(event), expected_attempt_id=link.digest)

    def _owner_route(self, event: EventEnvelope) -> DecisionRoute:
        link = _artifact_link(event, "route_artifact")
        return decode_decision_route(
            self._owner_data(event, "route_artifact"),
            expected_route_id=link.digest,
        )

    def _owner_usage(self, event: EventEnvelope) -> DecisionUsage:
        link = _artifact_link(event, "usage_artifact")
        return decode_decision_usage(
            self._owner_data(event, "usage_artifact"),
            expected_usage_id=link.digest,
        )

    def _owner_response(self, event: EventEnvelope) -> DecisionResponse:
        link = _artifact_link(event)
        return decode_decision_response(self._owner_data(event), expected_response_id=link.digest)

    def _owner_failure(self, event: EventEnvelope) -> DecisionFailure:
        link = _artifact_link(event)
        return decode_decision_failure(self._owner_data(event), expected_failure_id=link.digest)

    def _owner_proposal(self, event: EventEnvelope) -> ActionProposal:
        return decode_action_proposal(self._owner_data(event))

    def _owner_constraints(self, event: EventEnvelope) -> ConstraintEvaluation:
        return decode_constraint_evaluation(self._owner_data(event))

    def _owner_authorization(self, event: EventEnvelope) -> AuthorizationDecision:
        return decode_authorization_decision(self._owner_data(event))

    def _owner_execution(self, event: EventEnvelope) -> ExecutionResult:
        link = _artifact_link(event)
        return deserialize_execution_result(self._owner_data(event), expected_result_id=link.digest)

    def _owner_outcome(self, event: EventEnvelope) -> OutcomeObservation:
        return decode_outcome_observation(self._owner_data(event))

    def _owner_evaluation(
        self,
        event: EventEnvelope,
        spec: EvaluationSpec,
    ) -> OutcomeEvaluation:
        return decode_outcome_evaluation(self._owner_data(event), spec=spec)

    def _owner_transition(self, event: EventEnvelope) -> AcceptedStateTransition:
        return decode_accepted_state_transition(self._owner_data(event))


def _decode_owner(
    event_type: str,
    field: str,
    data: bytes,
    link: RunArtifactLink,
) -> object:
    if event_type == RUN_STARTED:
        return decode_daily_operator_v2_request(data)
    if event_type == EVALUATION_SPECIFIED:
        return decode_evaluation_spec(data)
    if event_type in {INITIAL_STATE_RECORDED, OUTCOME_STATE_RECORDED}:
        return decode_operational_state_snapshot(data, expected_snapshot_digest=link.digest)
    if event_type == CONTEXT_RECORDED:
        return decode_context_frame(data, expected_frame_id=link.digest)
    if event_type == MODEL_REQUESTED:
        return decode_decision_request(data, expected_request_digest=link.digest)
    if event_type == MODEL_ATTEMPT_RECORDED:
        if field == "route_artifact":
            return decode_decision_route(data, expected_route_id=link.digest)
        return decode_decision_attempt(data, expected_attempt_id=link.digest)
    if event_type == MODEL_RESPONDED:
        if field == "usage_artifact":
            return decode_decision_usage(data, expected_usage_id=link.digest)
        return decode_decision_response(data, expected_response_id=link.digest)
    if event_type == MODEL_FAILED:
        if field == "route_artifact":
            return decode_decision_route(data, expected_route_id=link.digest)
        if field == "usage_artifact":
            return decode_decision_usage(data, expected_usage_id=link.digest)
        return decode_decision_failure(data, expected_failure_id=link.digest)
    if event_type == PROPOSAL_RECORDED:
        return decode_action_proposal(data)
    if event_type == CONSTRAINTS_EVALUATED:
        return decode_constraint_evaluation(data)
    if event_type == AUTHORIZATION_DECIDED:
        return decode_authorization_decision(data)
    if event_type == EXECUTION_RECORDED:
        if field == "preparation_artifact":
            return deserialize_execution_preparation(data, expected_preparation_id=link.digest)
        return deserialize_execution_result(data, expected_result_id=link.digest)
    if event_type == OUTCOME_OBSERVED:
        return decode_outcome_observation(data)
    if event_type == STATE_TRANSITION_RECORDED:
        return decode_accepted_state_transition(data)
    if event_type in {TRACE_RECORDED, RUN_FAILED}:
        return json.loads(data)
    raise RunProtocolIntegrityError(f"event {event_type!r} cannot own an artifact")


def _verify_owner_identity(
    event: EventEnvelope,
    field: str,
    link: RunArtifactLink,
    owner: object,
) -> None:
    expected_schema, expected_logical = _owner_schema_and_logical(event, field, owner)
    if link.schema_version != expected_schema or link.logical_id != expected_logical:
        raise RunProtocolIntegrityError(f"{event.event_type}.{field} owner identity differs")


def _verify_owner_event_fields(
    event: EventEnvelope,
    field: str,
    link: RunArtifactLink,
    owner: object,
) -> None:
    expected: Mapping[str, object]
    if event.event_type == RUN_STARTED:
        value = cast("DailyOperatorV2Request", owner)
        expected = {
            "run_id": value.run_id,
            "request_digest": daily_operator_v2_request_digest(value),
            "workflow": RUN_WORKFLOW,
            "workflow_version": RUN_WORKFLOW_VERSION_V2,
            "task_id": value.context.task_id,
            "objective": value.context.objective,
            "domain": value.ingestion.domain,
            "observation_stream_id": value.ingestion.stream_id,
        }
    elif event.event_type == EVALUATION_SPECIFIED:
        value = cast("EvaluationSpec", owner)
        expected = {
            "evaluation_spec_id": value.spec_id,
            "evaluation_spec_digest": link.digest,
        }
    elif event.event_type in {INITIAL_STATE_RECORDED, OUTCOME_STATE_RECORDED}:
        value = cast("OperationalBeliefState", owner)
        expected = _state_payload(value, link)
    elif event.event_type == CONTEXT_RECORDED:
        value = cast("ContextFrame", owner)
        expected = {
            "frame_id": value.frame_id,
            "task_id": value.task_id,
            "state_domain": value.state_domain,
            "state_stream_id": value.state_stream_id,
            "state_global_position": value.state_global_position,
            "state_stream_position": value.state_stream_position,
            "source_packet_id": value.source_packet_id,
            "source_selection_id": value.source_selection_id,
        }
    elif event.event_type == MODEL_REQUESTED:
        value = cast("RequestDecision", owner)
        expected = {
            "request_id": value.request_id,
            "request_digest": value.request_digest,
            "context_frame_id": value.context_frame_id,
        }
    elif event.event_type == MODEL_ATTEMPT_RECORDED:
        if field == "route_artifact":
            value = cast("DecisionRoute", owner)
            expected = {"route_id": value.route_id}
        else:
            value = cast("DecisionAttempt", owner)
            expected = {
                "attempt_id": value.attempt_id,
                "request_id": value.request_id,
                "request_digest": value.request_digest,
                "route_id": value.route_id,
                "attempt_number": value.attempt_number,
            }
    elif event.event_type == MODEL_RESPONDED:
        if field == "usage_artifact":
            value = cast("DecisionUsage", owner)
            expected = _usage_payload(value)
        else:
            value = cast("DecisionResponse", owner)
            expected = {
                "response_id": value.response_id,
                "request_id": value.request_id,
                "request_digest": value.request_digest,
                "attempt_id": value.attempt_id,
                "route_id": value.route_id,
                "proposal_id": value.proposal.proposal_id,
            }
    elif event.event_type == MODEL_FAILED:
        if field == "route_artifact":
            value = cast("DecisionRoute", owner)
            expected = {"route_id": value.route_id}
        elif field == "usage_artifact":
            value = cast("DecisionUsage", owner)
            expected = _usage_payload(value)
        else:
            value = cast("DecisionFailure", owner)
            expected = {
                "failure_id": value.failure_id,
                "request_id": value.request_id,
                "request_digest": value.request_digest,
                "kind": value.kind.value,
                "code": value.code,
                "retryable": value.retryable,
                "route_id": value.route_id,
                "attempt_id": value.attempt_id,
            }
    elif event.event_type == PROPOSAL_RECORDED:
        value = cast("ActionProposal", owner)
        expected = {
            "proposal_id": value.proposal_id,
            "proposal_digest": value.proposal_digest,
            "action_digest": value.action_digest,
            "context_frame_id": value.context_frame_id,
        }
    elif event.event_type == CONSTRAINTS_EVALUATED:
        value = cast("ConstraintEvaluation", owner)
        expected = {
            "evaluation_id": value.evaluation_id,
            "context_frame_id": value.context_frame_id,
            "proof_ids": tuple(item.proof_id for item in value.proofs),
            "safe": value.safe,
        }
    elif event.event_type == AUTHORIZATION_DECIDED:
        value = cast("AuthorizationDecision", owner)
        expected = {
            "decision_id": value.decision_id,
            "proposal_id": value.proposal_id,
            "constraint_evaluation_id": value.constraint_evaluation_id,
            "outcome": value.outcome.value,
        }
    elif event.event_type == EXECUTION_RECORDED and field == "preparation_artifact":
        value = cast("ExecutionPreparation", owner)
        expected = {
            "preparation_id": value.preparation_id,
            "run_id": value.run_id,
            "invocation_id": value.invocation.invocation_id,
            "proposal_id": value.invocation.proposal_id,
            "authorization_decision_id": value.authorization_decision_id,
            "authorized_action_digest": value.authorized_action_digest,
            "affordance": value.invocation.affordance,
            "adapter_id": value.definition.adapter_id,
            "adapter_contract_version": value.adapter_contract_version,
            "arguments": tuple(
                {"name": item.name, "value": item.value} for item in value.invocation.arguments
            ),
        }
    elif event.event_type == EXECUTION_RECORDED:
        value = cast("ExecutionResult", owner)
        expected = {
            "result_id": value.result_id,
            "invocation_id": value.invocation_id,
            "proposal_id": value.proposal_id,
            "authorization_decision_id": value.authorization_decision_id,
            "authorized_action_digest": value.authorized_action_digest,
            "execution_identity_digest": value.execution_identity_digest,
            "status": value.status.value,
            "affordance": value.affordance,
            "adapter_id": value.adapter_id,
            "completed_at": value.completed_at.isoformat(),
        }
    elif event.event_type == OUTCOME_OBSERVED:
        value = cast("OutcomeObservation", owner)
        expected = {
            "observation_id": value.observation_id,
            "observation_digest": value.observation_digest,
            "evaluation_spec_id": value.evaluation_spec_id,
            "execution_binding_id": value.binding.binding_id,
            "status": value.status.value,
        }
    elif event.event_type == EVALUATION_RECORDED:
        value = cast("OutcomeEvaluation", owner)
        expected = {
            "evaluation_id": value.evaluation_id,
            "evaluation_spec_id": value.evaluation_spec_id,
            "verdict": value.verdict.value,
        }
    elif event.event_type == STATE_TRANSITION_RECORDED:
        value = cast("AcceptedStateTransition", owner)
        expected = {
            "transition_id": value.transition_id,
            "initial_snapshot_digest": value.initial_state.snapshot_digest,
            "outcome_snapshot_digest": value.outcome_state.snapshot_digest,
            "evaluation_id": value.evaluation.evaluation_id,
            "accepted_claim_ids": value.accepted_claim_ids,
            "accepted_source_event_ids": value.accepted_source_event_ids,
        }
    elif event.event_type == TRACE_RECORDED:
        value = cast("Mapping[str, object]", owner)
        entries = value.get("entries")
        expected = {
            "run_id": value.get("run_id"),
            "outcome": value.get("outcome"),
            "entry_count": len(entries) if isinstance(entries, list) else -1,
        }
    elif event.event_type == RUN_FAILED:
        value = cast("Mapping[str, object]", owner)
        expected_owner = {
            "schema_version": RUN_FAILURE_SCHEMA_VERSION,
            "run_id": event.payload.get("run_id"),
            "phase": event.payload.get("phase"),
            "error_type": event.payload.get("error_type"),
        }
        if value != expected_owner:
            raise RunProtocolIntegrityError("run failure artifact differs from its event")
        expected = {}
    else:
        raise RunProtocolIntegrityError(f"event {event.event_type!r} owner is unsupported")
    mismatches = tuple(key for key, value in expected.items() if event.payload.get(key) != value)
    if mismatches:
        raise RunProtocolIntegrityError(
            f"{event.event_type}.{field} owner differs from event fields: {', '.join(mismatches)}"
        )


def _result_matches_preparation(
    result: ExecutionResult,
    preparation: ExecutionPreparation,
) -> bool:
    binding = preparation.binding
    return (
        result.invocation_id == binding.invocation_id
        and result.proposal_id == binding.proposal_id
        and result.authorization_decision_id == binding.authorization_decision_id
        and result.affordance == binding.affordance
        and result.adapter_id == binding.adapter_id
        and result.idempotency_key == binding.idempotency_key
        and result.authorized_action_digest == binding.authorized_action_digest
        and result.execution_identity_digest == binding.execution_identity_digest
    )


def _owner_schema_and_logical(
    event: EventEnvelope,
    field: str,
    owner: object,
) -> tuple[str, str]:
    if event.event_type == RUN_STARTED:
        return DAILY_OPERATOR_REQUEST_V2_SCHEMA_VERSION, _text(event.payload, "request_digest")
    if event.event_type == EVALUATION_SPECIFIED:
        value = cast("EvaluationSpec", owner)
        return value.schema_version, value.spec_id
    if event.event_type in {INITIAL_STATE_RECORDED, OUTCOME_STATE_RECORDED}:
        return OPERATIONAL_STATE_SNAPSHOT_SCHEMA_VERSION, _text(event.payload, "snapshot_digest")
    if event.event_type == CONTEXT_RECORDED:
        value = cast("ContextFrame", owner)
        return value.schema_version, value.frame_id
    if event.event_type == MODEL_REQUESTED:
        value = cast("RequestDecision", owner)
        return value.schema_version, value.request_digest
    if event.event_type == MODEL_ATTEMPT_RECORDED:
        if field == "route_artifact":
            value = cast("DecisionRoute", owner)
            return value.schema_version, value.route_id
        value = cast("DecisionAttempt", owner)
        return value.schema_version, value.attempt_id
    if event.event_type == MODEL_RESPONDED:
        if field == "usage_artifact":
            value = cast("DecisionUsage", owner)
            return value.schema_version, value.usage_id
        value = cast("DecisionResponse", owner)
        return value.schema_version, value.response_id
    if event.event_type == MODEL_FAILED:
        if field == "route_artifact":
            value = cast("DecisionRoute", owner)
            return value.schema_version, value.route_id
        if field == "usage_artifact":
            value = cast("DecisionUsage", owner)
            return value.schema_version, value.usage_id
        value = cast("DecisionFailure", owner)
        return value.schema_version, value.failure_id
    if event.event_type == PROPOSAL_RECORDED:
        value = cast("ActionProposal", owner)
        return value.schema_version, value.proposal_digest
    if event.event_type == CONSTRAINTS_EVALUATED:
        value = cast("ConstraintEvaluation", owner)
        return value.schema_version, value.evaluation_id
    if event.event_type == AUTHORIZATION_DECIDED:
        value = cast("AuthorizationDecision", owner)
        return value.schema_version, value.decision_id
    if event.event_type == EXECUTION_RECORDED:
        if field == "preparation_artifact":
            value = cast("ExecutionPreparation", owner)
            return value.schema_version, value.preparation_id
        value = cast("ExecutionResult", owner)
        return value.schema_version, value.result_id
    if event.event_type == OUTCOME_OBSERVED:
        value = cast("OutcomeObservation", owner)
        return value.schema_version, value.observation_digest
    if event.event_type == EVALUATION_RECORDED:
        value = cast("OutcomeEvaluation", owner)
        return value.schema_version, value.evaluation_id
    if event.event_type == STATE_TRANSITION_RECORDED:
        value = cast("AcceptedStateTransition", owner)
        return value.schema_version, value.transition_id
    if event.event_type == TRACE_RECORDED:
        return RUN_TRACE_SCHEMA_VERSION_V2, f"trace:{_text(event.payload, 'run_id')}"
    if event.event_type == RUN_FAILED:
        return RUN_FAILURE_SCHEMA_VERSION, f"failure:{_text(event.payload, 'run_id')}"
    raise RunProtocolIntegrityError(f"event {event.event_type!r} owner is unsupported")


def _media_type(event_type: str, field: str) -> str:
    if event_type == RUN_STARTED:
        return DAILY_OPERATOR_REQUEST_V2_MEDIA_TYPE
    if event_type == EVALUATION_SPECIFIED:
        return EVALUATION_SPEC_MEDIA_TYPE
    if event_type in {INITIAL_STATE_RECORDED, OUTCOME_STATE_RECORDED}:
        return OPERATIONAL_STATE_SNAPSHOT_MEDIA_TYPE
    if event_type == CONTEXT_RECORDED:
        return CONTEXT_FRAME_MEDIA_TYPE
    if event_type == MODEL_REQUESTED:
        return DECISION_REQUEST_MEDIA_TYPE
    if event_type == MODEL_ATTEMPT_RECORDED:
        return (
            DECISION_ROUTE_MEDIA_TYPE if field == "route_artifact" else DECISION_ATTEMPT_MEDIA_TYPE
        )
    if event_type == MODEL_RESPONDED:
        return (
            DECISION_USAGE_MEDIA_TYPE if field == "usage_artifact" else DECISION_RESPONSE_MEDIA_TYPE
        )
    if event_type == MODEL_FAILED:
        if field == "route_artifact":
            return DECISION_ROUTE_MEDIA_TYPE
        if field == "usage_artifact":
            return DECISION_USAGE_MEDIA_TYPE
        return DECISION_FAILURE_MEDIA_TYPE
    if event_type == PROPOSAL_RECORDED:
        return ACTION_PROPOSAL_MEDIA_TYPE
    if event_type == CONSTRAINTS_EVALUATED:
        return CONSTRAINT_EVALUATION_MEDIA_TYPE
    if event_type == AUTHORIZATION_DECIDED:
        return AUTHORIZATION_DECISION_MEDIA_TYPE
    if event_type == EXECUTION_RECORDED:
        return (
            EXECUTION_PREPARATION_MEDIA_TYPE
            if field == "preparation_artifact"
            else EXECUTION_RESULT_MEDIA_TYPE
        )
    if event_type == OUTCOME_OBSERVED:
        return OUTCOME_OBSERVATION_MEDIA_TYPE
    if event_type == EVALUATION_RECORDED:
        return OUTCOME_EVALUATION_MEDIA_TYPE
    if event_type == STATE_TRANSITION_RECORDED:
        return ACCEPTED_STATE_TRANSITION_MEDIA_TYPE
    if event_type == TRACE_RECORDED:
        return RUN_TRACE_MEDIA_TYPE
    if event_type == RUN_FAILED:
        return RUN_FAILURE_MEDIA_TYPE
    raise RunProtocolIntegrityError(f"event {event_type!r} artifact type is unsupported")


def _link(reference: ArtifactRef, *, schema_version: str, logical_id: str) -> RunArtifactLink:
    return RunArtifactLink(
        digest=reference.digest,
        media_type=reference.media_type,
        encoding=reference.encoding,
        size_bytes=reference.size_bytes,
        schema_version=schema_version,
        logical_id=logical_id,
    )


def _run_link(value: Mapping[str, object]) -> RunArtifactLink:
    try:
        return RunArtifactLink(
            digest=_text(value, "digest"),
            media_type=_text(value, "media_type"),
            encoding=_optional_text(value.get("encoding"), "encoding"),
            size_bytes=_integer(value, "size_bytes"),
            schema_version=_text(value, "schema_version"),
            logical_id=_text(value, "logical_id"),
        )
    except (TypeError, ValueError) as error:
        raise RunProtocolIntegrityError("artifact link violates RunArtifactLink") from error


def _artifact_link(event: EventEnvelope, field: str = "artifact") -> RunArtifactLink:
    value = event.payload.get(field)
    if not isinstance(value, Mapping) or frozenset(value) != _ARTIFACT_KEYS:
        raise RunProtocolIntegrityError(f"{event.event_type}.{field} artifact link is invalid")
    return _run_link(cast("Mapping[str, object]", value))


def _artifact_digest(event: EventEnvelope) -> str:
    return _artifact_link(event).digest


def _snapshot_digest(data: bytes) -> str:
    from blackcell.kernel._json import bytes_digest

    return bytes_digest(data)


def _state_payload(state: OperationalBeliefState, link: RunArtifactLink) -> dict[str, JsonInput]:
    return {
        "snapshot_digest": link.digest,
        "domain": state.scope.domain,
        "stream_id": state.scope.stream_id,
        "cutoff_global_position": state.cutoff_global_position,
        "last_source_stream_sequence": state.last_source_stream_sequence,
        "effective_time_cutoff": (
            None if state.effective_time_cutoff is None else state.effective_time_cutoff.isoformat()
        ),
    }


def _event(events: Sequence[EventEnvelope], event_type: str) -> EventEnvelope:
    try:
        return next(item for item in events if item.event_type == event_type)
    except StopIteration as error:
        raise RunProtocolIntegrityError(f"run lacks required event {event_type!r}") from error


def _required_occurrence(events: EventStore, event_id: str) -> EventEnvelope:
    event = events.get(event_id)
    if event is None or event.global_position is None:
        raise RunProtocolIntegrityError(f"event {event_id!r} is not a stored occurrence")
    return event


def _execution_binding_id(events: Sequence[EventEnvelope]) -> str:
    execution = _event(events, EXECUTION_RECORDED)
    raw_arguments = execution.payload.get("arguments")
    if not isinstance(raw_arguments, tuple | list):
        raise RunProtocolIntegrityError("execution arguments must be an array")
    arguments: list[OutcomeArgument] = []
    for raw in raw_arguments:
        if not isinstance(raw, Mapping):
            raise RunProtocolIntegrityError("execution argument must be an object")
        value = raw.get("value")
        if isinstance(value, Mapping | tuple):
            raise RunProtocolIntegrityError("execution argument value must be scalar")
        arguments.append(
            OutcomeArgument(
                _text(cast("Mapping[str, object]", raw), "name"),
                cast("JsonScalar", value),
            )
        )
    try:
        completed_at = datetime.fromisoformat(_text(execution.payload, "completed_at"))
    except ValueError as error:
        raise RunProtocolIntegrityError("execution completion time is invalid") from error
    return OutcomeExecutionBinding(
        run_id=_text(execution.payload, "run_id"),
        invocation_id=_text(execution.payload, "invocation_id"),
        proposal_id=_text(execution.payload, "proposal_id"),
        proposal_digest=_text(execution.payload, "proposal_digest"),
        authorization_decision_id=_text(execution.payload, "authorization_decision_id"),
        authorized_action_digest=_text(execution.payload, "authorized_action_digest"),
        execution_result_id=_text(execution.payload, "result_id"),
        execution_identity_digest=_text(execution.payload, "execution_identity_digest"),
        execution_status=_text(execution.payload, "status"),
        affordance=_text(execution.payload, "affordance"),
        arguments=tuple(arguments),
        execution_adapter_id=_text(execution.payload, "adapter_id"),
        execution_adapter_contract_version=_text(
            execution.payload,
            "adapter_contract_version",
        ),
        completed_at=completed_at,
    ).binding_id


def _material_outcome(events: Sequence[EventEnvelope]) -> RunOutcome:
    authorization = _text(_event(events, AUTHORIZATION_DECIDED).payload, "outcome")
    execution = next((item for item in events if item.event_type == EXECUTION_RECORDED), None)
    status = None if execution is None else _text(execution.payload, "status")
    outcomes: Mapping[tuple[str, str | None], RunOutcome] = {
        ("deny", None): RunOutcome.DENIED,
        ("require-approval", None): RunOutcome.APPROVAL_REQUIRED,
        ("allow", "succeeded"): RunOutcome.EXECUTED,
        ("allow", "failed"): RunOutcome.EXECUTION_FAILED,
        ("allow", "unknown"): RunOutcome.REQUIRES_RECONCILIATION,
    }
    try:
        return outcomes[(authorization, status)]
    except KeyError as error:
        raise RunProtocolIntegrityError(
            "authorization/execution cannot produce a run outcome"
        ) from error


def _trace_entries(events: Sequence[EventEnvelope]) -> list[dict[str, JsonInput]]:
    return [
        {
            "event_id": event.event_id,
            "event_type": event.event_type,
            "schema_version": event.schema_version,
            "stream_sequence": event.stream_sequence,
            "global_position": event.global_position,
            "recorded_at": event.recorded_at.isoformat(),
            "effective_at": event.effective_at.isoformat(),
            "causation_id": event.causation_id,
            "artifact_links": [
                {"field": field, "digest": _run_link(cast("Mapping[str, object]", value)).digest}
                for field, value in sorted(event.payload.items())
                if (field == "artifact" or field.endswith("_artifact"))
                and isinstance(value, Mapping)
            ],
        }
        for event in events
    ]


def _text(value: Mapping[str, object], field: str) -> str:
    item = value.get(field)
    if not isinstance(item, str) or not item.strip():
        raise RunProtocolIntegrityError(f"{field} must be a non-empty string")
    return item


def _optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise RunProtocolIntegrityError(f"{field} must be a non-empty string or null")
    return value


def _integer(value: Mapping[str, object], field: str) -> int:
    item = value.get(field)
    if isinstance(item, bool) or not isinstance(item, int) or item < 0:
        raise RunProtocolIntegrityError(f"{field} must be a non-negative integer")
    return item


def _strings(value: Mapping[str, object], field: str) -> tuple[str, ...]:
    item = value.get(field)
    if not isinstance(item, tuple | list):
        raise RunProtocolIntegrityError(f"{field} must be an array")
    result = tuple(item)
    if any(not isinstance(element, str) or not element.strip() for element in result):
        raise RunProtocolIntegrityError(f"{field} values must be non-empty strings")
    return cast("tuple[str, ...]", result)


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


def _proposal_matches_response(
    proposal: ActionProposal,
    response: DecisionResponse,
) -> bool:
    model = response.proposal
    return (
        proposal.proposal_id == model.proposal_id
        and proposal.context_frame_id == model.context_frame_id
        and proposal.affordance == model.affordance
        and tuple((item.name, item.value) for item in proposal.arguments)
        == tuple((item.name, item.value) for item in model.arguments)
        and proposal.rationale == model.rationale
        and proposal.evidence_event_ids == model.evidence_event_ids
    )


def _execution_matches_run(
    run_id: str,
    request: DailyOperatorV2Request,
    proposal: ActionProposal,
    authorization: AuthorizationDecision,
    preparation: ExecutionPreparation,
    result: ExecutionResult,
) -> bool:
    binding = preparation.binding
    definition_read_only = preparation.definition.side_effect_class is SideEffectClass.READ_ONLY
    return (
        preparation.run_id == run_id
        and preparation.definition == request.execution_affordance
        and preparation.invocation.invocation_id == request.invocation_id
        and preparation.invocation.idempotency_key == request.idempotency_key
        and authorization.outcome is AuthorizationOutcome.ALLOW
        and authorization.authorized_read_only == definition_read_only
        and preparation.authorization_decision_id == authorization.decision_id
        and preparation.authorized_action_digest == authorization.authorized_action_digest
        and preparation.invocation.proposal_id == proposal.proposal_id
        and preparation.invocation.affordance == proposal.affordance
        and tuple((item.name, item.value) for item in preparation.invocation.arguments)
        == tuple((item.name, item.value) for item in proposal.arguments)
        and result.invocation_id == binding.invocation_id
        and result.proposal_id == binding.proposal_id
        and result.authorization_decision_id == binding.authorization_decision_id
        and result.affordance == binding.affordance
        and result.adapter_id == binding.adapter_id
        and result.idempotency_key == binding.idempotency_key
        and result.authorized_action_digest == binding.authorized_action_digest
        and result.execution_identity_digest == binding.execution_identity_digest
        and result.started_at >= authorization.evaluated_at
        and result.started_at >= preparation.invocation.requested_at
    )


def _usage_payload(usage: DecisionUsage | None) -> dict[str, JsonInput]:
    return {
        "usage_id": None if usage is None else usage.usage_id,
        "input_tokens": None if usage is None else usage.input_tokens,
        "output_tokens": None if usage is None else usage.output_tokens,
        "latency_ms": None if usage is None else usage.latency_ms,
        "cost_microusd": None if usage is None else usage.cost_microusd,
        "deterministic": None if usage is None else usage.deterministic,
    }


def _decision_affordance(request: DailyOperatorV2Request) -> DecisionAffordance:
    definition = request.execution_affordance
    return DecisionAffordance(
        definition.name,
        tuple(DecisionArgumentSpec(item.name, item.required) for item in definition.arguments),
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
        raise AssertionError("run verification must not write projection checkpoints")


__all__ = ["KernelFeedbackRunRecorder"]
