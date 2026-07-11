from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from blackcell.kernel import JsonScalar


@dataclass(frozen=True, slots=True)
class OperationalStateScope:
    """The bounded evidence partition represented by an operational state.

    ``stream_id`` is optional only for an empty, compatibility projection where
    there is no observation stream to infer.  A state containing evidence is
    always bound to both a domain and one source stream.
    """

    domain: str
    stream_id: str | None

    def __post_init__(self) -> None:
        if not self.domain.strip():
            raise ValueError("domain must not be empty")
        if self.stream_id is not None and not self.stream_id.strip():
            raise ValueError("stream_id must not be blank")

    @property
    def bound(self) -> bool:
        return self.stream_id is not None


@dataclass(frozen=True, slots=True)
class BeliefClaim:
    claim_id: str
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
    domain: str
    stream_id: str
    stream_sequence: int
    global_position: int

    @property
    def key(self) -> tuple[str, str]:
        return (self.subject, self.predicate)


@dataclass(frozen=True, slots=True)
class BeliefConflict:
    subject: str
    predicate: str
    source_event_ids: tuple[str, ...]
    claim_ids: tuple[str, ...]
    values: tuple[JsonScalar, ...]

    @property
    def key(self) -> tuple[str, str]:
        return (self.subject, self.predicate)


@dataclass(frozen=True, slots=True)
class OperationalBeliefState:
    scope: OperationalStateScope
    claims: tuple[BeliefClaim, ...]
    conflicts: tuple[BeliefConflict, ...]
    cutoff_global_position: int
    last_source_stream_sequence: int

    def __post_init__(self) -> None:
        if self.cutoff_global_position < 0:
            raise ValueError("cutoff_global_position must be non-negative")
        if self.last_source_stream_sequence < 0:
            raise ValueError("last_source_stream_sequence must be non-negative")
        if not self.scope.bound and (
            self.claims or self.conflicts or self.last_source_stream_sequence
        ):
            raise ValueError("an unbound operational state must be empty")
        if self.scope.stream_id is not None:
            outside_scope = tuple(
                claim
                for claim in self.claims
                if claim.domain != self.scope.domain or claim.stream_id != self.scope.stream_id
            )
            if outside_scope:
                raise ValueError("operational state claims must belong to its declared scope")
        if any(claim.global_position > self.cutoff_global_position for claim in self.claims):
            raise ValueError("operational state claims cannot exceed its ledger cutoff")
        if any(claim.stream_sequence > self.last_source_stream_sequence for claim in self.claims):
            raise ValueError("operational state claims cannot exceed its source stream position")

    @property
    def last_global_position(self) -> int:
        """Compatibility name for the complete ledger cutoff represented by this state."""

        return self.cutoff_global_position

    def claims_for(self, subject: str, predicate: str) -> tuple[BeliefClaim, ...]:
        return tuple(claim for claim in self.claims if claim.key == (subject, predicate))
