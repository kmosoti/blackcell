from datetime import UTC, datetime

from blackcell.features.build_context import ContextEvidence, ContextFrame
from blackcell.features.solve_constraints import (
    ConstraintDefinition,
    ConstraintOperator,
    ConstraintOutcome,
    DeterministicConstraintSolver,
    SolveConstraints,
)

NOW = datetime(2026, 7, 10, 19, tzinfo=UTC)


def test_solver_produces_evidence_linked_satisfied_and_violated_proofs() -> None:
    frame = _frame(_evidence("git.clean", True), _evidence("status", "blocked"))
    command = SolveConstraints(
        NOW,
        (
            _constraint("clean", "git.clean", ConstraintOperator.EQUALS, (True,)),
            _constraint("unblocked", "status", ConstraintOperator.NOT_EQUALS, ("blocked",)),
        ),
    )

    evaluation = DeterministicConstraintSolver().handle(command, frame)

    assert tuple(proof.outcome for proof in evaluation.proofs) == (
        ConstraintOutcome.SATISFIED,
        ConstraintOutcome.VIOLATED,
    )
    assert evaluation.proofs[0].evidence_event_ids == ("event:git.clean",)
    assert evaluation.proofs[1].code == "predicate_failed"
    assert not evaluation.safe
    assert evaluation.evaluation_id.startswith("sha256:")


def test_solver_treats_missing_stale_and_conflicting_evidence_explicitly() -> None:
    frame = _frame(
        _evidence("stale", True, stale=True),
        _evidence("conflict", True, conflicted=True),
    )
    command = SolveConstraints(
        NOW,
        (
            _constraint("missing", "unknown", ConstraintOperator.EXISTS),
            _constraint("stale", "stale", ConstraintOperator.EXISTS),
            _constraint("conflict", "conflict", ConstraintOperator.EXISTS),
        ),
    )

    evaluation = DeterministicConstraintSolver().handle(command, frame)

    assert tuple(proof.outcome for proof in evaluation.proofs) == (
        ConstraintOutcome.UNKNOWN,
        ConstraintOutcome.UNKNOWN,
        ConstraintOutcome.VIOLATED,
    )
    assert tuple(proof.code for proof in evaluation.proofs) == (
        "missing",
        "stale_or_low_confidence",
        "conflicting",
    )


def test_solver_uses_json_value_semantics() -> None:
    evaluation = DeterministicConstraintSolver().handle(
        SolveConstraints(
            NOW,
            (_constraint("boolean", "value", ConstraintOperator.EQUALS, (1,)),),
        ),
        _frame(_evidence("value", True)),
    )

    assert evaluation.proofs[0].outcome is ConstraintOutcome.VIOLATED


def _constraint(
    identifier: str,
    predicate: str,
    operator: ConstraintOperator,
    values=(),
) -> ConstraintDefinition:
    return ConstraintDefinition(
        identifier,
        f"constraint {identifier}",
        "project:blackcell",
        predicate,
        operator,
        values,
        minimum_confidence=0.5,
        max_age_seconds=3_600,
    )


def _evidence(predicate: str, value, *, stale: bool = False, conflicted: bool = False):
    return ContextEvidence(
        "project:blackcell",
        predicate,
        value,
        0.9,
        NOW,
        7_200 if stale else 0,
        stale,
        f"event:{predicate}",
        100,
        ("required",),
        conflicted,
    )


def _frame(*evidence: ContextEvidence) -> ContextFrame:
    return ContextFrame(
        "task:1",
        "safely update project",
        NOW,
        1,
        "packet:1",
        "selection:1",
        evidence,
        tuple(item.source_event_id for item in evidence),
        0,
        100,
    )
