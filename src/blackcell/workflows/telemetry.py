from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from enum import StrEnum
from typing import Protocol


class WorkflowSpanName(StrEnum):
    OBSERVE = "blackcell.observe"
    PROJECT_STATE = "blackcell.state.project"
    BUILD_CONTEXT = "blackcell.context.build"
    MODEL_DECIDE = "blackcell.model.decide"
    POLICY_EVALUATE = "blackcell.policy.evaluate"
    AFFORDANCE_EXECUTE = "blackcell.affordance.execute"
    OUTCOME_OBSERVE = "blackcell.outcome.observe"
    EVALUATION_GRADE = "blackcell.evaluation.grade"
    TRANSITION_COMMIT = "blackcell.transition.commit"


class WorkflowTelemetry(Protocol):
    """Workflow-owned diagnostic boundary with no domain authority."""

    def span(
        self,
        name: WorkflowSpanName,
        *,
        run_id: str,
    ) -> AbstractContextManager[None]: ...


class NullWorkflowTelemetry:
    @contextmanager
    def span(
        self,
        name: WorkflowSpanName,
        *,
        run_id: str,
    ) -> Iterator[None]:
        del name, run_id
        yield


__all__ = ["NullWorkflowTelemetry", "WorkflowSpanName", "WorkflowTelemetry"]
