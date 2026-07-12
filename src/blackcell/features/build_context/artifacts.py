from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import cast

from blackcell.features.build_context.models import (
    ContextClaimIdentity,
    ContextEpistemicStatus,
    ContextEvidence,
    ContextFrame,
    ContextOmission,
    ContextOmissionReason,
    ContextOmissionStage,
    ContextUnknownReason,
    serialize_context_frame,
)
from blackcell.features.build_context.storage import (
    ContextFrameIntegrityError,
    ContextFrameSchemaError,
)
from blackcell.kernel import JsonScalar
from blackcell.kernel._json import bytes_digest, canonical_json_bytes

CONTEXT_FRAME_MEDIA_TYPE = "application/vnd.blackcell.context-frame+json"
CONTEXT_FRAME_SCHEMA_VERSION_V3 = "context-frame/v3"
CONTEXT_FRAME_SCHEMA_VERSION_V4 = "context-frame/v4"
CONTEXT_FRAME_SCHEMA_VERSIONS = frozenset(
    {CONTEXT_FRAME_SCHEMA_VERSION_V3, CONTEXT_FRAME_SCHEMA_VERSION_V4}
)
CONTEXT_OMISSION_SCHEMA_VERSION_V2 = "context-omission/v2"
CONTEXT_OMISSION_SCHEMA_VERSION_V3 = "context-omission/v3"
CONTEXT_OMISSION_SCHEMA_VERSIONS = frozenset(
    {CONTEXT_OMISSION_SCHEMA_VERSION_V2, CONTEXT_OMISSION_SCHEMA_VERSION_V3}
)

_FRAME_SCHEMA_VERSIONS = CONTEXT_FRAME_SCHEMA_VERSIONS
_OMISSION_SCHEMA_VERSIONS = CONTEXT_OMISSION_SCHEMA_VERSIONS
_FRAME_V3_KEYS = frozenset(
    {
        "schema_version",
        "task_id",
        "objective",
        "generated_at",
        "source_packet_id",
        "source_packet_purpose",
        "source_selection_id",
        "state_domain",
        "state_stream_id",
        "state_global_position",
        "state_stream_position",
        "source_claim_identities",
        "evidence",
        "provenance_event_ids",
        "omissions",
        "model_payload_characters",
    }
)
_FRAME_KEYS_BY_SCHEMA = {
    "context-frame/v3": _FRAME_V3_KEYS,
    "context-frame/v4": _FRAME_V3_KEYS | {"state_effective_time"},
}
_CLAIM_IDENTITY_KEYS = frozenset({"source_event_id", "claim_id"})
_EVIDENCE_V3_KEYS = frozenset(
    {
        "claim_id",
        "subject",
        "predicate",
        "value",
        "confidence",
        "effective_at",
        "freshness_seconds",
        "stale",
        "source_event_id",
        "domain",
        "stream_id",
        "stream_sequence",
        "global_position",
        "relevance_score",
        "selection_reasons",
        "conflicted",
    }
)
_EVIDENCE_KEYS_BY_FRAME_SCHEMA = {
    "context-frame/v3": _EVIDENCE_V3_KEYS,
    "context-frame/v4": _EVIDENCE_V3_KEYS | {"epistemic_status", "unknown_reason", "expires_at"},
}
_OMISSION_V2_KEYS = frozenset(
    {
        "schema_version",
        "claim_id",
        "subject",
        "predicate",
        "value",
        "confidence",
        "effective_at",
        "freshness_seconds",
        "stale",
        "source_event_id",
        "domain",
        "stream_id",
        "stream_sequence",
        "global_position",
        "relevance_score",
        "selection_reasons",
        "conflicted",
        "stage",
        "reason",
        "model_payload_characters",
        "source_omission_id",
        "source_omission_schema_version",
    }
)
_OMISSION_KEYS_BY_SCHEMA = {
    "context-omission/v2": _OMISSION_V2_KEYS,
    "context-omission/v3": _OMISSION_V2_KEYS | {"epistemic_status", "unknown_reason", "expires_at"},
}


