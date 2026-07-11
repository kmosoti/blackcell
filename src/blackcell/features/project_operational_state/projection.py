from __future__ import annotations

from collections.abc import Mapping, Sequence

from blackcell.features.project_operational_state.models import (
    BeliefClaim,
    BeliefConflict,
    OperationalBeliefState,
    OperationalStateScope,
)
from blackcell.kernel import EventEnvelope
from blackcell.kernel._json import canonical_json

OBSERVATION_EVENT_TYPES = frozenset({"observation.recorded", "ObservationRecorded"})
LEGACY_OBSERVATION_DOMAIN = "repository"


class OperationalStateProjector:
    """Fold one domain and observation stream into an operational state.

    ``events`` must be a complete, globally ordered ledger prefix.  The optional
    ``as_of_position`` selects a historical cutoff within that prefix.  The
    state's global position is that ledger cutoff, while
    ``last_source_stream_sequence`` is the last matching observation in the
    selected domain/stream pair.

    ``scope`` should be supplied by production workflows.  Omitting it is a
    compatibility path: exactly one observation domain/stream pair is inferred,
    and ambiguous input is rejected rather than merged.
    """

    name = "operational-belief-state"
    version = 2

    def replay(
        self,
        events: Sequence[EventEnvelope],
        *,
        scope: OperationalStateScope | None = None,
        as_of_position: int | None = None,
    ) -> OperationalBeliefState:
        ordered, cutoff = _ledger_prefix(events, as_of_position)
        resolved_scope = scope or _infer_scope(ordered, cutoff)
        candidates: dict[tuple[str, str], list[BeliefClaim]] = {}
        last_source_sequence = 0

        for event in ordered:
            position = _stored_position(event)
            if position > cutoff:
                break
            if event.event_type not in OBSERVATION_EVENT_TYPES:
                continue
            if event.stream_id != resolved_scope.stream_id:
                continue
            if _event_domain(event) != resolved_scope.domain:
                continue
            last_source_sequence = event.stream_sequence
            for claim in _claims(event, resolved_scope.domain):
                current = candidates.get(claim.key, [])
                if not current or claim.effective_at > current[0].effective_at:
                    candidates[claim.key] = [claim]
                elif claim.effective_at == current[0].effective_at:
                    current.append(claim)

        claims = tuple(
            claim
            for key in sorted(candidates)
            for claim in sorted(
                candidates[key],
                key=lambda item: (item.source_event_id, item.claim_id),
            )
        )
        conflicts = tuple(
            BeliefConflict(
                subject=key[0],
                predicate=key[1],
                source_event_ids=tuple(claim.source_event_id for claim in group),
                claim_ids=tuple(claim.claim_id for claim in group),
                values=tuple(claim.value for claim in group),
            )
            for key in sorted(candidates)
            if (group := candidates[key]) and len({_value_key(claim) for claim in group}) > 1
        )
        return OperationalBeliefState(
            scope=resolved_scope,
            claims=claims,
            conflicts=conflicts,
            cutoff_global_position=cutoff,
            last_source_stream_sequence=last_source_sequence,
        )


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
        if _stored_position(event) <= cutoff and event.event_type in OBSERVATION_EVENT_TYPES
    }
    if len(scopes) > 1:
        raise ValueError("operational-state replay scope is ambiguous; provide an explicit scope")
    if not scopes:
        return OperationalStateScope(LEGACY_OBSERVATION_DOMAIN, None)
    domain, stream_id = scopes.pop()
    return OperationalStateScope(domain, stream_id)


def _event_domain(event: EventEnvelope) -> str:
    # observation/v1 predates explicit domain scope and represented repository facts.
    if "domain" not in event.payload:
        observation_version = event.payload.get("observation_schema_version")
        if observation_version not in (None, "observation/v1"):
            raise ValueError(f"observation event {event.event_id} requires a domain")
        return LEGACY_OBSERVATION_DOMAIN
    value = event.payload["domain"]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"observation event {event.event_id} requires a domain")
    return value


def _claims(event: EventEnvelope, domain: str) -> tuple[BeliefClaim, ...]:
    raw_claims = event.payload.get("claims")
    if not isinstance(raw_claims, tuple):
        raise ValueError(f"observation event {event.event_id} requires a claims array")
    return tuple(_claim(event, domain, raw, index) for index, raw in enumerate(raw_claims))


def _claim(event: EventEnvelope, domain: str, raw: object, index: int) -> BeliefClaim:
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


def _value_key(claim: BeliefClaim) -> str:
    """Preserve JSON distinctions Python equality otherwise erases, such as true and 1."""

    return canonical_json({"value": claim.value})
