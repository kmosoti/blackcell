from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

import msgspec

from blackcell.adapters.execution.worktree import (
    GitWorktreeLifecycle,
    WorktreeExecutionSpec,
    WorktreeFailureCode,
    WorktreeInspection,
    WorktreeLeaseIdentity,
    WorktreeLifecycleError,
    worktree_execution_spec_from_mapping,
    worktree_execution_spec_payload,
    worktree_inspection_from_mapping,
    worktree_inspection_payload,
    worktree_removal_from_mapping,
    worktree_removal_payload,
)
from blackcell.interfaces.http.alpha_contracts import (
    MAX_ALPHA_EVENT_PAGE_SIZE,
    AlphaCancelRunRequest,
    AlphaEventPageResponse,
    AlphaEventResponse,
    AlphaEventType,
    AlphaIntentRequest,
    AlphaIntentResponse,
    AlphaPlanNode,
    AlphaPlanRequest,
    AlphaPlanResponse,
    AlphaProjectRequest,
    AlphaProjectResponse,
    AlphaReplayArtifactResponse,
    AlphaReplayFindingResponse,
    AlphaReplayResponse,
    AlphaRunRequest,
    AlphaRunResponse,
    AlphaVerificationReplayResponse,
    alpha_plan_topological_order,
)
from blackcell.interfaces.http.ports import RuntimeApiError, RuntimeApiFailureCode
from blackcell.kernel import (
    ConcurrencyError,
    EventConflictError,
    EventEnvelope,
    EventStore,
    IdempotencyConflict,
    JsonValue,
    utc_now,
)
from blackcell.kernel._json import JsonInput, json_digest, thaw_json
from blackcell.orchestration.alpha_lifecycle import (
    ALPHA_EVENT_SOURCE,
    ALPHA_NODE_CANCELED,
    ALPHA_NODE_CLAIMED,
    ALPHA_NODE_FAILED,
    ALPHA_NODE_PROVIDER_DISPATCH_STARTED,
    ALPHA_NODE_RECONCILIATION_REQUIRED,
    ALPHA_NODE_REQUEUED,
    ALPHA_NODE_SUCCEEDED,
    ALPHA_NODE_WORKTREE_CLEANED,
    ALPHA_NODE_WORKTREE_CLEANUP_FAILED,
    ALPHA_NODE_WORKTREE_CLEANUP_REQUESTED,
    ALPHA_NODE_WORKTREE_PREPARED,
    ALPHA_RUN_CANCEL_REQUESTED,
    ALPHA_RUN_CANCELED,
    ALPHA_RUN_EVENT_TYPES,
    ALPHA_RUN_FAILED,
    ALPHA_RUN_QUEUED,
    ALPHA_RUN_RECONCILIATION_REQUIRED,
    ALPHA_RUN_SUCCEEDED,
    AlphaLifecycleError,
    AlphaNodeLifecycleStatus,
    AlphaRunLifecycleState,
    AlphaRunLifecycleStatus,
    AlphaWorktreeCleanupStatus,
    alpha_provider_request_id,
    alpha_run_lifecycle_payload,
    fold_alpha_run_lifecycle,
    thaw_successful_worktree_spec,
    thaw_worktree_spec,
)
from blackcell.orchestration.alpha_replay import (
    AlphaArtifactReaderPort,
    AlphaReplayCheckExpectation,
    AlphaReplayNodeExpectation,
    build_alpha_review_context_from_artifacts,
    verify_alpha_run_artifacts,
)
from blackcell.orchestration.alpha_review import AlphaReviewContext
from blackcell.orchestration.alpha_review_lifecycle import (
    ALPHA_REVIEW_EVENT_TYPES,
    AlphaReviewCandidate,
    alpha_review_id,
)
from blackcell.orchestration.alpha_verify_lifecycle import ALPHA_VERIFICATION_EVENT_TYPES
from blackcell.orchestration.alpha_verify_replay import replay_alpha_verification

_PROJECT_REGISTERED = "alpha.project.registered"
_INTENT_ACCEPTED = "alpha.intent.accepted"
_PLAN_ACCEPTED = "alpha.plan.accepted"
_ALPHA_EVENT_TYPES = frozenset(
    {
        _PROJECT_REGISTERED,
        _INTENT_ACCEPTED,
        _PLAN_ACCEPTED,
        *ALPHA_RUN_EVENT_TYPES,
        *ALPHA_REVIEW_EVENT_TYPES,
        *ALPHA_VERIFICATION_EVENT_TYPES,
    }
)
_PROVIDER_DISPATCH_AMBIGUOUS = "alpha-provider-dispatch-ambiguous"
_MAX_RETAINED_SUCCESSFUL_WORKTREES = 1_024


@dataclass(frozen=True, slots=True)
class AlphaReadyNode:
    """One dependency-ready node selected in durable queued-run order."""

    run_id: str
    node: AlphaPlanNode


@dataclass(frozen=True, slots=True)
class AlphaPreparedNode:
    """Host-only authority returned after a lease and clean checkout are durable."""

    spec: WorktreeExecutionSpec
    inspection: WorktreeInspection
    node: AlphaPlanNode
    intent: AlphaIntentRequest
    correlation_id: str
    claim_event_id: str
    prepared_event_id: str


@dataclass(frozen=True, slots=True)
class AlphaWorktreeMaintenanceReport:
    pending_recovered: int
    cleanup_requested: int
    cleaned: int
    failed: int
    retained: int
    quota_satisfied: bool


@dataclass(frozen=True, slots=True)
class _SuccessfulWorktreeCandidate:
    run_id: str
    node_id: str
    spec: WorktreeExecutionSpec
    head_commit: str
    success_position: int
    cleanup_status: AlphaWorktreeCleanupStatus


@dataclass(frozen=True, slots=True)
class _LoadedRun:
    request: AlphaRunRequest
    intent: AlphaIntentRequest
    plan: AlphaPlanRequest
    events: tuple[EventEnvelope, ...]
    state: AlphaRunLifecycleState


@dataclass(frozen=True, slots=True)
class _RunTransition:
    event_type: str
    payload: Mapping[str, JsonInput]
    idempotency_key: str


