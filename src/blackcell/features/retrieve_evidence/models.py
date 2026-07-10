from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from blackcell.kernel import JsonScalar
from blackcell.kernel._json import json_digest


@dataclass(frozen=True, slots=True)
class EvidenceCandidate:
    subject: str
    predicate: str
    value: JsonScalar
    confidence: float
    effective_at: datetime
    freshness_seconds: int
    stale: bool
    source_event_id: str
    score: int
    reasons: tuple[str, ...]
    conflicted: bool


@dataclass(frozen=True, slots=True)
class EvidenceSelection:
    objective: str
    source_packet_id: str
    state_position: int
    candidates: tuple[EvidenceCandidate, ...]
    omitted_count: int
    schema_version: str = "evidence-selection/v1"
    selection_id: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "selection_id",
            json_digest(
                {
                    "schema_version": self.schema_version,
                    "objective": self.objective,
                    "source_packet_id": self.source_packet_id,
                    "state_position": self.state_position,
                    "candidate_event_ids": [item.source_event_id for item in self.candidates],
                    "candidate_scores": [item.score for item in self.candidates],
                    "omitted_count": self.omitted_count,
                }
            ),
        )
