from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import cast

from blackcell.features.authorize_action.models import (
    ActionArgument,
    ActionProposal,
    AuthorizationDecision,
    AuthorizationFinding,
    AuthorizationOutcome,
)
from blackcell.kernel import JsonScalar
from blackcell.kernel._json import canonical_json_bytes

ACTION_PROPOSAL_MEDIA_TYPE = "application/vnd.blackcell.action-proposal+json"
AUTHORIZATION_DECISION_MEDIA_TYPE = "application/vnd.blackcell.authorization-decision+json"
ACTION_PROPOSAL_SCHEMA_VERSION = "action-proposal/v2"
AUTHORIZATION_DECISION_SCHEMA_VERSION = "authorization-decision/v2"

_PROPOSAL_KEYS = frozenset(
    {
        "schema_version",
        "proposal_id",
        "context_frame_id",
        "affordance",
        "arguments",
        "rationale",
        "evidence_event_ids",
        "action_digest",
        "proposal_digest",
    }
)
_ARGUMENT_KEYS = frozenset({"name", "value"})
_DECISION_KEYS = frozenset(
    {
        "schema_version",
        "decision_id",
        "proposal_id",
        "proposal_digest",
        "context_frame_id",
        "constraint_evaluation_id",
        "authorized_action_digest",
        "affordance_policy_digest",
        "authorized_read_only",
        "authorized_external",
        "authorized_mutates_state",
        "outcome",
        "findings",
        "evaluated_at",
        "approval_granted",
    }
)
_FINDING_KEYS = frozenset({"outcome", "code", "message", "proof_ids"})


class AuthorizationArtifactCodecError(ValueError):
    """An authorization artifact is malformed or fails its derived identity."""


def action_proposal_payload(proposal: ActionProposal) -> dict[str, object]:
    """Return the complete, explicit artifact representation of a proposal."""

    return {
        "schema_version": proposal.schema_version,
        "proposal_id": proposal.proposal_id,
        "context_frame_id": proposal.context_frame_id,
        "affordance": proposal.affordance,
        "arguments": [{"name": item.name, "value": item.value} for item in proposal.arguments],
        "rationale": proposal.rationale,
        "evidence_event_ids": list(proposal.evidence_event_ids),
        "action_digest": proposal.action_digest,
        "proposal_digest": proposal.proposal_digest,
    }


def encode_action_proposal(proposal: ActionProposal) -> bytes:
    _require_schema(proposal.schema_version, ACTION_PROPOSAL_SCHEMA_VERSION, "ActionProposal")
    return canonical_json_bytes(action_proposal_payload(proposal))


def decode_action_proposal(data: bytes) -> ActionProposal:
    payload = _decode_object(data, label="ActionProposal")
    _require_keys(payload, _PROPOSAL_KEYS, label="ActionProposal")
    _require_schema(payload["schema_version"], ACTION_PROPOSAL_SCHEMA_VERSION, "ActionProposal")
    arguments = tuple(
        _decode_argument(item, index=index)
        for index, item in enumerate(_require_list(payload["arguments"], "arguments"))
    )
    try:
        proposal = ActionProposal(
            proposal_id=_require_text(payload["proposal_id"], "proposal_id"),
            context_frame_id=_require_text(payload["context_frame_id"], "context_frame_id"),
            affordance=_require_text(payload["affordance"], "affordance"),
            arguments=arguments,
            rationale=_require_text(payload["rationale"], "rationale"),
            evidence_event_ids=_require_text_tuple(
                payload["evidence_event_ids"], "evidence_event_ids"
            ),
            schema_version=_require_text(payload["schema_version"], "schema_version"),
        )
    except ValueError as error:
        raise AuthorizationArtifactCodecError(
            "ActionProposal violates its domain contract"
        ) from error
    _require_derived_identity(payload, "action_digest", proposal.action_digest)
    _require_derived_identity(payload, "proposal_digest", proposal.proposal_digest)
    return proposal


def authorization_decision_payload(decision: AuthorizationDecision) -> dict[str, object]:
    return {
        "schema_version": decision.schema_version,
        "decision_id": decision.decision_id,
        "proposal_id": decision.proposal_id,
        "proposal_digest": decision.proposal_digest,
        "context_frame_id": decision.context_frame_id,
        "constraint_evaluation_id": decision.constraint_evaluation_id,
        "authorized_action_digest": decision.authorized_action_digest,
        "affordance_policy_digest": decision.affordance_policy_digest,
        "authorized_read_only": decision.authorized_read_only,
        "authorized_external": decision.authorized_external,
        "authorized_mutates_state": decision.authorized_mutates_state,
        "outcome": decision.outcome.value,
        "findings": [
            {
                "outcome": item.outcome.value,
                "code": item.code,
                "message": item.message,
                "proof_ids": list(item.proof_ids),
            }
            for item in decision.findings
        ],
        "evaluated_at": decision.evaluated_at.isoformat(),
        "approval_granted": decision.approval_granted,
    }


def encode_authorization_decision(decision: AuthorizationDecision) -> bytes:
    _require_schema(
        decision.schema_version,
        AUTHORIZATION_DECISION_SCHEMA_VERSION,
        "AuthorizationDecision",
    )
    return canonical_json_bytes(authorization_decision_payload(decision))


