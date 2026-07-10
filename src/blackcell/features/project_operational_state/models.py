from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from blackcell.kernel import JsonScalar


@dataclass(frozen=True, slots=True)
class BeliefClaim:
    subject: str
    predicate: str
    value: JsonScalar
    confidence: float
    effective_at: datetime
    recorded_at: datetime
    source_event_id: str
    source: str
    actor: str
    correlation_id: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.subject, self.predicate)


@dataclass(frozen=True, slots=True)
class BeliefConflict:
    subject: str
    predicate: str
    source_event_ids: tuple[str, ...]
    values: tuple[JsonScalar, ...]

    @property
    def key(self) -> tuple[str, str]:
        return (self.subject, self.predicate)


@dataclass(frozen=True, slots=True)
class OperationalBeliefState:
    claims: tuple[BeliefClaim, ...]
    conflicts: tuple[BeliefConflict, ...]
    last_global_position: int

    def claims_for(self, subject: str, predicate: str) -> tuple[BeliefClaim, ...]:
        return tuple(claim for claim in self.claims if claim.key == (subject, predicate))
