"""Replaceable reasoning adapters behind feature-owned ports."""

from blackcell.adapters.reasoning.clingo import (
    ClingoConstraintSolver,
    ConstraintSolverIntegrityError,
)

__all__ = ["ClingoConstraintSolver", "ConstraintSolverIntegrityError"]
