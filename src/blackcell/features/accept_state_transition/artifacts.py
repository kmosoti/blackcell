from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import cast

from blackcell.features.accept_state_transition.models import (
    ACCEPTED_STATE_TRANSITION_SCHEMA_VERSION,
    AcceptedStateTransition,
    AuthorizationReference,
    ClaimDelta,
    ConflictChange,
    EvaluationReference,
    EvidenceScopedConflict,
    ExecutionReference,
    ProposalReference,
    StateSnapshotReference,
    TransitionActionArgument,
    TransitionAuthorizationOutcome,
    TransitionClaim,
    TransitionEpistemicStatus,
    TransitionEvaluationFinding,
    TransitionEvaluationVerdict,
    TransitionEventReference,
    TransitionExecutionStatus,
    _transition_identity_payload,
)
from blackcell.kernel import JsonScalar
from blackcell.kernel._json import canonical_json_bytes

ACCEPTED_STATE_TRANSITION_MEDIA_TYPE = "application/vnd.blackcell.accepted-state-transition+json"

_ROOT_KEYS = frozenset(
    {
        "schema_version",
        "transition_id",
        "run_id",
        "initial_state",
        "outcome_state",
        "proposal",
        "authorization",
        "execution",
        "evaluation",
        "triggering_events",
        "accepted_claim_ids",
        "accepted_source_event_ids",
        "claim_deltas",
        "conflict_changes",
    }
)
_SNAPSHOT_KEYS = frozenset(
    {
        "snapshot_digest",
        "domain",
        "stream_id",
        "cutoff_global_position",
        "last_source_stream_sequence",
        "effective_time_cutoff",
    }
)
_PROPOSAL_KEYS = frozenset(
    {
        "proposal_id",
        "proposal_digest",
        "proposal_artifact_digest",
        "context_frame_id",
        "affordance",
        "arguments",
        "action_digest",
    }
)
_ARGUMENT_KEYS = frozenset({"name", "value"})
_AUTHORIZATION_KEYS = frozenset(
    {
        "decision_id",
        "decision_artifact_digest",
        "proposal_id",
        "proposal_digest",
        "constraint_evaluation_id",
        "authorized_action_digest",
        "affordance_policy_digest",
        "outcome",
        "approval_granted",
    }
)
_EXECUTION_KEYS = frozenset(
    {
        "run_id",
        "execution_event_id",
        "execution_result_id",
        "execution_result_digest",
        "invocation_id",
        "proposal_id",
        "proposal_digest",
        "authorization_decision_id",
        "execution_binding_id",
        "execution_identity_digest",
        "authorized_action_digest",
        "idempotency_key",
        "affordance",
        "arguments",
        "adapter_id",
        "adapter_contract_version",
        "status",
        "completed_at",
    }
)
_EVALUATION_KEYS = frozenset(
    {
        "evaluation_id",
        "evaluation_artifact_digest",
        "evaluation_spec_id",
        "evaluation_spec_digest",
        "run_id",
        "authorization_outcome",
        "execution_status",
        "verdict",
        "execution_event_id",
        "execution_binding_id",
        "evidence_binding_id",
        "owner_observation_id",
        "owner_observation_digest",
        "owner_observation_artifact_digest",
        "initial_state_position",
        "findings",
        "evaluated_at",
    }
)
_FINDING_KEYS = frozenset(
    {
        "criterion_id",
        "required",
        "verdict",
        "code",
        "expected_value",
        "actual_present",
        "actual_value",
        "actual_confidence",
        "observed_claim_ids",
        "source_event_ids",
    }
)
_EVENT_KEYS = frozenset(
    {
        "event_id",
        "global_position",
        "stream_sequence",
        "event_type",
        "stream_id",
        "correlation_id",
        "causation_id",
        "payload_hash",
    }
)
_DELTA_KEYS = frozenset({"subject", "predicate", "accepted_claim_ids", "before", "after"})
_CLAIM_KEYS = frozenset(
    {
        "claim_id",
        "subject",
        "predicate",
        "value",
        "confidence",
        "effective_at",
        "recorded_at",
        "source_event_id",
        "source",
        "actor",
        "correlation_id",
        "domain",
        "stream_id",
        "stream_sequence",
        "global_position",
        "correction_id",
        "supersedes_claim_ids",
        "expires_at",
        "epistemic_status",
        "unknown_reason",
    }
)
_CONFLICT_CHANGE_KEYS = frozenset({"subject", "predicate", "before", "after"})
_CONFLICT_KEYS = frozenset({"subject", "predicate", "source_event_ids", "claim_ids", "values"})


