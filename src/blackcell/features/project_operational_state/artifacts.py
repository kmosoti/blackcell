from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import cast

from blackcell.features.project_operational_state.models import (
    BeliefClaim,
    BeliefConflict,
    BeliefCorrection,
    EpistemicStatus,
    OperationalBeliefState,
    OperationalStateScope,
    UnknownReason,
)
from blackcell.kernel import JsonScalar
from blackcell.kernel._json import bytes_digest, canonical_json_bytes

OPERATIONAL_STATE_SNAPSHOT_SCHEMA_VERSION = "operational-state-snapshot/v1"
OPERATIONAL_STATE_SNAPSHOT_MEDIA_TYPE = "application/vnd.blackcell.operational-state-snapshot+json"

_SNAPSHOT_KEYS = frozenset(
    {
        "schema_version",
        "scope",
        "cutoff_global_position",
        "last_source_stream_sequence",
        "effective_time_cutoff",
        "claims",
        "conflicts",
        "correction_replacement_claims",
        "superseded_claims",
        "applied_corrections",
        "expired_claims",
    }
)
_SCOPE_KEYS = frozenset({"domain", "stream_id"})
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
_CONFLICT_KEYS = frozenset({"subject", "predicate", "source_event_ids", "claim_ids", "values"})
_CORRECTION_KEYS = frozenset(
    {
        "correction_id",
        "supersedes_claim_ids",
        "replacement_claim_id",
        "reason",
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
    }
)


class OperationalStateArtifactCodecError(ValueError):
    """An operational-state snapshot is malformed or fails content identity."""


def operational_state_snapshot_payload(state: OperationalBeliefState) -> dict[str, object]:
    """Return the complete canonical identity payload for one materialized state."""

    if not isinstance(state, OperationalBeliefState):
        raise TypeError("state must be an OperationalBeliefState")
    _validate_snapshot_semantics(state)
    claims = tuple(sorted(state.claims, key=_current_claim_order))
    conflicts = tuple(sorted(state.conflicts, key=_conflict_order))
    superseded = tuple(sorted(state.superseded_claims, key=_claim_order))
    corrections = tuple(sorted(state.applied_corrections, key=_correction_order))
    replacements_by_correction_id = {
        claim.correction_id: claim for claim in state.correction_replacement_claims
    }
    correction_replacements = tuple(
        replacements_by_correction_id[correction.correction_id] for correction in corrections
    )
    expired_by_id = {claim.claim_id: claim for claim in state.expired_claims}
    expired = tuple(
        expired_by_id[claim.claim_id]
        for claim in claims
        if claim.epistemic_status is EpistemicStatus.UNKNOWN
    )
    return {
        "schema_version": OPERATIONAL_STATE_SNAPSHOT_SCHEMA_VERSION,
        "scope": {"domain": state.scope.domain, "stream_id": state.scope.stream_id},
        "cutoff_global_position": state.cutoff_global_position,
        "last_source_stream_sequence": state.last_source_stream_sequence,
        "effective_time_cutoff": (
            _timestamp(state.effective_time_cutoff)
            if state.effective_time_cutoff is not None
            else None
        ),
        "claims": [_claim_payload(claim) for claim in claims],
        "conflicts": [_conflict_payload(conflict) for conflict in conflicts],
        "correction_replacement_claims": [
            _claim_payload(claim) for claim in correction_replacements
        ],
        "superseded_claims": [_claim_payload(claim) for claim in superseded],
        "applied_corrections": [_correction_payload(correction) for correction in corrections],
        "expired_claims": [_claim_payload(claim) for claim in expired],
    }


def encode_operational_state_snapshot(state: OperationalBeliefState) -> bytes:
    return canonical_json_bytes(operational_state_snapshot_payload(state))


def operational_state_snapshot_digest(state: OperationalBeliefState) -> str:
    """Return the kernel content address of the exact snapshot bytes."""

    return bytes_digest(encode_operational_state_snapshot(state))


