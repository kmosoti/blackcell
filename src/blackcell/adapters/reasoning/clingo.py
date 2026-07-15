from __future__ import annotations

import hashlib

import clingo

from blackcell.features.solve_constraints import (
    ConstraintDefinition,
    ConstraintEvaluation,
    ConstraintOperator,
    ConstraintOutcome,
    ConstraintSolver,
    DeterministicConstraintSolver,
    SolveConstraints,
)
from blackcell.features.solve_constraints.ports import ContextFrameLike
from blackcell.kernel import JsonScalar
from blackcell.kernel._json import canonical_json

_PARITY_FAILURE = "constraint solver parity check failed"
_DECISIVE_CODES = frozenset({"satisfied", "predicate_failed"})


class ConstraintSolverIntegrityError(RuntimeError):
    """An independent solver disagreed with Blackcell-owned policy semantics."""


class ClingoConstraintSolver:
    """Return deterministic proofs only after an independent Clingo parity check."""

    def __init__(self, reference: ConstraintSolver | None = None) -> None:
        self._reference = reference or DeterministicConstraintSolver()

    def handle(
        self,
        command: SolveConstraints,
        frame: ContextFrameLike,
    ) -> ConstraintEvaluation:
        evaluation = self._reference.handle(command, frame)
        definitions = {item.constraint_id: item for item in command.constraints}
        try:
            for proof in evaluation.proofs:
                if proof.code not in _DECISIVE_CODES:
                    continue
                definition = definitions[proof.constraint_id]
                evidence_ids = set(proof.evidence_event_ids)
                values = tuple(
                    item.value
                    for item in frame.evidence
                    if item.subject == definition.subject
                    and item.predicate == definition.predicate
                    and item.source_event_id in evidence_ids
                )
                clingo_holds = self._clingo_holds(definition, values)
                expected_outcome = (
                    ConstraintOutcome.SATISFIED if clingo_holds else ConstraintOutcome.VIOLATED
                )
                if proof.outcome is not expected_outcome:
                    raise ConstraintSolverIntegrityError(_PARITY_FAILURE)
        except ConstraintSolverIntegrityError:
            raise
        except KeyError, RuntimeError, TypeError, ValueError:
            raise ConstraintSolverIntegrityError(_PARITY_FAILURE) from None
        return evaluation

    def _clingo_holds(
        self,
        definition: ConstraintDefinition,
        values: tuple[JsonScalar, ...],
    ) -> bool:
        program = _program(definition, values)
        control = clingo.Control(["--warn=none", "--models=0"])
        control.add("base", [], program)
        control.ground([("base", [])])
        models: list[bool] = []
        with control.solve(yield_=True) as handle:
            for model in handle:
                models.append(any(symbol.name == "holds" for symbol in model.symbols(shown=True)))
            result = handle.get()
        if not result.satisfiable or len(models) != 1:
            raise ConstraintSolverIntegrityError(_PARITY_FAILURE)
        return models[0]


def _program(
    definition: ConstraintDefinition,
    values: tuple[JsonScalar, ...],
) -> str:
    actual = tuple(sorted({_atom(value) for value in values}))
    expected = tuple(sorted({_atom(value) for value in definition.expected_values}))
    facts = [*(f"actual({item})." for item in actual), *(f"expected({item})." for item in expected)]
    rules = {
        ConstraintOperator.EXISTS: ("holds :- actual(_).",),
        ConstraintOperator.EQUALS: (
            "missing_expected :- expected(X), not actual(X).",
            "unexpected_actual :- actual(X), not expected(X).",
            "holds :- not missing_expected, not unexpected_actual.",
        ),
        ConstraintOperator.NOT_EQUALS: (
            "overlap :- actual(X), expected(X).",
            "holds :- not overlap.",
        ),
        ConstraintOperator.IN: (
            "outside :- actual(X), not expected(X).",
            "holds :- not outside.",
        ),
        ConstraintOperator.NOT_IN: (
            "overlap :- actual(X), expected(X).",
            "holds :- not overlap.",
        ),
    }[definition.operator]
    return "\n".join((*facts, *rules, "#show holds/0."))


def _atom(value: JsonScalar) -> str:
    identity = canonical_json({"value": value}).encode()
    return "v" + hashlib.sha256(identity).hexdigest()


__all__ = ["ClingoConstraintSolver", "ConstraintSolverIntegrityError"]
