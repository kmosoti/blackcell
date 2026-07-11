from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime

from blackcell.features.project_operational_state.models import (
    BeliefClaim,
    BeliefConflict,
    BeliefCorrection,
    OperationalBeliefState,
    OperationalStateScope,
)
from blackcell.kernel import EventEnvelope
from blackcell.kernel._json import canonical_json

OBSERVATION_EVENT_TYPES = frozenset({"observation.recorded", "ObservationRecorded"})
CORRECTION_EVENT_TYPES = frozenset({"observation.corrected"})
STATE_EVENT_TYPES = OBSERVATION_EVENT_TYPES | CORRECTION_EVENT_TYPES
LEGACY_OBSERVATION_DOMAIN = "repository"
CORRECTION_SCHEMA_VERSION = "observation-correction/v1"


class OperationalStateProjector:
    """Fold one domain and observation stream into an operational state.

    ``events`` must be a complete, globally ordered ledger prefix. The optional
    ``as_of_position`` selects a historical ledger cutoff. ``as_of_time`` is an
    independent effective-time cutoff within that ledger prefix. Omitting the
    latter preserves the original unbounded behavior and includes all facts in
    the selected ledger prefix regardless of their effective time.

    ``scope`` should be supplied by production workflows. Omitting it is a
    compatibility path: exactly one state-event domain/stream pair is inferred,
    and ambiguous input is rejected rather than merged.

    Version 4 adds append-only corrections and bitemporal materialization while
    retaining source-aware version lineages and explicit conflicts. Expiry and
    epistemic-unknown semantics remain deferred to the next state contract.
    """

    name = "operational-belief-state"
    version = 4

    def replay(
        self,
        events: Sequence[EventEnvelope],
        *,
        scope: OperationalStateScope | None = None,
        as_of_position: int | None = None,
        as_of_time: datetime | None = None,
    ) -> OperationalBeliefState:
        if scope is not None and not scope.bound:
            raise ValueError("an explicitly supplied operational-state scope must be bound")
        _validate_effective_cutoff(as_of_time)
        ordered, cutoff = _ledger_prefix(events, as_of_position)
        resolved_scope = scope or _infer_scope(ordered, cutoff)
        claims_by_id: dict[str, BeliefClaim] = {}
        corrections: list[BeliefCorrection] = []
        correction_ids: set[str] = set()
        superseded_claim_ids: set[str] = set()
        last_source_sequence = 0

        for event in ordered:
            position = _stored_position(event)
            if position > cutoff:
                break
            if event.event_type not in STATE_EVENT_TYPES:
                continue
            if event.stream_id != resolved_scope.stream_id:
                continue
            if _event_domain(event) != resolved_scope.domain:
                continue
            last_source_sequence = event.stream_sequence

            if event.event_type in OBSERVATION_EVENT_TYPES:
                for claim in _claims(event, resolved_scope.domain):
                    _register_claim(claims_by_id, claim)
                continue

            correction, replacement = _correction(event, resolved_scope.domain)
            if correction.correction_id in correction_ids:
                raise ValueError(
                    f"correction id {correction.correction_id!r} was reused within "
                    "the projection scope"
                )
            targets = tuple(
                claims_by_id.get(claim_id) for claim_id in correction.supersedes_claim_ids
            )
            missing = tuple(
                claim_id
                for claim_id, target in zip(
                    correction.supersedes_claim_ids,
                    targets,
                    strict=True,
                )
                if target is None
            )
            if missing:
                rendered = ", ".join(repr(claim_id) for claim_id in missing)
                raise ValueError(f"correction targets must be earlier claims in scope: {rendered}")
            already_superseded = tuple(
                claim_id
                for claim_id in correction.supersedes_claim_ids
                if claim_id in superseded_claim_ids
            )
            if already_superseded:
                rendered = ", ".join(repr(claim_id) for claim_id in already_superseded)
                raise ValueError(f"correction targets were already superseded: {rendered}")
            resolved_targets = tuple(target for target in targets if target is not None)
            if any(target.key != replacement.key for target in resolved_targets):
                raise ValueError(
                    "a correction replacement must have the same fact key as its targets"
                )
            if any(correction.effective_at < target.effective_at for target in resolved_targets):
                raise ValueError("a correction cannot take effect before a target claim")

            _register_claim(claims_by_id, replacement)
            correction_ids.add(correction.correction_id)
            superseded_claim_ids.update(correction.supersedes_claim_ids)
            corrections.append(correction)

        eligible_claims = tuple(
            claim
            for claim in claims_by_id.values()
            if _effective_by(claim.effective_at, as_of_time)
        )
        applied_corrections = tuple(
            correction
            for correction in corrections
            if _effective_by(correction.effective_at, as_of_time)
        )
        applied_superseded_ids = {
            claim_id
            for correction in applied_corrections
            for claim_id in correction.supersedes_claim_ids
        }
        candidates = tuple(
            claim for claim in eligible_claims if claim.claim_id not in applied_superseded_ids
        )
        selected = _select_candidates(candidates)
        claims = tuple(
            claim
            for key in sorted(selected)
            for source in sorted(selected[key])
            for claim in sorted(selected[key][source], key=_claim_order)
        )
        conflicts = _conflicts(selected)
        explicitly_superseded = tuple(
            sorted(
                (claim for claim in eligible_claims if claim.claim_id in applied_superseded_ids),
                key=_claim_order,
            )
        )
        return OperationalBeliefState(
            scope=resolved_scope,
            claims=claims,
            conflicts=conflicts,
            cutoff_global_position=cutoff,
            last_source_stream_sequence=last_source_sequence,
            superseded_claims=explicitly_superseded,
            applied_corrections=applied_corrections,
            effective_time_cutoff=as_of_time,
        )


