from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from blackcell.kernel import JsonScalar
from blackcell.kernel._json import json_digest


@dataclass(frozen=True, slots=True)
class ContextEvidence:
    subject: str
    predicate: str
    value: JsonScalar
    confidence: float
    effective_at: datetime
    freshness_seconds: int
    stale: bool
    source_event_id: str
    relevance_score: int
    selection_reasons: tuple[str, ...]
    conflicted: bool


@dataclass(frozen=True, slots=True)
class ContextFrame:
    task_id: str
    objective: str
    generated_at: datetime
    state_position: int
    source_packet_id: str
    source_selection_id: str
    evidence: tuple[ContextEvidence, ...]
    provenance_event_ids: tuple[str, ...]
    omitted_evidence_count: int
    serialized_characters: int
    schema_version: str = "context-frame/v1"
    frame_id: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "frame_id",
            json_digest(
                {
                    "schema_version": self.schema_version,
                    "task_id": self.task_id,
                    "objective": self.objective,
                    "generated_at": self.generated_at.isoformat(),
                    "state_position": self.state_position,
                    "source_packet_id": self.source_packet_id,
                    "source_selection_id": self.source_selection_id,
                    "evidence_event_ids": list(self.provenance_event_ids),
                    "omitted_evidence_count": self.omitted_evidence_count,
                }
            ),
        )
