"""Strict event fold for the execution run execution lifecycle.

The fold grants no execution authority. It validates that persisted claims, fencing tokens,
worktree preparation, cancellation, reconciliation, and terminal outcomes form one legal history.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from itertools import pairwise
from pathlib import Path
from typing import cast

from blackcell.kernel import EventEnvelope, JsonInput, JsonValue
from blackcell.kernel._json import json_digest, thaw_json
from blackcell.orchestration.execution_plan import EXECUTION_EVENT_SOURCE, EXECUTION_EVENT_TYPES

RUNTIME_EVENT_SOURCE = "blackcell.runtime"
RUN_QUEUED = "run.queued"
NODE_CLAIMED = "node.claimed"
NODE_WORKTREE_PREPARED = "node.worktree-prepared"
NODE_PROVIDER_DISPATCH_STARTED = "node.provider-dispatch-started"
RUN_CANCEL_REQUESTED = "run.cancel-requested"
NODE_SUCCEEDED = "node.succeeded"
NODE_FAILED = "node.failed"
NODE_REQUEUED = "node.requeued"
NODE_CANCELED = "node.canceled"
NODE_RECONCILIATION_REQUIRED = "node.reconciliation-required"
NODE_WORKTREE_CLEANUP_REQUESTED = "node.worktree-cleanup-requested"
NODE_WORKTREE_CLEANED = "node.worktree-cleaned"
NODE_WORKTREE_CLEANUP_FAILED = "node.worktree-cleanup-failed"
RUN_SUCCEEDED = "run.succeeded"
RUN_FAILED = "run.failed"
RUN_CANCELED = "run.canceled"
RUN_RECONCILIATION_REQUIRED = "run.reconciliation-required"

RUN_EVENT_TYPES = frozenset(
    {
        RUN_QUEUED,
        NODE_CLAIMED,
        NODE_WORKTREE_PREPARED,
        NODE_PROVIDER_DISPATCH_STARTED,
        RUN_CANCEL_REQUESTED,
        NODE_SUCCEEDED,
        NODE_FAILED,
        NODE_REQUEUED,
        NODE_CANCELED,
        NODE_RECONCILIATION_REQUIRED,
        NODE_WORKTREE_CLEANUP_REQUESTED,
        NODE_WORKTREE_CLEANED,
        NODE_WORKTREE_CLEANUP_FAILED,
        RUN_SUCCEEDED,
        RUN_FAILED,
        RUN_CANCELED,
        RUN_RECONCILIATION_REQUIRED,
    }
)

_DIGEST_LENGTH = 71


class LifecycleError(ValueError):
    """Content-free indication that a run history violates the execution grammar."""

    def __init__(self) -> None:
        super().__init__("invalid-run-lifecycle")


class RunLifecycleStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCELING = "canceling"
    CANCELED = "canceled"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RECONCILIATION_REQUIRED = "reconciliation-required"


class NodeLifecycleStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"
    RECONCILIATION_REQUIRED = "reconciliation-required"


class WorktreeCleanupStatus(StrEnum):
    ELIGIBLE = "eligible"
    REQUESTED = "requested"
    FAILED = "failed"
    CLEANED = "cleaned"


@dataclass(frozen=True, slots=True)
class ActiveLease:
    node_id: str
    attempt: int
    fencing_token: int
    worker_id: str
    lease_digest: str
    worktree_spec_digest: str
    worktree_spec: Mapping[str, JsonValue]
    expires_at: datetime
    prepared: bool
    provider_request_id: str | None = None
    provider_context_digest: str | None = None
    provider_dispatch_event_id: str | None = None


@dataclass(frozen=True, slots=True)
class NodeLifecycleState:
    node_id: str
    status: NodeLifecycleStatus
    attempts: int
    fencing_token: int
    result_digest: str | None = None
    head_commit: str | None = None
    failure_code: str | None = None
    retained_worktree: bool = False
    lease_digest: str | None = None
    worktree_spec_digest: str | None = None
    worktree_spec: Mapping[str, JsonValue] | None = None
    cleanup_status: WorktreeCleanupStatus | None = None
    cleanup_failure_code: str | None = None


@dataclass(frozen=True, slots=True)
class RunLifecycleState:
    run_id: str
    status: RunLifecycleStatus
    nodes: tuple[NodeLifecycleState, ...]
    cancellation_requested: bool
    active_lease: ActiveLease | None
    queued_event: EventEnvelope
    latest_event: EventEnvelope
    last_stream_sequence: int

    @property
    def stream_sequence(self) -> int:
        return self.last_stream_sequence


@dataclass(slots=True)
class _MutableNode:
    node_id: str
    status: NodeLifecycleStatus = NodeLifecycleStatus.PENDING
    attempts: int = 0
    fencing_token: int = 0
    result_digest: str | None = None
    head_commit: str | None = None
    failure_code: str | None = None
    retained_worktree: bool = False
    lease_digest: str | None = None
    worktree_spec_digest: str | None = None
    worktree_spec: Mapping[str, JsonValue] | None = None
    cleanup_status: WorktreeCleanupStatus | None = None
    cleanup_failure_code: str | None = None


def fold_run_lifecycle(
    run_id: str,
    node_dependencies: Mapping[str, tuple[str, ...]],
    events: Sequence[EventEnvelope],
) -> RunLifecycleState:
    """Validate and fold one complete execution run stream."""

    if not run_id or not node_dependencies or not events:
        raise LifecycleError()
    known = set(node_dependencies)
    if any(
        not node_id
        or not isinstance(dependencies, tuple)
        or node_id in dependencies
        or not set(dependencies).issubset(known)
        for node_id, dependencies in node_dependencies.items()
    ):
        raise LifecycleError()
    nodes = {node_id: _MutableNode(node_id) for node_id in sorted(known)}
    stream_id = f"run:{run_id}"
    ordered_events = tuple(events)
    for sequence, event in enumerate(ordered_events, start=1):
        known_event = (
            event.source == RUNTIME_EVENT_SOURCE and event.event_type in RUN_EVENT_TYPES
        ) or (event.source == EXECUTION_EVENT_SOURCE and event.event_type in EXECUTION_EVENT_TYPES)
        if (
            not isinstance(event, EventEnvelope)
            or event.stream_id != stream_id
            or event.stream_sequence != sequence
            or event.schema_version != 1
            or not known_event
        ):
            raise LifecycleError()
    public_events = tuple(
        event
        for event in ordered_events
        if event.source == RUNTIME_EVENT_SOURCE and event.event_type in RUN_EVENT_TYPES
    )
    if not public_events:
        raise LifecycleError()
    queued = public_events[0]
    if queued.event_type != RUN_QUEUED:
        raise LifecycleError()
    _validate_queued(queued, run_id)
    if any(event.correlation_id != queued.correlation_id for event in public_events):
        raise LifecycleError()
    for previous, event in pairwise(ordered_events):
        if event.causation_id != previous.event_id:
            raise LifecycleError()

    status = RunLifecycleStatus.QUEUED
    cancellation_requested = False
    active: ActiveLease | None = None
    terminal = False
    reconciliation_recorded = False
    maximum_fence = 0
    for event in public_events[1:]:
        cleanup_event = event.event_type in {
            NODE_WORKTREE_CLEANUP_REQUESTED,
            NODE_WORKTREE_CLEANED,
            NODE_WORKTREE_CLEANUP_FAILED,
        }
        if terminal and not cleanup_event:
            raise LifecycleError()
        payload = _payload(event)
        _principal(payload, event)
        if event.event_type == NODE_CLAIMED:
            if (
                cancellation_requested
                or active is not None
                or status
                not in {
                    RunLifecycleStatus.QUEUED,
                    RunLifecycleStatus.RUNNING,
                }
            ):
                raise LifecycleError()
            _exact(
                payload,
                {
                    "principal_id",
                    "run_id",
                    "node_id",
                    "attempt",
                    "fencing_token",
                    "worker_id",
                    "lease_digest",
                    "expires_at",
                    "worktree_spec_digest",
                    "worktree_spec",
                    "status",
                },
            )
            _run(payload, run_id)
            node = _node(payload, nodes)
            if node.status is not NodeLifecycleStatus.PENDING or any(
                nodes[dependency].status is not NodeLifecycleStatus.SUCCEEDED
                for dependency in node_dependencies[node.node_id]
            ):
                raise LifecycleError()
            attempt = _positive_integer(payload.get("attempt"))
            fencing_token = _positive_integer(payload.get("fencing_token"))
            worker_id = _text(payload.get("worker_id"))
            lease_digest = _digest(payload.get("lease_digest"))
            spec_digest = _digest(payload.get("worktree_spec_digest"))
            expires_at = _timestamp(payload.get("expires_at"))
            spec = _mapping(payload.get("worktree_spec"))
            _exact(
                spec,
                {
                    "schema_version",
                    "lease_digest",
                    "lease",
                    "repository_root",
                    "isolation_root",
                    "base_commit",
                    "allowed_paths",
                    "max_changed_paths",
                },
            )
            if (
                attempt != node.attempts + 1
                or fencing_token != maximum_fence + 1
                or expires_at <= event.recorded_at
                or _worktree_spec_digest(spec, lease_digest) != spec_digest
                or payload.get("status") != "claimed"
            ):
                raise LifecycleError()
            raw_lease = _mapping(spec.get("lease"))
            if (
                raw_lease.get("run_id") != run_id
                or raw_lease.get("node_id") != node.node_id
                or raw_lease.get("attempt") != attempt
                or raw_lease.get("fencing_token") != fencing_token
                or raw_lease.get("worker_id") != worker_id
                or json_digest(raw_lease) != lease_digest
                or spec.get("lease_digest") != lease_digest
            ):
                raise LifecycleError()
            node.status = NodeLifecycleStatus.CLAIMED
            node.attempts = attempt
            node.fencing_token = fencing_token
            node.lease_digest = lease_digest
            node.worktree_spec_digest = spec_digest
            node.worktree_spec = spec
            node.cleanup_status = None
            node.cleanup_failure_code = None
            maximum_fence = fencing_token
            active = ActiveLease(
                node_id=node.node_id,
                attempt=attempt,
                fencing_token=fencing_token,
                worker_id=worker_id,
                lease_digest=lease_digest,
                worktree_spec_digest=spec_digest,
                worktree_spec=spec,
                expires_at=expires_at,
                prepared=False,
            )
            status = RunLifecycleStatus.RUNNING
        elif event.event_type == NODE_WORKTREE_PREPARED:
            _exact(
                payload,
                {
                    "principal_id",
                    "run_id",
                    "node_id",
                    "lease_digest",
                    "inspection_digest",
                    "inspection",
                    "status",
                },
            )
            _run(payload, run_id)
            active = _active(
                payload,
                active,
                nodes,
                require_prepared=False,
                require_worker=True,
            )
            if active.prepared or event.recorded_at > active.expires_at:
                raise LifecycleError()
            inspection = _inspection(payload, active, unchanged=True)
            del inspection
            node = nodes[active.node_id]
            node.status = NodeLifecycleStatus.RUNNING
            active = ActiveLease(
                node_id=active.node_id,
                attempt=active.attempt,
                fencing_token=active.fencing_token,
                worker_id=active.worker_id,
                lease_digest=active.lease_digest,
                worktree_spec_digest=active.worktree_spec_digest,
                worktree_spec=active.worktree_spec,
                expires_at=active.expires_at,
                prepared=True,
                provider_request_id=active.provider_request_id,
                provider_context_digest=active.provider_context_digest,
                provider_dispatch_event_id=active.provider_dispatch_event_id,
            )
            if payload.get("status") != "worktree-prepared":
                raise LifecycleError()
        elif event.event_type == NODE_PROVIDER_DISPATCH_STARTED:
            _exact(
                payload,
                {
                    "principal_id",
                    "run_id",
                    "node_id",
                    "lease_digest",
                    "provider_request_id",
                    "context_digest",
                    "context_artifact_digest",
                    "status",
                },
            )
            _run(payload, run_id)
            active = _active(
                payload,
                active,
                nodes,
                require_prepared=True,
                require_worker=True,
            )
            request_id = _identifier(payload.get("provider_request_id"))
            context_digest = _digest(payload.get("context_digest"))
            artifact_digest = _digest(payload.get("context_artifact_digest"))
            if (
                cancellation_requested
                or event.recorded_at > active.expires_at
                or active.provider_dispatch_event_id is not None
                or request_id != provider_request_id(active.lease_digest)
                or artifact_digest != context_digest
                or payload.get("status") != "provider-dispatch-started"
            ):
                raise LifecycleError()
            active = ActiveLease(
                node_id=active.node_id,
                attempt=active.attempt,
                fencing_token=active.fencing_token,
                worker_id=active.worker_id,
                lease_digest=active.lease_digest,
                worktree_spec_digest=active.worktree_spec_digest,
                worktree_spec=active.worktree_spec,
                expires_at=active.expires_at,
                prepared=True,
                provider_request_id=request_id,
                provider_context_digest=context_digest,
                provider_dispatch_event_id=event.event_id,
            )
        elif event.event_type == RUN_CANCEL_REQUESTED:
            _exact(
                payload,
                {"principal_id", "request", "request_digest", "status"},
            )
            request = _mapping(payload.get("request"))
            if (
                cancellation_requested
                or request.get("schema_version") != "execution-cancel-run-request/v1"
                or payload.get("request_digest") != json_digest(request)
                or payload.get("status") != "cancel-requested"
            ):
                raise LifecycleError()
            _identifier(request.get("idempotency_key"))
            cancellation_requested = True
            status = RunLifecycleStatus.CANCELING
        elif event.event_type == NODE_SUCCEEDED:
            _exact(
                payload,
                {
                    "principal_id",
                    "run_id",
                    "node_id",
                    "lease_digest",
                    "result_digest",
                    "head_commit",
                    "inspection_digest",
                    "inspection",
                    "retained_worktree",
                    "status",
                },
            )
            _run(payload, run_id)
            active = _active(
                payload,
                active,
                nodes,
                require_prepared=True,
                require_worker=True,
            )
            if cancellation_requested or event.recorded_at > active.expires_at:
                raise LifecycleError()
            inspection = _inspection(payload, active, unchanged=False)
            head_commit = _commit(payload.get("head_commit"))
            if (
                inspection.get("head_commit") != head_commit
                or inspection.get("uncommitted_paths") != ()
                or inspection.get("out_of_scope_paths") != ()
                or inspection.get("changed_path_limit_exceeded") is not False
                or payload.get("retained_worktree") is not True
            ):
                raise LifecycleError()
            node = nodes[active.node_id]
            node.status = NodeLifecycleStatus.SUCCEEDED
            node.result_digest = _digest(payload.get("result_digest"))
            node.head_commit = head_commit
            node.retained_worktree = True
            node.cleanup_status = WorktreeCleanupStatus.ELIGIBLE
            node.cleanup_failure_code = None
            if payload.get("status") != "succeeded":
                raise LifecycleError()
            active = None
            status = RunLifecycleStatus.QUEUED
        elif event.event_type == NODE_FAILED:
            _exact(
                payload,
                {
                    "principal_id",
                    "run_id",
                    "node_id",
                    "lease_digest",
                    "failure_code",
                    "result_digest",
                    "inspection_digest",
                    "inspection",
                    "retained_worktree",
                    "status",
                },
            )
            _run(payload, run_id)
            active = _active(
                payload,
                active,
                nodes,
                require_prepared=False,
                require_worker=True,
            )
            if event.recorded_at > active.expires_at:
                raise LifecycleError()
            node = nodes[active.node_id]
            node.status = NodeLifecycleStatus.FAILED
            node.failure_code = _failure_code(payload.get("failure_code"))
            result_digest = payload.get("result_digest")
            node.result_digest = None if result_digest is None else _digest(result_digest)
            retained = payload.get("retained_worktree")
            if retained is True:
                _inspection(payload, active, unchanged=False)
                node.retained_worktree = True
            elif retained is False:
                if (
                    payload.get("inspection") is not None
                    or payload.get("inspection_digest") is not None
                ):
                    raise LifecycleError()
            else:
                raise LifecycleError()
            if payload.get("status") != "failed":
                raise LifecycleError()
            active = None
            status = RunLifecycleStatus.FAILED
        elif event.event_type == NODE_REQUEUED:
            _exact(
                payload,
                {
                    "principal_id",
                    "run_id",
                    "node_id",
                    "lease_digest",
                    "disposition",
                    "inspection_digest",
                    "inspection",
                    "status",
                },
            )
            _run(payload, run_id)
            active = _active(payload, active, nodes, require_prepared=False)
            if active.provider_dispatch_event_id is not None:
                raise LifecycleError()
            disposition = payload.get("disposition")
            inspection = payload.get("inspection")
            inspection_digest = payload.get("inspection_digest")
            if disposition == "missing":
                if inspection is not None or inspection_digest is not None:
                    raise LifecycleError()
            elif disposition == "unchanged":
                _inspection(payload, active, unchanged=True)
            else:
                raise LifecycleError()
            if cancellation_requested or payload.get("status") != "requeued":
                raise LifecycleError()
            nodes[active.node_id].status = NodeLifecycleStatus.PENDING
            active = None
            status = RunLifecycleStatus.QUEUED
        elif event.event_type == NODE_CANCELED:
            _exact(
                payload,
                {
                    "principal_id",
                    "run_id",
                    "node_id",
                    "lease_digest",
                    "result_digest",
                    "inspection_digest",
                    "inspection",
                    "retained_worktree",
                    "status",
                },
            )
            _run(payload, run_id)
            active = _active(payload, active, nodes, require_prepared=False)
            retained = payload.get("retained_worktree")
            if retained is True:
                _inspection(payload, active, unchanged=False)
            elif retained is False:
                if (
                    payload.get("inspection") is not None
                    or payload.get("inspection_digest") is not None
                ):
                    raise LifecycleError()
            else:
                raise LifecycleError()
            if not cancellation_requested:
                raise LifecycleError()
            if payload.get("status") != "canceled":
                raise LifecycleError()
            node = nodes[active.node_id]
            node.status = NodeLifecycleStatus.CANCELED
            result_digest = payload.get("result_digest")
            node.result_digest = None if result_digest is None else _digest(result_digest)
            node.retained_worktree = retained
            active = None
            status = RunLifecycleStatus.CANCELING
        elif event.event_type == NODE_RECONCILIATION_REQUIRED:
            _exact(
                payload,
                {
                    "principal_id",
                    "run_id",
                    "node_id",
                    "lease_digest",
                    "failure_code",
                    "inspection_digest",
                    "inspection",
                    "retained_worktree",
                    "status",
                },
            )
            _run(payload, run_id)
            active = _active(payload, active, nodes, require_prepared=False)
            inspection = payload.get("inspection")
            inspection_digest = payload.get("inspection_digest")
            failure = payload.get("failure_code")
            retained = payload.get("retained_worktree")
            if inspection is None:
                if inspection_digest is not None or failure is None or retained is not False:
                    raise LifecycleError()
                _failure_code(failure)
            else:
                _inspection(payload, active, unchanged=False)
                if retained is not True:
                    raise LifecycleError()
                if failure is not None:
                    _failure_code(failure)
            if payload.get("status") != "reconciliation-required":
                raise LifecycleError()
            node = nodes[active.node_id]
            node.status = NodeLifecycleStatus.RECONCILIATION_REQUIRED
            node.failure_code = None if failure is None else cast("str", failure)
            node.retained_worktree = retained
            active = None
            status = RunLifecycleStatus.RECONCILIATION_REQUIRED
        elif event.event_type == NODE_WORKTREE_CLEANUP_REQUESTED:
            _exact(
                payload,
                {
                    "principal_id",
                    "run_id",
                    "node_id",
                    "lease_digest",
                    "worktree_spec_digest",
                    "head_commit",
                    "retained_worktree",
                    "status",
                },
            )
            _run(payload, run_id)
            node = _cleanup_node(
                payload,
                nodes,
                active,
                expected_status=WorktreeCleanupStatus.ELIGIBLE,
            )
            if (
                payload.get("retained_worktree") is not True
                or payload.get("status") != "worktree-cleanup-requested"
            ):
                raise LifecycleError()
            node.cleanup_status = WorktreeCleanupStatus.REQUESTED
        elif event.event_type == NODE_WORKTREE_CLEANED:
            _exact(
                payload,
                {
                    "principal_id",
                    "run_id",
                    "node_id",
                    "lease_digest",
                    "worktree_spec_digest",
                    "head_commit",
                    "removal_digest",
                    "removal",
                    "retained_worktree",
                    "status",
                },
            )
            _run(payload, run_id)
            node = _cleanup_node(
                payload,
                nodes,
                active,
                expected_status=WorktreeCleanupStatus.REQUESTED,
            )
            removal = _mapping(payload.get("removal"))
            _exact(
                removal,
                {
                    "schema_version",
                    "spec_digest",
                    "lease_digest",
                    "worktree_path",
                    "branch_name",
                    "retained_head_commit",
                    "disposition",
                },
            )
            spec_digest = _digest(node.worktree_spec_digest)
            lease_digest = _digest(node.lease_digest)
            head_commit = _commit(node.head_commit)
            spec = _mapping(node.worktree_spec)
            isolation_root = spec.get("isolation_root")
            suffix = spec_digest.removeprefix("sha256:")
            if (
                payload.get("removal_digest") != json_digest(removal)
                or removal.get("schema_version") != "blackcell.worktree-removal/v1"
                or removal.get("spec_digest") != spec_digest
                or removal.get("lease_digest") != lease_digest
                or removal.get("worktree_path")
                != (
                    str(Path(isolation_root) / f"worktree-{suffix}")
                    if isinstance(isolation_root, str)
                    else None
                )
                or removal.get("branch_name") != f"blackcell/execution-worktree/{suffix}"
                or removal.get("retained_head_commit") != head_commit
                or removal.get("disposition") != "removed"
                or payload.get("retained_worktree") is not False
                or payload.get("status") != "worktree-cleaned"
            ):
                raise LifecycleError()
            node.retained_worktree = False
            node.cleanup_status = WorktreeCleanupStatus.CLEANED
            node.cleanup_failure_code = None
        elif event.event_type == NODE_WORKTREE_CLEANUP_FAILED:
            _exact(
                payload,
                {
                    "principal_id",
                    "run_id",
                    "node_id",
                    "lease_digest",
                    "worktree_spec_digest",
                    "head_commit",
                    "failure_code",
                    "retained_worktree",
                    "status",
                },
            )
            _run(payload, run_id)
            node = _cleanup_node(
                payload,
                nodes,
                active,
                expected_status=WorktreeCleanupStatus.REQUESTED,
            )
            failure_code = _failure_code(payload.get("failure_code"))
            retained = payload.get("retained_worktree")
            if not isinstance(retained, bool) or payload.get("status") != "worktree-cleanup-failed":
                raise LifecycleError()
            node.retained_worktree = retained
            node.cleanup_status = WorktreeCleanupStatus.FAILED
            node.cleanup_failure_code = failure_code
        elif event.event_type in {
            RUN_SUCCEEDED,
            RUN_FAILED,
            RUN_CANCELED,
            RUN_RECONCILIATION_REQUIRED,
        }:
            _exact(payload, {"principal_id", "run_id", "status", "retained_worktree"})
            _run(payload, run_id)
            declared = payload.get("status")
            retained = payload.get("retained_worktree")
            if event.event_type == RUN_SUCCEEDED:
                if (
                    declared != "succeeded"
                    or not isinstance(retained, bool)
                    or active is not None
                    or cancellation_requested
                    or any(
                        node.status is not NodeLifecycleStatus.SUCCEEDED for node in nodes.values()
                    )
                    or retained != any(node.retained_worktree for node in nodes.values())
                ):
                    raise LifecycleError()
                status = RunLifecycleStatus.SUCCEEDED
            elif event.event_type == RUN_FAILED:
                if (
                    declared != "failed"
                    or not isinstance(retained, bool)
                    or active is not None
                    or not any(node.status is NodeLifecycleStatus.FAILED for node in nodes.values())
                    or retained
                    != any(
                        node.status is NodeLifecycleStatus.FAILED and node.retained_worktree
                        for node in nodes.values()
                    )
                ):
                    raise LifecycleError()
                status = RunLifecycleStatus.FAILED
            elif event.event_type == RUN_CANCELED:
                if (
                    declared != "canceled"
                    or not isinstance(retained, bool)
                    or active is not None
                    or not cancellation_requested
                    or retained != any(node.retained_worktree for node in nodes.values())
                ):
                    raise LifecycleError()
                status = RunLifecycleStatus.CANCELED
            else:
                if (
                    declared != "reconciliation-required"
                    or not isinstance(retained, bool)
                    or active is not None
                    or not any(
                        node.status is NodeLifecycleStatus.RECONCILIATION_REQUIRED
                        for node in nodes.values()
                    )
                    or retained
                    != any(
                        node.status is NodeLifecycleStatus.RECONCILIATION_REQUIRED
                        and node.retained_worktree
                        for node in nodes.values()
                    )
                ):
                    raise LifecycleError()
                status = RunLifecycleStatus.RECONCILIATION_REQUIRED
                reconciliation_recorded = True
            if event.event_type != RUN_RECONCILIATION_REQUIRED:
                terminal = True
        else:  # pragma: no cover - event set and branch list remain synchronized
            raise LifecycleError()

    if active is None and not terminal:
        if (
            cancellation_requested
            or all(node.status is NodeLifecycleStatus.SUCCEEDED for node in nodes.values())
            or any(node.status is NodeLifecycleStatus.FAILED for node in nodes.values())
        ):
            raise LifecycleError()
        if (
            any(
                node.status is NodeLifecycleStatus.RECONCILIATION_REQUIRED
                for node in nodes.values()
            )
            and not reconciliation_recorded
        ):
            raise LifecycleError()
    return RunLifecycleState(
        run_id=run_id,
        status=status,
        nodes=tuple(
            NodeLifecycleState(
                node_id=node.node_id,
                status=node.status,
                attempts=node.attempts,
                fencing_token=node.fencing_token,
                result_digest=node.result_digest,
                head_commit=node.head_commit,
                failure_code=node.failure_code,
                retained_worktree=node.retained_worktree,
                lease_digest=node.lease_digest,
                worktree_spec_digest=node.worktree_spec_digest,
                worktree_spec=node.worktree_spec,
                cleanup_status=node.cleanup_status,
                cleanup_failure_code=node.cleanup_failure_code,
            )
            for node in nodes.values()
        ),
        cancellation_requested=cancellation_requested,
        active_lease=active,
        queued_event=queued,
        latest_event=public_events[-1],
        last_stream_sequence=ordered_events[-1].stream_sequence,
    )


def run_lifecycle_payload(state: RunLifecycleState) -> dict[str, JsonInput]:
    """Return content-free replay state for hashing and clients."""

    active = state.active_lease
    return {
        "run_id": state.run_id,
        "status": state.status.value,
        "cancellation_requested": state.cancellation_requested,
        "active_lease": None
        if active is None
        else {
            "node_id": active.node_id,
            "attempt": active.attempt,
            "fencing_token": active.fencing_token,
            "worker_id": active.worker_id,
            "lease_digest": active.lease_digest,
            "worktree_spec_digest": active.worktree_spec_digest,
            "expires_at": active.expires_at.isoformat(),
            "prepared": active.prepared,
            "provider_request_id": active.provider_request_id,
            "provider_context_digest": active.provider_context_digest,
            "provider_dispatch_event_id": active.provider_dispatch_event_id,
        },
        "nodes": [
            {
                "node_id": node.node_id,
                "status": node.status.value,
                "attempts": node.attempts,
                "fencing_token": node.fencing_token,
                "result_digest": node.result_digest,
                "head_commit": node.head_commit,
                "failure_code": node.failure_code,
                "retained_worktree": node.retained_worktree,
                "lease_digest": node.lease_digest,
                "worktree_spec_digest": node.worktree_spec_digest,
                "cleanup_status": None
                if node.cleanup_status is None
                else node.cleanup_status.value,
                "cleanup_failure_code": node.cleanup_failure_code,
            }
            for node in state.nodes
        ],
        "stream_sequence": state.stream_sequence,
        "latest_event_id": state.latest_event.event_id,
        "latest_event_digest": state.latest_event.payload_hash,
    }


def _validate_queued(event: EventEnvelope, run_id: str) -> None:
    payload = _payload(event)
    _exact(
        payload,
        {"request", "request_digest", "principal_id", "references", "status"},
    )
    _principal(payload, event)
    request = _mapping(payload.get("request"))
    if (
        request.get("run_id") != run_id
        or payload.get("request_digest") != json_digest(request)
        or payload.get("status") != "queued"
    ):
        raise LifecycleError()


def _active(
    payload: Mapping[str, JsonValue],
    active: ActiveLease | None,
    nodes: Mapping[str, _MutableNode],
    *,
    require_prepared: bool,
    require_worker: bool = False,
) -> ActiveLease:
    if active is None:
        raise LifecycleError()
    node = _node(payload, nodes)
    if (
        node.node_id != active.node_id
        or payload.get("lease_digest") != active.lease_digest
        or (require_prepared and not active.prepared)
        or (require_worker and payload.get("principal_id") != active.worker_id)
    ):
        raise LifecycleError()
    return active


def _cleanup_node(
    payload: Mapping[str, JsonValue],
    nodes: Mapping[str, _MutableNode],
    active: ActiveLease | None,
    *,
    expected_status: WorktreeCleanupStatus,
) -> _MutableNode:
    node = _node(payload, nodes)
    if (
        active is not None
        or node.status is not NodeLifecycleStatus.SUCCEEDED
        or not node.retained_worktree
        or node.cleanup_status is not expected_status
        or payload.get("lease_digest") != node.lease_digest
        or payload.get("worktree_spec_digest") != node.worktree_spec_digest
        or payload.get("head_commit") != node.head_commit
        or node.worktree_spec is None
    ):
        raise LifecycleError()
    return node


def _inspection(
    payload: Mapping[str, JsonValue],
    active: ActiveLease,
    *,
    unchanged: bool,
) -> Mapping[str, JsonValue]:
    inspection = _mapping(payload.get("inspection"))
    if (
        payload.get("inspection_digest") != json_digest(inspection)
        or inspection.get("spec_digest") != active.worktree_spec_digest
        or inspection.get("lease_digest") != active.lease_digest
    ):
        raise LifecycleError()
    if unchanged and (
        inspection.get("head_commit") != inspection.get("base_commit")
        or inspection.get("changed_paths") != ()
        or inspection.get("uncommitted_paths") != ()
        or inspection.get("out_of_scope_paths") != ()
        or inspection.get("changed_path_limit_exceeded") is not False
    ):
        raise LifecycleError()
    return inspection


def _worktree_spec_digest(spec: Mapping[str, JsonValue], lease_digest: str) -> str:
    return json_digest(
        {
            "schema_version": spec.get("schema_version"),
            "lease_digest": lease_digest,
            "repository_root": spec.get("repository_root"),
            "isolation_root": spec.get("isolation_root"),
            "base_commit": spec.get("base_commit"),
            "allowed_paths": spec.get("allowed_paths"),
            "max_changed_paths": spec.get("max_changed_paths"),
        }
    )


def _payload(event: EventEnvelope) -> Mapping[str, JsonValue]:
    if not isinstance(event.payload, Mapping):  # pragma: no cover - EventEnvelope invariant
        raise LifecycleError()
    return event.payload


def _mapping(value: object) -> Mapping[str, JsonValue]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise LifecycleError()
    return cast("Mapping[str, JsonValue]", value)


def _exact(payload: Mapping[str, JsonValue], expected: set[str]) -> None:
    if set(payload) != expected:
        raise LifecycleError()


def _principal(payload: Mapping[str, JsonValue], event: EventEnvelope) -> None:
    if payload.get("principal_id") != event.actor:
        raise LifecycleError()


def _run(payload: Mapping[str, JsonValue], run_id: str) -> None:
    if payload.get("run_id") != run_id:
        raise LifecycleError()


def _node(payload: Mapping[str, JsonValue], nodes: Mapping[str, _MutableNode]) -> _MutableNode:
    node_id = payload.get("node_id")
    if not isinstance(node_id, str) or node_id not in nodes:
        raise LifecycleError()
    return nodes[node_id]


def _positive_integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise LifecycleError()
    return value


def _text(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise LifecycleError()
    return value


def _digest(value: object) -> str:
    text = _text(value)
    if len(text) != _DIGEST_LENGTH or not text.startswith("sha256:"):
        raise LifecycleError()
    try:
        int(text.removeprefix("sha256:"), 16)
    except ValueError as error:
        raise LifecycleError() from error
    return text


def _commit(value: object) -> str:
    text = _text(value)
    if len(text) != 40:
        raise LifecycleError()
    try:
        int(text, 16)
    except ValueError as error:
        raise LifecycleError() from error
    if text != text.lower():
        raise LifecycleError()
    return text


def _failure_code(value: object) -> str:
    text = _text(value)
    if any(
        not (character.isascii() and (character.isalnum() or character in "-._"))
        for character in text
    ):
        raise LifecycleError()
    return text


def _identifier(value: object) -> str:
    text = _text(value)
    if any(
        not (character.isascii() and (character.isalnum() or character in "-._"))
        for character in text
    ):
        raise LifecycleError()
    return text


def _timestamp(value: object) -> datetime:
    text = _text(value)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise LifecycleError() from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LifecycleError()
    return parsed


def provider_request_id(lease_digest: str) -> str:
    """Return the one stable provider request identity for an exact fenced lease."""

    digest = _digest(lease_digest)
    return f"change-{digest.removeprefix('sha256:')}"


def thaw_worktree_spec(active: ActiveLease) -> Mapping[str, object]:
    """Return mutable builtins for strict adapter reconstruction during startup."""

    value = thaw_json(cast("JsonValue", active.worktree_spec))
    if not isinstance(value, dict):  # pragma: no cover - active lease invariant
        raise LifecycleError()
    return cast("Mapping[str, object]", value)


def thaw_successful_worktree_spec(node: NodeLifecycleState) -> Mapping[str, object]:
    """Return mutable builtins for one successful node's retained checkout specification."""

    if (
        not isinstance(node, NodeLifecycleState)
        or node.status is not NodeLifecycleStatus.SUCCEEDED
        or node.worktree_spec is None
    ):
        raise LifecycleError()
    value = thaw_json(cast("JsonValue", node.worktree_spec))
    if not isinstance(value, dict):
        raise LifecycleError()
    return cast("Mapping[str, object]", value)