def decode_operational_state_snapshot(
    data: bytes,
    *,
    expected_snapshot_digest: str | None = None,
) -> OperationalBeliefState:
    payload = _decode_object(data, "operational-state snapshot")
    _keys(payload, _SNAPSHOT_KEYS, "operational-state snapshot")
    schema = _text(payload["schema_version"], "schema_version")
    if schema != OPERATIONAL_STATE_SNAPSHOT_SCHEMA_VERSION:
        raise OperationalStateArtifactCodecError(
            f"unsupported operational-state snapshot schema {schema!r}"
        )
    scope = _decode_scope(payload["scope"])
    try:
        state = OperationalBeliefState(
            scope=scope,
            claims=_decode_claims(payload["claims"], "claims"),
            conflicts=_decode_conflicts(payload["conflicts"]),
            cutoff_global_position=_integer(
                payload["cutoff_global_position"], "cutoff_global_position"
            ),
            last_source_stream_sequence=_integer(
                payload["last_source_stream_sequence"],
                "last_source_stream_sequence",
            ),
            superseded_claims=_decode_claims(payload["superseded_claims"], "superseded_claims"),
            applied_corrections=_decode_corrections(payload["applied_corrections"]),
            effective_time_cutoff=_optional_datetime(
                payload["effective_time_cutoff"], "effective_time_cutoff"
            ),
            expired_claims=_decode_claims(payload["expired_claims"], "expired_claims"),
            correction_replacement_claims=_decode_claims(
                payload["correction_replacement_claims"],
                "correction_replacement_claims",
            ),
        )
        _validate_snapshot_semantics(state)
    except OperationalStateArtifactCodecError:
        raise
    except (TypeError, ValueError) as error:
        raise OperationalStateArtifactCodecError(
            "operational-state snapshot violates its domain contract"
        ) from error

    if encode_operational_state_snapshot(state) != data:
        raise OperationalStateArtifactCodecError(
            "operational-state snapshot collections and timestamps must use canonical ordering"
        )
    actual_digest = bytes_digest(data)
    if expected_snapshot_digest is not None and expected_snapshot_digest != actual_digest:
        raise OperationalStateArtifactCodecError(
            "operational-state snapshot digest does not match its canonical content"
        )
    return state


def _claim_payload(claim: BeliefClaim) -> dict[str, object]:
    return {
        "claim_id": claim.claim_id,
        "subject": claim.subject,
        "predicate": claim.predicate,
        "value": claim.value,
        "confidence": claim.confidence,
        "effective_at": _timestamp(claim.effective_at),
        "recorded_at": _timestamp(claim.recorded_at),
        "source_event_id": claim.source_event_id,
        "source": claim.source,
        "actor": claim.actor,
        "correlation_id": claim.correlation_id,
        "domain": claim.domain,
        "stream_id": claim.stream_id,
        "stream_sequence": claim.stream_sequence,
        "global_position": claim.global_position,
        "correction_id": claim.correction_id,
        "supersedes_claim_ids": list(claim.supersedes_claim_ids),
        "expires_at": _timestamp(claim.expires_at) if claim.expires_at is not None else None,
        "epistemic_status": claim.epistemic_status.value,
        "unknown_reason": claim.unknown_reason.value if claim.unknown_reason is not None else None,
    }


def _conflict_payload(conflict: BeliefConflict) -> dict[str, object]:
    return {
        "subject": conflict.subject,
        "predicate": conflict.predicate,
        "source_event_ids": list(conflict.source_event_ids),
        "claim_ids": list(conflict.claim_ids),
        "values": list(conflict.values),
    }


def _correction_payload(correction: BeliefCorrection) -> dict[str, object]:
    return {
        "correction_id": correction.correction_id,
        "supersedes_claim_ids": list(correction.supersedes_claim_ids),
        "replacement_claim_id": correction.replacement_claim_id,
        "reason": correction.reason,
        "effective_at": _timestamp(correction.effective_at),
        "recorded_at": _timestamp(correction.recorded_at),
        "source_event_id": correction.source_event_id,
        "source": correction.source,
        "actor": correction.actor,
        "correlation_id": correction.correlation_id,
        "domain": correction.domain,
        "stream_id": correction.stream_id,
        "stream_sequence": correction.stream_sequence,
        "global_position": correction.global_position,
    }


def _decode_scope(value: object) -> OperationalStateScope:
    payload = _mapping(value, "scope")
    _keys(payload, _SCOPE_KEYS, "scope")
    try:
        return OperationalStateScope(
            _text(payload["domain"], "scope.domain"),
            _optional_text(payload["stream_id"], "scope.stream_id"),
        )
    except ValueError as error:
        raise OperationalStateArtifactCodecError("scope violates its contract") from error


