from __future__ import annotations

from dataclasses import dataclass, field
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
    RunQueryItem,
    RunQueryRequest,
    RunQueryResponse,
    RunRequest,
    RunResponse,
    RunSurfaceWindow,
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


@dataclass(frozen=True, slots=True)
class RuntimeArtifactPayload:
    digest: str
    size_bytes: int
    media_type: str
    encoding: str | None
    content: bytes = field(repr=False)

    def __post_init__(self) -> None:
        hexadecimal = self.digest.removeprefix("sha256:")
        if (
            not self.digest.startswith("sha256:")
            or len(hexadecimal) != 64
            or any(character not in "0123456789abcdef" for character in hexadecimal)
            or isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or self.size_bytes < 0
            or len(self.content) != self.size_bytes
            or not self.media_type.strip()
        ):
            raise ValueError("invalid runtime artifact payload")


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

    def presentation_run_window(self, *, limit: int) -> RunSurfaceWindow: ...

    def presentation_run_item(self, run_id: str) -> RunQueryItem: ...

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

    def read_run_artifact(self, run_id: str, digest: str) -> RuntimeArtifactPayload: ...


__all__ = [
    "RuntimeApiError",
    "RuntimeApiFailureCode",
    "RuntimeApiPort",
    "RuntimeArtifactPayload",
]
