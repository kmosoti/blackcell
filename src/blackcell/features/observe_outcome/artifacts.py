from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import cast

from blackcell.features.observe_outcome.models import (
    OUTCOME_EXECUTION_BINDING_SCHEMA_VERSION,
    OUTCOME_OBSERVATION_SCHEMA_VERSION,
    OutcomeArgument,
    OutcomeClaim,
    OutcomeEvidencePointer,
    OutcomeExecutionBinding,
    OutcomeObservation,
    OutcomeObservationStatus,
    _observation_identity_payload,
)
from blackcell.kernel import JsonScalar
from blackcell.kernel._json import canonical_json_bytes

OUTCOME_OBSERVATION_MEDIA_TYPE = "application/vnd.blackcell.outcome-observation+json"

_OBSERVATION_KEYS = frozenset(
    {
        "schema_version",
        "observation_id",
        "observation_digest",
        "binding",
        "evaluation_spec_id",
        "domain",
        "stream_id",
        "observer_id",
        "observer_contract_version",
        "status",
        "observed_at",
        "claims",
        "evidence",
    }
)
_BINDING_KEYS = frozenset(
    {
        "schema_version",
        "binding_id",
        "run_id",
        "invocation_id",
        "proposal_id",
        "proposal_digest",
        "authorization_decision_id",
        "authorized_action_digest",
        "execution_result_id",
        "execution_identity_digest",
        "execution_status",
        "affordance",
        "arguments",
        "execution_adapter_id",
        "execution_adapter_contract_version",
        "completed_at",
    }
)
_ARGUMENT_KEYS = frozenset({"name", "value"})
_CLAIM_KEYS = frozenset({"claim_id", "subject", "predicate", "value", "confidence"})
_EVIDENCE_KEYS = frozenset({"locator", "artifact_id", "digest"})


class OutcomeArtifactCodecError(ValueError):
    """An outcome artifact is malformed or fails its derived identity."""


def outcome_observation_payload(observation: OutcomeObservation) -> dict[str, object]:
    return {
        **_observation_identity_payload(observation),
        "observation_digest": observation.observation_digest,
    }


def encode_outcome_observation(observation: OutcomeObservation) -> bytes:
    _require_schema(
        observation.schema_version,
        OUTCOME_OBSERVATION_SCHEMA_VERSION,
        "OutcomeObservation",
    )
    _require_schema(
        observation.binding.schema_version,
        OUTCOME_EXECUTION_BINDING_SCHEMA_VERSION,
        "OutcomeExecutionBinding",
    )
    return canonical_json_bytes(outcome_observation_payload(observation))


def decode_outcome_observation(data: bytes) -> OutcomeObservation:
    payload = _decode_object(data, label="OutcomeObservation")
    _require_keys(payload, _OBSERVATION_KEYS, label="OutcomeObservation")
    _require_schema(
        payload["schema_version"],
        OUTCOME_OBSERVATION_SCHEMA_VERSION,
        "OutcomeObservation",
    )
    binding = _decode_binding(payload["binding"])
    claims = tuple(
        _decode_claim(item, index=index)
        for index, item in enumerate(_require_list(payload["claims"], "claims"))
    )
    evidence = tuple(
        _decode_evidence(item, index=index)
        for index, item in enumerate(_require_list(payload["evidence"], "evidence"))
    )
    status = _require_status(payload["status"], "status")
    try:
        observation = OutcomeObservation(
            observation_id=_require_text(payload["observation_id"], "observation_id"),
            binding=binding,
            evaluation_spec_id=_require_text(payload["evaluation_spec_id"], "evaluation_spec_id"),
            domain=_require_text(payload["domain"], "domain"),
            stream_id=_require_text(payload["stream_id"], "stream_id"),
            observer_id=_require_text(payload["observer_id"], "observer_id"),
            observer_contract_version=_require_text(
                payload["observer_contract_version"], "observer_contract_version"
            ),
            status=status,
            observed_at=_require_datetime(payload["observed_at"], "observed_at"),
            claims=claims,
            evidence=evidence,
            schema_version=_require_text(payload["schema_version"], "schema_version"),
        )
    except (TypeError, ValueError) as error:
        raise OutcomeArtifactCodecError(
            "OutcomeObservation violates its domain contract"
        ) from error
    _require_derived_identity(payload, "observation_digest", observation.observation_digest)
    if encode_outcome_observation(observation) != data:
        raise OutcomeArtifactCodecError(
            "OutcomeObservation collections and timestamps must use canonical domain ordering"
        )
    return observation


def _decode_binding(value: object) -> OutcomeExecutionBinding:
    payload = _require_mapping(value, "binding")
    _require_keys(payload, _BINDING_KEYS, label="binding")
    _require_schema(
        payload["schema_version"],
        OUTCOME_EXECUTION_BINDING_SCHEMA_VERSION,
        "binding",
    )
    arguments = tuple(
        _decode_argument(item, index=index)
        for index, item in enumerate(_require_list(payload["arguments"], "binding.arguments"))
    )
    try:
        binding = OutcomeExecutionBinding(
            run_id=_require_text(payload["run_id"], "binding.run_id"),
            invocation_id=_require_text(payload["invocation_id"], "binding.invocation_id"),
            proposal_id=_require_text(payload["proposal_id"], "binding.proposal_id"),
            proposal_digest=_require_text(payload["proposal_digest"], "binding.proposal_digest"),
            authorization_decision_id=_require_text(
                payload["authorization_decision_id"],
                "binding.authorization_decision_id",
            ),
            authorized_action_digest=_require_text(
                payload["authorized_action_digest"],
                "binding.authorized_action_digest",
            ),
            execution_result_id=_require_text(
                payload["execution_result_id"], "binding.execution_result_id"
            ),
            execution_identity_digest=_require_text(
                payload["execution_identity_digest"],
                "binding.execution_identity_digest",
            ),
            execution_status=_require_text(payload["execution_status"], "binding.execution_status"),
            affordance=_require_text(payload["affordance"], "binding.affordance"),
            arguments=arguments,
            execution_adapter_id=_require_text(
                payload["execution_adapter_id"], "binding.execution_adapter_id"
            ),
            execution_adapter_contract_version=_require_text(
                payload["execution_adapter_contract_version"],
                "binding.execution_adapter_contract_version",
            ),
            completed_at=_require_datetime(payload["completed_at"], "binding.completed_at"),
            schema_version=_require_text(payload["schema_version"], "binding.schema_version"),
        )
    except (TypeError, ValueError) as error:
        raise OutcomeArtifactCodecError(
            "OutcomeExecutionBinding violates its domain contract"
        ) from error
    _require_derived_identity(payload, "binding_id", binding.binding_id)
    return binding


