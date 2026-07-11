from datetime import UTC, datetime, timedelta, timezone

import pytest

from blackcell.features.build_context import ContextEvidence, ContextFrame
from blackcell.features.solve_constraints import (
    ConstraintDefinition,
    ConstraintEvaluation,
    ConstraintOperator,
    ConstraintOutcome,
    ConstraintProof,
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


def test_solver_recomputes_freshness_at_the_evaluation_time() -> None:
    effective_at = NOW.astimezone(timezone(timedelta(hours=-5)))
    evidence = _evidence(
        "git.clean",
        True,
        effective_at=effective_at,
        freshness_seconds=0,
    )
    constraint = _constraint("clean", "git.clean", ConstraintOperator.EQUALS, (True,))
    solver = DeterministicConstraintSolver()

    at_boundary = solver.handle(
        SolveConstraints(NOW + timedelta(seconds=3_600), (constraint,)),
        _frame(evidence),
    )
    past_boundary = solver.handle(
        SolveConstraints(
            NOW + timedelta(seconds=3_600, microseconds=1),
            (constraint,),
        ),
        _frame(evidence),
    )

    assert at_boundary.proofs[0].outcome is ConstraintOutcome.SATISFIED
    assert past_boundary.proofs[0].outcome is ConstraintOutcome.UNKNOWN
    assert past_boundary.proofs[0].code == "stale_or_low_confidence"


def test_solver_does_not_trust_frozen_staleness_metadata() -> None:
    evaluation = DeterministicConstraintSolver().handle(
        SolveConstraints(
            NOW,
            (_constraint("clean", "git.clean", ConstraintOperator.EXISTS),),
        ),
        _frame(
            _evidence(
                "git.clean",
                True,
                stale=True,
                effective_at=NOW,
                freshness_seconds=99_999,
            )
        ),
    )

    assert evaluation.proofs[0].outcome is ConstraintOutcome.SATISFIED


def test_solver_treats_future_effective_evidence_as_unknown() -> None:
    evaluation = DeterministicConstraintSolver().handle(
        SolveConstraints(
            NOW,
            (_constraint("clean", "git.clean", ConstraintOperator.EXISTS),),
        ),
        _frame(
            _evidence(
                "git.clean",
                True,
                effective_at=NOW + timedelta(microseconds=1),
            )
        ),
    )

    assert evaluation.proofs[0].outcome is ConstraintOutcome.UNKNOWN
    assert evaluation.proofs[0].code == "future_effective"


def test_solver_fails_closed_for_naive_evidence_effective_time() -> None:
    evaluation = DeterministicConstraintSolver().handle(
        SolveConstraints(
            NOW,
            (_constraint("clean", "git.clean", ConstraintOperator.EXISTS),),
        ),
        _frame(_evidence("git.clean", True, effective_at=NOW.replace(tzinfo=None))),
    )

    assert evaluation.proofs[0].outcome is ConstraintOutcome.UNKNOWN
    assert evaluation.proofs[0].code == "invalid_effective_at"

    mixed_evaluation = DeterministicConstraintSolver().handle(
        SolveConstraints(
            NOW,
            (_constraint("clean", "git.clean", ConstraintOperator.EXISTS),),
        ),
        _frame(
            _evidence("git.clean", True),
            _evidence(
                "git.clean",
                False,
                effective_at=NOW.replace(tzinfo=None),
                source_event_id="event:git.clean:malformed",
            ),
        ),
    )

    assert mixed_evaluation.proofs[0].outcome is ConstraintOutcome.UNKNOWN
    assert mixed_evaluation.proofs[0].code == "invalid_effective_at"


def test_solver_uses_json_value_semantics() -> None:
    evaluation = DeterministicConstraintSolver().handle(
        SolveConstraints(
            NOW,
            (_constraint("boolean", "value", ConstraintOperator.EQUALS, (1,)),),
        ),
        _frame(_evidence("value", True)),
    )

    assert evaluation.proofs[0].outcome is ConstraintOutcome.VIOLATED


def test_proof_and_evaluation_identity_bind_complete_rule_semantics() -> None:
    equality = _constraint("rule", "value", ConstraintOperator.EQUALS, (True,))
    membership = _constraint("rule", "value", ConstraintOperator.IN, (False, True))
    solver = DeterministicConstraintSolver()
    frame = _frame(_evidence("value", True))

    equality_evaluation = solver.handle(SolveConstraints(NOW, (equality,)), frame)
    membership_evaluation = solver.handle(SolveConstraints(NOW, (membership,)), frame)
    equality_proof = equality_evaluation.proofs[0]
    membership_proof = membership_evaluation.proofs[0]

    assert equality_proof.outcome is membership_proof.outcome
    assert equality_proof.code == membership_proof.code
    assert equality.definition_digest != membership.definition_digest
    assert equality_proof.constraint_definition_digest == equality.definition_digest
    assert membership_proof.constraint_definition_digest == membership.definition_digest
    assert equality_proof.schema_version == "constraint-proof/v2"
    assert equality_proof.proof_id != membership_proof.proof_id
    assert equality_evaluation.evaluation_id != membership_evaluation.evaluation_id


def test_definition_digest_uses_constraint_value_set_semantics() -> None:
    first = _constraint("membership", "value", ConstraintOperator.IN, (False, True))
    reordered = _constraint(
        "membership",
        "value",
        ConstraintOperator.IN,
        (True, False, True),
    )

    assert first.definition_digest == reordered.definition_digest


def test_constraint_artifacts_reject_empty_or_unbounded_identity() -> None:
    definition = _constraint("clean", "git.clean", ConstraintOperator.EXISTS)
    proof = ConstraintProof(
        "clean",
        definition.definition_digest,
        ConstraintOutcome.UNKNOWN,
        "missing",
        "required evidence is missing",
        (),
        NOW,
    )

    with pytest.raises(ValueError, match="constraint_id must not be empty"):
        ConstraintProof(
            "",
            definition.definition_digest,
            ConstraintOutcome.UNKNOWN,
            "missing",
            "required evidence is missing",
            (),
            NOW,
        )
    with pytest.raises(ValueError, match="constraint_definition_digest must not be empty"):
        ConstraintProof(
            "clean",
            "",
            ConstraintOutcome.UNKNOWN,
            "missing",
            "required evidence is missing",
            (),
            NOW,
        )
    with pytest.raises(ValueError, match="evaluated_at must be timezone-aware"):
        ConstraintProof(
            "clean",
            definition.definition_digest,
            ConstraintOutcome.UNKNOWN,
            "missing",
            "required evidence is missing",
            (),
            NOW.replace(tzinfo=None),
        )
    with pytest.raises(ValueError, match="context_frame_id must not be empty"):
        ConstraintEvaluation("", (proof,), NOW)
    with pytest.raises(ValueError, match="requires at least one proof"):
        ConstraintEvaluation("frame:1", (), NOW)


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


def _evidence(
    predicate: str,
    value,
    *,
    stale: bool = False,
    conflicted: bool = False,
    effective_at: datetime | None = None,
    freshness_seconds: int | None = None,
    source_event_id: str | None = None,
):
    observed_at = effective_at or (NOW - timedelta(seconds=7_200) if stale else NOW)
    return ContextEvidence(
        "project:blackcell",
        predicate,
        value,
        0.9,
        observed_at,
        freshness_seconds if freshness_seconds is not None else (7_200 if stale else 0),
        stale,
        source_event_id or f"event:{predicate}",
        100,
        ("required",),
        conflicted,
    )


def _frame(*evidence: ContextEvidence) -> ContextFrame:
    return ContextFrame(
        task_id="task:1",
        objective="safely update project",
        generated_at=NOW,
        state_position=1,
        source_packet_id="packet:1",
        source_selection_id="selection:1",
        evidence=evidence,
        provenance_event_ids=tuple(item.source_event_id for item in evidence),
        omissions=(),
        serialized_characters=100,
    )
