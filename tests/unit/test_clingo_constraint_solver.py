from __future__ import annotations

from datetime import UTC, datetime, timedelta

import clingo
import pytest

from blackcell.adapters.reasoning import (
    ClingoConstraintSolver,
    ConstraintSolverIntegrityError,
)
from blackcell.features.build_context import (
    ContextClaimIdentity,
    ContextEvidence,
    ContextFrame,
    serialize_context_evidence,
)
from blackcell.features.solve_constraints import (
    ConstraintDefinition,
    ConstraintOperator,
    ConstraintOutcome,
    DeterministicConstraintSolver,
    SolveConstraints,
)
from blackcell.kernel import JsonScalar

NOW = datetime(2026, 7, 13, 19, tzinfo=UTC)


@pytest.mark.parametrize(
    ("operator", "expected", "actual", "outcome"),
    [
        (ConstraintOperator.EXISTS, (), (True,), ConstraintOutcome.SATISFIED),
        (ConstraintOperator.EQUALS, (True,), (True,), ConstraintOutcome.SATISFIED),
        (ConstraintOperator.EQUALS, (1,), (True,), ConstraintOutcome.VIOLATED),
        (ConstraintOperator.NOT_EQUALS, ("blocked",), ("ready",), ConstraintOutcome.SATISFIED),
        (ConstraintOperator.NOT_EQUALS, ("ready",), ("ready",), ConstraintOutcome.VIOLATED),
        (ConstraintOperator.IN, ("ready", "running"), ("ready",), ConstraintOutcome.SATISFIED),
        (ConstraintOperator.IN, ("ready",), ("blocked",), ConstraintOutcome.VIOLATED),
        (ConstraintOperator.NOT_IN, ("blocked",), ("ready",), ConstraintOutcome.SATISFIED),
        (ConstraintOperator.NOT_IN, ("blocked",), ("blocked",), ConstraintOutcome.VIOLATED),
    ],
)
def test_clingo_checks_every_decisive_operator_with_exact_json_semantics(
    operator: ConstraintOperator,
    expected: tuple[JsonScalar, ...],
    actual: tuple[JsonScalar, ...],
    outcome: ConstraintOutcome,
) -> None:
    constraint = _constraint("rule", operator, expected)
    frame = _frame(*(_evidence(value, suffix=str(index)) for index, value in enumerate(actual)))
    command = SolveConstraints(NOW, (constraint,))

    reference = DeterministicConstraintSolver().handle(command, frame)
    promoted = ClingoConstraintSolver().handle(command, frame)

    assert promoted == reference
    assert promoted.proofs[0].outcome is outcome
    assert promoted.proofs[0].proof_id == reference.proofs[0].proof_id
    assert promoted.proofs[0].message == reference.proofs[0].message


def test_blackcell_policy_owns_missing_stale_conflict_and_provenance_semantics() -> None:
    command = SolveConstraints(
        NOW,
        (
            _constraint("missing", ConstraintOperator.EXISTS),
            _constraint("stale", ConstraintOperator.EXISTS, predicate="stale"),
            _constraint("conflict", ConstraintOperator.EXISTS, predicate="conflict"),
        ),
    )
    frame = _frame(
        _evidence(True, predicate="stale", effective_at=NOW - timedelta(hours=2)),
        _evidence(True, predicate="conflict", conflicted=True),
    )

    reference = DeterministicConstraintSolver().handle(command, frame)
    promoted = ClingoConstraintSolver().handle(command, frame)

    assert promoted == reference
    assert tuple(item.code for item in promoted.proofs) == (
        "missing",
        "stale_or_low_confidence",
        "conflicting",
    )
    assert promoted.proofs[2].evidence_event_ids == ("event:conflict:0",)


def test_parity_drift_fails_closed_without_evidence_content() -> None:
    class DriftingSolver(ClingoConstraintSolver):
        def _clingo_holds(
            self,
            definition: ConstraintDefinition,
            values: tuple[JsonScalar, ...],
        ) -> bool:
            del definition, values
            return False

    command = SolveConstraints(
        NOW,
        (_constraint("clean", ConstraintOperator.EQUALS, (True,)),),
    )

    with pytest.raises(ConstraintSolverIntegrityError) as captured:
        DriftingSolver().handle(command, _frame(_evidence(True)))

    assert str(captured.value) == "constraint solver parity check failed"
    assert "True" not in str(captured.value)
    assert "clean" not in str(captured.value)


def test_promoted_dependency_matches_recorded_compatibility_probe() -> None:
    assert clingo.__version__ == "5.8.0"


def _constraint(
    identifier: str,
    operator: ConstraintOperator,
    values: tuple[JsonScalar, ...] = (),
    *,
    predicate: str = "value",
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
    value: JsonScalar,
    *,
    suffix: str = "0",
    predicate: str = "value",
    effective_at: datetime = NOW,
    conflicted: bool = False,
) -> ContextEvidence:
    return ContextEvidence(
        claim_id=f"claim:{predicate}:{suffix}",
        subject="project:blackcell",
        predicate=predicate,
        value=value,
        confidence=0.9,
        effective_at=effective_at,
        freshness_seconds=max(0, int((NOW - effective_at).total_seconds())),
        stale=effective_at < NOW - timedelta(hours=1),
        source_event_id=f"event:{predicate}:{suffix}",
        domain="repository",
        stream_id="observations:test",
        stream_sequence=1,
        global_position=1,
        relevance_score=100,
        selection_reasons=("required",),
        conflicted=conflicted,
    )


def _frame(*evidence: ContextEvidence) -> ContextFrame:
    payload = "\n".join(serialize_context_evidence(item) for item in evidence)
    return ContextFrame(
        task_id="task:1",
        objective="evaluate policy parity",
        generated_at=NOW,
        source_packet_id="packet:1",
        source_packet_purpose="test",
        source_selection_id="selection:1",
        state_domain="repository",
        state_stream_id="observations:test",
        state_global_position=1,
        state_stream_position=1,
        source_claim_identities=tuple(
            sorted(ContextClaimIdentity(item.source_event_id, item.claim_id) for item in evidence)
        ),
        evidence=evidence,
        provenance_event_ids=tuple(item.source_event_id for item in evidence),
        omissions=(),
        model_payload_characters=len(payload),
    )