class StateTransitionArtifactCodecError(ValueError):
    """An accepted-transition artifact is malformed or fails content identity."""


def accepted_state_transition_payload(
    transition: AcceptedStateTransition,
) -> dict[str, object]:
    return {
        **_transition_identity_payload(transition),
        "transition_id": transition.transition_id,
    }


def encode_accepted_state_transition(transition: AcceptedStateTransition) -> bytes:
    if transition.schema_version != ACCEPTED_STATE_TRANSITION_SCHEMA_VERSION:
        raise StateTransitionArtifactCodecError(
            f"unsupported AcceptedStateTransition schema {transition.schema_version!r}"
        )
    return canonical_json_bytes(accepted_state_transition_payload(transition))


def decode_accepted_state_transition(data: bytes) -> AcceptedStateTransition:
    """Decode strict canonical content, without proving external artifacts or ledger events.

    Activation still requires the canonical workflow binder to verify every cited owner artifact
    and event.  A successful decode must never be treated as v2/replay provenance proof.
    """

    payload = _decode_object(data, "AcceptedStateTransition")
    _keys(payload, _ROOT_KEYS, "AcceptedStateTransition")
    schema = _text(payload["schema_version"], "schema_version")
    if schema != ACCEPTED_STATE_TRANSITION_SCHEMA_VERSION:
        raise StateTransitionArtifactCodecError(
            f"unsupported AcceptedStateTransition schema {schema!r}"
        )
    try:
        transition = AcceptedStateTransition(
            run_id=_text(payload["run_id"], "run_id"),
            initial_state=_snapshot(payload["initial_state"], "initial_state"),
            outcome_state=_snapshot(payload["outcome_state"], "outcome_state"),
            proposal=_proposal(payload["proposal"]),
            authorization=_authorization(payload["authorization"]),
            execution=_execution(payload["execution"]),
            evaluation=_evaluation(payload["evaluation"]),
            triggering_events=tuple(
                _event(item, index)
                for index, item in enumerate(
                    _array(payload["triggering_events"], "triggering_events")
                )
            ),
            accepted_claim_ids=_strings(payload["accepted_claim_ids"], "accepted_claim_ids"),
            accepted_source_event_ids=_strings(
                payload["accepted_source_event_ids"], "accepted_source_event_ids"
            ),
            claim_deltas=tuple(
                _delta(item, index)
                for index, item in enumerate(_array(payload["claim_deltas"], "claim_deltas"))
            ),
            conflict_changes=tuple(
                _conflict_change(item, index)
                for index, item in enumerate(
                    _array(payload["conflict_changes"], "conflict_changes")
                )
            ),
            schema_version=schema,
        )
    except StateTransitionArtifactCodecError:
        raise
    except (TypeError, ValueError) as error:
        raise StateTransitionArtifactCodecError(
            "AcceptedStateTransition violates its domain contract"
        ) from error
    if _text(payload["transition_id"], "transition_id") != transition.transition_id:
        raise StateTransitionArtifactCodecError(
            "transition_id does not match accepted transition content"
        )
    if encode_accepted_state_transition(transition) != data:
        raise StateTransitionArtifactCodecError(
            "AcceptedStateTransition collections and timestamps must use canonical domain ordering"
        )
    return transition


def _snapshot(value: object, label: str) -> StateSnapshotReference:
    payload = _mapping(value, label)
    _keys(payload, _SNAPSHOT_KEYS, label)
    return StateSnapshotReference(
        snapshot_digest=_text(payload["snapshot_digest"], f"{label}.snapshot_digest"),
        domain=_text(payload["domain"], f"{label}.domain"),
        stream_id=_text(payload["stream_id"], f"{label}.stream_id"),
        cutoff_global_position=_integer(
            payload["cutoff_global_position"], f"{label}.cutoff_global_position"
        ),
        last_source_stream_sequence=_integer(
            payload["last_source_stream_sequence"],
            f"{label}.last_source_stream_sequence",
        ),
        effective_time_cutoff=_optional_datetime(
            payload["effective_time_cutoff"], f"{label}.effective_time_cutoff"
        ),
    )


