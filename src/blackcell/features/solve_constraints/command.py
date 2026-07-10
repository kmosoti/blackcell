from dataclasses import dataclass
from datetime import datetime

from blackcell.features.solve_constraints.models import ConstraintDefinition


@dataclass(frozen=True, slots=True)
class SolveConstraints:
    evaluated_at: datetime
    constraints: tuple[ConstraintDefinition, ...]

    def __post_init__(self) -> None:
        if self.evaluated_at.tzinfo is None or self.evaluated_at.utcoffset() is None:
            raise ValueError("evaluated_at must be timezone-aware")
        if not self.constraints:
            raise ValueError("constraint solving requires at least one constraint")
        identifiers = tuple(item.constraint_id for item in self.constraints)
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("constraint ids must be unique")
