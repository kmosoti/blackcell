from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from blackcell.kernel import JsonScalar
from blackcell.kernel._json import json_digest


@dataclass(frozen=True, slots=True)
class EvidenceKey:
    subject: str
    predicate: str

    def __post_init__(self) -> None:
        if not self.subject.strip() or not self.predicate.strip():
            raise ValueError("evidence keys require subject and predicate")


class MissingRequiredEvidenceError(ValueError):
    def __init__(self, missing_keys: tuple[EvidenceKey, ...]) -> None:
        self.missing_keys = missing_keys
        rendered = ", ".join(f"{key.subject}/{key.predicate}" for key in missing_keys)
        super().__init__(f"required evidence is missing for: {rendered}")


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
    required_keys: tuple[EvidenceKey, ...] = ()
    required_match_count: int = 0
    schema_version: str = "evidence-selection/v2"
    selection_id: str = field(init=False)

    def __post_init__(self) -> None:
        required = {(key.subject, key.predicate) for key in self.required_keys}
        unexpected_required = tuple(
            item
            for item in self.candidates
            if "required" in item.reasons and (item.subject, item.predicate) not in required
        )
        if unexpected_required:
            raise ValueError("evidence candidate is marked required for an undeclared key")
        required_candidates = tuple(
            item
            for item in self.candidates
            if (item.subject, item.predicate) in required and "required" in item.reasons
        )
        missing_keys = tuple(
            key
            for key in self.required_keys
            if (key.subject, key.predicate)
            not in {(item.subject, item.predicate) for item in required_candidates}
        )
        if missing_keys:
            raise MissingRequiredEvidenceError(missing_keys)
        if len(required_candidates) != self.required_match_count:
            raise ValueError(
                "evidence selection does not contain every required match: "
                f"expected {self.required_match_count}, selected {len(required_candidates)}"
            )
        object.__setattr__(
            self,
            "selection_id",
            json_digest(
                {
                    "schema_version": self.schema_version,
                    "objective": self.objective,
                    "source_packet_id": self.source_packet_id,
                    "state_position": self.state_position,
                    "required_keys": [
                        {"subject": key.subject, "predicate": key.predicate}
                        for key in self.required_keys
                    ],
                    "required_match_count": self.required_match_count,
                    "candidate_event_ids": [item.source_event_id for item in self.candidates],
                    "candidate_scores": [item.score for item in self.candidates],
                    "omitted_count": self.omitted_count,
                }
            ),
        )