def _proposal(value: object) -> ProposalReference:
    payload = _mapping(value, "proposal")
    _keys(payload, _PROPOSAL_KEYS, "proposal")
    arguments: list[TransitionActionArgument] = []
    for index, item in enumerate(_array(payload["arguments"], "proposal.arguments")):
        label = f"proposal.arguments[{index}]"
        argument = _mapping(item, label)
        _keys(argument, _ARGUMENT_KEYS, label)
        arguments.append(
            TransitionActionArgument(
                _text(argument["name"], f"{label}.name"),
                _scalar(argument["value"], f"{label}.value"),
            )
        )
    return ProposalReference(
        proposal_id=_text(payload["proposal_id"], "proposal.proposal_id"),
        proposal_digest=_text(payload["proposal_digest"], "proposal.proposal_digest"),
        proposal_artifact_digest=_text(
            payload["proposal_artifact_digest"], "proposal.proposal_artifact_digest"
        ),
        context_frame_id=_text(payload["context_frame_id"], "proposal.context_frame_id"),
        affordance=_text(payload["affordance"], "proposal.affordance"),
        arguments=tuple(arguments),
        action_digest=_text(payload["action_digest"], "proposal.action_digest"),
    )


def _authorization(value: object) -> AuthorizationReference:
    payload = _mapping(value, "authorization")
    _keys(payload, _AUTHORIZATION_KEYS, "authorization")
    return AuthorizationReference(
        decision_id=_text(payload["decision_id"], "authorization.decision_id"),
        decision_artifact_digest=_text(
            payload["decision_artifact_digest"], "authorization.decision_artifact_digest"
        ),
        proposal_id=_text(payload["proposal_id"], "authorization.proposal_id"),
        proposal_digest=_text(payload["proposal_digest"], "authorization.proposal_digest"),
        constraint_evaluation_id=_text(
            payload["constraint_evaluation_id"],
            "authorization.constraint_evaluation_id",
        ),
        authorized_action_digest=_text(
            payload["authorized_action_digest"],
            "authorization.authorized_action_digest",
        ),
        affordance_policy_digest=_text(
            payload["affordance_policy_digest"],
            "authorization.affordance_policy_digest",
        ),
        outcome=_enum(
            TransitionAuthorizationOutcome,
            payload["outcome"],
            "authorization.outcome",
        ),
        approval_granted=_boolean(payload["approval_granted"], "authorization.approval_granted"),
    )


def _execution(value: object) -> ExecutionReference:
    payload = _mapping(value, "execution")
    _keys(payload, _EXECUTION_KEYS, "execution")
    arguments: list[TransitionActionArgument] = []
    for index, item in enumerate(_array(payload["arguments"], "execution.arguments")):
        label = f"execution.arguments[{index}]"
        argument = _mapping(item, label)
        _keys(argument, _ARGUMENT_KEYS, label)
        arguments.append(
            TransitionActionArgument(
                _text(argument["name"], f"{label}.name"),
                _scalar(argument["value"], f"{label}.value"),
            )
        )
    return ExecutionReference(
        run_id=_text(payload["run_id"], "execution.run_id"),
        execution_event_id=_text(payload["execution_event_id"], "execution.execution_event_id"),
        execution_result_id=_text(payload["execution_result_id"], "execution.execution_result_id"),
        execution_result_digest=_text(
            payload["execution_result_digest"], "execution.execution_result_digest"
        ),
        invocation_id=_text(payload["invocation_id"], "execution.invocation_id"),
        proposal_id=_text(payload["proposal_id"], "execution.proposal_id"),
        proposal_digest=_text(payload["proposal_digest"], "execution.proposal_digest"),
        authorization_decision_id=_text(
            payload["authorization_decision_id"],
            "execution.authorization_decision_id",
        ),
        execution_binding_id=_text(
            payload["execution_binding_id"], "execution.execution_binding_id"
        ),
        execution_identity_digest=_text(
            payload["execution_identity_digest"],
            "execution.execution_identity_digest",
        ),
        authorized_action_digest=_text(
            payload["authorized_action_digest"],
            "execution.authorized_action_digest",
        ),
        idempotency_key=_text(payload["idempotency_key"], "execution.idempotency_key"),
        affordance=_text(payload["affordance"], "execution.affordance"),
        arguments=tuple(arguments),
        adapter_id=_text(payload["adapter_id"], "execution.adapter_id"),
        adapter_contract_version=_text(
            payload["adapter_contract_version"],
            "execution.adapter_contract_version",
        ),
        status=_enum(TransitionExecutionStatus, payload["status"], "execution.status"),
        completed_at=_datetime(payload["completed_at"], "execution.completed_at"),
    )