def _decode_argument(value: object, *, index: int) -> OutcomeArgument:
    label = f"binding.arguments[{index}]"
    payload = _require_mapping(value, label)
    _require_keys(payload, _ARGUMENT_KEYS, label=label)
    try:
        return OutcomeArgument(
            _require_text(payload["name"], f"{label}.name"),
            _require_json_scalar(payload["value"], f"{label}.value"),
        )
    except (TypeError, ValueError) as error:
        raise OutcomeArtifactCodecError(f"{label} violates its contract") from error


def _decode_claim(value: object, *, index: int) -> OutcomeClaim:
    label = f"claims[{index}]"
    payload = _require_mapping(value, label)
    _require_keys(payload, _CLAIM_KEYS, label=label)
    try:
        return OutcomeClaim(
            claim_id=_require_text(payload["claim_id"], f"{label}.claim_id"),
            subject=_require_text(payload["subject"], f"{label}.subject"),
            predicate=_require_text(payload["predicate"], f"{label}.predicate"),
            value=_require_json_scalar(payload["value"], f"{label}.value"),
            confidence=_require_number(payload["confidence"], f"{label}.confidence"),
        )
    except (TypeError, ValueError) as error:
        raise OutcomeArtifactCodecError(f"{label} violates its contract") from error


def _decode_evidence(value: object, *, index: int) -> OutcomeEvidencePointer:
    label = f"evidence[{index}]"
    payload = _require_mapping(value, label)
    _require_keys(payload, _EVIDENCE_KEYS, label=label)
    try:
        return OutcomeEvidencePointer(
            locator=_require_optional_text(payload["locator"], f"{label}.locator"),
            artifact_id=_require_optional_text(payload["artifact_id"], f"{label}.artifact_id"),
            digest=_require_optional_text(payload["digest"], f"{label}.digest"),
        )
    except ValueError as error:
        raise OutcomeArtifactCodecError(f"{label} violates its contract") from error


def _decode_object(data: bytes, *, label: str) -> dict[str, object]:
    if not isinstance(data, bytes):
        raise TypeError("artifact data must be bytes")
    try:
        value = json.loads(data.decode("utf-8"))
        canonical = canonical_json_bytes(value)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise OutcomeArtifactCodecError(f"{label} must be UTF-8 canonical JSON") from error
    if canonical != data:
        raise OutcomeArtifactCodecError(f"{label} must use canonical JSON encoding")
    return _require_mapping(value, label)


def _require_mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise OutcomeArtifactCodecError(f"{label} must be a JSON object")
    if any(not isinstance(key, str) for key in value):
        raise OutcomeArtifactCodecError(f"{label} keys must be strings")
    return cast("dict[str, object]", value)


def _require_keys(payload: Mapping[str, object], expected: frozenset[str], *, label: str) -> None:
    actual = frozenset(payload)
    if actual != expected:
        raise OutcomeArtifactCodecError(
            f"{label} fields differ; missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}"
        )


def _require_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OutcomeArtifactCodecError(f"{label} must be a non-empty string")
    return value


def _require_optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _require_text(value, label)


def _require_list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise OutcomeArtifactCodecError(f"{label} must be a JSON array")
    return cast("list[object]", value)


def _require_datetime(value: object, label: str) -> datetime:
    text = _require_text(value, label)
    try:
        result = datetime.fromisoformat(text)
    except ValueError as error:
        raise OutcomeArtifactCodecError(f"{label} must be an ISO timestamp") from error
    if result.tzinfo is None or result.utcoffset() is None:
        raise OutcomeArtifactCodecError(f"{label} must be timezone-aware")
    return result


def _require_status(value: object, label: str) -> OutcomeObservationStatus:
    text = _require_text(value, label)
    try:
        return OutcomeObservationStatus(text)
    except ValueError as error:
        raise OutcomeArtifactCodecError(f"{label} is not recognized") from error


def _require_json_scalar(value: object, label: str) -> JsonScalar:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    raise OutcomeArtifactCodecError(f"{label} must be a JSON scalar")


def _require_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise OutcomeArtifactCodecError(f"{label} must be numeric")
    return float(value)


def _require_derived_identity(payload: Mapping[str, object], field: str, expected: str) -> None:
    actual = _require_text(payload[field], field)
    if actual != expected:
        raise OutcomeArtifactCodecError(f"{field} does not match artifact content")


def _require_schema(value: object, expected: str, label: str) -> None:
    actual = _require_text(value, f"{label}.schema_version")
    if actual != expected:
        raise OutcomeArtifactCodecError(
            f"unsupported {label} schema {actual!r}; expected {expected!r}"
        )