def _validate_effective_cutoff(as_of_time: datetime | None) -> None:
    if as_of_time is not None and (as_of_time.tzinfo is None or as_of_time.utcoffset() is None):
        raise ValueError("as_of_time must be timezone-aware")


def _effective_by(effective_at: datetime, as_of_time: datetime | None) -> bool:
    return as_of_time is None or effective_at <= as_of_time


def _register_claim(claims_by_id: dict[str, BeliefClaim], claim: BeliefClaim) -> None:
    if claim.claim_id in claims_by_id:
        raise ValueError(f"claim id {claim.claim_id!r} was reused within the projection scope")
    claims_by_id[claim.claim_id] = claim


def _select_candidates(
    claims: Sequence[BeliefClaim],
) -> dict[tuple[str, str], dict[str, list[BeliefClaim]]]:
    candidates: dict[tuple[str, str], dict[str, list[BeliefClaim]]] = {}
    for claim in claims:
        # A source is a version lineage. Newer evidence closes only an older
        # claim from that same lineage; independent sources stay concurrent.
        by_source = candidates.setdefault(claim.key, {})
        current = by_source.get(claim.source, [])
        if not current or claim.effective_at > current[0].effective_at:
            by_source[claim.source] = [claim]
        elif claim.effective_at == current[0].effective_at:
            current.append(claim)
    return candidates


def _conflicts(
    candidates: Mapping[tuple[str, str], Mapping[str, list[BeliefClaim]]],
) -> tuple[BeliefConflict, ...]:
    conflicts: list[BeliefConflict] = []
    for key in sorted(candidates):
        group = [
            claim
            for source in sorted(candidates[key])
            for claim in sorted(candidates[key][source], key=_claim_order)
        ]
        if len({_value_key(claim) for claim in group}) <= 1:
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


def _ledger_prefix(
    events: Sequence[EventEnvelope],
    as_of_position: int | None,
) -> tuple[tuple[EventEnvelope, ...], int]:
    if isinstance(as_of_position, bool) or (as_of_position is not None and as_of_position < 0):
        raise ValueError("as_of_position must be non-negative")

    ordered = tuple(events)
    previous = 0
    for event in ordered:
        position = _stored_position(event)
        if position <= previous:
            raise ValueError("operational-state replay events must be globally ordered")
        if position != previous + 1:
            raise ValueError(
                "operational-state replay requires a globally ordered complete ledger prefix"
            )
        previous = position

    cutoff = previous if as_of_position is None else as_of_position
    if cutoff > previous:
        raise ValueError("as_of_position exceeds the supplied ledger prefix")
    return ordered, cutoff


def _stored_position(event: EventEnvelope) -> int:
    position = event.global_position
    if position is None:
        raise ValueError("operational-state replay requires stored events")
    return position


def _infer_scope(
    events: tuple[EventEnvelope, ...],
    cutoff: int,
) -> OperationalStateScope:
    scopes = {
        (_event_domain(event), event.stream_id)
        for event in events
        if _stored_position(event) <= cutoff and event.event_type in STATE_EVENT_TYPES
    }
    if len(scopes) > 1:
        raise ValueError("operational-state replay scope is ambiguous; provide an explicit scope")
    if not scopes:
        return OperationalStateScope(LEGACY_OBSERVATION_DOMAIN, None)
    domain, stream_id = scopes.pop()
    return OperationalStateScope(domain, stream_id)


def _event_domain(event: EventEnvelope) -> str:
    if "domain" not in event.payload:
        if event.event_type in CORRECTION_EVENT_TYPES:
            raise ValueError(f"correction event {event.event_id} requires a domain")
        # observation/v1 predates explicit domain scope and represented repository facts.
        observation_version = event.payload.get("observation_schema_version")
        if observation_version not in (None, "observation/v1"):
            raise ValueError(f"observation event {event.event_id} requires a domain")
        return LEGACY_OBSERVATION_DOMAIN
    value = event.payload["domain"]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"state event {event.event_id} requires a domain")
    return value


