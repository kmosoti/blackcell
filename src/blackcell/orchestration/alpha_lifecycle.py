"""Strict event fold for the alpha run execution lifecycle.

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

ALPHA_EVENT_SOURCE = "blackcell.alpha.runtime"
ALPHA_RUN_QUEUED = "alpha.run.queued"
ALPHA_NODE_CLAIMED = "alpha.node.claimed"
ALPHA_NODE_WORKTREE_PREPARED = "alpha.node.worktree-prepared"
ALPHA_NODE_PROVIDER_DISPATCH_STARTED = "alpha.node.provider-dispatch-started"
ALPHA_RUN_CANCEL_REQUESTED = "alpha.run.cancel-requested"
ALPHA_NODE_SUCCEEDED = "alpha.node.succeeded"
ALPHA_NODE_FAILED = "alpha.node.failed"
ALPHA_NODE_REQUEUED = "alpha.node.requeued"
ALPHA_NODE_CANCELED = "alpha.node.canceled"
ALPHA_NODE_RECONCILIATION_REQUIRED = "alpha.node.reconciliation-required"
ALPHA_NODE_WORKTREE_CLEANUP_REQUESTED = "alpha.node.worktree-cleanup-requested"
ALPHA_NODE_WORKTREE_CLEANED = "alpha.node.worktree-cleaned"
ALPHA_NODE_WORKTREE_CLEANUP_FAILED = "alpha.node.worktree-cleanup-failed"
ALPHA_RUN_SUCCEEDED = "alpha.run.succeeded"
ALPHA_RUN_FAILED = "alpha.run.failed"
ALPHA_RUN_CANCELED = "alpha.run.canceled"
ALPHA_RUN_RECONCILIATION_REQUIRED = "alpha.run.reconciliation-required"

ALPHA_RUN_EVENT_TYPES = frozenset(
    {
        ALPHA_RUN_QUEUED,
        ALPHA_NODE_CLAIMED,
        ALPHA_NODE_WORKTREE_PREPARED,
        ALPHA_NODE_PROVIDER_DISPATCH_STARTED,
        ALPHA_RUN_CANCEL_REQUESTED,
        ALPHA_NODE_SUCCEEDED,
        ALPHA_NODE_FAILED,
        ALPHA_NODE_REQUEUED,
        ALPHA_NODE_CANCELED,
        ALPHA_NODE_RECONCILIATION_REQUIRED,
        ALPHA_NODE_WORKTREE_CLEANUP_REQUESTED,
        ALPHA_NODE_WORKTREE_CLEANED,
        ALPHA_NODE_WORKTREE_CLEANUP_FAILED,
        ALPHA_RUN_SUCCEEDED,
        ALPHA_RUN_FAILED,
        ALPHA_RUN_CANCELED,
        ALPHA_RUN_RECONCILIATION_REQUIRED,
    }
)

_DIGEST_LENGTH = 71


class AlphaLifecycleError(ValueError):
    """Content-free indication that a run history violates the alpha grammar."""

    def __init__(self) -> None:
        super().__init__("invalid-alpha-run-lifecycle")


class AlphaRunLifecycleStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCELING = "canceling"
    CANCELED = "canceled"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RECONCILIATION_REQUIRED = "reconciliation-required"


class AlphaNodeLifecycleStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"
    RECONCILIATION_REQUIRED = "reconciliation-required"


class AlphaWorktreeCleanupStatus(StrEnum):
    ELIGIBLE = "eligible"
    REQUESTED = "requested"
    FAILED = "failed"
    CLEANED = "cleaned"


@dataclass(frozen=True, slots=True)
class AlphaActiveLease:
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
class AlphaNodeLifecycleState:
    node_id: str
    status: AlphaNodeLifecycleStatus
    attempts: int
    fencing_token: int
    result_digest: str | None = None
    head_commit: str | None = None
    failure_code: str | None = None
    retained_worktree: bool = False
    lease_digest: str | None = None
    worktree_spec_digest: str | None = None
    worktree_spec: Mapping[str, JsonValue] | None = None
    cleanup_status: AlphaWorktreeCleanupStatus | None = None
    cleanup_failure_code: str | None = None


@dataclass(frozen=True, slots=True)
class AlphaRunLifecycleState:
    run_id: str
    status: AlphaRunLifecycleStatus
    nodes: tuple[AlphaNodeLifecycleState, ...]
    cancellation_requested: bool
    active_lease: AlphaActiveLease | None
    queued_event: EventEnvelope
    latest_event: EventEnvelope

    @property
    def stream_sequence(self) -> int:
        return self.latest_event.stream_sequence


@dataclass(slots=True)
class _MutableNode:
    node_id: str
    status: AlphaNodeLifecycleStatus = AlphaNodeLifecycleStatus.PENDING
    attempts: int = 0
    fencing_token: int = 0
    result_digest: str | None = None
    head_commit: str | None = None
    failure_code: str | None = None
    retained_worktree: bool = False
    lease_digest: str | None = None
    worktree_spec_digest: str | None = None
    worktree_spec: Mapping[str, JsonValue] | None = None
    cleanup_status: AlphaWorktreeCleanupStatus | None = None
    cleanup_failure_code: str | None = None


def fold_alpha_run_lifecycle(
    run_id: str,
    node_dependencies: Mapping[str, tuple[str, ...]],
    events: Sequence[EventEnvelope],
) -> AlphaRunLifecycleState:
    """Validate and fold one complete alpha run stream."""

    if not run_id or not node_dependencies or not events:
        raise AlphaLifecycleError()
    known = set(node_dependencies)
    if any(
        not node_id
        or not isinstance(dependencies, tuple)
        or node_id in dependencies
        or not set(dependencies).issubset(known)
        for node_id, dependencies in node_dependencies.items()
    ):
        raise AlphaLifecycleError()
    nodes = {node_id: _MutableNode(node_id) for node_id in sorted(known)}
    stream_id = f"alpha:run:{run_id}"
    ordered_events = tuple(events)
    for sequence, event in enumerate(ordered_events, start=1):
        if (
            not isinstance(event, EventEnvelope)
            or event.stream_id != stream_id
            or event.stream_sequence != sequence
            or event.schema_version != 1
            or event.source != ALPHA_EVENT_SOURCE
            or event.event_type not in ALPHA_RUN_EVENT_TYPES
        ):
            raise AlphaLifecycleError()
    queued = ordered_events[0]
    if queued.event_type != ALPHA_RUN_QUEUED:
        raise AlphaLifecycleError()
    _validate_queued(queued, run_id)
    for previous, event in pairwise(ordered_events):
        if event.correlation_id != queued.correlation_id or event.causation_id != previous.event_id:
            raise AlphaLifecycleError()

    status = AlphaRunLifecycleStatus.QUEUED
    cancellation_requested = False
    active: AlphaActiveLease | None = None
    terminal = False
    reconciliation_recorded = False
    maximum_fence = 0
    for event in ordered_events[1:]:
        cleanup_event = event.event_type in {
            ALPHA_NODE_WORKTREE_CLEANUP_REQUESTED,
            ALPHA_NODE_WORKTREE_CLEANED,
            ALPHA_NODE_WORKTREE_CLEANUP_FAILED,
        }
        if terminal and not cleanup_event:
            raise AlphaLifecycleError()
        payload = _payload(event)
        _principal(payload, event)
        if event.event_type == ALPHA_NODE_CLAIMED:
            if (
                cancellation_requested
                or active is not None
                or status
                not in {
                    AlphaRunLifecycleStatus.QUEUED,
                    AlphaRunLifecycleStatus.RUNNING,
                }
            ):
                raise AlphaLifecycleError()
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
            if node.status is not AlphaNodeLifecycleStatus.PENDING or any(
                nodes[dependency].status is not AlphaNodeLifecycleStatus.SUCCEEDED
                for dependency in node_dependencies[node.node_id]
            ):
                raise AlphaLifecycleError()
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
                raise AlphaLifecycleError()
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
                raise AlphaLifecycleError()
            node.status = AlphaNodeLifecycleStatus.CLAIMED
            node.attempts = attempt
            node.fencing_token = fencing_token
            node.lease_digest = lease_digest
            node.worktree_spec_digest = spec_digest
            node.worktree_spec = spec
            node.cleanup_status = None
            node.cleanup_failure_code = None
            maximum_fence = fencing_token
            active = AlphaActiveLease(
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
            status = AlphaRunLifecycleStatus.RUNNING
        elif event.event_type == ALPHA_NODE_WORKTREE_PREPARED:
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
                raise AlphaLifecycleError()
            inspection = _inspection(payload, active, unchanged=True)
            del inspection
            node = nodes[active.node_id]
            node.status = AlphaNodeLifecycleStatus.RUNNING
            active = AlphaActiveLease(
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
                raise AlphaLifecycleError()
        elif event.event_type == ALPHA_NODE_PROVIDER_DISPATCH_STARTED:
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
                or request_id != alpha_provider_request_id(active.lease_digest)
                or artifact_digest != context_digest
                or payload.get("status") != "provider-dispatch-started"
            ):
                raise AlphaLifecycleError()
            active = AlphaActiveLease(
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
        elif event.event_type == ALPHA_RUN_CANCEL_REQUESTED:
            _exact(
                payload,
                {"principal_id", "request", "request_digest", "status"},
            )
            request = _mapping(payload.get("request"))
            if (
                cancellation_requested
                or request.get("schema_version") != "alpha-cancel-run-request/v1"
                or payload.get("request_digest") != json_digest(request)
                or payload.get("status") != "cancel-requested"
            ):
                raise AlphaLifecycleError()
            _identifier(request.get("idempotency_key"))
            cancellation_requested = True
            status = AlphaRunLifecycleStatus.CANCELING
        elif event.event_type == ALPHA_NODE_SUCCEEDED:
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
                raise AlphaLifecycleError()
            inspection = _inspection(payload, active, unchanged=False)
            head_commit = _commit(payload.get("head_commit"))
            if (
                inspection.get("head_commit") != head_commit
                or inspection.get("uncommitted_paths") != ()
                or inspection.get("out_of_scope_paths") != ()
                or inspection.get("changed_path_limit_exceeded") is not False
                or payload.get("retained_worktree") is not True
            ):
                raise AlphaLifecycleError()
            node = nodes[active.node_id]
            node.status = AlphaNodeLifecycleStatus.SUCCEEDED
            node.result_digest = _digest(payload.get("result_digest"))
            node.head_commit = head_commit
            node.retained_worktree = True
            node.cleanup_status = AlphaWorktreeCleanupStatus.ELIGIBLE
            node.cleanup_failure_code = None
            if payload.get("status") != "succeeded":
                raise AlphaLifecycleError()
            active = None
            status = AlphaRunLifecycleStatus.QUEUED
        elif event.event_type == ALPHA_NODE_FAILED:
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
                raise AlphaLifecycleError()
            node = nodes[active.node_id]
            node.status = AlphaNodeLifecycleStatus.FAILED
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
                    raise AlphaLifecycleError()
            else:
                raise AlphaLifecycleError()
            if payload.get("status") != "failed":
                raise AlphaLifecycleError()
            active = None
            status = AlphaRunLifecycleStatus.FAILED
        elif event.event_type == ALPHA_NODE_REQUEUED:
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
                raise AlphaLifecycleError()
            disposition = payload.get("disposition")
            inspection = payload.get("inspection")
            inspection_digest = payload.get("inspection_digest")
            if disposition == "missing":
                if inspection is not None or inspection_digest is not None:
                    raise AlphaLifecycleError()
            elif disposition == "unchanged":
                _inspection(payload, active, unchanged=True)
            else:
                raise AlphaLifecycleError()
            if cancellation_requested or payload.get("status") != "requeued":
                raise AlphaLifecycleError()
            nodes[active.node_id].status = AlphaNodeLifecycleStatus.PENDING
            active = None
            status = AlphaRunLifecycleStatus.QUEUED
        elif event.event_type == ALPHA_NODE_CANCELED:
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
                    raise AlphaLifecycleError()
            else:
                raise AlphaLifecycleError()
            if not cancellation_requested:
                raise AlphaLifecycleError()
            if payload.get("status") != "canceled":
                raise AlphaLifecycleError()
            node = nodes[active.node_id]
            node.status = AlphaNodeLifecycleStatus.CANCELED
            result_digest = payload.get("result_digest")
            node.result_digest = None if result_digest is None else _digest(result_digest)
            node.retained_worktree = retained
            active = None
            status = AlphaRunLifecycleStatus.CANCELING
        elif event.event_type == ALPHA_NODE_RECONCILIATION_REQUIRED:
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
                    raise AlphaLifecycleError()
                _failure_code(failure)
            else:
                _inspection(payload, active, unchanged=False)
                if retained is not True:
                    raise AlphaLifecycleError()
                if failure is not None:
                    _failure_code(failure)
            if payload.get("status") != "reconciliation-required":
                raise AlphaLifecycleError()
            node = nodes[active.node_id]
            node.status = AlphaNodeLifecycleStatus.RECONCILIATION_REQUIRED
            node.failure_code = None if failure is None else cast("str", failure)
            node.retained_worktree = retained
            active = None
            status = AlphaRunLifecycleStatus.RECONCILIATION_REQUIRED
        elif event.event_type == ALPHA_NODE_WORKTREE_CLEANUP_REQUESTED:
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
                expected_status=AlphaWorktreeCleanupStatus.ELIGIBLE,
            )
            if (
                payload.get("retained_worktree") is not True
                or payload.get("status") != "worktree-cleanup-requested"
            ):
                raise AlphaLifecycleError()
            node.cleanup_status = AlphaWorktreeCleanupStatus.REQUESTED
        elif event.event_type == ALPHA_NODE_WORKTREE_CLEANED:
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
                expected_status=AlphaWorktreeCleanupStatus.REQUESTED,
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
                or removal.get("branch_name") != f"blackcell/alpha-worktree/{suffix}"
                or removal.get("retained_head_commit") != head_commit
                or removal.get("disposition") != "removed"
                or payload.get("retained_worktree") is not False
                or payload.get("status") != "worktree-cleaned"
            ):
                raise AlphaLifecycleError()
            node.retained_worktree = False
            node.cleanup_status = AlphaWorktreeCleanupStatus.CLEANED
            node.cleanup_failure_code = None
        elif event.event_type == ALPHA_NODE_WORKTREE_CLEANUP_FAILED:
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
                expected_status=AlphaWorktreeCleanupStatus.REQUESTED,
            )
            failure_code = _failure_code(payload.get("failure_code"))
            retained = payload.get("retained_worktree")
            if not isinstance(retained, bool) or payload.get("status") != "worktree-cleanup-failed":
                raise AlphaLifecycleError()
            node.retained_worktree = retained
            node.cleanup_status = AlphaWorktreeCleanupStatus.FAILED
            node.cleanup_failure_code = failure_code
        elif event.event_type in {
            ALPHA_RUN_SUCCEEDED,
            ALPHA_RUN_FAILED,
            ALPHA_RUN_CANCELED,
            ALPHA_RUN_RECONCILIATION_REQUIRED,
        }:
            _exact(payload, {"principal_id", "run_id", "status", "retained_worktree"})
            _run(payload, run_id)
            declared = payload.get("status")
            retained = payload.get("retained_worktree")
            if event.event_type == ALPHA_RUN_SUCCEEDED:
                if (
                    declared != "succeeded"
                    or not isinstance(retained, bool)
                    or active is not None
                    or cancellation_requested
                    or any(
                        node.status is not AlphaNodeLifecycleStatus.SUCCEEDED
                        for node in nodes.values()
                    )
                    or retained != any(node.retained_worktree for node in nodes.values())
                ):
                    raise AlphaLifecycleError()
                status = AlphaRunLifecycleStatus.SUCCEEDED
            elif event.event_type == ALPHA_RUN_FAILED:
                if (
                    declared != "failed"
                    or not isinstance(retained, bool)
                    or active is not None
                    or not any(
                        node.status is AlphaNodeLifecycleStatus.FAILED for node in nodes.values()
                    )
                    or retained
                    != any(
                        node.status is AlphaNodeLifecycleStatus.FAILED and node.retained_worktree
                        for node in nodes.values()
                    )
                ):
                    raise AlphaLifecycleError()
                status = AlphaRunLifecycleStatus.FAILED
            elif event.event_type == ALPHA_RUN_CANCELED:
                if (
                    declared != "canceled"
                    or not isinstance(retained, bool)
                    or active is not None
                    or not cancellation_requested
                    or retained != any(node.retained_worktree for node in nodes.values())
                ):
                    raise AlphaLifecycleError()
                status = AlphaRunLifecycleStatus.CANCELED
            else:
                if (
                    declared != "reconciliation-required"
                    or not isinstance(retained, bool)
                    or active is not None
                    or not any(
                        node.status is AlphaNodeLifecycleStatus.RECONCILIATION_REQUIRED
                        for node in nodes.values()
                    )
                    or retained
                    != any(
                        node.status is AlphaNodeLifecycleStatus.RECONCILIATION_REQUIRED
                        and node.retained_worktree
                        for node in nodes.values()
                    )
                ):
                    raise AlphaLifecycleError()
                status = AlphaRunLifecycleStatus.RECONCILIATION_REQUIRED
                reconciliation_recorded = True
            if event.event_type != ALPHA_RUN_RECONCILIATION_REQUIRED:
                terminal = True
        else:  # pragma: no cover - event set and branch list remain synchronized
            raise AlphaLifecycleError()

    if active is None and not terminal:
        if (
            cancellation_requested
            or all(node.status is AlphaNodeLifecycleStatus.SUCCEEDED for node in nodes.values())
            or any(node.status is AlphaNodeLifecycleStatus.FAILED for node in nodes.values())
        ):
            raise AlphaLifecycleError()
        if (
            any(
                node.status is AlphaNodeLifecycleStatus.RECONCILIATION_REQUIRED
                for node in nodes.values()
            )
            and not reconciliation_recorded
        ):
            raise AlphaLifecycleError()
    return AlphaRunLifecycleState(
        run_id=run_id,
        status=status,
        nodes=tuple(
            AlphaNodeLifecycleState(
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
        latest_event=ordered_events[-1],
    )


def alpha_run_lifecycle_payload(state: AlphaRunLifecycleState) -> dict[str, JsonInput]:
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
        raise AlphaLifecycleError()


def _active(
    payload: Mapping[str, JsonValue],
    active: AlphaActiveLease | None,
    nodes: Mapping[str, _MutableNode],
    *,
    require_prepared: bool,
    require_worker: bool = False,
) -> AlphaActiveLease:
    if active is None:
        raise AlphaLifecycleError()
    node = _node(payload, nodes)
    if (
        node.node_id != active.node_id
        or payload.get("lease_digest") != active.lease_digest
        or (require_prepared and not active.prepared)
        or (require_worker and payload.get("principal_id") != active.worker_id)
    ):
        raise AlphaLifecycleError()
    return active


def _cleanup_node(
    payload: Mapping[str, JsonValue],
    nodes: Mapping[str, _MutableNode],
    active: AlphaActiveLease | None,
    *,
    expected_status: AlphaWorktreeCleanupStatus,
) -> _MutableNode:
    node = _node(payload, nodes)
    if (
        active is not None
        or node.status is not AlphaNodeLifecycleStatus.SUCCEEDED
        or not node.retained_worktree
        or node.cleanup_status is not expected_status
        or payload.get("lease_digest") != node.lease_digest
        or payload.get("worktree_spec_digest") != node.worktree_spec_digest
        or payload.get("head_commit") != node.head_commit
        or node.worktree_spec is None
    ):
        raise AlphaLifecycleError()
    return node


def _inspection(
    payload: Mapping[str, JsonValue],
    active: AlphaActiveLease,
    *,
    unchanged: bool,
) -> Mapping[str, JsonValue]:
    inspection = _mapping(payload.get("inspection"))
    if (
        payload.get("inspection_digest") != json_digest(inspection)
        or inspection.get("spec_digest") != active.worktree_spec_digest
        or inspection.get("lease_digest") != active.lease_digest
    ):
        raise AlphaLifecycleError()
    if unchanged and (
        inspection.get("head_commit") != inspection.get("base_commit")
        or inspection.get("changed_paths") != ()
        or inspection.get("uncommitted_paths") != ()
        or inspection.get("out_of_scope_paths") != ()
        or inspection.get("changed_path_limit_exceeded") is not False
    ):
        raise AlphaLifecycleError()
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
        raise AlphaLifecycleError()
    return event.payload


def _mapping(value: object) -> Mapping[str, JsonValue]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise AlphaLifecycleError()
    return cast("Mapping[str, JsonValue]", value)


def _exact(payload: Mapping[str, JsonValue], expected: set[str]) -> None:
    if set(payload) != expected:
        raise AlphaLifecycleError()


def _principal(payload: Mapping[str, JsonValue], event: EventEnvelope) -> None:
    if payload.get("principal_id") != event.actor:
        raise AlphaLifecycleError()


def _run(payload: Mapping[str, JsonValue], run_id: str) -> None:
    if payload.get("run_id") != run_id:
        raise AlphaLifecycleError()


def _node(payload: Mapping[str, JsonValue], nodes: Mapping[str, _MutableNode]) -> _MutableNode:
    node_id = payload.get("node_id")
    if not isinstance(node_id, str) or node_id not in nodes:
        raise AlphaLifecycleError()
    return nodes[node_id]


def _positive_integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise AlphaLifecycleError()
    return value


def _text(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise AlphaLifecycleError()
    return value


def _digest(value: object) -> str:
    text = _text(value)
    if len(text) != _DIGEST_LENGTH or not text.startswith("sha256:"):
        raise AlphaLifecycleError()
    try:
        int(text.removeprefix("sha256:"), 16)
    except ValueError as error:
        raise AlphaLifecycleError() from error
    return text


def _commit(value: object) -> str:
    text = _text(value)
    if len(text) != 40:
        raise AlphaLifecycleError()
    try:
        int(text, 16)
    except ValueError as error:
        raise AlphaLifecycleError() from error
    if text != text.lower():
        raise AlphaLifecycleError()
    return text


def _failure_code(value: object) -> str:
    text = _text(value)
    if any(
        not (character.isascii() and (character.isalnum() or character in "-._"))
        for character in text
    ):
        raise AlphaLifecycleError()
    return text


def _identifier(value: object) -> str:
    text = _text(value)
    if any(
        not (character.isascii() and (character.isalnum() or character in "-._"))
        for character in text
    ):
        raise AlphaLifecycleError()
    return text


def _timestamp(value: object) -> datetime:
    text = _text(value)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise AlphaLifecycleError() from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AlphaLifecycleError()
    return parsed


def alpha_provider_request_id(lease_digest: str) -> str:
    """Return the one stable provider request identity for an exact fenced lease."""

    digest = _digest(lease_digest)
    return f"alpha-change-{digest.removeprefix('sha256:')}"


def thaw_worktree_spec(active: AlphaActiveLease) -> Mapping[str, object]:
    """Return mutable builtins for strict adapter reconstruction during startup."""

    value = thaw_json(cast("JsonValue", active.worktree_spec))
    if not isinstance(value, dict):  # pragma: no cover - active lease invariant
        raise AlphaLifecycleError()
    return cast("Mapping[str, object]", value)


def thaw_successful_worktree_spec(node: AlphaNodeLifecycleState) -> Mapping[str, object]:
    """Return mutable builtins for one successful node's retained checkout specification."""

    if (
        not isinstance(node, AlphaNodeLifecycleState)
        or node.status is not AlphaNodeLifecycleStatus.SUCCEEDED
        or node.worktree_spec is None
    ):
        raise AlphaLifecycleError()
    value = thaw_json(cast("JsonValue", node.worktree_spec))
    if not isinstance(value, dict):
        raise AlphaLifecycleError()
    return cast("Mapping[str, object]", value)


