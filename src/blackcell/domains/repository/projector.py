from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any, cast

from blackcell.domains.repository.events import (
    CLAIMS_RECORDED,
    CORRECTION_RECORDED,
    SemanticEventLike,
)
from blackcell.domains.repository.models import (
    Claim,
    ClaimBatch,
    ClaimConflict,
    ClaimCorrection,
    EpistemicStatus,
    EvidenceRef,
    OperationalStateEstimate,
    SourceReliability,
    claim_value_key,
)

_CLAIM_KINDS = frozenset({CLAIMS_RECORDED, "ObservationRecorded", "observation.recorded"})
_CORRECTION_KINDS = frozenset({CORRECTION_RECORDED, "CorrectionRecorded", "correction.recorded"})
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class ProjectionError(ValueError):
    pass


class RepositoryProjector:
    """Pure bitemporal projection over repository semantic events."""

    def project(
        self,
        events: Iterable[SemanticEventLike],
        *,
        repository_id: str = "repository",
        as_of_sequence: int | None = None,
        as_of_time: datetime | None = None,
    ) -> OperationalStateEstimate:
        ordered = sorted(events, key=lambda event: (_event_sequence(event), _event_id(event)))
        _validate_unique_sequences(ordered)
        maximum_sequence = max((_event_sequence(event) for event in ordered), default=0)
        projection_sequence = maximum_sequence if as_of_sequence is None else as_of_sequence
        if projection_sequence < 0:
            raise ProjectionError("as_of_sequence must be non-negative")
        included = tuple(
            event for event in ordered if _event_sequence(event) <= projection_sequence
        )
        projection_time = as_of_time or max(
            (_event_time(event) for event in included), default=_EPOCH
        )
        _require_aware(projection_time)

        claims_by_id: dict[str, Claim] = {}
        corrections: list[tuple[int, ClaimCorrection]] = []
        for event in included:
            parsed_claims, parsed_correction = _parse_event(event)
            for claim in parsed_claims:
                previous = claims_by_id.get(claim.claim_id)
                if previous is not None and previous != claim:
                    raise ProjectionError(
                        f"claim id {claim.claim_id!r} was reused with different content"
                    )
                claims_by_id[claim.claim_id] = claim
            if parsed_correction is not None:
                replacement = parsed_correction.replacement
                previous = claims_by_id.get(replacement.claim_id)
                if previous is not None and previous != replacement:
                    raise ProjectionError(
                        f"claim id {replacement.claim_id!r} was reused with different content"
                    )
                claims_by_id[replacement.claim_id] = replacement
                corrections.append((_event_sequence(event), parsed_correction))

        effective_claims = {
            claim_id: claim
            for claim_id, claim in claims_by_id.items()
            if claim.effective_at <= projection_time
        }
        superseded_ids: set[str] = set()
        applied_corrections: list[str] = []
        for _sequence, correction in sorted(
            corrections, key=lambda item: (item[0], item[1].correction_id)
        ):
            if correction.effective_at > projection_time:
                continue
            superseded_ids.update(correction.supersedes_claim_ids)
            applied_corrections.append(correction.correction_id)

        candidates = tuple(
            claim for claim_id, claim in effective_claims.items() if claim_id not in superseded_ids
        )
        claims, temporally_superseded = _select_current_claims(candidates)
        superseded_ids.update(claim.claim_id for claim in temporally_superseded)
        superseded = tuple(
            sorted(
                (
                    claim
                    for claim_id, claim in effective_claims.items()
                    if claim_id in superseded_ids
                ),
                key=_claim_order,
            )
        )
        conflicts = _find_conflicts(claims, projection_time)
        unknowns = tuple(
            claim for claim in claims if claim.epistemic_status is EpistemicStatus.UNKNOWN
        )
        return OperationalStateEstimate(
            repository_id=repository_id,
            as_of_sequence=projection_sequence,
            as_of_time=projection_time,
            claims=claims,
            superseded_claims=superseded,
            conflicts=conflicts,
            unknowns=unknowns,
            applied_corrections=tuple(applied_corrections),
        )