class AlphaRuntimeApiService:
    """Immutable A03 application boundary over the existing event ledger."""

    def __init__(
        self,
        events: EventStore,
        repository_root: Path | str,
        *,
        isolation_root: Path | str | None = None,
        worktrees: GitWorktreeLifecycle | None = None,
        artifacts: AlphaArtifactReaderPort | None = None,
    ) -> None:
        try:
            root = Path(repository_root).resolve(strict=True)
        except OSError as error:
            raise ValueError("alpha repository root must exist") from error
        if not root.is_dir():
            raise ValueError("alpha repository root must be a directory")
        if isolation_root is None:
            resolved_isolation = events.path.parent.resolve() / "alpha-worktrees"
        else:
            candidate = Path(isolation_root)
            if not candidate.is_absolute():
                raise ValueError("alpha isolation root must be absolute")
            try:
                resolved_parent = candidate.parent.resolve(strict=True)
            except OSError as error:
                raise ValueError("alpha isolation parent must exist") from error
            resolved_isolation = resolved_parent / candidate.name
        if artifacts is not None and artifacts.database_path.resolve() != events.path.resolve():
            raise ValueError("alpha artifact store does not match the event database")
        self._events = events
        self._repository_root = root
        self._isolation_root = resolved_isolation
        self._worktrees = worktrees or GitWorktreeLifecycle()
        self._artifacts = artifacts

    def register_project(
        self,
        request: AlphaProjectRequest,
        *,
        principal_id: str,
    ) -> AlphaProjectResponse:
        _principal(principal_id)
        self._require_project_root(request.root)
        event = self._record_immutable(
            stream_id=_project_stream(request.project_id),
            event_type=_PROJECT_REGISTERED,
            request=request,
            principal_id=principal_id,
        )
        return _project_response(event)

    def accept_intent(
        self,
        request: AlphaIntentRequest,
        *,
        principal_id: str,
    ) -> AlphaIntentResponse:
        _principal(principal_id)
        project_event = self._required_event(
            _project_stream(request.project_id), _PROJECT_REGISTERED
        )
        project = _decode_request(project_event, AlphaProjectRequest)
        if project.project_id != request.project_id:
            raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
        event = self._record_immutable(
            stream_id=_intent_stream(request.intent_id),
            event_type=_INTENT_ACCEPTED,
            request=request,
            principal_id=principal_id,
            correlation_id=project_event.correlation_id,
            causation_id=project_event.event_id,
            references={"project": _event_reference(project_event)},
        )
        return _intent_response(event)

    def accept_plan(
        self,
        request: AlphaPlanRequest,
        *,
        principal_id: str,
    ) -> AlphaPlanResponse:
        _principal(principal_id)
        project_event = self._required_event(
            _project_stream(request.project_id), _PROJECT_REGISTERED
        )
        intent_event = self._required_event(_intent_stream(request.intent_id), _INTENT_ACCEPTED)
        project = _decode_request(project_event, AlphaProjectRequest)
        intent = _decode_request(intent_event, AlphaIntentRequest)
        if (
            project.project_id != request.project_id
            or intent.project_id != request.project_id
            or intent.intent_id != request.intent_id
        ):
            raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
        _require_reference(intent_event, "project", project_event)
        event = self._record_immutable(
            stream_id=_plan_stream(request.plan_id),
            event_type=_PLAN_ACCEPTED,
            request=request,
            principal_id=principal_id,
            correlation_id=project_event.correlation_id,
            causation_id=intent_event.event_id,
            references={
                "project": _event_reference(project_event),
                "intent": _event_reference(intent_event),
            },
        )
        return _plan_response(event)

    def submit_run(
        self,
        request: AlphaRunRequest,
        *,
        principal_id: str,
    ) -> AlphaRunResponse:
        _principal(principal_id)
        project_event = self._required_event(
            _project_stream(request.project_id), _PROJECT_REGISTERED
        )
        intent_event = self._required_event(_intent_stream(request.intent_id), _INTENT_ACCEPTED)
        plan_event = self._required_event(_plan_stream(request.plan_id), _PLAN_ACCEPTED)
        project = _decode_request(project_event, AlphaProjectRequest)
        intent = _decode_request(intent_event, AlphaIntentRequest)
        plan = _decode_request(plan_event, AlphaPlanRequest)
        if (
            project.project_id != request.project_id
            or intent.project_id != request.project_id
            or intent.intent_id != request.intent_id
            or plan.project_id != request.project_id
            or plan.intent_id != request.intent_id
            or plan.plan_id != request.plan_id
        ):
            raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
        _require_reference(intent_event, "project", project_event)
        _require_reference(plan_event, "project", project_event)
        _require_reference(plan_event, "intent", intent_event)
        self._record_run_queued(
            stream_id=_run_stream(request.run_id),
            request=request,
            principal_id=principal_id,
            correlation_id=project_event.correlation_id,
            causation_id=plan_event.event_id,
            references={
                "project": _event_reference(project_event),
                "intent": _event_reference(intent_event),
                "plan": _event_reference(plan_event),
            },
            extra={"status": "queued"},
        )
        return _run_response(self._load_run(request.run_id))

    def inspect_run(self, run_id: str) -> AlphaRunResponse:
        _identifier(run_id)
        return _run_response(self._load_run(run_id))

    def next_ready_node(self) -> AlphaReadyNode | None:
        """Return the first dependency-ready node in global queued-run order."""

        for run_id in self._run_ids():
            loaded = self._load_run(run_id)
            if (
                loaded.state.status
                not in {AlphaRunLifecycleStatus.QUEUED, AlphaRunLifecycleStatus.RUNNING}
                or loaded.state.cancellation_requested
                or loaded.state.active_lease is not None
            ):
                continue
            states = {node.node_id: node for node in loaded.state.nodes}
            by_id = {node.node_id: node for node in loaded.plan.nodes}
            for node_id in alpha_plan_topological_order(loaded.plan.nodes):
                state = states[node_id]
                node = by_id[node_id]
                if state.status is AlphaNodeLifecycleStatus.PENDING and all(
                    states[dependency].status is AlphaNodeLifecycleStatus.SUCCEEDED
                    for dependency in node.depends_on
                ):
                    return AlphaReadyNode(run_id=run_id, node=node)
        return None

    def should_cancel_node(self, spec: WorktreeExecutionSpec) -> bool:
        """Return true when cancellation or fencing requires an active worker to stop."""

        if not isinstance(spec, WorktreeExecutionSpec):
            return True
        try:
            loaded = self._load_run(spec.lease.run_id)
        except RuntimeApiError:
            return True
        active = loaded.state.active_lease
        return (
            loaded.state.cancellation_requested
            or active is None
            or active.lease_digest != spec.lease.digest
            or active.worktree_spec_digest != spec.digest
            or active.worker_id != spec.lease.worker_id
        )

    def cancel_run(
        self,
        run_id: str,
        request: AlphaCancelRunRequest,
        *,
        principal_id: str,
    ) -> AlphaRunResponse:
        """Durably request cooperative cancellation without running cleanup code inline."""

        _identifier(run_id)
        _principal(principal_id)
        if not isinstance(request, AlphaCancelRunRequest):
            raise RuntimeApiError(RuntimeApiFailureCode.INVALID_REQUEST)
        loaded = self._load_run(run_id)
        if loaded.state.status is AlphaRunLifecycleStatus.CANCELED:
            return _run_response(loaded)
        if loaded.state.status in {
            AlphaRunLifecycleStatus.SUCCEEDED,
            AlphaRunLifecycleStatus.FAILED,
        }:
            raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
        if loaded.state.cancellation_requested:
            return _run_response(loaded)

        request_value = _struct_value(request)
        transitions = [
            _RunTransition(
                ALPHA_RUN_CANCEL_REQUESTED,
                {
                    "request": request_value,
                    "request_digest": json_digest(request_value),
                    "status": "cancel-requested",
                },
                f"cancel:{request.idempotency_key}",
            )
        ]
        if loaded.state.active_lease is None:
            retained = any(node.retained_worktree for node in loaded.state.nodes)
            transitions.append(
                _RunTransition(
                    ALPHA_RUN_CANCELED,
                    {
                        "run_id": run_id,
                        "status": "canceled",
                        "retained_worktree": retained,
                    },
                    f"cancel-terminal:{request.idempotency_key}",
                )
            )
        self._append_run_transitions(loaded, tuple(transitions), principal_id=principal_id)
        return _run_response(self._load_run(run_id))

    def prepare_node(
        self,
        run_id: str,
        node_id: str,
        *,
        worker_id: str,
        lease_expires_at: datetime,
        claimed_at: datetime | None = None,
    ) -> AlphaPreparedNode:
        """Persist one fenced claim, then create and record its exact clean worktree."""

        _identifier(run_id)
        _identifier(node_id)
        _principal(worker_id)
        loaded = self._load_run(run_id)
        if (
            loaded.state.status
            not in {
                AlphaRunLifecycleStatus.QUEUED,
                AlphaRunLifecycleStatus.RUNNING,
            }
            or loaded.state.cancellation_requested
            or loaded.state.active_lease is not None
        ):
            raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
        try:
            node = next(item for item in loaded.state.nodes if item.node_id == node_id)
            plan_node = next(item for item in loaded.plan.nodes if item.node_id == node_id)
        except StopIteration:
            raise RuntimeApiError(RuntimeApiFailureCode.NOT_FOUND) from None
        if node.status is not AlphaNodeLifecycleStatus.PENDING:
            raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)

        at = _aware_timestamp(claimed_at or utc_now())
        expires_at = _aware_timestamp(lease_expires_at)
        if expires_at <= at:
            raise RuntimeApiError(RuntimeApiFailureCode.INVALID_REQUEST)
        fence = max(item.fencing_token for item in loaded.state.nodes) + 1
        lease = WorktreeLeaseIdentity(
            run_id=run_id,
            node_id=node_id,
            attempt=node.attempts + 1,
            fencing_token=fence,
            worker_id=worker_id,
        )
        spec = WorktreeExecutionSpec(
            lease=lease,
            repository_root=self._repository_root,
            isolation_root=self._isolation_root,
            base_commit=_node_base_commit(
                loaded.plan,
                node_id,
                {
                    state.node_id: state.head_commit
                    for state in loaded.state.nodes
                    if state.head_commit is not None
                },
            ),
            allowed_paths=plan_node.allowed_paths,
            max_changed_paths=plan_node.budget.max_changed_files,
        )
        spec_payload = worktree_execution_spec_payload(spec)
        claimed = self._append_run_transitions(
            loaded,
            (
                _RunTransition(
                    ALPHA_NODE_CLAIMED,
                    {
                        "run_id": run_id,
                        "node_id": node_id,
                        "attempt": lease.attempt,
                        "fencing_token": lease.fencing_token,
                        "worker_id": worker_id,
                        "lease_digest": lease.digest,
                        "expires_at": expires_at.isoformat(),
                        "worktree_spec_digest": spec.digest,
                        "worktree_spec": spec_payload,
                        "status": "claimed",
                    },
                    f"claim:{lease.digest}",
                ),
            ),
            principal_id=worker_id,
            recorded_at=at,
        )[0]
        try:
            inspection = self._worktrees.create(spec)
        except WorktreeLifecycleError as error:
            self._record_node_failure(
                spec,
                failure_code=error.code.value,
                result_digest=None,
                principal_id=worker_id,
                allow_unprepared=True,
            )
            raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT) from error
        inspection_payload = worktree_inspection_payload(inspection)
        current = self._load_run(run_id)
        if current.state.cancellation_requested:
            self._record_canceled_node(
                current,
                spec,
                principal_id=worker_id,
                inspection=inspection,
            )
            raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
        prepared = self._append_run_transitions(
            current,
            (
                _RunTransition(
                    ALPHA_NODE_WORKTREE_PREPARED,
                    {
                        "run_id": run_id,
                        "node_id": node_id,
                        "lease_digest": lease.digest,
                        "inspection_digest": json_digest(inspection_payload),
                        "inspection": inspection_payload,
                        "status": "worktree-prepared",
                    },
                    f"worktree-prepared:{lease.digest}",
                ),
            ),
            principal_id=worker_id,
        )[0]
        return AlphaPreparedNode(
            spec=spec,
            inspection=inspection,
            node=plan_node,
            intent=loaded.intent,
            correlation_id=loaded.state.queued_event.correlation_id,
            claim_event_id=claimed.event_id,
            prepared_event_id=prepared.event_id,
        )

    def record_provider_dispatch(
        self,
        spec: WorktreeExecutionSpec,
        *,
        provider_request_id: str,
        context_digest: str,
        context_artifact_digest: str,
        principal_id: str,
        dispatched_at: datetime | None = None,
    ) -> str:
        """Fence one provider call after its exact canonical context is durably stored."""

        _principal(principal_id)
        _identifier(provider_request_id)
        _content_digest(context_digest)
        _content_digest(context_artifact_digest)
        if (
            provider_request_id != alpha_provider_request_id(spec.lease.digest)
            or context_artifact_digest != context_digest
        ):
            raise RuntimeApiError(RuntimeApiFailureCode.INVALID_REQUEST)
        loaded = self._load_run(spec.lease.run_id)
        self._require_active_spec(loaded, spec, require_prepared=True)
        self._require_active_worker(loaded, principal_id)
        plan_node = next(
            (node for node in loaded.plan.nodes if node.node_id == spec.lease.node_id),
            None,
        )
        active = loaded.state.active_lease
        if (
            plan_node is None
            or "repository-write" not in plan_node.effects
            or active is None
            or active.provider_dispatch_event_id is not None
        ):
            raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
        event = self._append_run_transitions(
            loaded,
            (
                _RunTransition(
                    ALPHA_NODE_PROVIDER_DISPATCH_STARTED,
                    {
                        "run_id": spec.lease.run_id,
                        "node_id": spec.lease.node_id,
                        "lease_digest": spec.lease.digest,
                        "provider_request_id": provider_request_id,
                        "context_digest": context_digest,
                        "context_artifact_digest": context_artifact_digest,
                        "status": "provider-dispatch-started",
                    },
                    f"provider-dispatch-started:{spec.lease.digest}",
                ),
            ),
            principal_id=principal_id,
            recorded_at=dispatched_at,
        )[0]
        return event.event_id

    def record_node_success(
        self,
        spec: WorktreeExecutionSpec,
        *,
        result_digest: str,
        principal_id: str,
        completed_at: datetime | None = None,
    ) -> AlphaRunResponse:
        """Record success only for the exact live fence and an already-committed checkout."""

        _principal(principal_id)
        _content_digest(result_digest)
        loaded = self._load_run(spec.lease.run_id)
        self._require_active_spec(loaded, spec, require_prepared=True)
        self._require_active_worker(loaded, principal_id)
        try:
            inspection = self._worktrees.retain(spec)
        except WorktreeLifecycleError as error:
            raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT) from error
        if not inspection.clean or not inspection.path_policy_compliant:
            raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
        inspection_payload = worktree_inspection_payload(inspection)
        transitions = [
            _RunTransition(
                ALPHA_NODE_SUCCEEDED,
                {
                    "run_id": spec.lease.run_id,
                    "node_id": spec.lease.node_id,
                    "lease_digest": spec.lease.digest,
                    "result_digest": result_digest,
                    "head_commit": inspection.head_commit,
                    "inspection_digest": json_digest(inspection_payload),
                    "inspection": inspection_payload,
                    "retained_worktree": True,
                    "status": "succeeded",
                },
                f"node-succeeded:{spec.lease.digest}",
            )
        ]
        if all(
            node.node_id == spec.lease.node_id or node.status is AlphaNodeLifecycleStatus.SUCCEEDED
            for node in loaded.state.nodes
        ):
            transitions.append(
                _RunTransition(
                    ALPHA_RUN_SUCCEEDED,
                    {
                        "run_id": spec.lease.run_id,
                        "status": "succeeded",
                        "retained_worktree": True,
                    },
                    f"run-succeeded:{spec.lease.digest}",
                )
            )
        self._append_run_transitions(
            loaded,
            tuple(transitions),
            principal_id=principal_id,
            recorded_at=completed_at,
        )
        return _run_response(self._load_run(spec.lease.run_id))

    def record_node_failure(
        self,
        spec: WorktreeExecutionSpec,
        *,
        failure_code: str,
        result_digest: str | None = None,
        principal_id: str,
        failed_at: datetime | None = None,
    ) -> AlphaRunResponse:
        """Record a stable content-free terminal failure for the exact active fence."""

        return self._record_node_failure(
            spec,
            failure_code=failure_code,
            result_digest=result_digest,
            principal_id=principal_id,
            failed_at=failed_at,
            allow_unprepared=False,
        )

    def acknowledge_cancellation(
        self,
        spec: WorktreeExecutionSpec,
        *,
        result_digest: str | None = None,
        principal_id: str,
    ) -> AlphaRunResponse:
        """Retain the active checkout and close cooperative cancellation for its exact fence."""

        _principal(principal_id)
        if result_digest is not None:
            _content_digest(result_digest)
        loaded = self._load_run(spec.lease.run_id)
        self._require_active_spec(loaded, spec, require_prepared=False)
        self._require_active_worker(loaded, principal_id)
        if not loaded.state.cancellation_requested:
            raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
        try:
            inspection = self._worktrees.retain(spec)
        except WorktreeLifecycleError as error:
            self._record_reconciliation_required(
                loaded,
                spec,
                principal_id=principal_id,
                inspection=None,
                failure_code=error.code.value,
            )
            raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT) from error
        self._record_canceled_node(
            loaded,
            spec,
            principal_id=principal_id,
            inspection=inspection,
            result_digest=result_digest,
        )
        return _run_response(self._load_run(spec.lease.run_id))

    def reconcile_startup(self, *, principal_id: str) -> tuple[AlphaRunResponse, ...]:
        """Invalidate every surviving active alpha lease after exclusive daemon restart."""

        _principal(principal_id)
        reconciled: list[AlphaRunResponse] = []
        for run_id in self._run_ids():
            loaded = self._load_run(run_id)
            active = loaded.state.active_lease
            if active is None:
                continue
            try:
                spec = worktree_execution_spec_from_mapping(thaw_worktree_spec(active))
            except (AlphaLifecycleError, WorktreeLifecycleError) as error:
                raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT) from error
            if (
                spec.repository_root != self._repository_root
                or spec.isolation_root != self._isolation_root
                or spec.digest != active.worktree_spec_digest
                or spec.lease.digest != active.lease_digest
            ):
                raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
            try:
                inspection = self._worktrees.retain(spec)
            except WorktreeLifecycleError as error:
                if error.code is WorktreeFailureCode.WORKTREE_NOT_FOUND:
                    if loaded.state.cancellation_requested:
                        self._record_missing_canceled_node(
                            loaded,
                            spec,
                            principal_id=principal_id,
                        )
                    elif active.provider_dispatch_event_id is not None:
                        self._record_reconciliation_required(
                            loaded,
                            spec,
                            principal_id=principal_id,
                            inspection=None,
                            failure_code=_PROVIDER_DISPATCH_AMBIGUOUS,
                        )
                    else:
                        self._record_requeued_node(
                            loaded,
                            spec,
                            principal_id=principal_id,
                            inspection=None,
                        )
                else:
                    self._record_reconciliation_required(
                        loaded,
                        spec,
                        principal_id=principal_id,
                        inspection=None,
                        failure_code=error.code.value,
                    )
            else:
                if loaded.state.cancellation_requested:
                    self._record_canceled_node(
                        loaded,
                        spec,
                        principal_id=principal_id,
                        inspection=inspection,
                    )
                elif active.provider_dispatch_event_id is not None:
                    self._record_reconciliation_required(
                        loaded,
                        spec,
                        principal_id=principal_id,
                        inspection=inspection,
                        failure_code=_PROVIDER_DISPATCH_AMBIGUOUS,
                    )
                elif _inspection_is_unchanged(inspection):
                    self._record_requeued_node(
                        loaded,
                        spec,
                        principal_id=principal_id,
                        inspection=inspection,
                    )
                else:
                    self._record_reconciliation_required(
                        loaded,
                        spec,
                        principal_id=principal_id,
                        inspection=inspection,
                        failure_code=None,
                    )
            reconciled.append(_run_response(self._load_run(run_id)))
        return tuple(reconciled)

    def maintain_successful_worktrees(
        self,
        *,
        max_retained: int,
        principal_id: str,
    ) -> AlphaWorktreeMaintenanceReport:
        """Recover requested cleanup, then remove oldest eligible successful checkouts."""

        _principal(principal_id)
        if (
            isinstance(max_retained, bool)
            or not isinstance(max_retained, int)
            or not 0 <= max_retained <= _MAX_RETAINED_SUCCESSFUL_WORKTREES
        ):
            raise RuntimeApiError(RuntimeApiFailureCode.INVALID_REQUEST)
        pending_recovered = 0
        requested = 0
        cleaned = 0
        failed = 0

        pending = tuple(
            candidate
            for candidate in self._successful_worktree_candidates()
            if candidate.cleanup_status is AlphaWorktreeCleanupStatus.REQUESTED
        )
        for candidate in pending:
            completed = self._finish_successful_worktree_cleanup(
                candidate,
                principal_id=principal_id,
            )
            pending_recovered += 1
            cleaned += int(completed)
            failed += int(not completed)

        candidates = self._successful_worktree_candidates()
        excess = max(0, len(candidates) - max_retained)
        eligible = tuple(
            candidate
            for candidate in candidates
            if candidate.cleanup_status is AlphaWorktreeCleanupStatus.ELIGIBLE
        )
        for candidate in eligible[:excess]:
            self._request_successful_worktree_cleanup(candidate, principal_id=principal_id)
            requested += 1
            completed = self._finish_successful_worktree_cleanup(
                candidate,
                principal_id=principal_id,
            )
            cleaned += int(completed)
            failed += int(not completed)

        retained = len(self._successful_worktree_candidates())
        return AlphaWorktreeMaintenanceReport(
            pending_recovered=pending_recovered,
            cleanup_requested=requested,
            cleaned=cleaned,
            failed=failed,
            retained=retained,
            quota_satisfied=retained <= max_retained,
        )

    def list_events(self, *, after_cursor: int, limit: int) -> AlphaEventPageResponse:
        if (
            isinstance(after_cursor, bool)
            or not isinstance(after_cursor, int)
            or after_cursor < 0
            or isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_ALPHA_EVENT_PAGE_SIZE
        ):
            raise RuntimeApiError(RuntimeApiFailureCode.INVALID_REQUEST)
        scanned = self._events.read_all(after_position=after_cursor, limit=limit + 1)
        window = scanned[:limit]
        alpha_events = tuple(
            _event_response(event) for event in window if event.event_type.startswith("alpha.")
        )
        next_cursor = _global_position(window[-1]) if window else after_cursor
        return AlphaEventPageResponse(
            after_cursor=after_cursor,
            limit=limit,
            scanned_events=len(window),
            events=alpha_events,
            next_cursor=next_cursor,
            has_more=len(scanned) > limit,
        )

    def replay_run(self, run_id: str) -> AlphaReplayResponse:
        _identifier(run_id)
        loaded = self._load_run(run_id)
        run_event = loaded.state.queued_event
        run = loaded.request
        project_event = self._required_event(_project_stream(run.project_id), _PROJECT_REGISTERED)
        intent_event = self._required_event(_intent_stream(run.intent_id), _INTENT_ACCEPTED)
        plan_event = self._required_event(_plan_stream(run.plan_id), _PLAN_ACCEPTED)
        _require_reference(run_event, "project", project_event)
        _require_reference(run_event, "intent", intent_event)
        _require_reference(run_event, "plan", plan_event)
        _require_reference(intent_event, "project", project_event)
        _require_reference(plan_event, "project", project_event)
        _require_reference(plan_event, "intent", intent_event)

        project_response = _project_response(project_event)
        intent_response = _intent_response(intent_event)
        plan_response = _plan_response(plan_event)
        run_response = _run_response(loaded)
        state_digest = json_digest(
            {
                "project": _request_value(project_event),
                "intent": _request_value(intent_event),
                "plan": _request_value(plan_event),
                "run": _request_value(run_event),
                "lifecycle": alpha_run_lifecycle_payload(loaded.state),
            }
        )
        artifact_report = verify_alpha_run_artifacts(
            self._artifacts,
            run_id=run_id,
            nodes=_artifact_expectations(loaded),
        )
        verification_report = replay_alpha_verification(
            self._events,
            self._artifacts,
            run_id=run_id,
        )
        return AlphaReplayResponse(
            run_id=run_id,
            project=project_response,
            intent=intent_response,
            plan=plan_response,
            run=run_response,
            processed_events=3 + len(loaded.events),
            state_digest=state_digest,
            artifact_integrity=artifact_report.status.value,
            artifacts=tuple(
                AlphaReplayArtifactResponse(
                    node_id=artifact.node_id,
                    role=artifact.role.value,
                    check_id=artifact.check_id,
                    digest=artifact.digest,
                    size_bytes=artifact.size_bytes,
                    media_type=artifact.media_type,
                    encoding=artifact.encoding,
                    verified=artifact.verified,
                )
                for artifact in artifact_report.artifacts
            ),
            findings=tuple(
                AlphaReplayFindingResponse(
                    code=finding.code.value,
                    node_id=finding.node_id,
                    role=None if finding.role is None else finding.role.value,
                    check_id=finding.check_id,
                    artifact_digest=finding.artifact_digest,
                )
                for finding in artifact_report.findings
            ),
            artifact_evidence_digest=artifact_report.evidence_digest,
            verification=AlphaVerificationReplayResponse(
                lifecycle_status=verification_report.lifecycle_status.value,
                verification_id=verification_report.verification_id,
                review_id=verification_report.review_id,
                attempt=verification_report.attempt,
                fencing_token=verification_report.fencing_token,
                verdict=(
                    None
                    if verification_report.verdict is None
                    else verification_report.verdict.value
                ),
                failure_code=verification_report.failure_code,
                report_artifact_digest=verification_report.report_artifact_digest,
                report_size_bytes=verification_report.report_size_bytes,
                report_media_type=verification_report.report_media_type,
                report_encoding=verification_report.report_encoding,
                matrix_digest=verification_report.matrix_digest,
                artifact_integrity=verification_report.artifact_integrity.value,
                finding_code=(
                    None
                    if verification_report.finding_code is None
                    else verification_report.finding_code.value
                ),
                processed_events=verification_report.processed_events,
                evidence_digest=verification_report.evidence_digest,
            ),
        )

    def review_candidates(self) -> tuple[AlphaReviewCandidate, ...]:
        """Return successful execution snapshots in durable run order."""

        return tuple(self.review_candidate(run_id) for run_id in self.review_run_ids())

    def review_run_ids(self) -> tuple[str, ...]:
        """Return successful execution run IDs without reading their artifact graphs."""

        run_ids: list[str] = []
        for run_id in self._run_ids():
            loaded = self._load_run(run_id)
            if loaded.state.status is AlphaRunLifecycleStatus.SUCCEEDED:
                run_ids.append(run_id)
        return tuple(run_ids)

    def review_candidate(self, run_id: str) -> AlphaReviewCandidate:
        """Build one exact terminal execution and artifact-evidence snapshot."""

        _identifier(run_id)
        loaded = self._load_run(run_id)
        if loaded.state.status is not AlphaRunLifecycleStatus.SUCCEEDED:
            raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
        return self._review_candidate(loaded)

    def prepare_review_context(self, candidate: AlphaReviewCandidate) -> AlphaReviewContext:
        """Revalidate one claimed execution snapshot and build its live-free review context."""

        if not isinstance(candidate, AlphaReviewCandidate):
            raise RuntimeApiError(RuntimeApiFailureCode.INVALID_REQUEST)
        loaded = self._load_run(candidate.run_id)
        if (
            loaded.state.status is not AlphaRunLifecycleStatus.SUCCEEDED
            or self._review_candidate(loaded) != candidate
        ):
            raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
        return build_alpha_review_context_from_artifacts(
            self._artifacts,
            run_id=candidate.run_id,
            project_id=loaded.request.project_id,
            intent_id=loaded.request.intent_id,
            plan_id=loaded.request.plan_id,
            objective=loaded.intent.objective,
            constraints=loaded.intent.constraints,
            base_commit=loaded.plan.base_commit,
            state_digest=candidate.state_digest,
            nodes=_artifact_expectations(loaded),
        )

    def _review_candidate(self, loaded: _LoadedRun) -> AlphaReviewCandidate:
        terminal_index = next(
            (
                index
                for index, event in reversed(tuple(enumerate(loaded.events)))
                if event.event_type == ALPHA_RUN_SUCCEEDED
            ),
            None,
        )
        if terminal_index is None:
            raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
        terminal = loaded.events[terminal_index]
        terminal_state = fold_alpha_run_lifecycle(
            loaded.request.run_id,
            {node.node_id: node.depends_on for node in loaded.plan.nodes},
            loaded.events[: terminal_index + 1],
        )
        project_event = self._required_event(
            _project_stream(loaded.request.project_id),
            _PROJECT_REGISTERED,
        )
        intent_event = self._required_event(
            _intent_stream(loaded.request.intent_id),
            _INTENT_ACCEPTED,
        )
        plan_event = self._required_event(
            _plan_stream(loaded.request.plan_id),
            _PLAN_ACCEPTED,
        )
        state_digest = json_digest(
            {
                "project": _request_value(project_event),
                "intent": _request_value(intent_event),
                "plan": _request_value(plan_event),
                "run": _request_value(loaded.state.queued_event),
                "lifecycle": alpha_run_lifecycle_payload(terminal_state),
            }
        )
        artifact_report = verify_alpha_run_artifacts(
            self._artifacts,
            run_id=loaded.request.run_id,
            nodes=_artifact_expectations(loaded),
        )
        return AlphaReviewCandidate(
            run_id=loaded.request.run_id,
            review_id=alpha_review_id(loaded.request.run_id, terminal.payload_hash),
            correlation_id=loaded.state.queued_event.correlation_id,
            run_event_id=terminal.event_id,
            run_event_digest=terminal.payload_hash,
            state_digest=state_digest,
            artifact_evidence_digest=artifact_report.evidence_digest,
        )

    def _record_run_queued(
        self,
        *,
        stream_id: str,
        request: AlphaRunRequest,
        principal_id: str,
        correlation_id: str,
        causation_id: str,
        references: Mapping[str, Mapping[str, JsonInput]],
        extra: Mapping[str, JsonInput],
    ) -> EventEnvelope:
        payload = _request_event_payload(
            request,
            principal_id=principal_id,
            references=references,
            extra=extra,
        )
        existing = self._events.read_stream(stream_id)
        if existing:
            return _require_run_idempotent(existing, payload)
        event = EventEnvelope.create(
            stream_id=stream_id,
            stream_sequence=1,
            event_type=ALPHA_RUN_QUEUED,
            actor=principal_id,
            source=ALPHA_EVENT_SOURCE,
            payload=payload,
            correlation_id=correlation_id,
            causation_id=causation_id,
            idempotency_key=request.idempotency_key,
        )
        try:
            return self._events.append(event, expected_sequence=0)
        except ConcurrencyError, EventConflictError, IdempotencyConflict:
            raced = self._events.read_stream(stream_id)
            if raced:
                return _require_run_idempotent(raced, payload)
            raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT) from None

    def _load_run(self, run_id: str) -> _LoadedRun:
        events = self._events.read_stream(_run_stream(run_id))
        if not events:
            raise RuntimeApiError(RuntimeApiFailureCode.NOT_FOUND)
        queued = events[0]
        if queued.event_type != ALPHA_RUN_QUEUED:
            raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
        request = _decode_request(queued, AlphaRunRequest)
        if request.run_id != run_id:
            raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
        project_event = self._required_event(
            _project_stream(request.project_id), _PROJECT_REGISTERED
        )
        intent_event = self._required_event(_intent_stream(request.intent_id), _INTENT_ACCEPTED)
        plan_event = self._required_event(_plan_stream(request.plan_id), _PLAN_ACCEPTED)
        project = _decode_request(project_event, AlphaProjectRequest)
        intent = _decode_request(intent_event, AlphaIntentRequest)
        plan = _decode_request(plan_event, AlphaPlanRequest)
        if (
            project.project_id != request.project_id
            or intent.project_id != request.project_id
            or intent.intent_id != request.intent_id
            or plan.project_id != request.project_id
            or plan.intent_id != request.intent_id
            or plan.plan_id != request.plan_id
        ):
            raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
        _require_reference(queued, "project", project_event)
        _require_reference(queued, "intent", intent_event)
        _require_reference(queued, "plan", plan_event)
        dependencies = {node.node_id: node.depends_on for node in plan.nodes}
        try:
            state = fold_alpha_run_lifecycle(run_id, dependencies, events)
        except AlphaLifecycleError as error:
            raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT) from error
        self._validate_worktree_evidence(events, plan)
        return _LoadedRun(request=request, intent=intent, plan=plan, events=events, state=state)

    def _validate_worktree_evidence(
        self,
        events: tuple[EventEnvelope, ...],
        plan: AlphaPlanRequest,
    ) -> None:
        specifications: dict[str, WorktreeExecutionSpec] = {}
        successful_heads: dict[str, str] = {}
        plan_nodes = {node.node_id: node for node in plan.nodes}
        try:
            for event in events:
                if event.event_type == ALPHA_NODE_CLAIMED:
                    raw_spec = _thawed_mapping(event.payload.get("worktree_spec"))
                    spec = worktree_execution_spec_from_mapping(raw_spec)
                    node = plan_nodes[spec.lease.node_id]
                    if (
                        spec.repository_root != self._repository_root
                        or spec.isolation_root != self._isolation_root
                        or spec.base_commit
                        != _node_base_commit(plan, spec.lease.node_id, successful_heads)
                        or spec.allowed_paths != node.allowed_paths
                        or spec.max_changed_paths != node.budget.max_changed_files
                        or event.payload.get("worktree_spec_digest") != spec.digest
                        or event.payload.get("lease_digest") != spec.lease.digest
                    ):
                        raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_SPEC)
                    specifications[spec.lease.digest] = spec
                if event.event_type == ALPHA_NODE_PROVIDER_DISPATCH_STARTED:
                    lease_digest = event.payload.get("lease_digest")
                    if not isinstance(lease_digest, str):
                        raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_SPEC)
                    spec = specifications[lease_digest]
                    if "repository-write" not in plan_nodes[spec.lease.node_id].effects:
                        raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_SPEC)
                if event.event_type == ALPHA_NODE_WORKTREE_CLEANED:
                    raw_removal = _thawed_mapping(event.payload.get("removal"))
                    removal = worktree_removal_from_mapping(raw_removal)
                    spec = specifications[removal.lease_digest]
                    if (
                        removal.spec_digest != spec.digest
                        or removal.worktree_path != spec.worktree_path
                        or removal.branch_name != spec.branch_name
                        or removal.retained_head_commit != successful_heads[spec.lease.node_id]
                        or event.payload.get("worktree_spec_digest") != spec.digest
                        or event.payload.get("lease_digest") != spec.lease.digest
                    ):
                        raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_GIT_OUTPUT)
                raw_inspection = event.payload.get("inspection")
                if raw_inspection is None:
                    continue
                inspection = worktree_inspection_from_mapping(_thawed_mapping(raw_inspection))
                spec = specifications[inspection.lease_digest]
                if (
                    inspection.spec_digest != spec.digest
                    or inspection.worktree_path != spec.worktree_path
                    or inspection.branch_name != spec.branch_name
                    or inspection.base_commit != spec.base_commit
                    or inspection.allowed_paths != spec.allowed_paths
                    or inspection.max_changed_paths != spec.max_changed_paths
                ):
                    raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_GIT_OUTPUT)
                if event.event_type == ALPHA_NODE_SUCCEEDED:
                    head_commit = _commit_id(event.payload.get("head_commit"))
                    if inspection.head_commit != head_commit:
                        raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_GIT_OUTPUT)
                    successful_heads[spec.lease.node_id] = head_commit
        except (KeyError, AlphaLifecycleError, WorktreeLifecycleError) as error:
            raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT) from error

    def _append_run_transitions(
        self,
        loaded: _LoadedRun,
        transitions: tuple[_RunTransition, ...],
        *,
        principal_id: str,
        recorded_at: datetime | None = None,
    ) -> tuple[EventEnvelope, ...]:
        if not transitions:
            raise RuntimeApiError(RuntimeApiFailureCode.INVALID_REQUEST)
        at = _aware_timestamp(recorded_at or utc_now())
        events: list[EventEnvelope] = []
        causation_id = loaded.state.latest_event.event_id
        sequence = loaded.state.stream_sequence
        for transition in transitions:
            sequence += 1
            payload: dict[str, JsonInput] = {
                "principal_id": principal_id,
                **dict(transition.payload),
            }
            event = EventEnvelope.create(
                stream_id=_run_stream(loaded.request.run_id),
                stream_sequence=sequence,
                event_type=transition.event_type,
                actor=principal_id,
                source=ALPHA_EVENT_SOURCE,
                payload=payload,
                recorded_at=at,
                effective_at=at,
                correlation_id=loaded.state.queued_event.correlation_id,
                causation_id=causation_id,
                idempotency_key=transition.idempotency_key,
            )
            events.append(event)
            causation_id = event.event_id
        dependencies = {node.node_id: node.depends_on for node in loaded.plan.nodes}
        try:
            fold_alpha_run_lifecycle(
                loaded.request.run_id,
                dependencies,
                (*loaded.events, *events),
            )
            self._validate_worktree_evidence((*loaded.events, *events), loaded.plan)
        except AlphaLifecycleError as error:
            raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT) from error
        try:
            stored = self._events.append_many(
                tuple(events),
                expected_sequences={
                    _run_stream(loaded.request.run_id): loaded.state.stream_sequence
                },
            )
        except (ConcurrencyError, EventConflictError, IdempotencyConflict) as error:
            raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT) from error
        try:
            fold_alpha_run_lifecycle(
                loaded.request.run_id,
                dependencies,
                (*loaded.events, *stored),
            )
        except AlphaLifecycleError as error:
            raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT) from error
        return stored

    @staticmethod
    def _require_active_spec(
        loaded: _LoadedRun,
        spec: WorktreeExecutionSpec,
        *,
        require_prepared: bool,
    ) -> None:
        active = loaded.state.active_lease
        if (
            not isinstance(spec, WorktreeExecutionSpec)
            or active is None
            or active.node_id != spec.lease.node_id
            or active.lease_digest != spec.lease.digest
            or active.worktree_spec_digest != spec.digest
            or (require_prepared and not active.prepared)
        ):
            raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)

    @staticmethod
    def _require_active_worker(loaded: _LoadedRun, principal_id: str) -> None:
        active = loaded.state.active_lease
        if active is None or active.worker_id != principal_id:
            raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)

    def _record_node_failure(
        self,
        spec: WorktreeExecutionSpec,
        *,
        failure_code: str,
        result_digest: str | None,
        principal_id: str,
        failed_at: datetime | None = None,
        allow_unprepared: bool,
    ) -> AlphaRunResponse:
        _principal(principal_id)
        _failure_code(failure_code)
        if result_digest is not None:
            _content_digest(result_digest)
        loaded = self._load_run(spec.lease.run_id)
        self._require_active_spec(loaded, spec, require_prepared=not allow_unprepared)
        self._require_active_worker(loaded, principal_id)
        inspection: WorktreeInspection | None
        if allow_unprepared and not (
            spec.worktree_path.exists() or spec.worktree_path.is_symlink()
        ):
            inspection = None
        else:
            try:
                inspection = self._worktrees.retain(spec)
            except WorktreeLifecycleError as error:
                self._record_reconciliation_required(
                    loaded,
                    spec,
                    principal_id=principal_id,
                    inspection=None,
                    failure_code=error.code.value,
                )
                return _run_response(self._load_run(spec.lease.run_id))
        inspection_payload = None if inspection is None else worktree_inspection_payload(inspection)
        retained = inspection is not None
        self._append_run_transitions(
            loaded,
            (
                _RunTransition(
                    ALPHA_NODE_FAILED,
                    {
                        "run_id": spec.lease.run_id,
                        "node_id": spec.lease.node_id,
                        "lease_digest": spec.lease.digest,
                        "failure_code": failure_code,
                        "result_digest": result_digest,
                        "inspection_digest": None
                        if inspection_payload is None
                        else json_digest(inspection_payload),
                        "inspection": inspection_payload,
                        "retained_worktree": retained,
                        "status": "failed",
                    },
                    f"node-failed:{spec.lease.digest}",
                ),
                _RunTransition(
                    ALPHA_RUN_FAILED,
                    {
                        "run_id": spec.lease.run_id,
                        "status": "failed",
                        "retained_worktree": retained,
                    },
                    f"run-failed:{spec.lease.digest}",
                ),
            ),
            principal_id=principal_id,
            recorded_at=failed_at,
        )
        return _run_response(self._load_run(spec.lease.run_id))

    def _record_canceled_node(
        self,
        loaded: _LoadedRun,
        spec: WorktreeExecutionSpec,
        *,
        principal_id: str,
        inspection: WorktreeInspection,
        result_digest: str | None = None,
    ) -> None:
        self._require_active_spec(loaded, spec, require_prepared=False)
        payload = worktree_inspection_payload(inspection)
        self._append_run_transitions(
            loaded,
            (
                _RunTransition(
                    ALPHA_NODE_CANCELED,
                    {
                        "run_id": spec.lease.run_id,
                        "node_id": spec.lease.node_id,
                        "lease_digest": spec.lease.digest,
                        "result_digest": result_digest,
                        "inspection_digest": json_digest(payload),
                        "inspection": payload,
                        "retained_worktree": True,
                        "status": "canceled",
                    },
                    f"node-canceled:{spec.lease.digest}",
                ),
                _RunTransition(
                    ALPHA_RUN_CANCELED,
                    {
                        "run_id": spec.lease.run_id,
                        "status": "canceled",
                        "retained_worktree": True,
                    },
                    f"run-canceled:{spec.lease.digest}",
                ),
            ),
            principal_id=principal_id,
        )

    def _record_missing_canceled_node(
        self,
        loaded: _LoadedRun,
        spec: WorktreeExecutionSpec,
        *,
        principal_id: str,
    ) -> None:
        self._require_active_spec(loaded, spec, require_prepared=False)
        self._append_run_transitions(
            loaded,
            (
                _RunTransition(
                    ALPHA_NODE_CANCELED,
                    {
                        "run_id": spec.lease.run_id,
                        "node_id": spec.lease.node_id,
                        "lease_digest": spec.lease.digest,
                        "result_digest": None,
                        "inspection_digest": None,
                        "inspection": None,
                        "retained_worktree": False,
                        "status": "canceled",
                    },
                    f"node-canceled:{spec.lease.digest}",
                ),
                _RunTransition(
                    ALPHA_RUN_CANCELED,
                    {
                        "run_id": spec.lease.run_id,
                        "status": "canceled",
                        "retained_worktree": False,
                    },
                    f"run-canceled:{spec.lease.digest}",
                ),
            ),
            principal_id=principal_id,
        )

    def _record_requeued_node(
        self,
        loaded: _LoadedRun,
        spec: WorktreeExecutionSpec,
        *,
        principal_id: str,
        inspection: WorktreeInspection | None,
    ) -> None:
        self._require_active_spec(loaded, spec, require_prepared=False)
        payload = None if inspection is None else worktree_inspection_payload(inspection)
        self._append_run_transitions(
            loaded,
            (
                _RunTransition(
                    ALPHA_NODE_REQUEUED,
                    {
                        "run_id": spec.lease.run_id,
                        "node_id": spec.lease.node_id,
                        "lease_digest": spec.lease.digest,
                        "disposition": "missing" if payload is None else "unchanged",
                        "inspection_digest": None if payload is None else json_digest(payload),
                        "inspection": payload,
                        "status": "requeued",
                    },
                    f"node-requeued:{spec.lease.digest}",
                ),
            ),
            principal_id=principal_id,
        )

    def _record_reconciliation_required(
        self,
        loaded: _LoadedRun,
        spec: WorktreeExecutionSpec,
        *,
        principal_id: str,
        inspection: WorktreeInspection | None,
        failure_code: str | None,
    ) -> None:
        self._require_active_spec(loaded, spec, require_prepared=False)
        if failure_code is not None:
            _failure_code(failure_code)
        payload = None if inspection is None else worktree_inspection_payload(inspection)
        retained = payload is not None
        self._append_run_transitions(
            loaded,
            (
                _RunTransition(
                    ALPHA_NODE_RECONCILIATION_REQUIRED,
                    {
                        "run_id": spec.lease.run_id,
                        "node_id": spec.lease.node_id,
                        "lease_digest": spec.lease.digest,
                        "failure_code": failure_code,
                        "inspection_digest": None if payload is None else json_digest(payload),
                        "inspection": payload,
                        "retained_worktree": retained,
                        "status": "reconciliation-required",
                    },
                    f"node-reconciliation-required:{spec.lease.digest}",
                ),
                _RunTransition(
                    ALPHA_RUN_RECONCILIATION_REQUIRED,
                    {
                        "run_id": spec.lease.run_id,
                        "status": "reconciliation-required",
                        "retained_worktree": retained,
                    },
                    f"run-reconciliation-required:{spec.lease.digest}",
                ),
            ),
            principal_id=principal_id,
        )

    def _successful_worktree_candidates(self) -> tuple[_SuccessfulWorktreeCandidate, ...]:
        candidates: list[_SuccessfulWorktreeCandidate] = []
        for run_id in self._run_ids():
            loaded = self._load_run(run_id)
            success_positions = {
                cast("str", event.payload.get("node_id")): _global_position(event)
                for event in loaded.events
                if event.event_type == ALPHA_NODE_SUCCEEDED
                and isinstance(event.payload.get("node_id"), str)
            }
            for node in loaded.state.nodes:
                if (
                    node.status is not AlphaNodeLifecycleStatus.SUCCEEDED
                    or not node.retained_worktree
                ):
                    continue
                if (
                    node.cleanup_status is None
                    or node.head_commit is None
                    or node.lease_digest is None
                    or node.worktree_spec_digest is None
                ):
                    raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
                try:
                    spec = worktree_execution_spec_from_mapping(thaw_successful_worktree_spec(node))
                    success_position = success_positions[node.node_id]
                except (AlphaLifecycleError, KeyError, WorktreeLifecycleError) as error:
                    raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT) from error
                if (
                    spec.repository_root != self._repository_root
                    or spec.isolation_root != self._isolation_root
                    or spec.lease.run_id != run_id
                    or spec.lease.node_id != node.node_id
                    or spec.lease.digest != node.lease_digest
                    or spec.digest != node.worktree_spec_digest
                ):
                    raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
                candidates.append(
                    _SuccessfulWorktreeCandidate(
                        run_id=run_id,
                        node_id=node.node_id,
                        spec=spec,
                        head_commit=node.head_commit,
                        success_position=success_position,
                        cleanup_status=node.cleanup_status,
                    )
                )
        return tuple(
            sorted(
                candidates,
                key=lambda item: (item.success_position, item.run_id, item.node_id),
            )
        )

    def _request_successful_worktree_cleanup(
        self,
        candidate: _SuccessfulWorktreeCandidate,
        *,
        principal_id: str,
    ) -> None:
        loaded = self._load_run(candidate.run_id)
        self._require_successful_worktree_candidate(
            loaded,
            candidate,
            expected_status=AlphaWorktreeCleanupStatus.ELIGIBLE,
        )
        self._append_run_transitions(
            loaded,
            (
                _RunTransition(
                    ALPHA_NODE_WORKTREE_CLEANUP_REQUESTED,
                    {
                        **self._successful_worktree_identity(candidate),
                        "retained_worktree": True,
                        "status": "worktree-cleanup-requested",
                    },
                    f"worktree-cleanup-requested:{candidate.spec.digest}",
                ),
            ),
            principal_id=principal_id,
        )

    def _finish_successful_worktree_cleanup(
        self,
        candidate: _SuccessfulWorktreeCandidate,
        *,
        principal_id: str,
    ) -> bool:
        loaded = self._load_run(candidate.run_id)
        self._require_successful_worktree_candidate(
            loaded,
            candidate,
            expected_status=AlphaWorktreeCleanupStatus.REQUESTED,
        )
        try:
            removal = self._worktrees.remove_success(
                candidate.spec,
                expected_head_commit=candidate.head_commit,
            )
        except WorktreeLifecycleError as error:
            retained = candidate.spec.worktree_path.exists() or (
                candidate.spec.worktree_path.is_symlink()
            )
            current = self._load_run(candidate.run_id)
            self._require_successful_worktree_candidate(
                current,
                candidate,
                expected_status=AlphaWorktreeCleanupStatus.REQUESTED,
            )
            self._append_run_transitions(
                current,
                (
                    _RunTransition(
                        ALPHA_NODE_WORKTREE_CLEANUP_FAILED,
                        {
                            **self._successful_worktree_identity(candidate),
                            "failure_code": error.code.value,
                            "retained_worktree": retained,
                            "status": "worktree-cleanup-failed",
                        },
                        f"worktree-cleanup-failed:{candidate.spec.digest}",
                    ),
                ),
                principal_id=principal_id,
            )
            return False

        removal_payload = worktree_removal_payload(removal)
        current = self._load_run(candidate.run_id)
        self._require_successful_worktree_candidate(
            current,
            candidate,
            expected_status=AlphaWorktreeCleanupStatus.REQUESTED,
        )
        self._append_run_transitions(
            current,
            (
                _RunTransition(
                    ALPHA_NODE_WORKTREE_CLEANED,
                    {
                        **self._successful_worktree_identity(candidate),
                        "removal_digest": json_digest(removal_payload),
                        "removal": removal_payload,
                        "retained_worktree": False,
                        "status": "worktree-cleaned",
                    },
                    f"worktree-cleaned:{candidate.spec.digest}",
                ),
            ),
            principal_id=principal_id,
        )
        return True

    @staticmethod
    def _successful_worktree_identity(
        candidate: _SuccessfulWorktreeCandidate,
    ) -> dict[str, JsonInput]:
        return {
            "run_id": candidate.run_id,
            "node_id": candidate.node_id,
            "lease_digest": candidate.spec.lease.digest,
            "worktree_spec_digest": candidate.spec.digest,
            "head_commit": candidate.head_commit,
        }

    @staticmethod
    def _require_successful_worktree_candidate(
        loaded: _LoadedRun,
        candidate: _SuccessfulWorktreeCandidate,
        *,
        expected_status: AlphaWorktreeCleanupStatus,
    ) -> None:
        node = next(
            (item for item in loaded.state.nodes if item.node_id == candidate.node_id),
            None,
        )
        if (
            loaded.state.active_lease is not None
            or node is None
            or node.status is not AlphaNodeLifecycleStatus.SUCCEEDED
            or not node.retained_worktree
            or node.cleanup_status is not expected_status
            or node.head_commit != candidate.head_commit
            or node.lease_digest != candidate.spec.lease.digest
            or node.worktree_spec_digest != candidate.spec.digest
        ):
            raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)

    def _run_ids(self) -> tuple[str, ...]:
        run_ids: list[str] = []
        seen: set[str] = set()
        cursor = 0
        while True:
            events = self._events.read_all(after_position=cursor, limit=200)
            if not events:
                break
            for event in events:
                if event.event_type == ALPHA_RUN_QUEUED and event.stream_id.startswith(
                    "alpha:run:"
                ):
                    run_id = event.stream_id.removeprefix("alpha:run:")
                    if run_id not in seen:
                        seen.add(run_id)
                        run_ids.append(run_id)
            last = events[-1].global_position
            if last is None:
                raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
            cursor = last
        return tuple(run_ids)

    def _record_immutable(
        self,
        *,
        stream_id: str,
        event_type: str,
        request: msgspec.Struct,
        principal_id: str,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        references: Mapping[str, Mapping[str, JsonInput]] | None = None,
        extra: Mapping[str, JsonInput] | None = None,
    ) -> EventEnvelope:
        payload = _request_event_payload(
            request,
            principal_id=principal_id,
            references=references,
            extra=extra,
        )

        existing = self._events.read_stream(stream_id, limit=2)
        if existing:
            return _require_idempotent(existing, event_type, payload)

        idempotency_key = getattr(request, "idempotency_key", None)
        if not isinstance(idempotency_key, str):
            raise RuntimeApiError(RuntimeApiFailureCode.INVALID_REQUEST)
        event = EventEnvelope.create(
            stream_id=stream_id,
            stream_sequence=1,
            event_type=event_type,
            actor=principal_id,
            source=ALPHA_EVENT_SOURCE,
            payload=payload,
            correlation_id=correlation_id,
            causation_id=causation_id,
            idempotency_key=idempotency_key,
        )
        try:
            return self._events.append(event, expected_sequence=0)
        except ConcurrencyError, EventConflictError, IdempotencyConflict:
            raced = self._events.read_stream(stream_id, limit=2)
            if raced:
                return _require_idempotent(raced, event_type, payload)
            raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT) from None

    def _required_event(self, stream_id: str, event_type: str) -> EventEnvelope:
        events = self._events.read_stream(stream_id, limit=2)
        if not events:
            raise RuntimeApiError(RuntimeApiFailureCode.NOT_FOUND)
        if (
            len(events) != 1
            or events[0].event_type != event_type
            or events[0].schema_version != 1
            or events[0].stream_sequence != 1
            or events[0].source != ALPHA_EVENT_SOURCE
        ):
            raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
        return events[0]

    def _require_project_root(self, value: str) -> None:
        path = Path(value)
        if not path.is_absolute() or ".." in path.parts:
            raise RuntimeApiError(RuntimeApiFailureCode.INVALID_REQUEST)
        try:
            resolved = path.resolve(strict=True)
        except OSError as error:
            raise RuntimeApiError(RuntimeApiFailureCode.INVALID_REQUEST) from error
        if path != resolved or resolved != self._repository_root or not resolved.is_dir():
            raise RuntimeApiError(RuntimeApiFailureCode.INVALID_REQUEST)


