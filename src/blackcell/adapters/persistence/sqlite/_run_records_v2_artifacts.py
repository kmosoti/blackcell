from __future__ import annotations

import json
from collections.abc import Mapping
from typing import cast

from blackcell.features.accept_state_transition import (
    ACCEPTED_STATE_TRANSITION_MEDIA_TYPE,
    AcceptedStateTransition,
    decode_accepted_state_transition,
)
from blackcell.features.authorize_action import (
    ACTION_PROPOSAL_MEDIA_TYPE,
    AUTHORIZATION_DECISION_MEDIA_TYPE,
    ActionProposal,
    AuthorizationDecision,
    decode_action_proposal,
    decode_authorization_decision,
)
from blackcell.features.build_context import (
    CONTEXT_FRAME_MEDIA_TYPE,
    ContextFrame,
    decode_context_frame,
)
from blackcell.features.evaluate_outcome import (
    EVALUATION_SPEC_MEDIA_TYPE,
    OUTCOME_EVALUATION_MEDIA_TYPE,
    EvaluationSpec,
    OutcomeEvaluation,
    decode_evaluation_spec,
)
from blackcell.features.execute_affordance import (
    EXECUTION_PREPARATION_MEDIA_TYPE,
    EXECUTION_RESULT_MEDIA_TYPE,
    ExecutionPreparation,
    ExecutionResult,
    deserialize_execution_preparation,
    deserialize_execution_result,
)
from blackcell.features.observe_outcome import (
    OUTCOME_OBSERVATION_MEDIA_TYPE,
    OutcomeObservation,
    decode_outcome_observation,
)
from blackcell.features.project_operational_state import (
    OPERATIONAL_STATE_SNAPSHOT_MEDIA_TYPE,
    OPERATIONAL_STATE_SNAPSHOT_SCHEMA_VERSION,
    OperationalBeliefState,
    decode_operational_state_snapshot,
)
from blackcell.features.request_decision import (
    DECISION_ATTEMPT_MEDIA_TYPE,
    DECISION_FAILURE_MEDIA_TYPE,
    DECISION_REQUEST_MEDIA_TYPE,
    DECISION_RESPONSE_MEDIA_TYPE,
    DECISION_ROUTE_MEDIA_TYPE,
    DECISION_USAGE_MEDIA_TYPE,
    DecisionAttempt,
    DecisionFailure,
    DecisionResponse,
    DecisionRoute,
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
    decode_constraint_evaluation,
)
from blackcell.kernel import ArtifactRef, EventEnvelope, JsonInput
from blackcell.workflows.daily_operator_v2_identity import (
    DAILY_OPERATOR_REQUEST_V2_MEDIA_TYPE,
    DAILY_OPERATOR_REQUEST_V2_SCHEMA_VERSION,
    daily_operator_v2_request_digest,
    decode_daily_operator_v2_request,
)
from blackcell.workflows.daily_operator_v2_request import DailyOperatorV2Request
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
    RunArtifactLink,
    RunProtocolIntegrityError,
)

_ARTIFACT_KEYS = frozenset(
    {"digest", "media_type", "encoding", "size_bytes", "schema_version", "logical_id"}
)


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


def _usage_payload(usage: DecisionUsage | None) -> dict[str, JsonInput]:
    return {
        "usage_id": None if usage is None else usage.usage_id,
        "input_tokens": None if usage is None else usage.input_tokens,
        "output_tokens": None if usage is None else usage.output_tokens,
        "latency_ms": None if usage is None else usage.latency_ms,
        "cost_microusd": None if usage is None else usage.cost_microusd,
        "deterministic": None if usage is None else usage.deterministic,
    }


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