def _parse_event(event: SemanticEventLike) -> tuple[tuple[Claim, ...], ClaimCorrection | None]:
    kind = _event_kind(event)
    payload = event.payload
    if kind in _CLAIM_KINDS:
        if isinstance(payload, ClaimBatch):
            return payload.claims, None
        if isinstance(payload, Mapping):
            payload = cast(Mapping[str, object], payload)
            if payload.get("domain") not in (None, "repository"):
                return (), None
            raw_claims = payload.get("claims")
            if raw_claims is None and "claim" in payload:
                raw_claims = [payload["claim"]]
            if raw_claims is None:
                return (), None
            if not isinstance(raw_claims, (list, tuple)):
                raise ProjectionError("claims payload must be a sequence")
            return tuple(_claim_from(value) for value in raw_claims), None
        raise ProjectionError(f"unsupported observation payload: {type(payload).__name__}")
    if kind in _CORRECTION_KINDS:
        if isinstance(payload, ClaimCorrection):
            return (), payload
        if isinstance(payload, Mapping):
            payload = cast(Mapping[str, object], payload)
            if payload.get("domain") not in (None, "repository"):
                return (), None
            raw = payload.get("correction", payload)
            if not isinstance(raw, Mapping):
                raise ProjectionError("correction payload must be a mapping")
            return (), _correction_from(cast(Mapping[str, object], raw))
        raise ProjectionError(f"unsupported correction payload: {type(payload).__name__}")
    return (), None