def _project_response(event: EventEnvelope) -> AlphaProjectResponse:
    request = _decode_request(event, AlphaProjectRequest)
    return AlphaProjectResponse(
        project_id=request.project_id,
        root=request.root,
        configuration_provider=request.configuration_provider,
        configuration_version=request.configuration_version,
        configuration_digest=request.configuration_digest,
        principal_id=_event_principal(event),
        event_id=event.event_id,
        cursor=_global_position(event),
        event_digest=event.payload_hash,
    )


def _intent_response(event: EventEnvelope) -> AlphaIntentResponse:
    request = _decode_request(event, AlphaIntentRequest)
    return AlphaIntentResponse(
        intent_id=request.intent_id,
        project_id=request.project_id,
        objective=request.objective,
        constraints=request.constraints,
        assumptions=request.assumptions,
        unresolved_questions=request.unresolved_questions,
        principal_id=_event_principal(event),
        event_id=event.event_id,
        cursor=_global_position(event),
        event_digest=event.payload_hash,
    )


def _plan_response(event: EventEnvelope) -> AlphaPlanResponse:
    request = _decode_request(event, AlphaPlanRequest)
    return AlphaPlanResponse(
        plan_id=request.plan_id,
        project_id=request.project_id,
        intent_id=request.intent_id,
        base_commit=request.base_commit,
        allowed_effects=request.allowed_effects,
        nodes=request.nodes,
        topological_order=alpha_plan_topological_order(request.nodes),
        principal_id=_event_principal(event),
        event_id=event.event_id,
        cursor=_global_position(event),
        event_digest=event.payload_hash,
    )