__all__ = [
    "NODE_CANCELED",
    "NODE_CLAIMED",
    "NODE_FAILED",
    "NODE_PROVIDER_DISPATCH_STARTED",
    "NODE_RECONCILIATION_REQUIRED",
    "NODE_REQUEUED",
    "NODE_SUCCEEDED",
    "NODE_WORKTREE_CLEANED",
    "NODE_WORKTREE_CLEANUP_FAILED",
    "NODE_WORKTREE_CLEANUP_REQUESTED",
    "NODE_WORKTREE_PREPARED",
    "RUNTIME_EVENT_SOURCE",
    "RUN_CANCELED",
    "RUN_CANCEL_REQUESTED",
    "RUN_EVENT_TYPES",
    "RUN_FAILED",
    "RUN_QUEUED",
    "RUN_RECONCILIATION_REQUIRED",
    "RUN_SUCCEEDED",
    "ActiveLease",
    "LifecycleError",
    "NodeLifecycleState",
    "NodeLifecycleStatus",
    "RunLifecycleState",
    "RunLifecycleStatus",
    "WorktreeCleanupStatus",
    "fold_run_lifecycle",
    "provider_request_id",
    "run_lifecycle_payload",
    "thaw_successful_worktree_spec",
    "thaw_worktree_spec",
]