def _decode_claims(value: object, label: str) -> tuple[BeliefClaim, ...]:
    return tuple(
        _decode_claim(item, f"{label}[{index}]") for index, item in enumerate(_array(value, label))
    )


def _decode_claim(value: object, label: str) -> BeliefClaim:
    payload = _mapping(value, label)
    _keys(payload, _CLAIM_KEYS, label)
    try:
        return BeliefClaim(
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
            supersedes_claim_ids=_string_tuple(
                payload["supersedes_claim_ids"], f"{label}.supersedes_claim_ids"
            ),
            expires_at=_optional_datetime(payload["expires_at"], f"{label}.expires_at"),
            epistemic_status=_enum(
                EpistemicStatus,
                payload["epistemic_status"],
                f"{label}.epistemic_status",
            ),
            unknown_reason=_optional_enum(
                UnknownReason,
                payload["unknown_reason"],
                f"{label}.unknown_reason",
            ),
        )
    except (TypeError, ValueError) as error:
        raise OperationalStateArtifactCodecError(f"{label} violates its contract") from error


def _decode_conflicts(value: object) -> tuple[BeliefConflict, ...]:
    conflicts: list[BeliefConflict] = []
    for index, item in enumerate(_array(value, "conflicts")):
        label = f"conflicts[{index}]"
        payload = _mapping(item, label)
        _keys(payload, _CONFLICT_KEYS, label)
        source_event_ids = _string_tuple(payload["source_event_ids"], f"{label}.source_event_ids")
        claim_ids = _string_tuple(payload["claim_ids"], f"{label}.claim_ids")
        values = tuple(
            _scalar(raw, f"{label}.values[{value_index}]")
            for value_index, raw in enumerate(_array(payload["values"], f"{label}.values"))
        )
        if not claim_ids or not (len(source_event_ids) == len(claim_ids) == len(values)):
            raise OperationalStateArtifactCodecError(
                f"{label} provenance arrays must be non-empty and aligned"
            )
        conflicts.append(
            BeliefConflict(
                subject=_text(payload["subject"], f"{label}.subject"),
                predicate=_text(payload["predicate"], f"{label}.predicate"),
                source_event_ids=source_event_ids,
                claim_ids=claim_ids,
                values=values,
            )
        )
    return tuple(conflicts)


def _decode_corrections(value: object) -> tuple[BeliefCorrection, ...]:
    corrections: list[BeliefCorrection] = []
    for index, item in enumerate(_array(value, "applied_corrections")):
        label = f"applied_corrections[{index}]"
        payload = _mapping(item, label)
        _keys(payload, _CORRECTION_KEYS, label)
        try:
            correction = BeliefCorrection(
                correction_id=_text(payload["correction_id"], f"{label}.correction_id"),
                supersedes_claim_ids=_string_tuple(
                    payload["supersedes_claim_ids"],
                    f"{label}.supersedes_claim_ids",
                ),
                replacement_claim_id=_text(
                    payload["replacement_claim_id"],
                    f"{label}.replacement_claim_id",
                ),
                reason=_text(payload["reason"], f"{label}.reason"),
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
            )
        except (TypeError, ValueError) as error:
            raise OperationalStateArtifactCodecError(f"{label} violates its contract") from error
        corrections.append(correction)
    return tuple(corrections)