def _claim_from(value: object) -> Claim:
    if isinstance(value, Claim):
        return value
    if not isinstance(value, Mapping):
        raise ProjectionError("serialized claim must be a mapping")
    value = cast(Mapping[str, object], value)
    try:
        raw_evidence = value.get("evidence", ())
        if not isinstance(raw_evidence, (list, tuple)):
            raise TypeError("evidence must be a sequence")
        evidence = tuple(_evidence_from(item) for item in raw_evidence)
        expires = value.get("expires_at")
        return Claim(
            claim_id=str(value["claim_id"]),
            subject=str(value["subject"]),
            predicate=str(value["predicate"]),
            value=_scalar(value.get("value")),
            epistemic_status=EpistemicStatus(str(value["epistemic_status"])),
            source_reliability=SourceReliability(str(value["source_reliability"])),
            evidence=evidence,
            observed_at=_datetime_from(value["observed_at"]),
            effective_at=_datetime_from(value["effective_at"]),
            expires_at=_datetime_from(expires) if expires is not None else None,
            conflict_group=_optional_text(value.get("conflict_group")),
            derivation_version=str(value.get("derivation_version", "repository-observation/v1")),
            schema_version=str(value.get("schema_version", "claim/v1")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ProjectionError(f"invalid serialized claim: {exc}") from exc


def _evidence_from(value: object) -> EvidenceRef:
    if isinstance(value, EvidenceRef):
        return value
    if not isinstance(value, Mapping):
        raise ProjectionError("serialized evidence reference must be a mapping")
    value = cast(Mapping[str, object], value)
    sequence = value.get("sequence")
    if sequence is not None and not isinstance(sequence, int | str):
        raise ProjectionError("evidence sequence must be an integer")
    return EvidenceRef(
        event_id=str(value["event_id"]),
        source=str(value["source"]),
        sequence=int(sequence) if sequence is not None else None,
        artifact_id=_optional_text(value.get("artifact_id")),
        locator=_optional_text(value.get("locator")),
        digest=_optional_text(value.get("digest")),
    )


def _correction_from(value: Mapping[str, object]) -> ClaimCorrection:
    try:
        supersedes = value["supersedes_claim_ids"]
        if not isinstance(supersedes, (list, tuple)):
            raise TypeError("supersedes_claim_ids must be a sequence")
        evidence = value.get("evidence", ())
        if not isinstance(evidence, (list, tuple)):
            raise TypeError("evidence must be a sequence")
        return ClaimCorrection(
            correction_id=str(value["correction_id"]),
            supersedes_claim_ids=tuple(str(item) for item in supersedes),
            replacement=_claim_from(value["replacement"]),
            effective_at=_datetime_from(value["effective_at"]),
            reason=str(value["reason"]),
            evidence=tuple(_evidence_from(item) for item in evidence),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ProjectionError(f"invalid serialized correction: {exc}") from exc


def _find_conflicts(claims: tuple[Claim, ...], as_of_time: datetime) -> tuple[ClaimConflict, ...]:
    grouped: dict[str, list[Claim]] = {}
    for claim in claims:
        if (
            claim.conflict_group is None
            or claim.epistemic_status is EpistemicStatus.UNKNOWN
            or claim.is_expired(as_of_time)
        ):
            continue
        grouped.setdefault(claim.conflict_group, []).append(claim)
    conflicts = []
    for group, members in sorted(grouped.items()):
        if len({claim_value_key(claim.value) for claim in members}) > 1:
            conflicts.append(ClaimConflict(group, tuple(sorted(members, key=_claim_order))))
    return tuple(conflicts)


def _select_current_claims(
    claims: tuple[Claim, ...],
) -> tuple[tuple[Claim, ...], tuple[Claim, ...]]:
    """Close older same-source claim versions without erasing event history."""

    grouped: dict[tuple[str, str, tuple[str, ...], str], list[Claim]] = {}
    for claim in claims:
        sources = tuple(sorted({evidence.source for evidence in claim.evidence}))
        key = (claim.subject, claim.predicate, sources, claim.derivation_version)
        grouped.setdefault(key, []).append(claim)

    current: list[Claim] = []
    superseded: list[Claim] = []
    for members in grouped.values():
        latest = max(_claim_version_key(claim) for claim in members)
        for claim in members:
            if _claim_version_key(claim) == latest:
                current.append(claim)
            else:
                superseded.append(claim)
    return (
        tuple(sorted(current, key=_claim_order)),
        tuple(sorted(superseded, key=_claim_order)),
    )


def _claim_order(claim: Claim) -> tuple[datetime, datetime, str]:
    return claim.effective_at, claim.observed_at, claim.claim_id


def _claim_version_key(claim: Claim) -> tuple[datetime, datetime, int]:
    sequence = max(
        (evidence.sequence for evidence in claim.evidence if evidence.sequence is not None),
        default=-1,
    )
    return claim.effective_at, claim.observed_at, sequence


def _event_id(event: SemanticEventLike) -> str:
    value = getattr(event, "event_id", None)
    return str(value) if value is not None else f"sequence:{_event_sequence(event)}"


def _event_time(event: SemanticEventLike) -> datetime:
    value = getattr(event, "occurred_at", getattr(event, "recorded_at", None))
    if value is None and isinstance(event.payload, Mapping):
        value = event.payload.get("occurred_at")
    if value is None:
        return _EPOCH
    return _datetime_from(value)


def _event_sequence(event: SemanticEventLike) -> int:
    value = getattr(event, "stream_sequence", getattr(event, "sequence", None))
    if not isinstance(value, int):
        raise ProjectionError("event must expose integer stream_sequence or sequence")
    return value


def _event_kind(event: SemanticEventLike) -> str:
    value = getattr(event, "event_type", getattr(event, "kind", None))
    if not isinstance(value, str):
        raise ProjectionError("event must expose event_type or kind")
    return value


def _validate_unique_sequences(events: list[SemanticEventLike]) -> None:
    seen: set[int] = set()
    for event in events:
        sequence = _event_sequence(event)
        if sequence in seen:
            raise ProjectionError(f"duplicate event sequence: {sequence}")
        seen.add(sequence)


def _datetime_from(value: object) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise TypeError("timestamp must be a datetime or ISO-8601 string")
    _require_aware(result)
    return result


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ProjectionError("projection timestamps must be timezone-aware")


def _scalar(value: Any) -> str | int | float | bool | None:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise TypeError("claim value must be a JSON scalar")


def _optional_text(value: object) -> str | None:
    return None if value is None else str(value)
