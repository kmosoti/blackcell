from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from blackcell.interfaces.http.contracts import (
    CancelRunRequest,
    HealthResponse,
    IntentRequest,
    IntentResponse,
    PlanRequest,
    PlanResponse,
    ProjectRequest,
    ProjectResponse,
    ReplayResponse,
    RunQueryRequest,
    RunQueryResponse,
    RunRequest,
    RunResponse,
    RuntimeEventPageResponse,
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

    def register_project(
        self,
        request: ProjectRequest,
        *,
        principal_id: str,
    ) -> ProjectResponse: ...

    def accept_intent(
        self,
        request: IntentRequest,
        *,
        principal_id: str,
    ) -> IntentResponse: ...

    def accept_plan(
        self,
        request: PlanRequest,
        *,
        principal_id: str,
    ) -> PlanResponse: ...

    def submit_run(
        self,
        request: RunRequest,
        *,
        principal_id: str,
    ) -> RunResponse: ...

    def inspect_run(self, run_id: str) -> RunResponse: ...

    def query_runs(self, request: RunQueryRequest) -> RunQueryResponse: ...

    def cancel_run(
        self,
        run_id: str,
        request: CancelRunRequest,
        *,
        principal_id: str,
    ) -> RunResponse: ...

    def list_events(
        self,
        *,
        after_cursor: int,
        limit: int,
    ) -> RuntimeEventPageResponse: ...

    def replay_run(self, run_id: str) -> ReplayResponse: ...


__all__ = ["RuntimeApiError", "RuntimeApiFailureCode", "RuntimeApiPort"]