__all__ = [
    "ALPHA_EVENT_SOURCE",
    "ALPHA_NODE_CANCELED",
    "ALPHA_NODE_CLAIMED",
    "ALPHA_NODE_FAILED",
    "ALPHA_NODE_PROVIDER_DISPATCH_STARTED",
    "ALPHA_NODE_RECONCILIATION_REQUIRED",
    "ALPHA_NODE_REQUEUED",
    "ALPHA_NODE_SUCCEEDED",
    "ALPHA_NODE_WORKTREE_CLEANED",
    "ALPHA_NODE_WORKTREE_CLEANUP_FAILED",
    "ALPHA_NODE_WORKTREE_CLEANUP_REQUESTED",
    "ALPHA_NODE_WORKTREE_PREPARED",
    "ALPHA_RUN_CANCELED",
    "ALPHA_RUN_CANCEL_REQUESTED",
    "ALPHA_RUN_EVENT_TYPES",
    "ALPHA_RUN_FAILED",
    "ALPHA_RUN_QUEUED",
    "ALPHA_RUN_RECONCILIATION_REQUIRED",
    "ALPHA_RUN_SUCCEEDED",
    "AlphaActiveLease",
    "AlphaLifecycleError",
    "AlphaNodeLifecycleState",
    "AlphaNodeLifecycleStatus",
    "AlphaRunLifecycleState",
    "AlphaRunLifecycleStatus",
    "AlphaWorktreeCleanupStatus",
    "alpha_provider_request_id",
    "alpha_run_lifecycle_payload",
    "fold_alpha_run_lifecycle",
    "thaw_successful_worktree_spec",
    "thaw_worktree_spec",
]
