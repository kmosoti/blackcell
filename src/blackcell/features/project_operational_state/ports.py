from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from blackcell.kernel import EventEnvelope, ProjectionCheckpoint


class EventHistory(Protocol):
    def read_all(
        self,
        *,
        after_position: int = 0,
        limit: int | None = None,
    ) -> Sequence[EventEnvelope]: ...


class ProjectionCheckpoints(Protocol):
    def load(
        self,
        projection_name: str,
        projection_version: int,
        *,
        stream_id: str | None = None,
    ) -> ProjectionCheckpoint | None: ...

    def save(
        self,
        checkpoint: ProjectionCheckpoint,
        *,
        expected_position: int | None = None,
    ) -> ProjectionCheckpoint: ...