def _decode_frame(value: object, *, expected_frame_id: str) -> ContextFrame:
    if not isinstance(value, dict):
        raise ContextFrameIntegrityError("ContextFrame must be a JSON object")
    schema_version = _require_string(value.get("schema_version"), label="schema_version")
    if schema_version not in _FRAME_SCHEMA_VERSIONS:
        raise ContextFrameSchemaError(
            f"unsupported ContextFrame schema {schema_version!r}; "
            f"expected one of {sorted(_FRAME_SCHEMA_VERSIONS)!r}"
        )
    payload = _require_mapping(
        value,
        keys=_FRAME_KEYS_BY_SCHEMA[schema_version],
        label="ContextFrame",
    )
    generated_at = _require_datetime(payload["generated_at"], label="generated_at")
    state_stream_id = _require_optional_string(payload["state_stream_id"], label="state_stream_id")
    state_effective_time = (
        _require_optional_datetime(
            payload["state_effective_time"],
            label="state_effective_time",
        )
        if schema_version == "context-frame/v4"
        else None
    )
    raw_identities = payload["source_claim_identities"]
    if not isinstance(raw_identities, list):
        raise ContextFrameIntegrityError("source_claim_identities must be a JSON array")
    identities = tuple(
        _decode_claim_identity(item, index=index) for index, item in enumerate(raw_identities)
    )
    raw_evidence = payload["evidence"]
    if not isinstance(raw_evidence, list):
        raise ContextFrameIntegrityError("ContextFrame evidence must be a JSON array")
    evidence = tuple(
        _decode_evidence(item, index=index, frame_schema_version=schema_version)
        for index, item in enumerate(raw_evidence)
    )
    raw_omissions = payload["omissions"]
    if not isinstance(raw_omissions, list):
        raise ContextFrameIntegrityError("ContextFrame omissions must be a JSON array")
    omissions = tuple(
        _decode_omission(item, index=index) for index, item in enumerate(raw_omissions)
    )
    provenance_event_ids = _require_string_tuple(
        payload["provenance_event_ids"], label="provenance_event_ids"
    )
    try:
        frame = ContextFrame(
            task_id=_require_string(payload["task_id"], label="task_id"),
            objective=_require_string(payload["objective"], label="objective"),
            generated_at=generated_at,
            source_packet_id=_require_string(payload["source_packet_id"], label="source_packet_id"),
            source_packet_purpose=_require_string(
                payload["source_packet_purpose"], label="source_packet_purpose"
            ),
            source_selection_id=_require_string(
                payload["source_selection_id"], label="source_selection_id"
            ),
            state_domain=_require_string(payload["state_domain"], label="state_domain"),
            state_stream_id=state_stream_id,
            state_global_position=_require_non_negative_int(
                payload["state_global_position"], label="state_global_position"
            ),
            state_stream_position=_require_non_negative_int(
                payload["state_stream_position"], label="state_stream_position"
            ),
            source_claim_identities=identities,
            evidence=evidence,
            provenance_event_ids=provenance_event_ids,
            omissions=omissions,
            model_payload_characters=_require_non_negative_int(
                payload["model_payload_characters"], label="model_payload_characters"
            ),
            schema_version=schema_version,
            state_effective_time=state_effective_time,
        )
    except ValueError as error:
        raise ContextFrameIntegrityError("payload violates the ContextFrame contract") from error
    if frame.frame_id != expected_frame_id:
        raise ContextFrameIntegrityError(
            f"ContextFrame digest mismatch: expected {expected_frame_id!r}, got {frame.frame_id!r}"
        )
    return frame


def _decode_claim_identity(value: object, *, index: int) -> ContextClaimIdentity:
    label = f"source_claim_identities[{index}]"
    payload = _require_mapping(value, keys=_CLAIM_IDENTITY_KEYS, label=label)
    try:
        return ContextClaimIdentity(
            source_event_id=_require_string(
                payload["source_event_id"], label=f"{label} source_event_id"
            ),
            claim_id=_require_string(payload["claim_id"], label=f"{label} claim_id"),
        )
    except ValueError as error:
        raise ContextFrameIntegrityError(f"{label} violates its contract") from error