def _evaluation(value: object) -> EvaluationReference:
    payload = _mapping(value, "evaluation")
    _keys(payload, _EVALUATION_KEYS, "evaluation")
    findings: list[TransitionEvaluationFinding] = []
    for index, item in enumerate(_array(payload["findings"], "evaluation.findings")):
        label = f"evaluation.findings[{index}]"
        finding = _mapping(item, label)
        _keys(finding, _FINDING_KEYS, label)
        findings.append(
            TransitionEvaluationFinding(
                criterion_id=_text(finding["criterion_id"], f"{label}.criterion_id"),
                required=_boolean(finding["required"], f"{label}.required"),
                verdict=_enum(
                    TransitionEvaluationVerdict,
                    finding["verdict"],
                    f"{label}.verdict",
                ),
                code=_text(finding["code"], f"{label}.code"),
                expected_value=_scalar(finding["expected_value"], f"{label}.expected_value"),
                actual_present=_boolean(finding["actual_present"], f"{label}.actual_present"),
                actual_value=_scalar(finding["actual_value"], f"{label}.actual_value"),
                actual_confidence=_optional_number(
                    finding["actual_confidence"], f"{label}.actual_confidence"
                ),
                observed_claim_ids=_strings(
                    finding["observed_claim_ids"], f"{label}.observed_claim_ids"
                ),
                source_event_ids=_strings(finding["source_event_ids"], f"{label}.source_event_ids"),
            )
        )
    return EvaluationReference(
        evaluation_id=_text(payload["evaluation_id"], "evaluation.evaluation_id"),
        evaluation_artifact_digest=_text(
            payload["evaluation_artifact_digest"],
            "evaluation.evaluation_artifact_digest",
        ),
        evaluation_spec_id=_text(payload["evaluation_spec_id"], "evaluation.evaluation_spec_id"),
        evaluation_spec_digest=_text(
            payload["evaluation_spec_digest"], "evaluation.evaluation_spec_digest"
        ),
        run_id=_text(payload["run_id"], "evaluation.run_id"),
        authorization_outcome=_enum(
            TransitionAuthorizationOutcome,
            payload["authorization_outcome"],
            "evaluation.authorization_outcome",
        ),
        execution_status=_optional_enum(
            TransitionExecutionStatus,
            payload["execution_status"],
            "evaluation.execution_status",
        ),
        verdict=_enum(TransitionEvaluationVerdict, payload["verdict"], "evaluation.verdict"),
        execution_event_id=_optional_text(
            payload["execution_event_id"], "evaluation.execution_event_id"
        ),
        execution_binding_id=_optional_text(
            payload["execution_binding_id"], "evaluation.execution_binding_id"
        ),
        evidence_binding_id=_optional_text(
            payload["evidence_binding_id"], "evaluation.evidence_binding_id"
        ),
        owner_observation_id=_optional_text(
            payload["owner_observation_id"], "evaluation.owner_observation_id"
        ),
        owner_observation_digest=_optional_text(
            payload["owner_observation_digest"],
            "evaluation.owner_observation_digest",
        ),
        owner_observation_artifact_digest=_optional_text(
            payload["owner_observation_artifact_digest"],
            "evaluation.owner_observation_artifact_digest",
        ),
        initial_state_position=_integer(
            payload["initial_state_position"], "evaluation.initial_state_position"
        ),
        findings=tuple(findings),
        evaluated_at=_datetime(payload["evaluated_at"], "evaluation.evaluated_at"),
    )


