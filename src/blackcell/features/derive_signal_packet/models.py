from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from blackcell.kernel import JsonScalar
from blackcell.kernel._json import json_digest


@dataclass(frozen=True, slots=True)
class SignalClaim:
    subject: str
    predicate: str
    value: JsonScalar
    confidence: float
    effective_at: datetime
    freshness_seconds: int
    stale: bool
    source_event_id: str


@dataclass(frozen=True, slots=True)
class SignalConflict:
    subject: str
    predicate: str
    source_event_ids: tuple[str, ...]
    values: tuple[JsonScalar, ...]


@dataclass(frozen=True, slots=True)
class SignalPacket:
    scope: str
    generated_at: datetime
    state_position: int
    claims: tuple[SignalClaim, ...]
    conflicts: tuple[SignalConflict, ...]
    provenance_event_ids: tuple[str, ...]
    mean_confidence: float
    stale_claim_count: int
    schema_version: str = "signal-packet/v1"
    packet_id: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "packet_id",
            json_digest(
                {
                    "schema_version": self.schema_version,
                    "scope": self.scope,
                    "generated_at": self.generated_at.isoformat(),
                    "state_position": self.state_position,
                    "claims": [
                        {
                            "subject": claim.subject,
                            "predicate": claim.predicate,
                            "value": claim.value,
                            "confidence": claim.confidence,
                            "effective_at": claim.effective_at.isoformat(),
                            "freshness_seconds": claim.freshness_seconds,
                            "stale": claim.stale,
                            "source_event_id": claim.source_event_id,
                        }
                        for claim in self.claims
                    ],
                    "conflicts": [
                        {
                            "subject": conflict.subject,
                            "predicate": conflict.predicate,
                            "source_event_ids": list(conflict.source_event_ids),
                            "values": list(conflict.values),
                        }
                        for conflict in self.conflicts
                    ],
                }
            ),
        )
