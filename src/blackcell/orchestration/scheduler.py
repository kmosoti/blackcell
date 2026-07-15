from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from blackcell.orchestration.models import (
    DagDefinition,
    DagNode,
    NodeStatus,
    NodeUsage,
    OrchestrationRole,
    OrchestrationRunStatus,
)


class OrchestrationSchedulerError(RuntimeError):
    pass


class OrchestrationRunConflict(OrchestrationSchedulerError):
    pass


class OrchestrationApprovalConflict(OrchestrationSchedulerError):
    pass


class OrchestrationLeaseConflict(OrchestrationSchedulerError):
    pass


class OrchestrationResultConflict(OrchestrationSchedulerError):
    pass


class OrchestrationSchedulerIntegrityError(OrchestrationSchedulerError):
    pass


@dataclass(frozen=True, slots=True)
class OrchestrationApproval:
    node_id: str
    role: OrchestrationRole
    principal_id: str
    approved: bool
    decided_at: datetime
    decision_digest: str

    def __post_init__(self) -> None:
        if not self.node_id.strip() or not self.principal_id.strip():
            raise ValueError("approval identities must not be empty")
        _require_aware(self.decided_at, "decided_at")


@dataclass(frozen=True, slots=True)
class OrchestrationNodeLease:
    run_id: str
    node: DagNode
    attempt: int
    fencing_token: int
    worker_id: str
    acquired_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        if not self.run_id.strip() or not self.worker_id.strip():
            raise ValueError("lease identities must not be empty")
        if self.attempt < 1 or self.fencing_token < 1:
            raise ValueError("lease attempt and fencing token must be positive")
        _require_aware(self.acquired_at, "acquired_at")
        _require_aware(self.expires_at, "expires_at")
        if self.expires_at <= self.acquired_at:
            raise ValueError("lease expiry must follow acquisition")


@dataclass(frozen=True, slots=True)
class OrchestrationNodeSnapshot:
    node_id: str
    status: NodeStatus
    attempts: int
    fencing_token: int
    available_at: datetime
    lease_worker_id: str | None
    lease_acquired_at: datetime | None
    lease_expires_at: datetime | None
    result_digest: str | None
    failure_code: str | None
    usage: NodeUsage
    updated_at: datetime

    def __post_init__(self) -> None:
        if not self.node_id.strip() or self.attempts < 0 or self.fencing_token < 0:
            raise ValueError("node snapshot identity and counters are invalid")
        _require_aware(self.available_at, "available_at")
        _require_aware(self.updated_at, "updated_at")
        lease_values = (
            self.lease_worker_id,
            self.lease_acquired_at,
            self.lease_expires_at,
        )
        if self.status is NodeStatus.RUNNING:
            if any(item is None for item in lease_values):
                raise ValueError("running nodes require complete lease evidence")
        elif any(item is not None for item in lease_values):
            raise ValueError("only running nodes may retain lease evidence")
        if self.lease_acquired_at is not None:
            _require_aware(self.lease_acquired_at, "lease_acquired_at")
        if self.lease_expires_at is not None:
            _require_aware(self.lease_expires_at, "lease_expires_at")


@dataclass(frozen=True, slots=True)
class OrchestrationRunSnapshot:
    run_id: str
    definition: DagDefinition
    status: OrchestrationRunStatus
    nodes: tuple[OrchestrationNodeSnapshot, ...]
    approvals: tuple[OrchestrationApproval, ...]
    submitted_by: str
    submitted_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if not self.run_id.strip() or not self.submitted_by.strip():
            raise ValueError("run snapshot identities must not be empty")
        _require_aware(self.submitted_at, "submitted_at")
        _require_aware(self.updated_at, "updated_at")
        if tuple(sorted(self.nodes, key=lambda item: item.node_id)) != self.nodes:
            raise ValueError("run snapshot nodes must use stable order")


class OrchestrationSchedulerPort(Protocol):
    def submit(
        self,
        run_id: str,
        definition: DagDefinition,
        *,
        submitted_by: str,
        submitted_at: datetime | None = None,
    ) -> OrchestrationRunSnapshot: ...

    def record_approval(
        self,
        run_id: str,
        node_id: str,
        role: OrchestrationRole,
        *,
        principal_id: str,
        approved: bool,
        decided_at: datetime | None = None,
    ) -> OrchestrationApproval: ...

    def acquire(
        self,
        worker_id: str,
        *,
        lease_seconds: int,
        acquired_at: datetime | None = None,
    ) -> OrchestrationNodeLease | None: ...

    def complete(
        self,
        lease: OrchestrationNodeLease,
        *,
        result_digest: str,
        output_schema: str,
        usage: NodeUsage,
        completed_at: datetime | None = None,
    ) -> OrchestrationNodeSnapshot: ...

    def fail(
        self,
        lease: OrchestrationNodeLease,
        *,
        failure_code: str,
        usage: NodeUsage,
        failed_at: datetime | None = None,
    ) -> OrchestrationNodeSnapshot: ...

    def recover_expired(
        self,
        *,
        recovered_at: datetime | None = None,
    ) -> tuple[OrchestrationNodeSnapshot, ...]: ...

    def inspect(self, run_id: str) -> OrchestrationRunSnapshot: ...


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


__all__ = [
    "OrchestrationApproval",
    "OrchestrationApprovalConflict",
    "OrchestrationLeaseConflict",
    "OrchestrationNodeLease",
    "OrchestrationNodeSnapshot",
    "OrchestrationResultConflict",
    "OrchestrationRunConflict",
    "OrchestrationRunSnapshot",
    "OrchestrationSchedulerError",
    "OrchestrationSchedulerIntegrityError",
    "OrchestrationSchedulerPort",
]