def _artifact_expectations(loaded: _LoadedRun) -> tuple[AlphaReplayNodeExpectation, ...]:
    plan_nodes = {node.node_id: node for node in loaded.plan.nodes}
    expectations: list[AlphaReplayNodeExpectation] = []
    for state in loaded.state.nodes:
        node = plan_nodes[state.node_id]
        raw_spec = state.worktree_spec
        base_commit_value = None if raw_spec is None else raw_spec.get("base_commit")
        base_commit = base_commit_value if isinstance(base_commit_value, str) else None
        expectations.append(
            AlphaReplayNodeExpectation(
                node_id=node.node_id,
                objective=node.objective,
                constraints=loaded.intent.constraints,
                depends_on=node.depends_on,
                repository_write="repository-write" in node.effects,
                effects=node.effects,
                allowed_paths=node.allowed_paths,
                max_changed_paths=node.budget.max_changed_files,
                checks=tuple(
                    AlphaReplayCheckExpectation(
                        check_id=check.check_id,
                        argv=check.argv,
                        expected_exit_code=check.expected_exit_code,
                        timeout_seconds=node.budget.timeout_seconds,
                    )
                    for check in node.checks
                ),
                status=state.status.value,
                attempt=state.attempts,
                fencing_token=state.fencing_token,
                lease_digest=state.lease_digest,
                worktree_spec_digest=state.worktree_spec_digest,
                base_commit=base_commit,
                head_commit=_terminal_inspection_head(
                    loaded.events,
                    state.node_id,
                    state.lease_digest,
                ),
                failure_code=state.failure_code,
                result_digest=state.result_digest,
                provider_context_digest=_provider_context_digest(
                    loaded.events,
                    state.node_id,
                    state.lease_digest,
                ),
            )
        )
    return tuple(expectations)