def _validate_snapshot_semantics(state: OperationalBeliefState) -> None:
    expired_ids = {claim.claim_id for claim in state.expired_claims}
    superseded_ids = {claim.claim_id for claim in state.superseded_claims}
    if expired_ids & superseded_ids:
        raise OperationalStateArtifactCodecError(
            "expired and superseded claim identities must be disjoint"
        )
    if any(
        claim.epistemic_status is not EpistemicStatus.OBSERVED or claim.unknown_reason is not None
        for claim in (*state.expired_claims, *state.superseded_claims)
    ):
        raise OperationalStateArtifactCodecError(
            "expired origins and superseded claims must remain observed evidence"
        )

    corrections_by_id = {
        correction.correction_id: correction for correction in state.applied_corrections
    }
    target_id_sequence = tuple(
        claim_id
        for correction in state.applied_corrections
        for claim_id in correction.supersedes_claim_ids
    )
    target_ids = set(target_id_sequence)
    if len(target_id_sequence) != len(target_ids):
        raise OperationalStateArtifactCodecError(
            "applied corrections cannot target one claim more than once"
        )
    if target_ids != superseded_ids:
        raise OperationalStateArtifactCodecError(
            "superseded claims must exactly match applied correction targets"
        )
    replacement_ids = tuple(
        correction.replacement_claim_id for correction in state.applied_corrections
    )
    if len(replacement_ids) != len(set(replacement_ids)):
        raise OperationalStateArtifactCodecError(
            "applied correction replacement identities must be unique"
        )
    correction_identities = (
        tuple(correction.global_position for correction in state.applied_corrections),
        tuple(correction.stream_sequence for correction in state.applied_corrections),
        tuple(correction.source_event_id for correction in state.applied_corrections),
    )
    if any(len(values) != len(set(values)) for values in correction_identities):
        raise OperationalStateArtifactCodecError(
            "applied correction event positions must be unique"
        )

    original_claims = (
        *state.observed_claims,
        *state.correction_replacement_claims,
        *state.superseded_claims,
        *state.expired_claims,
    )
    originals_by_id = {claim.claim_id: claim for claim in original_claims}
    for correction in state.applied_corrections:
        replacement = originals_by_id.get(correction.replacement_claim_id)
        if replacement is None or (
            replacement.correction_id != correction.correction_id
            or replacement.supersedes_claim_ids != correction.supersedes_claim_ids
            or replacement.effective_at != correction.effective_at
            or replacement.recorded_at != correction.recorded_at
            or replacement.source_event_id != correction.source_event_id
            or replacement.source != correction.source
            or replacement.actor != correction.actor
            or replacement.correlation_id != correction.correlation_id
            or replacement.domain != correction.domain
            or replacement.stream_id != correction.stream_id
            or replacement.stream_sequence != correction.stream_sequence
            or replacement.global_position != correction.global_position
        ):
            raise OperationalStateArtifactCodecError(
                "applied correction replacement identity is inconsistent"
            )
        targets = tuple(originals_by_id[claim_id] for claim_id in correction.supersedes_claim_ids)
        if any(
            target.global_position >= correction.global_position
            or target.effective_at > correction.effective_at
            for target in targets
        ):
            raise OperationalStateArtifactCodecError(
                "applied correction target lineage is inconsistent"
            )
        if any(target.key != replacement.key for target in targets):
            raise OperationalStateArtifactCodecError(
                "applied correction target lineage is inconsistent"
            )
    for claim in original_claims:
        if claim.correction_id is None:
            continue
        correction = corrections_by_id.get(claim.correction_id)
        if (
            correction is None
            or correction.replacement_claim_id != claim.claim_id
            or correction.supersedes_claim_ids != claim.supersedes_claim_ids
        ):
            raise OperationalStateArtifactCodecError(
                "correction replacement claim lineage is inconsistent"
            )

    expected_conflicts = _expected_conflicts(state.claims)
    actual_conflicts = tuple(sorted(state.conflicts, key=_conflict_order))
    if canonical_json_bytes([_conflict_payload(item) for item in actual_conflicts]) != (
        canonical_json_bytes([_conflict_payload(item) for item in expected_conflicts])
    ):
        raise OperationalStateArtifactCodecError(
            "conflicts must exactly match current observed claims"
        )

    if state.effective_time_cutoff is not None and (
        any(claim.effective_at > state.effective_time_cutoff for claim in original_claims)
        or any(
            correction.effective_at > state.effective_time_cutoff
            for correction in state.applied_corrections
        )
    ):
        raise OperationalStateArtifactCodecError(
            "snapshot evidence cannot exceed its effective-time cutoff"
        )


def _expected_conflicts(claims: Sequence[BeliefClaim]) -> tuple[BeliefConflict, ...]:
    grouped: dict[tuple[str, str], list[BeliefClaim]] = {}
    for claim in claims:
        if claim.epistemic_status is EpistemicStatus.OBSERVED:
            grouped.setdefault(claim.key, []).append(claim)
    conflicts: list[BeliefConflict] = []
    for key in sorted(grouped):
        group = sorted(grouped[key], key=_claim_order)
        if len({canonical_json_bytes({"value": claim.value}) for claim in group}) <= 1:
            continue
        conflicts.append(
            BeliefConflict(
                subject=key[0],
                predicate=key[1],
                source_event_ids=tuple(claim.source_event_id for claim in group),
                claim_ids=tuple(claim.claim_id for claim in group),
                values=tuple(claim.value for claim in group),
            )
        )
    return tuple(conflicts)