def decode_authorization_decision(data: bytes) -> AuthorizationDecision:
    payload = _decode_object(data, label="AuthorizationDecision")
    _require_keys(payload, _DECISION_KEYS, label="AuthorizationDecision")
    _require_schema(
        payload["schema_version"],
        AUTHORIZATION_DECISION_SCHEMA_VERSION,
        "AuthorizationDecision",
    )
    findings = tuple(
        _decode_finding(item, index=index)
        for index, item in enumerate(_require_list(payload["findings"], "findings"))
    )
    try:
        decision = AuthorizationDecision(
            proposal_id=_require_text(payload["proposal_id"], "proposal_id"),
            proposal_digest=_require_text(payload["proposal_digest"], "proposal_digest"),
            context_frame_id=_require_text(payload["context_frame_id"], "context_frame_id"),
            constraint_evaluation_id=_require_text(
                payload["constraint_evaluation_id"], "constraint_evaluation_id"
            ),
            authorized_action_digest=_require_text(
                payload["authorized_action_digest"], "authorized_action_digest"
            ),
            affordance_policy_digest=_require_text(
                payload["affordance_policy_digest"], "affordance_policy_digest"
            ),
            authorized_read_only=_require_bool(
                payload["authorized_read_only"], "authorized_read_only"
            ),
            authorized_external=_require_bool(
                payload["authorized_external"], "authorized_external"
            ),
            authorized_mutates_state=_require_bool(
                payload["authorized_mutates_state"], "authorized_mutates_state"
            ),
            outcome=_require_outcome(payload["outcome"], "outcome"),
            findings=findings,
            evaluated_at=_require_datetime(payload["evaluated_at"], "evaluated_at"),
            approval_granted=_require_bool(payload["approval_granted"], "approval_granted"),
            schema_version=_require_text(payload["schema_version"], "schema_version"),
        )
    except ValueError as error:
        raise AuthorizationArtifactCodecError(
            "AuthorizationDecision violates its domain contract"
        ) from error
    _require_derived_identity(payload, "decision_id", decision.decision_id)
    return decision


def _decode_argument(value: object, *, index: int) -> ActionArgument:
    label = f"arguments[{index}]"
    payload = _require_mapping(value, label)
    _require_keys(payload, _ARGUMENT_KEYS, label=label)
    return ActionArgument(
        _require_text(payload["name"], f"{label}.name"),
        _require_json_scalar(payload["value"], f"{label}.value"),
    )


def _decode_finding(value: object, *, index: int) -> AuthorizationFinding:
    label = f"findings[{index}]"
    payload = _require_mapping(value, label)
    _require_keys(payload, _FINDING_KEYS, label=label)
    try:
        return AuthorizationFinding(
            outcome=_require_outcome(payload["outcome"], f"{label}.outcome"),
            code=_require_text(payload["code"], f"{label}.code"),
            message=_require_text(payload["message"], f"{label}.message"),
            proof_ids=_require_text_tuple(payload["proof_ids"], f"{label}.proof_ids"),
        )
    except ValueError as error:
        raise AuthorizationArtifactCodecError(f"{label} violates its contract") from error


def _decode_object(data: bytes, *, label: str) -> dict[str, object]:
    if not isinstance(data, bytes):
        raise TypeError("artifact data must be bytes")
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AuthorizationArtifactCodecError(f"{label} must be UTF-8 JSON") from error
    if canonical_json_bytes(value) != data:
        raise AuthorizationArtifactCodecError(f"{label} must use canonical JSON encoding")
    return _require_mapping(value, label)


def _require_mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise AuthorizationArtifactCodecError(f"{label} must be a JSON object")
    if any(not isinstance(key, str) for key in value):
        raise AuthorizationArtifactCodecError(f"{label} keys must be strings")
    return cast("dict[str, object]", value)


def _require_keys(payload: Mapping[str, object], expected: frozenset[str], *, label: str) -> None:
    actual = frozenset(payload)
    if actual != expected:
        raise AuthorizationArtifactCodecError(
            f"{label} fields differ; missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}"
        )


def _require_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AuthorizationArtifactCodecError(f"{label} must be a non-empty string")
    return value


def _require_text_tuple(value: object, label: str) -> tuple[str, ...]:
    items = _require_list(value, label)
    return tuple(_require_text(item, f"{label}[]") for item in items)


def _require_list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise AuthorizationArtifactCodecError(f"{label} must be a JSON array")
    return cast("list[object]", value)


def _require_bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise AuthorizationArtifactCodecError(f"{label} must be a boolean")
    return value


def _require_datetime(value: object, label: str) -> datetime:
    text = _require_text(value, label)
    try:
        result = datetime.fromisoformat(text)
    except ValueError as error:
        raise AuthorizationArtifactCodecError(f"{label} must be an ISO timestamp") from error
    if result.tzinfo is None or result.utcoffset() is None:
        raise AuthorizationArtifactCodecError(f"{label} must be timezone-aware")
    return result


def _require_outcome(value: object, label: str) -> AuthorizationOutcome:
    text = _require_text(value, label)
    try:
        return AuthorizationOutcome(text)
    except ValueError as error:
        raise AuthorizationArtifactCodecError(f"{label} is not recognized") from error


def _require_json_scalar(value: object, label: str) -> JsonScalar:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    raise AuthorizationArtifactCodecError(f"{label} must be a JSON scalar")


def _require_derived_identity(payload: Mapping[str, object], field: str, expected: str) -> None:
    actual = _require_text(payload[field], field)
    if actual != expected:
        raise AuthorizationArtifactCodecError(f"{field} does not match artifact content")


def _require_schema(value: object, expected: str, label: str) -> None:
    actual = _require_text(value, f"{label}.schema_version")
    if actual != expected:
        raise AuthorizationArtifactCodecError(
            f"unsupported {label} schema {actual!r}; expected {expected!r}"
        )