def _decode_evidence(
    value: object,
    *,
    index: int,
    frame_schema_version: str,
) -> ContextEvidence:
    label = f"ContextFrame evidence[{index}]"
    payload = _require_mapping(
        value,
        keys=_EVIDENCE_KEYS_BY_FRAME_SCHEMA[frame_schema_version],
        label=label,
    )
    confidence = _require_confidence(payload["confidence"], label=f"{label} confidence")
    try:
        return ContextEvidence(
            claim_id=_require_string(payload["claim_id"], label=f"{label} claim_id"),
            subject=_require_string(payload["subject"], label=f"{label} subject"),
            predicate=_require_string(payload["predicate"], label=f"{label} predicate"),
            value=_require_json_scalar(payload["value"], label=f"{label} value"),
            confidence=confidence,
            effective_at=_require_datetime(payload["effective_at"], label=f"{label} effective_at"),
            freshness_seconds=_require_non_negative_int(
                payload["freshness_seconds"], label=f"{label} freshness_seconds"
            ),
            stale=_require_bool(payload["stale"], label=f"{label} stale"),
            source_event_id=_require_string(
                payload["source_event_id"], label=f"{label} source_event_id"
            ),
            domain=_require_string(payload["domain"], label=f"{label} domain"),
            stream_id=_require_string(payload["stream_id"], label=f"{label} stream_id"),
            stream_sequence=_require_positive_int(
                payload["stream_sequence"], label=f"{label} stream_sequence"
            ),
            global_position=_require_positive_int(
                payload["global_position"], label=f"{label} global_position"
            ),
            relevance_score=_require_int(
                payload["relevance_score"], label=f"{label} relevance_score"
            ),
            selection_reasons=_require_string_tuple(
                payload["selection_reasons"], label=f"{label} selection_reasons"
            ),
            conflicted=_require_bool(payload["conflicted"], label=f"{label} conflicted"),
            epistemic_status=(
                _require_enum(
                    ContextEpistemicStatus,
                    payload["epistemic_status"],
                    label=f"{label} epistemic_status",
                )
                if frame_schema_version == "context-frame/v4"
                else ContextEpistemicStatus.OBSERVED
            ),
            unknown_reason=(
                _require_optional_enum(
                    ContextUnknownReason,
                    payload["unknown_reason"],
                    label=f"{label} unknown_reason",
                )
                if frame_schema_version == "context-frame/v4"
                else None
            ),
            expires_at=(
                _require_optional_datetime(
                    payload["expires_at"],
                    label=f"{label} expires_at",
                )
                if frame_schema_version == "context-frame/v4"
                else None
            ),
        )
    except (TypeError, ValueError) as error:
        raise ContextFrameIntegrityError(f"{label} violates its contract") from error