def _claims(event: EventEnvelope, domain: str) -> tuple[BeliefClaim, ...]:
    raw_claims = event.payload.get("claims")
    if not isinstance(raw_claims, tuple):
        raise ValueError(f"observation event {event.event_id} requires a claims array")
    return tuple(_claim(event, domain, raw, index) for index, raw in enumerate(raw_claims))


def _correction(event: EventEnvelope, domain: str) -> tuple[BeliefCorrection, BeliefClaim]:
    version = event.payload.get("correction_schema_version")
    if version != CORRECTION_SCHEMA_VERSION:
        raise ValueError(f"correction event {event.event_id} has an unsupported schema")
    correction_id = _event_text(event.payload.get("correction_id"), "correction_id", event)
    reason = _event_text(event.payload.get("reason"), "reason", event)
    raw_supersedes = event.payload.get("supersedes_claim_ids")
    if not isinstance(raw_supersedes, tuple) or not raw_supersedes:
        raise ValueError(f"correction event {event.event_id} requires superseded claim ids")
    supersedes = tuple(_event_text(value, "superseded claim id", event) for value in raw_supersedes)
    if len(supersedes) != len(set(supersedes)):
        raise ValueError(f"correction event {event.event_id} repeats a superseded claim id")
    raw_evidence = event.payload.get("evidence")
    if not isinstance(raw_evidence, tuple) or not raw_evidence:
        raise ValueError(f"correction event {event.event_id} requires explicit evidence")
    if any(not isinstance(pointer, Mapping) for pointer in raw_evidence):
        raise ValueError(f"correction event {event.event_id} has invalid evidence")
    raw_replacement = event.payload.get("replacement")
    replacement = _claim(
        event,
        domain,
        raw_replacement,
        0,
        correction_id=correction_id,
        supersedes_claim_ids=supersedes,
    )
    if replacement.claim_id in supersedes:
        raise ValueError("a correction replacement requires a new claim id")
    correction = BeliefCorrection(
        correction_id=correction_id,
        supersedes_claim_ids=supersedes,
        replacement_claim_id=replacement.claim_id,
        reason=reason,
        effective_at=event.effective_at,
        recorded_at=event.recorded_at,
        source_event_id=event.event_id,
        source=event.source,
        actor=event.actor,
        correlation_id=event.correlation_id,
        domain=domain,
        stream_id=event.stream_id,
        stream_sequence=event.stream_sequence,
        global_position=_stored_position(event),
    )
    return correction, replacement


def _claim(
    event: EventEnvelope,
    domain: str,
    raw: object,
    index: int,
    *,
    correction_id: str | None = None,
    supersedes_claim_ids: tuple[str, ...] = (),
) -> BeliefClaim:
    if not isinstance(raw, Mapping):
        raise ValueError(f"claim {index} in event {event.event_id} must be an object")
    subject = _text(raw.get("subject"), "subject", event, index)
    predicate = _text(raw.get("predicate"), "predicate", event, index)
    raw_claim_id = raw.get("claim_id")
    claim_id = (
        f"{event.event_id}#claim:{index}"
        if raw_claim_id is None
        else _text(raw_claim_id, "claim_id", event, index)
    )
    value = raw.get("value")
    if not isinstance(value, (str, int, float, bool)) and value is not None:
        raise ValueError(f"claim {index} in event {event.event_id} requires a scalar value")
    confidence_value = raw.get("confidence", 1.0)
    if isinstance(confidence_value, bool) or not isinstance(confidence_value, (int, float)):
        raise ValueError(f"claim {index} in event {event.event_id} has invalid confidence")
    confidence = float(confidence_value)
    if not 0.0 <= confidence <= 1.0:
        raise ValueError(f"claim {index} in event {event.event_id} has invalid confidence")
    return BeliefClaim(
        claim_id=claim_id,
        subject=subject,
        predicate=predicate,
        value=value,
        confidence=confidence,
        effective_at=event.effective_at,
        recorded_at=event.recorded_at,
        source_event_id=event.event_id,
        source=event.source,
        actor=event.actor,
        correlation_id=event.correlation_id,
        domain=domain,
        stream_id=event.stream_id,
        stream_sequence=event.stream_sequence,
        global_position=_stored_position(event),
        correction_id=correction_id,
        supersedes_claim_ids=supersedes_claim_ids,
    )


def _text(
    value: object,
    field: str,
    event: EventEnvelope,
    index: int,
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"claim {index} in event {event.event_id} requires {field}")
    return value


def _event_text(value: object, field: str, event: EventEnvelope) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"correction event {event.event_id} requires {field}")
    return value


def _value_key(claim: BeliefClaim) -> str:
    """Preserve JSON distinctions Python equality erases, such as true and 1."""

    return canonical_json({"value": claim.value})


def _claim_order(claim: BeliefClaim) -> tuple[datetime, datetime, int, str, str]:
    return (
        claim.effective_at,
        claim.recorded_at,
        claim.stream_sequence,
        claim.source_event_id,
        claim.claim_id,
    )
