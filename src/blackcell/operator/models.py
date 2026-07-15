from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class CanonicalOperatorRunResult:
    """Compact product view of one replay-verified Daily Operator v2 run."""

    run_id: str
    status: str
    outcome: str | None
    workflow_version: str | None
    repository_stream_id: str
    run_stream_id: str
    context_frame_id: str | None
    authorization_outcome: str | None
    execution_status: str | None
    evaluation_verdict: str | None
    transition_recorded: bool
    run_event_count: int
    artifact_count: int
    schema_version: str = "canonical-operator-run-result/v1"

    def __post_init__(self) -> None:
        for name in ("run_id", "status", "repository_stream_id", "run_stream_id"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        if self.run_event_count < 1 or self.artifact_count < 0:
            raise ValueError("operator run counts must be non-negative")


@dataclass(frozen=True, slots=True)
class StoredContextFrame:
    run_id: str
    frame_id: str
    artifact_digest: str
    payload: Mapping[str, Any]
    schema_version: str = "stored-context-frame/v1"
