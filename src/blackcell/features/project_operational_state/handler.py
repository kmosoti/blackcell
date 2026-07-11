from __future__ import annotations

from blackcell.features.project_operational_state.command import ProjectOperationalState
from blackcell.features.project_operational_state.fold import OperationalStateFold
from blackcell.features.project_operational_state.models import OperationalBeliefState
from blackcell.features.project_operational_state.ports import (
    EventHistory,
    ProjectionCheckpoints,
)
from blackcell.kernel import ProjectionCheckpoint, ProjectionRunner


class ProjectOperationalStateHandler:
    """Resume a raw fold, then derive a time-specific state from immutable events."""

    def __init__(
        self,
        events: EventHistory,
        checkpoints: ProjectionCheckpoints,
    ) -> None:
        self._events = events
        self._checkpoints = checkpoints
        self._runner = ProjectionRunner()

    def handle(self, command: ProjectOperationalState) -> OperationalBeliefState:
        fold = OperationalStateFold(command.scope)
        checkpoint = self._checkpoints.load(fold.name, fold.version)
        if checkpoint is not None:
            self._validate_ledger_anchor(checkpoint)
        after_position = checkpoint.last_global_position if checkpoint is not None else 0
        suffix = tuple(self._events.read_all(after_position=after_position))
        result = self._runner.replay(fold, suffix, checkpoint=checkpoint)
        if checkpoint is None or result.processed_events:
            self._checkpoints.save(
                result.checkpoint(fold),
                expected_position=after_position,
            )
        return fold.materialize(
            result.state,
            cutoff_global_position=result.last_global_position,
            as_of_time=command.as_of_time,
        )

    def _validate_ledger_anchor(self, checkpoint: ProjectionCheckpoint) -> None:
        if checkpoint.last_global_position == 0:
            return
        anchor = tuple(
            self._events.read_all(
                after_position=checkpoint.last_global_position - 1,
                limit=1,
            )
        )
        if not anchor or anchor[0].global_position != checkpoint.last_global_position:
            raise ValueError("operational-state checkpoint exceeds its event ledger")
