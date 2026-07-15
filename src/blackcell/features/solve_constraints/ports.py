from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from blackcell.features.solve_constraints.command import SolveConstraints
from blackcell.features.solve_constraints.models import ConstraintEvaluation
from blackcell.kernel import JsonScalar


class ContextEvidenceLike(Protocol):
    @property
    def subject(self) -> str: ...

    @property
    def predicate(self) -> str: ...

    @property
    def value(self) -> JsonScalar: ...

    @property
    def confidence(self) -> float: ...

    @property
    def effective_at(self) -> datetime: ...

    @property
    def source_event_id(self) -> str: ...

    @property
    def conflicted(self) -> bool: ...


class ContextFrameLike(Protocol):
    @property
    def frame_id(self) -> str: ...

    @property
    def evidence(self) -> Sequence[ContextEvidenceLike]: ...


class ConstraintSolver(Protocol):
    def handle(
        self,
        command: SolveConstraints,
        frame: ContextFrameLike,
    ) -> ConstraintEvaluation: ...
