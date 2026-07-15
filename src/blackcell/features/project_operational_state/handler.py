from __future__ import annotations

from blackcell.features.project_operational_state.command import ProjectOperationalState
from blackcell.features.project_operational_state.fold import OperationalStateFold
from blackcell.features.project_operational_state.models import OperationalBeliefState
from blackcell.features.project_operational_state.ports import (
    EventHistory,
    ProjectionCheckpoints,
)
from blackcell.kernel import EventEnvelope, ProjectionCheckpoint, ProjectionRunner


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
        if command.as_of_position is not None:
            return self._rebuild_historical(fold, command)

        stored_checkpoint = self._checkpoints.load(fold.name, fold.version)
        if stored_checkpoint is not None:
            self._validate_ledger_anchor(stored_checkpoint)

        after_position = (
            stored_checkpoint.last_global_position if stored_checkpoint is not None else 0
        )
        suffix = tuple(self._events.read_all(after_position=after_position))
        result = self._runner.replay(fold, suffix, checkpoint=stored_checkpoint)
        if stored_checkpoint is None or result.processed_events:
            self._checkpoints.save(
                result.checkpoint(fold),
                expected_position=after_position,
            )
        return fold.materialize(
            result.state,
            cutoff_global_position=result.last_global_position,
            as_of_time=command.as_of_time,
        )

    def _rebuild_historical(
        self,
        fold: OperationalStateFold,
        command: ProjectOperationalState,
    ) -> OperationalBeliefState:
        cutoff = command.as_of_position
        if cutoff is None:  # pragma: no cover - private call contract
            raise ValueError("historical projection requires as_of_position")
        prefix: tuple[EventEnvelope, ...] = (
            () if cutoff == 0 else tuple(self._events.read_all(after_position=0, limit=cutoff))
        )
        actual_positions = tuple(event.global_position for event in prefix)
        expected_positions = tuple(range(1, cutoff + 1))
        if actual_positions != expected_positions:
            raise ValueError("as_of_position requires an exact, complete event-ledger prefix")
        result = self._runner.replay(fold, prefix)
        if result.last_global_position != cutoff:
            raise ValueError("operational-state replay did not reach the requested ledger cutoff")
        return fold.materialize(
            result.state,
            cutoff_global_position=cutoff,
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
