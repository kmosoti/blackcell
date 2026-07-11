from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from blackcell.kernel import JsonScalar
from blackcell.kernel._json import canonical_json, json_digest


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
    schema_version: str = "constraint-definition/v1"
    definition_digest: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.operator, ConstraintOperator):
            raise ValueError("operator must be a recognized constraint operator")
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
        if not self.schema_version.strip():
            raise ValueError("schema_version must not be empty")
        object.__setattr__(
            self,
            "definition_digest",
            json_digest(
                {
                    "schema_version": self.schema_version,
                    "constraint_id": self.constraint_id,
                    "description": self.description,
                    "subject": self.subject,
                    "predicate": self.predicate,
                    "operator": self.operator.value,
                    # Constraint values have set semantics in the solver. Canonicalize
                    # them the same way so ordering and duplicates do not change a
                    # rule's identity.
                    "expected_values": sorted(
                        {canonical_json({"value": value}) for value in self.expected_values}
                    ),
                    "minimum_confidence": self.minimum_confidence,
                    "max_age_seconds": self.max_age_seconds,
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class ConstraintProof:
    constraint_id: str
    constraint_definition_digest: str
    outcome: ConstraintOutcome
    code: str
    message: str
    evidence_event_ids: tuple[str, ...]
    evaluated_at: datetime
    schema_version: str = "constraint-proof/v2"
    proof_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, ConstraintOutcome):
            raise ValueError("outcome must be a recognized constraint outcome")
        for name in (
            "constraint_id",
            "constraint_definition_digest",
            "code",
            "message",
            "schema_version",
        ):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        _require_aware(self.evaluated_at, name="evaluated_at")
        if any(not event_id.strip() for event_id in self.evidence_event_ids):
            raise ValueError("evidence event ids must not be empty")
        if len(self.evidence_event_ids) != len(set(self.evidence_event_ids)):
            raise ValueError("evidence event ids must be unique")
        object.__setattr__(
            self,
            "proof_id",
            json_digest(
                {
                    "schema_version": self.schema_version,
                    "constraint_id": self.constraint_id,
                    "constraint_definition_digest": self.constraint_definition_digest,
                    "outcome": self.outcome.value,
                    "code": self.code,
                    "message": self.message,
                    "evidence_event_ids": list(self.evidence_event_ids),
                    "evaluated_at": _canonical_timestamp(self.evaluated_at),
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
        for name in ("context_frame_id", "schema_version"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        _require_aware(self.evaluated_at, name="evaluated_at")
        if not self.proofs:
            raise ValueError("constraint evaluation requires at least one proof")
        if any(proof.evaluated_at != self.evaluated_at for proof in self.proofs):
            raise ValueError("proof and evaluation timestamps must match")
        constraint_ids = tuple(proof.constraint_id for proof in self.proofs)
        if len(constraint_ids) != len(set(constraint_ids)):
            raise ValueError("constraint ids must be unique across proofs")
        proof_ids = tuple(proof.proof_id for proof in self.proofs)
        if len(proof_ids) != len(set(proof_ids)):
            raise ValueError("proof ids must be unique")
        object.__setattr__(
            self,
            "evaluation_id",
            json_digest(
                {
                    "schema_version": self.schema_version,
                    "context_frame_id": self.context_frame_id,
                    "proof_ids": list(proof_ids),
                    "evaluated_at": _canonical_timestamp(self.evaluated_at),
                }
            ),
        )

    @property
    def safe(self) -> bool:
        return all(proof.outcome is ConstraintOutcome.SATISFIED for proof in self.proofs)


def _require_aware(value: datetime, *, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _canonical_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()
