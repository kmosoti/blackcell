from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol, cast

from blackcell.context.models import BaselineContext
from blackcell.domains.repository import OperationalStateEstimate, SemanticEventLike


class BaselineRenderer(Protocol):
    def render(
        self, source: object, *, token_budget: int = 2_000, character_budget: int = 8_000
    ) -> BaselineContext: ...


class RawEventBaselineRenderer:
    def render(
        self,
        source: object,
        *,
        token_budget: int = 2_000,
        character_budget: int = 8_000,
    ) -> BaselineContext:
        if not isinstance(source, Iterable):
            raise TypeError("raw event baseline requires an iterable of events")
        events = sorted(cast(Iterable[SemanticEventLike], source), key=_sequence)
        rows = [
            json.dumps(
                {
                    "sequence": _sequence(event),
                    "event_type": _event_type(event),
                    "payload": _jsonable(event.payload),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            for event in events
        ]
        return _bounded(
            "raw-events",
            rows,
            tuple(str(_sequence(event)) for event in events),
            token_budget,
            character_budget,
        )


class LatestNBaselineRenderer:
    def __init__(self, n: int = 10) -> None:
        if n <= 0:
            raise ValueError("n must be positive")
        self._n = n

    def render(
        self,
        source: object,
        *,
        token_budget: int = 2_000,
        character_budget: int = 8_000,
    ) -> BaselineContext:
        if not isinstance(source, OperationalStateEstimate):
            raise TypeError("latest-N baseline requires an OperationalStateEstimate")
        latest = sorted(
            source.claims,
            key=lambda claim: (claim.effective_at, claim.observed_at, claim.claim_id),
            reverse=True,
        )[: self._n]
        rows = [
            json.dumps(
                {
                    "claim_id": claim.claim_id,
                    "subject": claim.subject,
                    "predicate": claim.predicate,
                    "value": claim.value,
                    "epistemic_status": claim.epistemic_status.value,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            for claim in latest
        ]
        return _bounded(
            f"latest-{self._n}",
            rows,
            tuple(claim.claim_id for claim in latest),
            token_budget,
            character_budget,
            source_total=len(source.claims),
        )


def _bounded(
    renderer: str,
    rows: list[str],
    references: tuple[str, ...],
    token_budget: int,
    character_budget: int,
    *,
    source_total: int | None = None,
) -> BaselineContext:
    if token_budget <= 0 or character_budget <= 0:
        raise ValueError("budgets must be positive")
    limit = min(character_budget, token_budget * 4)
    selected: list[str] = []
    selected_refs: list[str] = []
    for row, reference in zip(rows, references, strict=True):
        candidate = "\n".join((*selected, row))
        if len(candidate) > limit:
            break
        selected.append(row)
        selected_refs.append(reference)
    total = len(rows) if source_total is None else source_total
    return BaselineContext(
        renderer=renderer,
        rendered_context="\n".join(selected),
        included_references=tuple(selected_refs),
        omitted_count=total - len(selected),
        token_budget=token_budget,
        character_budget=character_budget,
    )


def _sequence(event: object) -> int:
    value = getattr(event, "stream_sequence", getattr(event, "sequence", None))
    if not isinstance(value, int):
        raise TypeError("event must expose stream_sequence or sequence")
    return value


def _event_type(event: object) -> str:
    value = getattr(event, "event_type", getattr(event, "kind", None))
    if not isinstance(value, str):
        raise TypeError("event must expose event_type or kind")
    return value


def _jsonable(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_jsonable(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    return value
