from __future__ import annotations

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
        event_ids = tuple(dict.fromkeys(item.source_event_id for item in matching))
        if not matching:
            return _proof(constraint, command, ConstraintOutcome.UNKNOWN, "missing", (), "missing")
        if any(item.conflicted for item in matching):
            return _proof(
                constraint,
                command,
                ConstraintOutcome.VIOLATED,
                "conflicting",
                event_ids,
                "conflicting evidence",
            )
        current = tuple(item for item in matching if _current(item, constraint))
        if not current:
            return _proof(
                constraint,
                command,
                ConstraintOutcome.UNKNOWN,
                "stale_or_low_confidence",
                event_ids,
                "no current evidence meets the constraint threshold",
            )
        values = tuple(item.value for item in current)
        satisfied = _satisfied(constraint, values)
        outcome = ConstraintOutcome.SATISFIED if satisfied else ConstraintOutcome.VIOLATED
        code = "satisfied" if satisfied else "predicate_failed"
        message = "constraint satisfied" if satisfied else "evidence violates the constraint"
        return _proof(constraint, command, outcome, code, event_ids, message)


def _current(evidence: ContextEvidenceLike, constraint: ConstraintDefinition) -> bool:
    if evidence.stale or evidence.confidence < constraint.minimum_confidence:
        return False
    return (
        constraint.max_age_seconds is None
        or evidence.freshness_seconds <= constraint.max_age_seconds
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
        constraint.constraint_id,
        outcome,
        code,
        f"{constraint.description}: {detail}",
        event_ids,
        command.evaluated_at,
    )