def _terminal_inspection_head(
    events: tuple[EventEnvelope, ...],
    node_id: str,
    lease_digest: str | None,
) -> str | None:
    if lease_digest is None:
        return None
    for event in reversed(events):
        if (
            event.event_type not in {ALPHA_NODE_SUCCEEDED, ALPHA_NODE_FAILED, ALPHA_NODE_CANCELED}
            or event.payload.get("node_id") != node_id
            or event.payload.get("lease_digest") != lease_digest
        ):
            continue
        if event.payload.get("retained_worktree") is not True:
            return None
        inspection = event.payload.get("inspection")
        if isinstance(inspection, Mapping):
            head_commit = inspection.get("head_commit")
            if isinstance(head_commit, str):
                return head_commit
        return None
    return None


def _provider_context_digest(
    events: tuple[EventEnvelope, ...],
    node_id: str,
    lease_digest: str | None,
) -> str | None:
    if lease_digest is None:
        return None
    for event in reversed(events):
        if (
            event.event_type == ALPHA_NODE_PROVIDER_DISPATCH_STARTED
            and event.payload.get("node_id") == node_id
            and event.payload.get("lease_digest") == lease_digest
        ):
            digest = event.payload.get("context_artifact_digest")
            return digest if isinstance(digest, str) else None
    return None