def _decode_omission(value: object, *, index: int) -> ContextOmission:
    label = f"ContextFrame omission[{index}]"
    if not isinstance(value, dict):
        raise ContextFrameIntegrityError(f"{label} must be a JSON object")
    schema_version = _require_string(
        value.get("schema_version"),
        label=f"{label} schema_version",
    )
    if schema_version not in _OMISSION_SCHEMA_VERSIONS:
        raise ContextFrameSchemaError(
            f"unsupported ContextOmission schema {schema_version!r}; "
            f"expected one of {sorted(_OMISSION_SCHEMA_VERSIONS)!r}"
        )
    payload = _require_mapping(
        value,
        keys=_OMISSION_KEYS_BY_SCHEMA[schema_version],
        label=label,
    )
    try:
        return ContextOmission(
            claim_id=_require_string(payload["claim_id"], label=f"{label} claim_id"),
            subject=_require_string(payload["subject"], label=f"{label} subject"),
            predicate=_require_string(payload["predicate"], label=f"{label} predicate"),
            value=_require_json_scalar(payload["value"], label=f"{label} value"),
            confidence=_require_confidence(payload["confidence"], label=f"{label} confidence"),
            effective_at=_require_datetime(payload["effective_at"], label=f"{label} effective_at"),
            freshness_seconds=_require_non_negative_int(
                payload["freshness_seconds"], label=f"{label} freshness_seconds"
            ),
            stale=_require_bool(payload["stale"], label=f"{label} stale"),
            source_event_id=_require_string(
                payload["source_event_id"], label=f"{label} source_event_id"
            ),
            domain=_require_string(payload["domain"], label=f"{label} domain"),
            stream_id=_require_string(payload["stream_id"], label=f"{label} stream_id"),
            stream_sequence=_require_positive_int(
                payload["stream_sequence"], label=f"{label} stream_sequence"
            ),
            global_position=_require_positive_int(
                payload["global_position"], label=f"{label} global_position"
            ),
            relevance_score=_require_int(
                payload["relevance_score"], label=f"{label} relevance_score"
            ),
            selection_reasons=_require_string_tuple(
                payload["selection_reasons"], label=f"{label} selection_reasons"
            ),
            conflicted=_require_bool(payload["conflicted"], label=f"{label} conflicted"),
            stage=_require_enum(ContextOmissionStage, payload["stage"], label=f"{label} stage"),
            reason=_require_enum(ContextOmissionReason, payload["reason"], label=f"{label} reason"),
            model_payload_characters=_require_optional_int(
                payload["model_payload_characters"],
                label=f"{label} model_payload_characters",
            ),
            source_omission_id=_require_optional_string(
                payload["source_omission_id"], label=f"{label} source_omission_id"
            ),
            source_omission_schema_version=_require_optional_string(
                payload["source_omission_schema_version"],
                label=f"{label} source_omission_schema_version",
            ),
            schema_version=schema_version,
            epistemic_status=(
                _require_enum(
                    ContextEpistemicStatus,
                    payload["epistemic_status"],
                    label=f"{label} epistemic_status",
                )
                if schema_version == "context-omission/v3"
                else ContextEpistemicStatus.OBSERVED
            ),
            unknown_reason=(
                _require_optional_enum(
                    ContextUnknownReason,
                    payload["unknown_reason"],
                    label=f"{label} unknown_reason",
                )
                if schema_version == "context-omission/v3"
                else None
            ),
            expires_at=(
                _require_optional_datetime(
                    payload["expires_at"],
                    label=f"{label} expires_at",
                )
                if schema_version == "context-omission/v3"
                else None
            ),
        )
    except ValueError as error:
        raise ContextFrameIntegrityError(
            f"{label} violates the ContextOmission contract"
        ) from error


def _require_mapping(
    value: object,
    *,
    keys: frozenset[str],
    label: str,
) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ContextFrameIntegrityError(f"{label} must be a JSON object")
    actual = frozenset(value)
    if actual != keys:
        missing = sorted(keys - actual)
        unexpected = sorted(actual - keys)
        raise ContextFrameIntegrityError(
            f"{label} fields do not match its schema; missing={missing}, unexpected={unexpected}"
        )
    return cast("Mapping[str, object]", value)


