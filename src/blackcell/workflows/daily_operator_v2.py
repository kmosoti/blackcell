from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from blackcell.features.authorize_action import (
    ActionProposal,
    AuthorizationDecision,
    AuthorizationOutcome,
    AuthorizeAction,
    authorize_action,
)
from blackcell.features.build_context import ContextFrame
from blackcell.features.evaluate_outcome import (
    EvaluateOutcome,
    EvaluationAuthorizationOutcome,
    EvaluationExecutionStatus,
    OutcomeEvaluator,
)
from blackcell.features.execute_affordance import (
    AffordanceArgument,
    AffordanceExecutionHandler,
    AffordanceInvocation,
    ExecutionResult,
    ExecutionStatus,
)
from blackcell.features.execute_affordance.ports import ExecutionJournal
from blackcell.features.ingest_observation import IngestObservationHandler
from blackcell.features.observe_outcome import (
    CollectOutcomeHandler,
    ObserveOutcome,
    OutcomeArgument,
    OutcomeExecutionBinding,
    OutcomeObserver,
    OutcomeTarget,
)
from blackcell.features.project_operational_state import (
    OperationalBeliefState,
    OperationalStateScope,
    ProjectOperationalState,
    ProjectOperationalStateHandler,
)
from blackcell.features.request_decision import (
    DecisionAffordance,
    DecisionArgumentSpec,
    DecisionAttemptClaim,
    DecisionFailureRecord,
    DecisionPreparation,
    DecisionSuccessRecord,
    DecisionTerminalRecord,
    RequestDecision,
    RequestDecisionHandler,
)
from blackcell.features.solve_constraints import ConstraintSolver, DeterministicConstraintSolver
from blackcell.workflows.daily_operator_v2_evidence import rebuild_requested_context
from blackcell.workflows.daily_operator_v2_request import DailyOperatorV2Request
from blackcell.workflows.decision_bridge import action_proposal_from_decision
from blackcell.workflows.outcome_evidence import (
    OutcomeEvidenceWriter,
    WriteOutcomeEvidence,
    bind_evaluation_observation,
)
from blackcell.workflows.run_protocol import RunOutcome, RunTerminal
from blackcell.workflows.run_protocol_v2 import FeedbackRunRecorder
from blackcell.workflows.state_transition import (
    StateTransitionArtifacts,
    StateTransitionHistory,
    bind_and_accept_state_transition,
)
from blackcell.workflows.telemetry import (
    NullWorkflowTelemetry,
    WorkflowSpanName,
    WorkflowTelemetry,
)