def _event(value: object, index: int) -> TransitionEventReference:
    label = f"triggering_events[{index}]"
    payload = _mapping(value, label)
    _keys(payload, _EVENT_KEYS, label)
    return TransitionEventReference(
        event_id=_text(payload["event_id"], f"{label}.event_id"),
        global_position=_integer(payload["global_position"], f"{label}.global_position"),
        stream_sequence=_integer(payload["stream_sequence"], f"{label}.stream_sequence"),
        event_type=_text(payload["event_type"], f"{label}.event_type"),
        stream_id=_text(payload["stream_id"], f"{label}.stream_id"),
        correlation_id=_text(payload["correlation_id"], f"{label}.correlation_id"),
        causation_id=_text(payload["causation_id"], f"{label}.causation_id"),
        payload_hash=_text(payload["payload_hash"], f"{label}.payload_hash"),
    )


def _delta(value: object, index: int) -> ClaimDelta:
    label = f"claim_deltas[{index}]"
    payload = _mapping(value, label)
    _keys(payload, _DELTA_KEYS, label)
    return ClaimDelta(
        subject=_text(payload["subject"], f"{label}.subject"),
        predicate=_text(payload["predicate"], f"{label}.predicate"),
        accepted_claim_ids=_strings(payload["accepted_claim_ids"], f"{label}.accepted_claim_ids"),
        before=_claims(payload["before"], f"{label}.before"),
        after=_claims(payload["after"], f"{label}.after"),
    )


def _claims(value: object, label: str) -> tuple[TransitionClaim, ...]:
    return tuple(
        _claim(item, f"{label}[{index}]") for index, item in enumerate(_array(value, label))
    )


def _claim(value: object, label: str) -> TransitionClaim:
    payload = _mapping(value, label)
    _keys(payload, _CLAIM_KEYS, label)
    return TransitionClaim(
        claim_id=_text(payload["claim_id"], f"{label}.claim_id"),
        subject=_text(payload["subject"], f"{label}.subject"),
        predicate=_text(payload["predicate"], f"{label}.predicate"),
        value=_scalar(payload["value"], f"{label}.value"),
        confidence=_number(payload["confidence"], f"{label}.confidence"),
        effective_at=_datetime(payload["effective_at"], f"{label}.effective_at"),
        recorded_at=_datetime(payload["recorded_at"], f"{label}.recorded_at"),
        source_event_id=_text(payload["source_event_id"], f"{label}.source_event_id"),
        source=_text(payload["source"], f"{label}.source"),
        actor=_text(payload["actor"], f"{label}.actor"),
        correlation_id=_text(payload["correlation_id"], f"{label}.correlation_id"),
        domain=_text(payload["domain"], f"{label}.domain"),
        stream_id=_text(payload["stream_id"], f"{label}.stream_id"),
        stream_sequence=_integer(payload["stream_sequence"], f"{label}.stream_sequence"),
        global_position=_integer(payload["global_position"], f"{label}.global_position"),
        correction_id=_optional_text(payload["correction_id"], f"{label}.correction_id"),
        supersedes_claim_ids=_strings(
            payload["supersedes_claim_ids"], f"{label}.supersedes_claim_ids"
        ),
        expires_at=_optional_datetime(payload["expires_at"], f"{label}.expires_at"),
        epistemic_status=_enum(
            TransitionEpistemicStatus,
            payload["epistemic_status"],
            f"{label}.epistemic_status",
        ),
        unknown_reason=_optional_text(payload["unknown_reason"], f"{label}.unknown_reason"),
    )


def _conflict_change(value: object, index: int) -> ConflictChange:
    label = f"conflict_changes[{index}]"
    payload = _mapping(value, label)
    _keys(payload, _CONFLICT_CHANGE_KEYS, label)
    return ConflictChange(
        subject=_text(payload["subject"], f"{label}.subject"),
        predicate=_text(payload["predicate"], f"{label}.predicate"),
        before=_optional_conflict(payload["before"], f"{label}.before"),
        after=_optional_conflict(payload["after"], f"{label}.after"),
    )