def _decode_object(data: bytes, label: str) -> dict[str, object]:
    if not isinstance(data, bytes):
        raise TypeError("artifact data must be bytes")
    try:
        value = json.loads(data.decode("utf-8"))
        encoded = canonical_json_bytes(value)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise OperationalStateArtifactCodecError(f"{label} must be UTF-8 canonical JSON") from error
    if encoded != data:
        raise OperationalStateArtifactCodecError(f"{label} must use canonical JSON encoding")
    return _mapping(value, label)


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise OperationalStateArtifactCodecError(f"{label} must be a JSON object")
    return cast("dict[str, object]", value)


def _array(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise OperationalStateArtifactCodecError(f"{label} must be a JSON array")
    return cast("list[object]", value)


def _keys(payload: Mapping[str, object], expected: frozenset[str], label: str) -> None:
    actual = frozenset(payload)
    if actual != expected:
        raise OperationalStateArtifactCodecError(
            f"{label} fields differ; missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}"
        )


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OperationalStateArtifactCodecError(f"{label} must be a non-empty string")
    return value


def _optional_text(value: object, label: str) -> str | None:
    return None if value is None else _text(value, label)


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise OperationalStateArtifactCodecError(f"{label} must be an integer")
    return value


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise OperationalStateArtifactCodecError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise OperationalStateArtifactCodecError(f"{label} must be finite")
    return result


def _scalar(value: object, label: str) -> JsonScalar:
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise OperationalStateArtifactCodecError(f"{label} must be a finite JSON scalar")


def _string_tuple(value: object, label: str) -> tuple[str, ...]:
    return tuple(
        _text(item, f"{label}[{index}]") for index, item in enumerate(_array(value, label))
    )


def _datetime(value: object, label: str) -> datetime:
    try:
        result = datetime.fromisoformat(_text(value, label))
    except ValueError as error:
        raise OperationalStateArtifactCodecError(f"{label} must be an ISO timestamp") from error
    if result.tzinfo is None or result.utcoffset() is None:
        raise OperationalStateArtifactCodecError(f"{label} must be timezone-aware")
    return result


def _optional_datetime(value: object, label: str) -> datetime | None:
    return None if value is None else _datetime(value, label)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _enum[EnumT: (EpistemicStatus, UnknownReason)](
    enum_type: type[EnumT], value: object, label: str
) -> EnumT:
    try:
        return enum_type(_text(value, label))
    except ValueError as error:
        raise OperationalStateArtifactCodecError(f"{label} is not recognized") from error


def _optional_enum[EnumT: (EpistemicStatus, UnknownReason)](
    enum_type: type[EnumT], value: object, label: str
) -> EnumT | None:
    return None if value is None else _enum(enum_type, value, label)


def _claim_order(claim: BeliefClaim) -> tuple[datetime, datetime, int, str, str]:
    return (
        claim.effective_at,
        claim.recorded_at,
        claim.stream_sequence,
        claim.source_event_id,
        claim.claim_id,
    )


def _current_claim_order(
    claim: BeliefClaim,
) -> tuple[str, str, str, datetime, datetime, int, str, str]:
    return (claim.subject, claim.predicate, claim.source, *_claim_order(claim))


def _conflict_order(conflict: BeliefConflict) -> tuple[str, str]:
    return (conflict.subject, conflict.predicate)


def _correction_order(correction: BeliefCorrection) -> tuple[int, int, str, str]:
    return (
        correction.global_position,
        correction.stream_sequence,
        correction.source_event_id,
        correction.correction_id,
    )


__all__ = [
    "OPERATIONAL_STATE_SNAPSHOT_MEDIA_TYPE",
    "OPERATIONAL_STATE_SNAPSHOT_SCHEMA_VERSION",
    "OperationalStateArtifactCodecError",
    "decode_operational_state_snapshot",
    "encode_operational_state_snapshot",
    "operational_state_snapshot_digest",
    "operational_state_snapshot_payload",
]