class DailyOperatorV2Workflow:
    """Execute one fresh, evidence-bound ``daily-operator/v2`` delivery."""

    def __init__(
        self,
        *,
        history: StateTransitionHistory,
        artifacts: StateTransitionArtifacts,
        state: ProjectOperationalStateHandler,
        ingestion: IngestObservationHandler,
        runs: FeedbackRunRecorder,
        decisions: RequestDecisionHandler,
        execution: AffordanceExecutionHandler,
        execution_journal: ExecutionJournal,
        outcome_observer: OutcomeObserver,
        outcome_evidence: OutcomeEvidenceWriter,
        evaluator: OutcomeEvaluator | None = None,
        constraint_solver: ConstraintSolver | None = None,
        telemetry: WorkflowTelemetry | None = None,
    ) -> None:
        self._history = history
        self._artifacts = artifacts
        self._state = state
        self._ingestion = ingestion
        self._runs = runs
        self._decisions = decisions
        self._execution = execution
        self._execution_journal = execution_journal
        self._outcome_observer = outcome_observer
        self._outcome_evidence = outcome_evidence
        self._constraints = constraint_solver or DeterministicConstraintSolver()
        self._authorization = authorize_action
        self._evaluator = evaluator or OutcomeEvaluator()
        self._telemetry = telemetry or NullWorkflowTelemetry()

    def run(self, request: DailyOperatorV2Request) -> RunTerminal:
        self._validate_observer(request)
        opening = self._runs.open(request)
        phase = "ingestion"
        try:
            with self._telemetry.span(WorkflowSpanName.OBSERVE, run_id=request.run_id):
                observations = self._ingestion.handle(
                    replace(request.ingestion, causation_id=opening.started.event_id)
                )
                final_observation_position = observations[-1].global_position
                if final_observation_position is None:
                    raise ValueError("requested observation is not a stored ledger occurrence")

            phase = "initial-state"
            with self._telemetry.span(WorkflowSpanName.PROJECT_STATE, run_id=request.run_id):
                initial_state = self._project_state(
                    request,
                    position=final_observation_position,
                    effective_time=request.initial_effective_time_cutoff,
                )
                self._runs.record_initial_state(request.run_id, initial_state)

            phase = "context"
            with self._telemetry.span(WorkflowSpanName.BUILD_CONTEXT, run_id=request.run_id):
                frame = rebuild_requested_context(request, initial_state)
                context_event = self._runs.record_context(request.run_id, frame)

            phase = "decision"
            with self._telemetry.span(WorkflowSpanName.MODEL_DECIDE, run_id=request.run_id):
                decision = self._request_decision(request, frame, context_event.event_id)
                terminal = self._run_decision(request.run_id, decision)
            if isinstance(terminal, DecisionFailureRecord):
                return self._runs.fail(
                    request.run_id,
                    phase="decision",
                    error_type=type(terminal.failure).__name__,
                )

            phase = "proposal"
            with self._telemetry.span(WorkflowSpanName.POLICY_EVALUATE, run_id=request.run_id):
                proposal = action_proposal_from_decision(terminal.response.proposal)
                self._runs.record_proposal(request.run_id, proposal)

                phase = "constraints"
                constraints = self._constraints.handle(request.constraints, frame)
                self._runs.record_constraints(request.run_id, constraints)

                phase = "authorization"
                authorization = self._authorization(
                    AuthorizeAction(
                        proposal,
                        request.authorization_affordance,
                        request.constraints.evaluated_at,
                        frame.provenance_event_ids,
                        request.approval_granted,
                    ),
                    constraints,
                )
                self._runs.record_authorization(request.run_id, authorization)
            if authorization.outcome is not AuthorizationOutcome.ALLOW:
                return self._finish_without_execution(
                    request,
                    initial_state=initial_state,
                    authorization=authorization,
                )

            phase = "execution"
            with self._telemetry.span(WorkflowSpanName.AFFORDANCE_EXECUTE, run_id=request.run_id):
                result = self._execution.handle(
                    AffordanceInvocation(
                        invocation_id=request.invocation_id,
                        proposal_id=proposal.proposal_id,
                        affordance=proposal.affordance,
                        arguments=tuple(
                            AffordanceArgument(item.name, item.value) for item in proposal.arguments
                        ),
                        idempotency_key=request.idempotency_key,
                        requested_at=request.constraints.evaluated_at,
                    ),
                    request.execution_affordance,
                    authorization,
                    run_id=request.run_id,
                )
                entry = self._execution_journal.get_entry_by_invocation(request.invocation_id)
                if entry is None or entry.current_result != result:
                    raise ValueError("execution result lacks its exact terminal journal entry")
                execution_event = self._runs.record_execution(request.run_id, entry)

            if result.status is ExecutionStatus.UNKNOWN:
                return self._finish_unknown_execution(
                    request,
                    initial_state=initial_state,
                    proposal=proposal,
                    authorization=authorization,
                    execution=result,
                    execution_event_id=execution_event.event_id,
                    adapter_contract_version=entry.binding.adapter_contract_version,
                )

            phase = "outcome-observation"
            with self._telemetry.span(WorkflowSpanName.OUTCOME_OBSERVE, run_id=request.run_id):
                observation = CollectOutcomeHandler(self._outcome_observer).handle(
                    ObserveOutcome(
                        binding=self._outcome_binding(
                            request,
                            proposal=proposal,
                            authorization=authorization,
                            execution=result,
                            adapter_contract_version=entry.binding.adapter_contract_version,
                        ),
                        evaluation_spec_id=request.evaluation_spec.spec_id,
                        domain=request.ingestion.domain,
                        stream_id=request.ingestion.stream_id,
                        targets=tuple(
                            OutcomeTarget(item.subject, item.predicate)
                            for item in request.evaluation_spec.criteria
                        ),
                    )
                )

                phase = "outcome-evidence"
                outcome_event = self._outcome_evidence.handle(
                    WriteOutcomeEvidence(
                        outcome=observation,
                        expected_sequence=initial_state.last_source_stream_sequence,
                        actor=request.ingestion.actor,
                        execution_event_id=execution_event.event_id,
                    )
                )
                self._runs.record_outcome(
                    request.run_id,
                    observation,
                    outcome_event_ids=(outcome_event.event_id,),
                )

                phase = "outcome-state"
                outcome_position = outcome_event.global_position
                if outcome_position is None:
                    raise ValueError("outcome observation is not a stored ledger occurrence")
                outcome_state = self._project_state(
                    request,
                    position=outcome_position,
                    effective_time=observation.observed_at,
                )
                self._runs.record_outcome_state(request.run_id, outcome_state)

            phase = "evaluation"
            with self._telemetry.span(WorkflowSpanName.EVALUATION_GRADE, run_id=request.run_id):
                bound_observation = bind_evaluation_observation(
                    observation,
                    self._history,
                    self._artifacts,
                    execution_event_id=execution_event.event_id,
                    outcome_event_ids=(outcome_event.event_id,),
                )
                evaluation = self._evaluator.handle(
                    EvaluateOutcome(
                        run_id=request.run_id,
                        spec=request.evaluation_spec,
                        authorization_outcome=EvaluationAuthorizationOutcome.ALLOW,
                        execution_status=EvaluationExecutionStatus(result.status.value),
                        execution_event_id=execution_event.event_id,
                        execution_binding_id=observation.binding.binding_id,
                        observation=bound_observation,
                        initial_state_position=initial_state.cutoff_global_position,
                    )
                )
                self._runs.record_evaluation(request.run_id, evaluation)
            phase = "transition"
            with self._telemetry.span(WorkflowSpanName.TRANSITION_COMMIT, run_id=request.run_id):
                self._record_transition(request.run_id)
            return self._runs.complete(request.run_id, _run_outcome(authorization, result))
        except Exception as error:
            try:
                self._runs.fail(
                    request.run_id,
                    phase=phase,
                    error_type=type(error).__name__,
                )
            except Exception as recording_error:
                raise ExceptionGroup(
                    "Daily Operator v2 failed and durable failure recording also failed",
                    (error, recording_error),
                ) from error
            raise

    def _validate_observer(self, request: DailyOperatorV2Request) -> None:
        if (
            self._outcome_observer.observer_id != request.expected_observer_id
            or self._outcome_observer.contract_version != request.expected_observer_contract_version
        ):
            raise ValueError("configured outcome observer differs from the request policy")

    def _project_state(
        self,
        request: DailyOperatorV2Request,
        *,
        position: int,
        effective_time: datetime,
    ) -> OperationalBeliefState:
        return self._state.handle(
            ProjectOperationalState(
                OperationalStateScope(
                    request.ingestion.domain,
                    request.ingestion.stream_id,
                ),
                as_of_time=effective_time,
                as_of_position=position,
            )
        )

    def _request_decision(
        self,
        request: DailyOperatorV2Request,
        frame: ContextFrame,
        causation_id: str,
    ) -> RequestDecision:
        return RequestDecision(
            requirements=request.gateway_requirements,
            run_id=request.run_id,
            correlation_id=request.run_id,
            causation_id=causation_id,
            context_frame_id=frame.frame_id,
            objective=frame.objective,
            context_payload=frame.model_payload,
            evidence_event_ids=frame.provenance_event_ids,
            affordances=(
                DecisionAffordance(
                    request.execution_affordance.name,
                    tuple(
                        DecisionArgumentSpec(item.name, item.required)
                        for item in request.execution_affordance.arguments
                    ),
                ),
            ),
        )

    def _run_decision(
        self,
        run_id: str,
        command: RequestDecision,
    ) -> DecisionTerminalRecord:
        prepared = self._decisions.prepare(command)
        if isinstance(prepared, DecisionFailureRecord):
            self._runs.record_model_request(run_id, prepared.request_record)
            if prepared.attempt_record is not None and prepared.preparation is not None:
                self._runs.record_model_attempt(
                    run_id,
                    prepared.preparation,
                    prepared.attempt_record,
                )
            self._runs.record_model_terminal(run_id, prepared)
            return prepared
        if isinstance(prepared, DecisionSuccessRecord):
            self._runs.record_model_request(run_id, prepared.preparation.request_record)
            self._runs.record_model_attempt(
                run_id,
                prepared.preparation,
                prepared.attempt_record,
            )
            self._runs.record_model_terminal(run_id, prepared)
            return prepared

        assert isinstance(prepared, DecisionPreparation)
        self._runs.record_model_request(run_id, prepared.request_record)
        acquired = self._decisions.acquire(prepared)
        if isinstance(acquired, DecisionAttemptClaim):
            self._runs.record_model_attempt(run_id, prepared, acquired.attempt_record)
            terminal = self._decisions.invoke(prepared, acquired)
        else:
            terminal = acquired
            attempt = terminal.attempt_record
            preparation = terminal.preparation
            if attempt is not None and preparation is not None:
                self._runs.record_model_attempt(run_id, preparation, attempt)
        self._runs.record_model_terminal(run_id, terminal)
        return terminal

    def _finish_without_execution(
        self,
        request: DailyOperatorV2Request,
        *,
        initial_state: OperationalBeliefState,
        authorization: AuthorizationDecision,
    ) -> RunTerminal:
        with self._telemetry.span(WorkflowSpanName.EVALUATION_GRADE, run_id=request.run_id):
            evaluation = self._evaluator.handle(
                EvaluateOutcome(
                    run_id=request.run_id,
                    spec=request.evaluation_spec,
                    authorization_outcome=EvaluationAuthorizationOutcome(
                        authorization.outcome.value
                    ),
                    execution_status=None,
                    execution_event_id=None,
                    execution_binding_id=None,
                    observation=None,
                    initial_state_position=initial_state.cutoff_global_position,
                )
            )
            self._runs.record_evaluation(request.run_id, evaluation)
        with self._telemetry.span(WorkflowSpanName.TRANSITION_COMMIT, run_id=request.run_id):
            self._record_transition(request.run_id)
        return self._runs.complete(request.run_id, _run_outcome(authorization, None))

    def _finish_unknown_execution(
        self,
        request: DailyOperatorV2Request,
        *,
        initial_state: OperationalBeliefState,
        proposal: ActionProposal,
        authorization: AuthorizationDecision,
        execution: ExecutionResult,
        execution_event_id: str,
        adapter_contract_version: str,
    ) -> RunTerminal:
        with self._telemetry.span(WorkflowSpanName.EVALUATION_GRADE, run_id=request.run_id):
            evaluation = self._evaluator.handle(
                EvaluateOutcome(
                    run_id=request.run_id,
                    spec=request.evaluation_spec,
                    authorization_outcome=EvaluationAuthorizationOutcome.ALLOW,
                    execution_status=EvaluationExecutionStatus.UNKNOWN,
                    execution_event_id=execution_event_id,
                    execution_binding_id=self._outcome_binding(
                        request,
                        proposal=proposal,
                        authorization=authorization,
                        execution=execution,
                        adapter_contract_version=adapter_contract_version,
                    ).binding_id,
                    observation=None,
                    initial_state_position=initial_state.cutoff_global_position,
                )
            )
            self._runs.record_evaluation(request.run_id, evaluation)
        with self._telemetry.span(WorkflowSpanName.TRANSITION_COMMIT, run_id=request.run_id):
            self._record_transition(request.run_id)
        return self._runs.complete(request.run_id, _run_outcome(authorization, execution))

    def _record_transition(self, run_id: str) -> None:
        acceptance = bind_and_accept_state_transition(
            run_id,
            self._history,
            self._artifacts,
        )
        if acceptance.transition is not None:
            self._runs.record_accepted_transition(run_id, acceptance.transition)

    @staticmethod
    def _outcome_binding(
        request: DailyOperatorV2Request,
        *,
        proposal: ActionProposal,
        authorization: AuthorizationDecision,
        execution: ExecutionResult,
        adapter_contract_version: str,
    ) -> OutcomeExecutionBinding:
        return OutcomeExecutionBinding(
            run_id=request.run_id,
            invocation_id=execution.invocation_id,
            proposal_id=proposal.proposal_id,
            proposal_digest=proposal.proposal_digest,
            authorization_decision_id=authorization.decision_id,
            authorized_action_digest=authorization.authorized_action_digest,
            execution_result_id=execution.result_id,
            execution_identity_digest=execution.execution_identity_digest,
            execution_status=execution.status.value,
            affordance=execution.affordance,
            arguments=tuple(OutcomeArgument(item.name, item.value) for item in proposal.arguments),
            execution_adapter_id=execution.adapter_id,
            execution_adapter_contract_version=adapter_contract_version,
            completed_at=execution.completed_at,
        )


def _run_outcome(
    authorization: AuthorizationDecision,
    execution: ExecutionResult | None,
) -> RunOutcome:
    if authorization.outcome is AuthorizationOutcome.DENY:
        return RunOutcome.DENIED
    if authorization.outcome is AuthorizationOutcome.REQUIRE_APPROVAL:
        return RunOutcome.APPROVAL_REQUIRED
    if execution is None:
        raise ValueError("allowed authorization requires an execution result")
    return {
        ExecutionStatus.SUCCEEDED: RunOutcome.EXECUTED,
        ExecutionStatus.FAILED: RunOutcome.EXECUTION_FAILED,
        ExecutionStatus.UNKNOWN: RunOutcome.REQUIRES_RECONCILIATION,
    }[execution.status]


__all__ = ["DailyOperatorV2Workflow"]
