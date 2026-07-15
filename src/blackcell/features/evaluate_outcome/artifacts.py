from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import cast

from blackcell.features.evaluate_outcome.models import (
    EVALUATION_RESULT_SCHEMA_VERSION,
    EVALUATION_SPEC_SCHEMA_VERSION,
    EvaluationAuthorizationOutcome,
    EvaluationCriterion,
    EvaluationExecutionStatus,
    EvaluationFinding,
    EvaluationSpec,
    EvaluationVerdict,
    OutcomeEvaluation,
    _evaluation_identity_payload,
    _spec_identity_payload,
    scalar_values_equal,
)
from blackcell.kernel import JsonScalar
from blackcell.kernel._json import canonical_json_bytes

EVALUATION_SPEC_MEDIA_TYPE = "application/vnd.blackcell.evaluation-spec+json"
OUTCOME_EVALUATION_MEDIA_TYPE = "application/vnd.blackcell.outcome-evaluation+json"

_SPEC_KEYS = frozenset({"schema_version", "spec_id", "name", "objective", "criteria"})
_CRITERION_KEYS = frozenset(
    {
        "criterion_id",
        "subject",
        "predicate",
        "expected_value",
        "minimum_confidence",
        "required",
    }
)
_EVALUATION_KEYS = frozenset(
    {
        "schema_version",
        "evaluation_id",
        "run_id",
        "evaluation_spec_id",
        "authorization_outcome",
        "execution_status",
        "execution_event_id",
        "execution_binding_id",
        "outcome_observation_id",
        "outcome_observation_digest",
        "outcome_evidence_binding_id",
        "initial_state_position",
        "verdict",
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


class EvaluationArtifactCodecError(ValueError):
    """An evaluation artifact is malformed or fails its derived identity."""


def evaluation_spec_payload(spec: EvaluationSpec) -> dict[str, object]:
    return {**_spec_identity_payload(spec), "spec_id": spec.spec_id}


def encode_evaluation_spec(spec: EvaluationSpec) -> bytes:
    if spec.schema_version != EVALUATION_SPEC_SCHEMA_VERSION:
        raise EvaluationArtifactCodecError(
            f"unsupported EvaluationSpec schema {spec.schema_version!r}"
        )
    return canonical_json_bytes(evaluation_spec_payload(spec))


def decode_evaluation_spec(data: bytes) -> EvaluationSpec:
    payload = _decode_object(data, "EvaluationSpec")
    _keys(payload, _SPEC_KEYS, "EvaluationSpec")
    _schema(payload, EVALUATION_SPEC_SCHEMA_VERSION, "EvaluationSpec")
    criteria = tuple(
        _decode_criterion(item, index)
        for index, item in enumerate(_array(payload["criteria"], "criteria"))
    )
    try:
        spec = EvaluationSpec(
            name=_text(payload["name"], "name"),
            objective=_text(payload["objective"], "objective"),
            criteria=criteria,
            schema_version=_text(payload["schema_version"], "schema_version"),
        )
    except (TypeError, ValueError) as error:
        raise EvaluationArtifactCodecError("EvaluationSpec violates its contract") from error
    _identity(payload, "spec_id", spec.spec_id)
    if encode_evaluation_spec(spec) != data:
        raise EvaluationArtifactCodecError(
            "EvaluationSpec collections and values must use canonical domain ordering"
        )
    return spec


def outcome_evaluation_payload(evaluation: OutcomeEvaluation) -> dict[str, object]:
    return {
        **_evaluation_identity_payload(evaluation),
        "evaluation_id": evaluation.evaluation_id,
    }


def encode_outcome_evaluation(evaluation: OutcomeEvaluation) -> bytes:
    if evaluation.schema_version != EVALUATION_RESULT_SCHEMA_VERSION:
        raise EvaluationArtifactCodecError(
            f"unsupported OutcomeEvaluation schema {evaluation.schema_version!r}"
        )
    return canonical_json_bytes(outcome_evaluation_payload(evaluation))


def decode_outcome_evaluation(
    data: bytes,
    *,
    spec: EvaluationSpec,
) -> OutcomeEvaluation:
    payload = _decode_object(data, "OutcomeEvaluation")
    _keys(payload, _EVALUATION_KEYS, "OutcomeEvaluation")
    _schema(payload, EVALUATION_RESULT_SCHEMA_VERSION, "OutcomeEvaluation")
    findings = tuple(
        _decode_finding(item, index)
        for index, item in enumerate(_array(payload["findings"], "findings"))
    )
    try:
        evaluation = OutcomeEvaluation(
            run_id=_text(payload["run_id"], "run_id"),
            evaluation_spec_id=_text(payload["evaluation_spec_id"], "evaluation_spec_id"),
            authorization_outcome=_enum(
                EvaluationAuthorizationOutcome,
                payload["authorization_outcome"],
                "authorization_outcome",
            ),
            execution_status=_optional_enum(
                EvaluationExecutionStatus,
                payload["execution_status"],
                "execution_status",
            ),
            execution_event_id=_optional_text(payload["execution_event_id"], "execution_event_id"),
            execution_binding_id=_optional_text(
                payload["execution_binding_id"], "execution_binding_id"
            ),
            outcome_observation_id=_optional_text(
                payload["outcome_observation_id"], "outcome_observation_id"
            ),
            outcome_observation_digest=_optional_text(
                payload["outcome_observation_digest"], "outcome_observation_digest"
            ),
            outcome_evidence_binding_id=_optional_text(
                payload["outcome_evidence_binding_id"],
                "outcome_evidence_binding_id",
            ),
            initial_state_position=_integer(
                payload["initial_state_position"], "initial_state_position"
            ),
            verdict=_enum(EvaluationVerdict, payload["verdict"], "verdict"),
            findings=findings,
            evaluated_at=_datetime(payload["evaluated_at"], "evaluated_at"),
            schema_version=_text(payload["schema_version"], "schema_version"),
        )
    except (TypeError, ValueError) as error:
        raise EvaluationArtifactCodecError("OutcomeEvaluation violates its contract") from error
    _identity(payload, "evaluation_id", evaluation.evaluation_id)
    _validate_against_spec(evaluation, spec)
    if encode_outcome_evaluation(evaluation) != data:
        raise EvaluationArtifactCodecError(
            "OutcomeEvaluation collections and values must use canonical domain ordering"
        )
    return evaluation


def _decode_criterion(value: object, index: int) -> EvaluationCriterion:
    label = f"criteria[{index}]"
    payload = _mapping(value, label)
    _keys(payload, _CRITERION_KEYS, label)
    try:
        return EvaluationCriterion(
            criterion_id=_text(payload["criterion_id"], f"{label}.criterion_id"),
            subject=_text(payload["subject"], f"{label}.subject"),
            predicate=_text(payload["predicate"], f"{label}.predicate"),
            expected_value=_scalar(payload["expected_value"], f"{label}.expected_value"),
            minimum_confidence=_number(
                payload["minimum_confidence"], f"{label}.minimum_confidence"
            ),
            required=_boolean(payload["required"], f"{label}.required"),
        )
    except (TypeError, ValueError) as error:
        raise EvaluationArtifactCodecError(f"{label} violates its contract") from error


def _decode_finding(value: object, index: int) -> EvaluationFinding:
    label = f"findings[{index}]"
    payload = _mapping(value, label)
    _keys(payload, _FINDING_KEYS, label)
    try:
        return EvaluationFinding(
            criterion_id=_text(payload["criterion_id"], f"{label}.criterion_id"),
            required=_boolean(payload["required"], f"{label}.required"),
            verdict=_enum(EvaluationVerdict, payload["verdict"], f"{label}.verdict"),
            code=_text(payload["code"], f"{label}.code"),
            expected_value=_scalar(payload["expected_value"], f"{label}.expected_value"),
            actual_present=_boolean(payload["actual_present"], f"{label}.actual_present"),
            actual_value=_scalar(payload["actual_value"], f"{label}.actual_value"),
            actual_confidence=_optional_number(
                payload["actual_confidence"], f"{label}.actual_confidence"
            ),
            observed_claim_ids=_string_tuple(
                payload["observed_claim_ids"], f"{label}.observed_claim_ids"
            ),
            source_event_ids=_string_tuple(
                payload["source_event_ids"], f"{label}.source_event_ids"
            ),
        )
    except (TypeError, ValueError) as error:
        raise EvaluationArtifactCodecError(f"{label} violates its contract") from error


def _validate_against_spec(evaluation: OutcomeEvaluation, spec: EvaluationSpec) -> None:
    if evaluation.evaluation_spec_id != spec.spec_id:
        raise EvaluationArtifactCodecError(
            "OutcomeEvaluation belongs to a different EvaluationSpec"
        )
    expected = {item.criterion_id: item for item in spec.criteria}
    actual = {item.criterion_id: item for item in evaluation.findings}
    if actual.keys() != expected.keys():
        raise EvaluationArtifactCodecError(
            "OutcomeEvaluation findings do not match EvaluationSpec criteria"
        )
    for criterion_id, finding in actual.items():
        criterion = expected[criterion_id]
        if finding.required is not criterion.required or not scalar_values_equal(
            finding.expected_value, criterion.expected_value
        ):
            raise EvaluationArtifactCodecError(
                "OutcomeEvaluation finding policy does not match EvaluationSpec"
            )
        if finding.verdict in {EvaluationVerdict.PASS, EvaluationVerdict.FAIL} and (
            finding.actual_confidence is None
            or finding.actual_confidence < criterion.minimum_confidence
        ):
            raise EvaluationArtifactCodecError(
                "OutcomeEvaluation definitive finding is below the required confidence"
            )
        if (
            finding.code == "outcome-confidence-below-threshold"
            and finding.actual_confidence is not None
            and finding.actual_confidence >= criterion.minimum_confidence
        ):
            raise EvaluationArtifactCodecError(
                "OutcomeEvaluation low-confidence finding meets the required threshold"
            )


def _decode_object(data: bytes, label: str) -> dict[str, object]:
    if not isinstance(data, bytes):
        raise TypeError("artifact data must be bytes")
    try:
        value = json.loads(data.decode("utf-8"))
        encoded = canonical_json_bytes(value)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise EvaluationArtifactCodecError(f"{label} must be UTF-8 canonical JSON") from error
    if encoded != data:
        raise EvaluationArtifactCodecError(f"{label} must use canonical JSON encoding")
    return _mapping(value, label)


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise EvaluationArtifactCodecError(f"{label} must be a JSON object")
    return cast("dict[str, object]", value)


def _array(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise EvaluationArtifactCodecError(f"{label} must be a JSON array")
    return cast("list[object]", value)


def _keys(payload: Mapping[str, object], expected: frozenset[str], label: str) -> None:
    actual = frozenset(payload)
    if actual != expected:
        raise EvaluationArtifactCodecError(
            f"{label} fields differ; missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}"
        )


def _schema(payload: Mapping[str, object], expected: str, label: str) -> None:
    actual = _text(payload["schema_version"], f"{label}.schema_version")
    if actual != expected:
        raise EvaluationArtifactCodecError(
            f"unsupported {label} schema {actual!r}; expected {expected!r}"
        )


def _identity(payload: Mapping[str, object], key: str, expected: str) -> None:
    if _text(payload[key], key) != expected:
        raise EvaluationArtifactCodecError(f"{key} does not match artifact content")


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvaluationArtifactCodecError(f"{label} must be a non-empty string")
    return value


def _optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _text(value, label)


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise EvaluationArtifactCodecError(f"{label} must be a boolean")
    return value


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EvaluationArtifactCodecError(f"{label} must be an integer")
    return value


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise EvaluationArtifactCodecError(f"{label} must be numeric")
    return float(value)


def _optional_number(value: object, label: str) -> float | None:
    if value is None:
        return None
    return _number(value, label)


def _scalar(value: object, label: str) -> JsonScalar:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    raise EvaluationArtifactCodecError(f"{label} must be a JSON scalar")


def _string_tuple(value: object, label: str) -> tuple[str, ...]:
    return tuple(
        _text(item, f"{label}[{index}]") for index, item in enumerate(_array(value, label))
    )


def _datetime(value: object, label: str) -> datetime:
    try:
        result = datetime.fromisoformat(_text(value, label))
    except ValueError as error:
        raise EvaluationArtifactCodecError(f"{label} must be an ISO timestamp") from error
    if result.tzinfo is None or result.utcoffset() is None:
        raise EvaluationArtifactCodecError(f"{label} must be timezone-aware")
    return result


def _enum[EnumT: (EvaluationAuthorizationOutcome, EvaluationExecutionStatus, EvaluationVerdict)](
    enum_type: type[EnumT], value: object, label: str
) -> EnumT:
    try:
        return enum_type(_text(value, label))
    except ValueError as error:
        raise EvaluationArtifactCodecError(f"{label} is not recognized") from error


def _optional_enum[
    EnumT: (EvaluationAuthorizationOutcome, EvaluationExecutionStatus, EvaluationVerdict)
](
    enum_type: type[EnumT],
    value: object,
    label: str,
) -> EnumT | None:
    if value is None:
        return None
    return _enum(enum_type, value, label)


__all__ = [
    "EVALUATION_SPEC_MEDIA_TYPE",
    "OUTCOME_EVALUATION_MEDIA_TYPE",
    "EvaluationArtifactCodecError",
    "decode_evaluation_spec",
    "decode_outcome_evaluation",
    "encode_evaluation_spec",
    "encode_outcome_evaluation",
    "evaluation_spec_payload",
    "outcome_evaluation_payload",
]
