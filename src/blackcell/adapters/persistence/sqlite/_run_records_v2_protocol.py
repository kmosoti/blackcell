from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import cast

from blackcell.features.authorize_action import (
    ActionProposal,
    AuthorizationDecision,
    AuthorizationOutcome,
)
from blackcell.features.execute_affordance import (
    ExecutionPreparation,
    ExecutionResult,
    SideEffectClass,
)
from blackcell.features.observe_outcome import OutcomeArgument, OutcomeExecutionBinding
from blackcell.features.request_decision import (
    DecisionAffordance,
    DecisionArgumentSpec,
    DecisionResponse,
    DecisionRoute,
    DecisionUsage,
    RequestDecision,
)
from blackcell.kernel import EventEnvelope, EventStore, JsonInput, JsonScalar, ProjectionCheckpoint
from blackcell.workflows.daily_operator_v2_request import DailyOperatorV2Request
from blackcell.workflows.run_protocol import (
    AUTHORIZATION_DECIDED,
    EXECUTION_RECORDED,
    RunOutcome,
    RunProtocolIntegrityError,
)

from ._run_records_v2_artifacts import _run_link, _text


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
