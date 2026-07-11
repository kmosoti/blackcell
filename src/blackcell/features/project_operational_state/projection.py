from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from blackcell.features.project_operational_state.fold import (
    LEGACY_OBSERVATION_DOMAIN,
    STATE_EVENT_TYPES,
    OperationalStateFold,
    event_domain,
)
from blackcell.features.project_operational_state.models import (
    OperationalBeliefState,
    OperationalStateScope,
)
from blackcell.kernel import EventEnvelope


class OperationalStateProjector:
    """Fold one domain and observation stream into an operational state.

    ``events`` must be a complete, globally ordered ledger prefix. The optional
    ``as_of_position`` selects a historical ledger cutoff. ``as_of_time`` is an
    independent effective-time cutoff within that ledger prefix. Omitting the
    latter preserves the original unbounded behavior and does not expire facts.

    ``scope`` should be supplied by production workflows. Omitting it is a
    compatibility path: exactly one state-event domain/stream pair is inferred,
    and ambiguous input is rejected rather than merged.

    Version 5 adds expiry-derived unknowns and delegates normalization to the
    same pure fold used by disposable checkpoints.
    """

    name = "operational-belief-state"
    version = 5

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
        if not resolved_scope.bound:
            return OperationalBeliefState(
                scope=resolved_scope,
                claims=(),
                conflicts=(),
                cutoff_global_position=cutoff,
                last_source_stream_sequence=0,
                effective_time_cutoff=as_of_time,
            )

        fold = OperationalStateFold(resolved_scope)
        raw = fold.initial_state()
        for event in ordered:
            if _stored_position(event) > cutoff:
                break
            raw = fold.apply(raw, event)
        return fold.materialize(
            raw,
            cutoff_global_position=cutoff,
            as_of_time=as_of_time,
        )


def _validate_effective_cutoff(as_of_time: datetime | None) -> None:
    if as_of_time is not None and (as_of_time.tzinfo is None or as_of_time.utcoffset() is None):
        raise ValueError("as_of_time must be timezone-aware")


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
        (event_domain(event), event.stream_id)
        for event in events
        if _stored_position(event) <= cutoff and event.event_type in STATE_EVENT_TYPES
    }
    if len(scopes) > 1:
        raise ValueError("operational-state replay scope is ambiguous; provide an explicit scope")
    if not scopes:
        return OperationalStateScope(LEGACY_OBSERVATION_DOMAIN, None)
    domain, stream_id = scopes.pop()
    return OperationalStateScope(domain, stream_id)
