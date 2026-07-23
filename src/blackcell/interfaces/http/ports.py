from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from blackcell.interfaces.http.alpha_contracts import (
    AlphaCancelRunRequest,
    AlphaEventPageResponse,
    AlphaIntentRequest,
    AlphaIntentResponse,
    AlphaPlanRequest,
    AlphaPlanResponse,
    AlphaProjectRequest,
    AlphaProjectResponse,
    AlphaReplayResponse,
    AlphaRunRequest,
    AlphaRunResponse,
)
from blackcell.interfaces.http.contracts import (
    ApprovalRequest,
    ContextResponse,
    EvaluationResponse,
    EventPageResponse,
    HealthResponse,
    ObservationIngestRequest,
    ObservationIngestResponse,
    OrchestrationApprovalResponse,
    OrchestrationRunResponse,
    ReplayResponse,
    RunResponse,
    RunSubmissionRequest,
)


class RuntimeApiFailureCode(StrEnum):
    INVALID_REQUEST = "invalid-request"
    NOT_FOUND = "not-found"
    CONFLICT = "conflict"
    NOT_READY = "not-ready"
    STORAGE_QUOTA_EXCEEDED = "storage-quota-exceeded"


class RuntimeApiError(RuntimeError):
    def __init__(self, code: RuntimeApiFailureCode) -> None:
        self.code = code
        super().__init__(code.value)


class RuntimeApiPort(Protocol):
    def readiness(self) -> HealthResponse: ...

    def ingest_observations(
        self,
        request: ObservationIngestRequest,
        *,
        principal_id: str,
    ) -> ObservationIngestResponse: ...

    def submit_run(
        self,
        request: RunSubmissionRequest,
        *,
        principal_id: str,
    ) -> RunResponse: ...

    def inspect_run(self, run_id: str) -> RunResponse: ...

    def inspect_context(self, run_id: str) -> ContextResponse: ...

    def replay_run(self, run_id: str) -> ReplayResponse: ...

    def inspect_evaluation(self, run_id: str) -> EvaluationResponse: ...

    def list_events(self, *, after_position: int, limit: int) -> EventPageResponse: ...

    def inspect_orchestration(self, run_id: str) -> OrchestrationRunResponse: ...

    def record_orchestration_approval(
        self,
        run_id: str,
        node_id: str,
        request: ApprovalRequest,
        *,
        principal_id: str,
    ) -> OrchestrationApprovalResponse: ...


class AlphaRuntimeApiPort(Protocol):
    def register_alpha_project(
        self,
        request: AlphaProjectRequest,
        *,
        principal_id: str,
    ) -> AlphaProjectResponse: ...

    def accept_alpha_intent(
        self,
        request: AlphaIntentRequest,
        *,
        principal_id: str,
    ) -> AlphaIntentResponse: ...

    def accept_alpha_plan(
        self,
        request: AlphaPlanRequest,
        *,
        principal_id: str,
    ) -> AlphaPlanResponse: ...

    def submit_alpha_run(
        self,
        request: AlphaRunRequest,
        *,
        principal_id: str,
    ) -> AlphaRunResponse: ...

    def inspect_alpha_run(self, run_id: str) -> AlphaRunResponse: ...

    def cancel_alpha_run(
        self,
        run_id: str,
        request: AlphaCancelRunRequest,
        *,
        principal_id: str,
    ) -> AlphaRunResponse: ...

    def list_alpha_events(
        self,
        *,
        after_cursor: int,
        limit: int,
    ) -> AlphaEventPageResponse: ...

    def replay_alpha_run(self, run_id: str) -> AlphaReplayResponse: ...


__all__ = [
    "AlphaRuntimeApiPort",
    "RuntimeApiError",
    "RuntimeApiFailureCode",
    "RuntimeApiPort",
]
