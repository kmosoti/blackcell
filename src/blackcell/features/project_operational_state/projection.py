from __future__ import annotations

from collections.abc import Mapping, Sequence

from blackcell.features.project_operational_state.models import (
    BeliefClaim,
    BeliefConflict,
    OperationalBeliefState,
)
from blackcell.kernel import EventEnvelope
from blackcell.kernel._json import canonical_json

OBSERVATION_EVENT_TYPES = frozenset({"observation.recorded", "ObservationRecorded"})


class OperationalStateProjector:
    """Fold recorded observations into a provenance-preserving belief state."""

    name = "operational-belief-state"
    version = 1

    def replay(self, events: Sequence[EventEnvelope]) -> OperationalBeliefState:
        candidates: dict[tuple[str, str], list[BeliefClaim]] = {}
        last_position = 0
        for event in events:
            position = event.global_position
            if position is None:
                raise ValueError("operational-state replay requires stored events")
            if position <= last_position:
                raise ValueError("operational-state replay events must be globally ordered")
            last_position = position
            if event.event_type not in OBSERVATION_EVENT_TYPES:
                continue
            for claim in _claims(event):
                current = candidates.get(claim.key, [])
                if not current or claim.effective_at > current[0].effective_at:
                    candidates[claim.key] = [claim]
                elif claim.effective_at == current[0].effective_at:
                    current.append(claim)

        claims = tuple(
            claim
            for key in sorted(candidates)
            for claim in sorted(candidates[key], key=lambda item: item.source_event_id)
        )
        conflicts = tuple(
            BeliefConflict(
                subject=key[0],
                predicate=key[1],
                source_event_ids=tuple(claim.source_event_id for claim in group),
                values=tuple(claim.value for claim in group),
            )
            for key in sorted(candidates)
            if (group := candidates[key]) and len({_value_key(claim) for claim in group}) > 1
        )
        return OperationalBeliefState(claims, conflicts, last_position)


def _claims(event: EventEnvelope) -> tuple[BeliefClaim, ...]:
    raw_claims = event.payload.get("claims")
    if not isinstance(raw_claims, tuple):
        raise ValueError(f"observation event {event.event_id} requires a claims array")
    return tuple(_claim(event, raw, index) for index, raw in enumerate(raw_claims))


def _claim(event: EventEnvelope, raw: object, index: int) -> BeliefClaim:
    if not isinstance(raw, Mapping):
        raise ValueError(f"claim {index} in event {event.event_id} must be an object")
    subject = _text(raw.get("subject"), "subject", event, index)
    predicate = _text(raw.get("predicate"), "predicate", event, index)
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
