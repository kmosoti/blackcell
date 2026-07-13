from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from blackcell.telemetry import TraceRecorder
from blackcell.workflows.telemetry import WorkflowSpanName


class TraceWorkflowTelemetry:
    """Adapt workflow phase observations to BlackCell's sanitized recorder."""

    def __init__(self, recorder: TraceRecorder) -> None:
        self._recorder = recorder

    @contextmanager
    def span(self, name: WorkflowSpanName, *, run_id: str) -> Iterator[None]:
        with self._recorder.span(
            name.value,
            trace_id=run_id,
            correlation_ids={"run_id": run_id},
            attributes={"workflow.version": "daily-operator/v2"},
        ):
            yield


__all__ = ["TraceWorkflowTelemetry"]