def _run_response(loaded: _LoadedRun) -> AlphaRunResponse:
    request = loaded.request
    state = loaded.state
    event = state.latest_event
    active = state.active_lease
    return AlphaRunResponse(
        run_id=request.run_id,
        project_id=request.project_id,
        intent_id=request.intent_id,
        plan_id=request.plan_id,
        status=state.status.value,
        cancellation_requested=state.cancellation_requested,
        active_node_id=None if active is None else active.node_id,
        attempt=max(node.attempts for node in state.nodes),
        fencing_token=max(node.fencing_token for node in state.nodes),
        retained_worktree=any(node.retained_worktree for node in state.nodes),
        principal_id=_event_principal(state.queued_event),
        event_id=event.event_id,
        cursor=_global_position(event),
        event_digest=event.payload_hash,
    )


def _event_response(event: EventEnvelope) -> AlphaEventResponse:
    payload = thaw_json(event.payload)
    if (
        not isinstance(payload, dict)
        or event.event_type not in _ALPHA_EVENT_TYPES
        or event.schema_version != 1
    ):
        raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
    return AlphaEventResponse(
        event_id=event.event_id,
        cursor=_global_position(event),
        stream_id=event.stream_id,
        stream_sequence=event.stream_sequence,
        event_type=cast("AlphaEventType", event.event_type),
        event_schema_version=1,
        recorded_at=event.recorded_at.isoformat(),
        correlation_id=event.correlation_id,
        causation_id=event.causation_id,
        actor=event.actor,
        payload_digest=event.payload_hash,
        payload=cast("dict[str, object]", payload),
    )


