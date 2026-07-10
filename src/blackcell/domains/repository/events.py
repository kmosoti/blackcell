from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from blackcell.domains.repository.models import ClaimBatch, ClaimCorrection, Scalar

CLAIMS_RECORDED = "repository.claims-recorded"
CORRECTION_RECORDED = "repository.correction-recorded"

type EventPayload = ClaimBatch | ClaimCorrection | Mapping[str, object]


@runtime_checkable
class SemanticEventLike(Protocol):
    """Structural boundary implemented by both domain events and kernel envelopes."""

    stream_sequence: int
    event_type: str
    payload: object


@dataclass(frozen=True, slots=True)
class RepositorySemanticEvent:
    event_id: str
    sequence: int
    kind: str
    source: str
    occurred_at: datetime
    payload: EventPayload
    schema_version: str = "repository-event/v1"

    def __post_init__(self) -> None:
        if not self.event_id or not self.source:
            raise ValueError("event_id and source must be non-empty")
        if self.sequence < 0:
            raise ValueError("event sequence must be non-negative")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")

    @property
    def stream_sequence(self) -> int:
        return self.sequence

    @property
    def event_type(self) -> str:
        return self.kind

    @property
    def recorded_at(self) -> datetime:
        return self.occurred_at

    @property
    def effective_at(self) -> datetime:
        return self.occurred_at


type SerializedPayload = Mapping[str, Scalar | list[object] | dict[str, object]]
