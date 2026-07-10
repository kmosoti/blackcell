from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from blackcell.kernel import JsonScalar
from blackcell.kernel._json import json_digest


class ConstraintOperator(StrEnum):
    EXISTS = "exists"
    EQUALS = "equals"
    NOT_EQUALS = "not-equals"
    IN = "in"
    NOT_IN = "not-in"


class ConstraintOutcome(StrEnum):
    SATISFIED = "satisfied"
    VIOLATED = "violated"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ConstraintDefinition:
    constraint_id: str
    description: str
    subject: str
    predicate: str
    operator: ConstraintOperator
    expected_values: tuple[JsonScalar, ...] = ()
    minimum_confidence: float = 0.0
    max_age_seconds: int | None = None

    def __post_init__(self) -> None:
        for name in ("constraint_id", "description", "subject", "predicate"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        if not 0.0 <= self.minimum_confidence <= 1.0:
            raise ValueError("minimum_confidence must be between zero and one")
        if self.max_age_seconds is not None and self.max_age_seconds < 0:
            raise ValueError("max_age_seconds must be non-negative")
        if self.operator is ConstraintOperator.EXISTS and self.expected_values:
            raise ValueError("exists constraints do not accept expected values")
        if self.operator is not ConstraintOperator.EXISTS and not self.expected_values:
            raise ValueError("value constraints require expected values")


@dataclass(frozen=True, slots=True)
class ConstraintProof:
    constraint_id: str
    outcome: ConstraintOutcome
    code: str
    message: str
    evidence_event_ids: tuple[str, ...]
    evaluated_at: datetime
    proof_id: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "proof_id",
            json_digest(
                {
                    "constraint_id": self.constraint_id,
                    "outcome": self.outcome.value,
                    "code": self.code,
                    "evidence_event_ids": list(self.evidence_event_ids),
                    "evaluated_at": self.evaluated_at.isoformat(),
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class ConstraintEvaluation:
    context_frame_id: str
    proofs: tuple[ConstraintProof, ...]
    evaluated_at: datetime
    schema_version: str = "constraint-evaluation/v1"
    evaluation_id: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "evaluation_id",
            json_digest(
                {
                    "schema_version": self.schema_version,
                    "context_frame_id": self.context_frame_id,
                    "proof_ids": [proof.proof_id for proof in self.proofs],
                    "evaluated_at": self.evaluated_at.isoformat(),
                }
            ),
        )

    @property
    def safe(self) -> bool:
        return all(proof.outcome is ConstraintOutcome.SATISFIED for proof in self.proofs)