def _decode_request[RequestT: msgspec.Struct](
    event: EventEnvelope,
    request_type: type[RequestT],
) -> RequestT:
    raw = _request_value(event)
    try:
        request = msgspec.convert(raw, type=request_type, strict=True)
    except (msgspec.ValidationError, TypeError, ValueError) as error:
        raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT) from error
    if event.payload.get("request_digest") != json_digest(raw):
        raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
    return request


def _request_value(event: EventEnvelope) -> dict[str, object]:
    value = thaw_json(event.payload.get("request"))
    if not isinstance(value, dict):
        raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
    return cast("dict[str, object]", value)


def _thawed_mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise AlphaLifecycleError()
    built = thaw_json(cast("JsonValue", value))
    if not isinstance(built, dict) or any(not isinstance(key, str) for key in built):
        raise AlphaLifecycleError()
    return cast("Mapping[str, object]", built)


def _event_principal(event: EventEnvelope) -> str:
    principal = event.payload.get("principal_id")
    if not isinstance(principal, str) or principal != event.actor:
        raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
    return principal


def _event_reference(event: EventEnvelope) -> dict[str, JsonInput]:
    return {"event_id": event.event_id, "event_digest": event.payload_hash}


def _require_reference(owner: EventEnvelope, name: str, expected: EventEnvelope) -> None:
    references = owner.payload.get("references")
    if not isinstance(references, Mapping):
        raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
    reference = references.get(name)
    if not isinstance(reference, Mapping):
        raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
    if (
        reference.get("event_id") != expected.event_id
        or reference.get("event_digest") != expected.payload_hash
    ):
        raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)


