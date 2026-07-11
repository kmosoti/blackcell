from __future__ import annotations

from datetime import UTC, datetime, timedelta

from blackcell.features.solve_constraints.command import SolveConstraints
from blackcell.features.solve_constraints.models import (
    ConstraintDefinition,
    ConstraintEvaluation,
    ConstraintOperator,
    ConstraintOutcome,
    ConstraintProof,
)
from blackcell.features.solve_constraints.ports import ContextEvidenceLike, ContextFrameLike
from blackcell.kernel import JsonScalar
from blackcell.kernel._json import canonical_json


class DeterministicConstraintSolver:
    def handle(
        self,
        command: SolveConstraints,
        frame: ContextFrameLike,
    ) -> ConstraintEvaluation:
        proofs = tuple(self._evaluate(item, command, frame) for item in command.constraints)
        return ConstraintEvaluation(frame.frame_id, proofs, command.evaluated_at)

    def _evaluate(
        self,
        constraint: ConstraintDefinition,
        command: SolveConstraints,
        frame: ContextFrameLike,
    ) -> ConstraintProof:
        matching = tuple(
            item
            for item in frame.evidence
            if item.subject == constraint.subject and item.predicate == constraint.predicate
        )
        if not matching:
            return _proof(constraint, command, ConstraintOutcome.UNKNOWN, "missing", (), "missing")
        if any(_age(item, command.evaluated_at) is None for item in matching):
            event_ids = tuple(dict.fromkeys(item.source_event_id for item in matching))
            return _proof(
                constraint,
                command,
                ConstraintOutcome.UNKNOWN,
                "invalid_effective_at",
                event_ids,
                "evidence effective time is not timezone-aware",
            )
        current = tuple(
            item for item in matching if _current(item, constraint, command.evaluated_at)
        )
        if not current:
            code, detail = _unavailable_reason(matching, command)
            event_ids = tuple(dict.fromkeys(item.source_event_id for item in matching))
            return _proof(
                constraint,
                command,
                ConstraintOutcome.UNKNOWN,
                code,
                event_ids,
                detail,
            )
        event_ids = tuple(dict.fromkeys(item.source_event_id for item in current))
        if any(item.conflicted for item in current):
            return _proof(
                constraint,
                command,
                ConstraintOutcome.VIOLATED,
                "conflicting",
                event_ids,
                "conflicting evidence",
            )
        values = tuple(item.value for item in current)
        satisfied = _satisfied(constraint, values)
        outcome = ConstraintOutcome.SATISFIED if satisfied else ConstraintOutcome.VIOLATED
        code = "satisfied" if satisfied else "predicate_failed"
        message = "constraint satisfied" if satisfied else "evidence violates the constraint"
        return _proof(constraint, command, outcome, code, event_ids, message)


def _current(
    evidence: ContextEvidenceLike,
    constraint: ConstraintDefinition,
    evaluated_at: datetime,
) -> bool:
    # Packet freshness and stale flags describe the packet's projection time.
    # Solver policy must instead measure evidence at this evaluation's clock.
    age = _age(evidence, evaluated_at)
    if age is None or age < timedelta(0):
        return False
    if evidence.confidence < constraint.minimum_confidence:
        return False
    return constraint.max_age_seconds is None or age <= timedelta(
        seconds=constraint.max_age_seconds
    )


def _age(evidence: ContextEvidenceLike, evaluated_at: datetime) -> timedelta | None:
    effective_at = evidence.effective_at
    if effective_at.tzinfo is None or effective_at.utcoffset() is None:
        return None
    return evaluated_at.astimezone(UTC) - effective_at.astimezone(UTC)


def _unavailable_reason(
    evidence: tuple[ContextEvidenceLike, ...],
    command: SolveConstraints,
) -> tuple[str, str]:
    ages = tuple(_age(item, command.evaluated_at) for item in evidence)
    if any(age is None for age in ages):
        return "invalid_effective_at", "evidence effective time is not timezone-aware"
    valid_ages = tuple(age for age in ages if age is not None)
    if all(age < timedelta(0) for age in valid_ages):
        return "future_effective", "evidence is not effective yet"
    return (
        "stale_or_low_confidence",
        "no current evidence meets the constraint threshold",
    )


def _satisfied(constraint: ConstraintDefinition, values: tuple[JsonScalar, ...]) -> bool:
    if constraint.operator is ConstraintOperator.EXISTS:
        return bool(values)
    actual = {_key(value) for value in values}
    expected = {_key(value) for value in constraint.expected_values}
    if constraint.operator is ConstraintOperator.EQUALS:
        return actual == expected
    if constraint.operator is ConstraintOperator.NOT_EQUALS:
        return actual.isdisjoint(expected)
    if constraint.operator is ConstraintOperator.IN:
        return actual <= expected
    if constraint.operator is ConstraintOperator.NOT_IN:
        return actual.isdisjoint(expected)
    return False


def _key(value: JsonScalar) -> str:
    return canonical_json({"value": value})


def _proof(
    constraint: ConstraintDefinition,
    command: SolveConstraints,
    outcome: ConstraintOutcome,
    code: str,
    event_ids: tuple[str, ...],
    detail: str,
) -> ConstraintProof:
    return ConstraintProof(
        constraint_id=constraint.constraint_id,
        constraint_definition_digest=constraint.definition_digest,
        outcome=outcome,
        code=code,
        message=f"{constraint.description}: {detail}",
        evidence_event_ids=event_ids,
        evaluated_at=command.evaluated_at,
    )