def _require_string(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContextFrameIntegrityError(f"{label} must be a non-empty string")
    return value


def _require_optional_string(value: object, *, label: str) -> str | None:
    if value is None:
        return None
    return _require_string(value, label=label)


def _require_string_tuple(value: object, *, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ContextFrameIntegrityError(f"{label} must be a JSON array")
    return tuple(_require_string(item, label=f"{label} item") for item in value)


def _require_datetime(value: object, *, label: str) -> datetime:
    text = _require_string(value, label=label)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise ContextFrameIntegrityError(f"{label} must be an ISO 8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContextFrameIntegrityError(f"{label} must be timezone-aware")
    if parsed.isoformat() != text:
        raise ContextFrameIntegrityError(f"{label} must use canonical ISO 8601 formatting")
    return parsed


def _require_optional_datetime(value: object, *, label: str) -> datetime | None:
    if value is None:
        return None
    return _require_datetime(value, label=label)


def _require_int(value: object, *, label: str) -> int:
    if type(value) is not int:
        raise ContextFrameIntegrityError(f"{label} must be an integer")
    return value


def _require_non_negative_int(value: object, *, label: str) -> int:
    parsed = _require_int(value, label=label)
    if parsed < 0:
        raise ContextFrameIntegrityError(f"{label} must be non-negative")
    return parsed


def _require_positive_int(value: object, *, label: str) -> int:
    parsed = _require_int(value, label=label)
    if parsed < 1:
        raise ContextFrameIntegrityError(f"{label} must be positive")
    return parsed


def _require_optional_int(value: object, *, label: str) -> int | None:
    if value is None:
        return None
    return _require_int(value, label=label)


def _require_bool(value: object, *, label: str) -> bool:
    if type(value) is not bool:
        raise ContextFrameIntegrityError(f"{label} must be a boolean")
    return value


def _require_confidence(value: object, *, label: str) -> float:
    if type(value) is not float or not 0.0 <= value <= 1.0:
        raise ContextFrameIntegrityError(f"{label} must be a float from zero to one")
    return value


def _require_enum[EnumT](enum_type: type[EnumT], value: object, *, label: str) -> EnumT:
    text = _require_string(value, label=label)
    try:
        return enum_type(text)
    except (TypeError, ValueError) as error:
        raise ContextFrameIntegrityError(f"{label} is not recognized") from error


def _require_optional_enum[EnumT](
    enum_type: type[EnumT],
    value: object,
    *,
    label: str,
) -> EnumT | None:
    if value is None:
        return None
    return _require_enum(enum_type, value, label=label)


def _require_json_scalar(value: object, *, label: str) -> JsonScalar:
    if value is None or type(value) in (bool, int, float, str):
        try:
            canonical_json_bytes({"value": value})
        except (TypeError, ValueError) as error:
            raise ContextFrameIntegrityError(f"{label} must be a finite JSON scalar") from error
        return cast("JsonScalar", value)
    raise ContextFrameIntegrityError(f"{label} must be a JSON scalar")


def encode_context_frame(frame: ContextFrame) -> bytes:
    """Encode the complete canonical owner artifact for one ContextFrame."""

    if not isinstance(frame, ContextFrame):
        raise TypeError("frame must be a ContextFrame")
    return serialize_context_frame(frame).encode("utf-8")


def decode_context_frame(
    data: bytes,
    *,
    expected_frame_id: str | None = None,
) -> ContextFrame:
    """Strictly decode and verify a feature-owned ContextFrame artifact."""

    if not isinstance(data, bytes):
        raise TypeError("ContextFrame artifact data must be bytes")
    try:
        payload = json.loads(data.decode("utf-8"))
        canonical = canonical_json_bytes(payload)
    except (TypeError, ValueError, UnicodeDecodeError) as error:
        raise ContextFrameIntegrityError("ContextFrame artifact is not canonical JSON") from error
    if canonical != data:
        raise ContextFrameIntegrityError("ContextFrame artifact is not canonical JSON")
    actual_frame_id = bytes_digest(data)
    if expected_frame_id is not None and expected_frame_id != actual_frame_id:
        raise ContextFrameIntegrityError(
            f"ContextFrame digest mismatch: expected {expected_frame_id!r}, got {actual_frame_id!r}"
        )
    return _decode_frame(payload, expected_frame_id=actual_frame_id)


__all__ = [
    "CONTEXT_FRAME_MEDIA_TYPE",
    "CONTEXT_FRAME_SCHEMA_VERSIONS",
    "CONTEXT_FRAME_SCHEMA_VERSION_V3",
    "CONTEXT_FRAME_SCHEMA_VERSION_V4",
    "CONTEXT_OMISSION_SCHEMA_VERSIONS",
    "CONTEXT_OMISSION_SCHEMA_VERSION_V2",
    "CONTEXT_OMISSION_SCHEMA_VERSION_V3",
    "decode_context_frame",
    "encode_context_frame",
]