def _require_idempotent(
    events: tuple[EventEnvelope, ...],
    event_type: str,
    payload: Mapping[str, JsonInput],
) -> EventEnvelope:
    if len(events) != 1:
        raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
    existing = events[0]
    if (
        existing.event_type != event_type
        or existing.schema_version != 1
        or existing.stream_sequence != 1
        or existing.source != ALPHA_EVENT_SOURCE
        or existing.payload_hash != json_digest(payload)
    ):
        raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
    return existing


def _require_run_idempotent(
    events: tuple[EventEnvelope, ...],
    payload: Mapping[str, JsonInput],
) -> EventEnvelope:
    existing = events[0]
    if (
        existing.event_type != ALPHA_RUN_QUEUED
        or existing.schema_version != 1
        or existing.stream_sequence != 1
        or existing.source != ALPHA_EVENT_SOURCE
        or existing.payload_hash != json_digest(payload)
    ):
        raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
    return existing


def _request_event_payload(
    request: msgspec.Struct,
    *,
    principal_id: str,
    references: Mapping[str, Mapping[str, JsonInput]] | None,
    extra: Mapping[str, JsonInput] | None,
) -> dict[str, JsonInput]:
    request_value = _struct_value(request)
    payload: dict[str, JsonInput] = {
        "request": request_value,
        "request_digest": json_digest(request_value),
        "principal_id": principal_id,
    }
    if references is not None:
        payload["references"] = dict(references)
    if extra is not None:
        payload.update(extra)
    return payload


def _struct_value(value: msgspec.Struct) -> dict[str, JsonInput]:
    built = msgspec.to_builtins(value)
    if not isinstance(built, dict) or any(not isinstance(key, str) for key in built):
        raise RuntimeApiError(RuntimeApiFailureCode.INVALID_REQUEST)
    return cast("dict[str, JsonInput]", built)


def _global_position(event: EventEnvelope) -> int:
    if event.global_position is None:
        raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
    return event.global_position


def _principal(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 200
        or any(not 0x21 <= ord(character) <= 0x7E for character in value)
    ):
        raise RuntimeApiError(RuntimeApiFailureCode.INVALID_REQUEST)


def _identifier(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 120
        or any(
            not (character.isascii() and (character.isalnum() or character in "-._"))
            for character in value
        )
    ):
        raise RuntimeApiError(RuntimeApiFailureCode.INVALID_REQUEST)


def _aware_timestamp(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise RuntimeApiError(RuntimeApiFailureCode.INVALID_REQUEST)
    return value


def _content_digest(value: str) -> None:
    if not isinstance(value, str) or len(value) != 71 or not value.startswith("sha256:"):
        raise RuntimeApiError(RuntimeApiFailureCode.INVALID_REQUEST)
    try:
        int(value.removeprefix("sha256:"), 16)
    except ValueError as error:
        raise RuntimeApiError(RuntimeApiFailureCode.INVALID_REQUEST) from error


def _commit_id(value: object) -> str:
    if not isinstance(value, str) or len(value) != 40 or value != value.lower():
        raise AlphaLifecycleError()
    try:
        int(value, 16)
    except ValueError as error:
        raise AlphaLifecycleError() from error
    return value


def _node_base_commit(
    plan: AlphaPlanRequest,
    node_id: str,
    successful_heads: Mapping[str, str],
) -> str:
    by_id = {node.node_id: node for node in plan.nodes}
    try:
        node = by_id[node_id]
    except KeyError as error:
        raise AlphaLifecycleError() from error
    ancestors: set[str] = set()
    pending = list(node.depends_on)
    while pending:
        dependency = pending.pop()
        if dependency in ancestors:
            continue
        try:
            dependency_node = by_id[dependency]
        except KeyError as error:
            raise AlphaLifecycleError() from error
        ancestors.add(dependency)
        pending.extend(dependency_node.depends_on)
    writers = tuple(
        candidate
        for candidate in alpha_plan_topological_order(plan.nodes)
        if candidate in ancestors and "repository-write" in by_id[candidate].effects
    )
    if not writers:
        return plan.base_commit
    try:
        return _commit_id(successful_heads[writers[-1]])
    except KeyError as error:
        raise AlphaLifecycleError() from error


def _failure_code(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or any(
            not (character.isascii() and (character.isalnum() or character in "-._"))
            for character in value
        )
    ):
        raise RuntimeApiError(RuntimeApiFailureCode.INVALID_REQUEST)


def _inspection_is_unchanged(inspection: WorktreeInspection) -> bool:
    return (
        inspection.head_commit == inspection.base_commit
        and not inspection.changed_paths
        and not inspection.uncommitted_paths
        and inspection.path_policy_compliant
    )


def _project_stream(project_id: str) -> str:
    return f"alpha:project:{project_id}"


def _intent_stream(intent_id: str) -> str:
    return f"alpha:intent:{intent_id}"


def _plan_stream(plan_id: str) -> str:
    return f"alpha:plan:{plan_id}"


def _run_stream(run_id: str) -> str:
    return f"alpha:run:{run_id}"


__all__ = [
    "AlphaPreparedNode",
    "AlphaReadyNode",
    "AlphaRuntimeApiService",
    "AlphaWorktreeMaintenanceReport",
]