def _optional_conflict(value: object, label: str) -> EvidenceScopedConflict | None:
    if value is None:
        return None
    payload = _mapping(value, label)
    _keys(payload, _CONFLICT_KEYS, label)
    return EvidenceScopedConflict(
        subject=_text(payload["subject"], f"{label}.subject"),
        predicate=_text(payload["predicate"], f"{label}.predicate"),
        source_event_ids=_strings(payload["source_event_ids"], f"{label}.source_event_ids"),
        claim_ids=_strings(payload["claim_ids"], f"{label}.claim_ids"),
        values=tuple(
            _scalar(item, f"{label}.values[{index}]")
            for index, item in enumerate(_array(payload["values"], f"{label}.values"))
        ),
    )


def _decode_object(data: bytes, label: str) -> dict[str, object]:
    if not isinstance(data, bytes):
        raise TypeError("artifact data must be bytes")
    try:
        value = json.loads(data.decode("utf-8"))
        encoded = canonical_json_bytes(value)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise StateTransitionArtifactCodecError(f"{label} must be UTF-8 canonical JSON") from error
    if encoded != data:
        raise StateTransitionArtifactCodecError(f"{label} must use canonical JSON encoding")
    return _mapping(value, label)


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise StateTransitionArtifactCodecError(f"{label} must be a JSON object")
    return cast("dict[str, object]", value)


def _array(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise StateTransitionArtifactCodecError(f"{label} must be a JSON array")
    return cast("list[object]", value)


def _keys(payload: Mapping[str, object], expected: frozenset[str], label: str) -> None:
    actual = frozenset(payload)
    if actual != expected:
        raise StateTransitionArtifactCodecError(
            f"{label} fields differ; missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}"
        )


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StateTransitionArtifactCodecError(f"{label} must be a non-empty string")
    return value


def _optional_text(value: object, label: str) -> str | None:
    return None if value is None else _text(value, label)


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise StateTransitionArtifactCodecError(f"{label} must be a boolean")
    return value


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise StateTransitionArtifactCodecError(f"{label} must be an integer")
    return value


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise StateTransitionArtifactCodecError(f"{label} must be numeric")
    return float(value)


def _optional_number(value: object, label: str) -> float | None:
    return None if value is None else _number(value, label)


def _scalar(value: object, label: str) -> JsonScalar:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    raise StateTransitionArtifactCodecError(f"{label} must be a JSON scalar")


def _strings(value: object, label: str) -> tuple[str, ...]:
    return tuple(
        _text(item, f"{label}[{index}]") for index, item in enumerate(_array(value, label))
    )


def _datetime(value: object, label: str) -> datetime:
    try:
        result = datetime.fromisoformat(_text(value, label))
    except ValueError as error:
        raise StateTransitionArtifactCodecError(f"{label} must be an ISO timestamp") from error
    if result.tzinfo is None or result.utcoffset() is None:
        raise StateTransitionArtifactCodecError(f"{label} must be timezone-aware")
    return result


def _optional_datetime(value: object, label: str) -> datetime | None:
    return None if value is None else _datetime(value, label)


def _enum[
    EnumT: (
        TransitionAuthorizationOutcome,
        TransitionEpistemicStatus,
        TransitionEvaluationVerdict,
        TransitionExecutionStatus,
    )
](enum_type: type[EnumT], value: object, label: str) -> EnumT:
    try:
        return enum_type(_text(value, label))
    except ValueError as error:
        raise StateTransitionArtifactCodecError(f"{label} is not recognized") from error


def _optional_enum[
    EnumT: (
        TransitionAuthorizationOutcome,
        TransitionEpistemicStatus,
        TransitionEvaluationVerdict,
        TransitionExecutionStatus,
    )
](enum_type: type[EnumT], value: object, label: str) -> EnumT | None:
    return None if value is None else _enum(enum_type, value, label)


__all__ = [
    "ACCEPTED_STATE_TRANSITION_MEDIA_TYPE",
    "StateTransitionArtifactCodecError",
    "accepted_state_transition_payload",
    "decode_accepted_state_transition",
    "encode_accepted_state_transition",
]
