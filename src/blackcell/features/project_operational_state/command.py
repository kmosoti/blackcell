from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from blackcell.features.project_operational_state.models import OperationalStateScope


@dataclass(frozen=True, slots=True)
class ProjectOperationalState:
    scope: OperationalStateScope
    as_of_time: datetime | None = None
    as_of_position: int | None = None

    def __post_init__(self) -> None:
        if not self.scope.bound:
            raise ValueError("incremental operational-state projection requires a bound scope")
        if self.as_of_time is not None and (
            self.as_of_time.tzinfo is None or self.as_of_time.utcoffset() is None
        ):
            raise ValueError("as_of_time must be timezone-aware")
        if self.as_of_position is not None and (
            isinstance(self.as_of_position, bool)
            or not isinstance(self.as_of_position, int)
            or self.as_of_position < 0
        ):
            raise ValueError("as_of_position must be a non-negative integer")
