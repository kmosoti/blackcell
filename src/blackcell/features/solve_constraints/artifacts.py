from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import cast

from blackcell.features.solve_constraints.models import (
    ConstraintEvaluation,
    ConstraintOutcome,
    ConstraintProof,
)
from blackcell.kernel._json import canonical_json_bytes

CONSTRAINT_EVALUATION_MEDIA_TYPE = "application/vnd.blackcell.constraint-evaluation+json"
CONSTRAINT_EVALUATION_SCHEMA_VERSION = "constraint-evaluation/v1"
CONSTRAINT_PROOF_SCHEMA_VERSION = "constraint-proof/v2"

_EVALUATION_KEYS = frozenset(
    {
        "schema_version",
        "evaluation_id",
        "context_frame_id",
        "proofs",
        "evaluated_at",
    }
)
_PROOF_KEYS = frozenset(
    {
        "schema_version",
        "proof_id",
        "constraint_id",
        "constraint_definition_digest",
        "outcome",
        "code",
        "message",
        "evidence_event_ids",
        "evaluated_at",
    }
)


class ConstraintArtifactCodecError(ValueError):
    """A constraint artifact is malformed or fails its derived identity."""


def constraint_evaluation_payload(evaluation: ConstraintEvaluation) -> dict[str, object]:
    return {
        "schema_version": evaluation.schema_version,
        "evaluation_id": evaluation.evaluation_id,
        "context_frame_id": evaluation.context_frame_id,
        "proofs": [_proof_payload(item) for item in evaluation.proofs],
        "evaluated_at": evaluation.evaluated_at.isoformat(),
    }


def encode_constraint_evaluation(evaluation: ConstraintEvaluation) -> bytes:
    _require_schema(
        evaluation.schema_version,
        CONSTRAINT_EVALUATION_SCHEMA_VERSION,
        "ConstraintEvaluation",
    )
    for index, proof in enumerate(evaluation.proofs):
        _require_schema(
            proof.schema_version,
            CONSTRAINT_PROOF_SCHEMA_VERSION,
            f"proofs[{index}]",
        )
    return canonical_json_bytes(constraint_evaluation_payload(evaluation))


def decode_constraint_evaluation(data: bytes) -> ConstraintEvaluation:
    payload = _decode_object(data, label="ConstraintEvaluation")
    _require_keys(payload, _EVALUATION_KEYS, label="ConstraintEvaluation")
    _require_schema(
        payload["schema_version"],
        CONSTRAINT_EVALUATION_SCHEMA_VERSION,
        "ConstraintEvaluation",
    )
    proofs = tuple(
        _decode_proof(item, index=index)
        for index, item in enumerate(_require_list(payload["proofs"], "proofs"))
    )
    try:
        evaluation = ConstraintEvaluation(
            context_frame_id=_require_text(payload["context_frame_id"], "context_frame_id"),
            proofs=proofs,
            evaluated_at=_require_datetime(payload["evaluated_at"], "evaluated_at"),
            schema_version=_require_text(payload["schema_version"], "schema_version"),
        )
    except ValueError as error:
        raise ConstraintArtifactCodecError(
            "ConstraintEvaluation violates its domain contract"
        ) from error
    _require_derived_identity(payload, "evaluation_id", evaluation.evaluation_id)
    return evaluation


def _proof_payload(proof: ConstraintProof) -> dict[str, object]:
    return {
        "schema_version": proof.schema_version,
        "proof_id": proof.proof_id,
        "constraint_id": proof.constraint_id,
        "constraint_definition_digest": proof.constraint_definition_digest,
        "outcome": proof.outcome.value,
        "code": proof.code,
        "message": proof.message,
        "evidence_event_ids": list(proof.evidence_event_ids),
        "evaluated_at": proof.evaluated_at.isoformat(),
    }


def _decode_proof(value: object, *, index: int) -> ConstraintProof:
    label = f"proofs[{index}]"
    payload = _require_mapping(value, label)
    _require_keys(payload, _PROOF_KEYS, label=label)
    _require_schema(payload["schema_version"], CONSTRAINT_PROOF_SCHEMA_VERSION, label)
    try:
        proof = ConstraintProof(
            constraint_id=_require_text(payload["constraint_id"], f"{label}.constraint_id"),
            constraint_definition_digest=_require_text(
                payload["constraint_definition_digest"],
                f"{label}.constraint_definition_digest",
            ),
            outcome=_require_outcome(payload["outcome"], f"{label}.outcome"),
            code=_require_text(payload["code"], f"{label}.code"),
            message=_require_text(payload["message"], f"{label}.message"),
            evidence_event_ids=_require_text_tuple(
                payload["evidence_event_ids"], f"{label}.evidence_event_ids"
            ),
            evaluated_at=_require_datetime(payload["evaluated_at"], f"{label}.evaluated_at"),
            schema_version=_require_text(payload["schema_version"], f"{label}.schema_version"),
        )
    except ValueError as error:
        raise ConstraintArtifactCodecError(f"{label} violates its contract") from error
    _require_derived_identity(payload, "proof_id", proof.proof_id)
    return proof


def _decode_object(data: bytes, *, label: str) -> dict[str, object]:
    if not isinstance(data, bytes):
        raise TypeError("artifact data must be bytes")
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConstraintArtifactCodecError(f"{label} must be UTF-8 JSON") from error
    if canonical_json_bytes(value) != data:
        raise ConstraintArtifactCodecError(f"{label} must use canonical JSON encoding")
    return _require_mapping(value, label)


def _require_mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ConstraintArtifactCodecError(f"{label} must be a JSON object")
    if any(not isinstance(key, str) for key in value):
        raise ConstraintArtifactCodecError(f"{label} keys must be strings")
    return cast("dict[str, object]", value)


def _require_keys(payload: Mapping[str, object], expected: frozenset[str], *, label: str) -> None:
    actual = frozenset(payload)
    if actual != expected:
        raise ConstraintArtifactCodecError(
            f"{label} fields differ; missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}"
        )


def _require_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConstraintArtifactCodecError(f"{label} must be a non-empty string")
    return value


def _require_list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ConstraintArtifactCodecError(f"{label} must be a JSON array")
    return cast("list[object]", value)


def _require_text_tuple(value: object, label: str) -> tuple[str, ...]:
    return tuple(_require_text(item, f"{label}[]") for item in _require_list(value, label))


def _require_datetime(value: object, label: str) -> datetime:
    text = _require_text(value, label)
    try:
        result = datetime.fromisoformat(text)
    except ValueError as error:
        raise ConstraintArtifactCodecError(f"{label} must be an ISO timestamp") from error
    if result.tzinfo is None or result.utcoffset() is None:
        raise ConstraintArtifactCodecError(f"{label} must be timezone-aware")
    return result


def _require_outcome(value: object, label: str) -> ConstraintOutcome:
    text = _require_text(value, label)
    try:
        return ConstraintOutcome(text)
    except ValueError as error:
        raise ConstraintArtifactCodecError(f"{label} is not recognized") from error


def _require_derived_identity(payload: Mapping[str, object], field: str, expected: str) -> None:
    actual = _require_text(payload[field], field)
    if actual != expected:
        raise ConstraintArtifactCodecError(f"{field} does not match artifact content")


def _require_schema(value: object, expected: str, label: str) -> None:
    actual = _require_text(value, f"{label}.schema_version")
    if actual != expected:
        raise ConstraintArtifactCodecError(
            f"unsupported {label} schema {actual!r}; expected {expected!r}"
        )
