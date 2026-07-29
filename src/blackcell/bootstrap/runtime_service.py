from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from math import isfinite
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
from blackcell.config import RuntimeSecurityConfig
from blackcell.gateway import GatewayBudget
from blackcell.interfaces.http.contracts import (
    MAX_RUN_QUERY_PAGE_SIZE,
    MAX_RUN_QUERY_SCAN_EVENTS,
    MAX_RUNTIME_EVENT_PAGE_SIZE,
    CancelRunRequest,
    HealthResponse,
    IntentRequest,
    IntentResponse,
    PlanNode,
    PlanRequest,
    PlanResponse,
    ProjectRequest,
    ProjectResponse,
    ReplayArtifactIntegrity,
    ReplayArtifactResponse,
    ReplayFindingResponse,
    ReplayResponse,
    RunBudgetUsageResponse,
    RunNodeQueryResponse,
    RunQueryItem,
    RunQueryRequest,
    RunQueryResponse,
    RunRequest,
    RunResponse,
    RunStatus,
    RunSurfaceSnapshot,
    RunSurfaceWindow,
    RuntimeEventPageResponse,
    RuntimeEventResponse,
    RuntimeEventType,
    VerificationReplayResponse,
    plan_topological_order,
)
from blackcell.interfaces.http.ports import (
    RuntimeApiError,
    RuntimeApiFailureCode,
    RuntimeArtifactPayload,
)
from blackcell.kernel import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactStore,
    ConcurrencyError,
    EventConflictError,
    EventEnvelope,
    EventStore,
    IdempotencyConflict,
    JsonValue,
    ProjectionRunner,
    utc_now,
)
from blackcell.kernel._json import JsonInput, bytes_digest, json_digest, thaw_json
from blackcell.orchestration.execution_artifacts import (
    OUTCOME_MEDIA_TYPE,
    NodeOutcomeManifest,
    node_outcome_from_mapping,
)
from blackcell.orchestration.execution_plan import (
    EXECUTION_GOAL_ADMITTED,
    EXECUTION_PLAN_ADMITTED,
    EXECUTION_TASK_VERIFIED,
    ExecutionAuthority,
    GoalSpec,
    Plan,
    VerificationCheck,
    goal_from_payload,
    plan_from_payload,
)
from blackcell.orchestration.execution_plan import (
    RunLifecycleStatus as ExecutionRunStatus,
)
from blackcell.orchestration.execution_plan import (
    TaskLifecycleStatus as ExecutionTaskStatus,
)
from blackcell.orchestration.execution_runtime import (
    ExecutionRunProjection,
    ExecutionRunState,
    ExecutionRuntimeError,
    ExecutionTaskState,
)
from blackcell.orchestration.replay import (
    ExecutionArtifactReaderPort,
    ReplayCheckExpectation,
    ReplayNodeExpectation,
    build_review_context_from_artifacts,
    verify_run_artifacts,
)
from blackcell.orchestration.review import (
    MAX_REVIEW_EVIDENCE_ITEMS,
    ReviewContext,
)
from blackcell.orchestration.review_lifecycle import (
    REVIEW_EVENT_TYPES,
    ReviewCandidate,
    review_id,
)
from blackcell.orchestration.run_lifecycle import (
    NODE_CANCELED,
    NODE_CLAIMED,
    NODE_FAILED,
    NODE_PROVIDER_DISPATCH_STARTED,
    NODE_RECONCILIATION_REQUIRED,
    NODE_REQUEUED,
    NODE_SUCCEEDED,
    NODE_WORKTREE_CLEANED,
    NODE_WORKTREE_CLEANUP_FAILED,
    NODE_WORKTREE_CLEANUP_REQUESTED,
    NODE_WORKTREE_PREPARED,
    RUN_CANCEL_REQUESTED,
    RUN_CANCELED,
    RUN_EVENT_TYPES,
    RUN_FAILED,
    RUN_QUEUED,
    RUN_RECONCILIATION_REQUIRED,
    RUN_SUCCEEDED,
    RUNTIME_EVENT_SOURCE,
    LifecycleError,
    NodeLifecycleStatus,
    RunLifecycleState,
    RunLifecycleStatus,
    WorktreeCleanupStatus,
    fold_run_lifecycle,
    run_lifecycle_payload,
    thaw_successful_worktree_spec,
    thaw_worktree_spec,
)
from blackcell.orchestration.run_lifecycle import (
    provider_request_id as provider_request_id_for_lease,
)
from blackcell.orchestration.verification_lifecycle import VERIFICATION_EVENT_TYPES
from blackcell.orchestration.verification_replay import replay_verification
from blackcell.runtime import StorageQuotaPort

_PROJECT_REGISTERED = "project.registered"
_INTENT_ACCEPTED = "intent.accepted"
_PLAN_ACCEPTED = "plan.accepted"
_RUNTIME_EVENT_TYPES = frozenset(
    {
        _PROJECT_REGISTERED,
        _INTENT_ACCEPTED,
        _PLAN_ACCEPTED,
        *RUN_EVENT_TYPES,
        *REVIEW_EVENT_TYPES,
        *VERIFICATION_EVENT_TYPES,
    }
)
_PROVIDER_DISPATCH_AMBIGUOUS = "provider-dispatch-ambiguous"
_MAX_RETAINED_SUCCESSFUL_WORKTREES = 1_024
_MAX_KERNEL_REPLAY_ARTIFACTS = 4_096
_MAX_KERNEL_REPLAY_BYTES = 512 * 1024 * 1024
_MAX_UI_ARTIFACT_BYTES = 8 * 1024 * 1024
_EXECUTION_TERMINAL_STATUSES = frozenset(
    {
        ExecutionRunStatus.SUCCEEDED,
        ExecutionRunStatus.BLOCKED,
        ExecutionRunStatus.CANCELED,
        ExecutionRunStatus.ESCALATED,
        ExecutionRunStatus.TERMINAL_FAILURE,
    }
)


@dataclass(frozen=True, slots=True)
class ReadyNode:
    """One dependency-ready node selected in durable queued-run order."""

    run_id: str
    node: PlanNode


@dataclass(frozen=True, slots=True)
class GeneratedRun:
    """One public execution run awaiting or resuming deterministic generated-plan execution."""

    run_id: str
    goal: GoalSpec
    authority: ExecutionAuthority


@dataclass(frozen=True, slots=True)
class PreparedNode:
    """Host-only authority returned after a lease and clean checkout are durable."""

    spec: WorktreeExecutionSpec
    inspection: WorktreeInspection
    node: PlanNode
    intent: IntentRequest
    correlation_id: str
    claim_event_id: str
    prepared_event_id: str


@dataclass(frozen=True, slots=True)
class WorktreeMaintenanceReport:
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
    cleanup_status: WorktreeCleanupStatus


@dataclass(frozen=True, slots=True)
class _LoadedRun:
    request: RunRequest
    intent: IntentRequest
    plan: PlanRequest
    events: tuple[EventEnvelope, ...]
    state: RunLifecycleState
    kernel_state: ExecutionRunState | None


@dataclass(frozen=True, slots=True)
class _RunTransition:
    event_type: str
    payload: Mapping[str, JsonInput]
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class _KernelArtifactReport:
    status: ReplayArtifactIntegrity
    artifacts: tuple[ReplayArtifactResponse, ...]
    findings: tuple[ReplayFindingResponse, ...]
    evidence_digest: str


@dataclass(frozen=True, slots=True)
class _EventStoreSnapshot:
    events: EventStore
    through_position: int

    def read_stream(self, stream_id: str) -> tuple[EventEnvelope, ...]:
        return self.events.read_stream(
            stream_id,
            through_position=self.through_position,
        )

    def get(self, event_id: str) -> EventEnvelope | None:
        event = self.events.get(event_id)
        if event is None or _global_position(event) > self.through_position:
            return None
        return event


class RuntimeService:
    """Canonical application boundary over the durable project event ledger."""

    def __init__(
        self,
        events: EventStore,
        repository_root: Path | str,
        *,
        isolation_root: Path | str | None = None,
        worktrees: GitWorktreeLifecycle | None = None,
        artifacts: ExecutionArtifactReaderPort | None = None,
        storage_quota: StorageQuotaPort | None = None,
    ) -> None:
        try:
            root = Path(repository_root).resolve(strict=True)
        except OSError as error:
            raise ValueError("execution repository root must exist") from error
        if not root.is_dir():
            raise ValueError("execution repository root must be a directory")
        if isolation_root is None:
            resolved_isolation = events.path.parent.resolve() / "execution-worktrees"
        else:
            candidate = Path(isolation_root)
            if not candidate.is_absolute():
                raise ValueError("execution isolation root must be absolute")
            try:
                resolved_parent = candidate.parent.resolve(strict=True)
            except OSError as error:
                raise ValueError("execution isolation parent must exist") from error
            resolved_isolation = resolved_parent / candidate.name
        if artifacts is not None and artifacts.database_path.resolve() != events.path.resolve():
            raise ValueError("execution artifact store does not match the event database")
        self._events = events
        self._repository_root = root
        self._isolation_root = resolved_isolation
        self._worktrees = worktrees or GitWorktreeLifecycle()
        self._artifacts = artifacts
        self._storage_quota = storage_quota

    @classmethod
    def from_config(
        cls,
        config: RuntimeSecurityConfig,
        *,
        repository_root: Path | str,
        artifact_max_total_bytes: int | None = None,
        isolation_root: Path | str | None = None,
        storage_quota: StorageQuotaPort | None = None,
    ) -> RuntimeService:
        database_path = config.paths.ensure_database_file()
        return cls(
            EventStore(database_path),
            repository_root,
            isolation_root=isolation_root,
            artifacts=ArtifactStore(
                config.paths.artifact_root,
                database_path=database_path,
                max_total_bytes=artifact_max_total_bytes,
            ),
            storage_quota=storage_quota,
        )

    def readiness(self) -> HealthResponse:
        try:
            self._events.read_all(after_position=0, limit=1)
            if self._storage_quota is not None and not self._storage_quota.has_mutation_capacity():
                return HealthResponse(status="not-ready")
        except Exception:
            return HealthResponse(status="not-ready")
        return HealthResponse(status="ready")

    def register_project(
        self,
        request: ProjectRequest,
        *,
        principal_id: str,
    ) -> ProjectResponse:
        _principal(principal_id)
        self._require_storage()
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
        request: IntentRequest,
        *,
        principal_id: str,
    ) -> IntentResponse:
        _principal(principal_id)
        self._require_storage()
        project_event = self._required_event(
            _project_stream(request.project_id), _PROJECT_REGISTERED
        )
        project = _decode_request(project_event, ProjectRequest)
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
        request: PlanRequest,
        *,
        principal_id: str,
    ) -> PlanResponse:
        _principal(principal_id)
        self._require_storage()
        project_event = self._required_event(
            _project_stream(request.project_id), _PROJECT_REGISTERED
        )
        intent_event = self._required_event(_intent_stream(request.intent_id), _INTENT_ACCEPTED)
        project = _decode_request(project_event, ProjectRequest)
        intent = _decode_request(intent_event, IntentRequest)
        if (
            project.project_id != request.project_id
            or intent.project_id != request.project_id
            or intent.intent_id != request.intent_id
        ):
            raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
        if request.planning_mode == "generated":
            _validate_generated_plan(intent, request)
        _require_reference(intent_event, "project", project_event)
        _require_review_evidence_capacity(request)
        plan_stream = _plan_stream(request.plan_id)
        prior_plan = self._events.read_stream(plan_stream, limit=1)
        retained_plan = (
            _decode_request(self._required_event(plan_stream, _PLAN_ACCEPTED), PlanRequest)
            if prior_plan
            else request
        )
        self._retain_plan_base_commit(retained_plan.plan_id, retained_plan.base_commit)
        event = self._record_immutable(
            stream_id=plan_stream,
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
        request: RunRequest,
        *,
        principal_id: str,
    ) -> RunResponse:
        _principal(principal_id)
        self._require_storage()
        project_event = self._required_event(
            _project_stream(request.project_id), _PROJECT_REGISTERED
        )
        intent_event = self._required_event(_intent_stream(request.intent_id), _INTENT_ACCEPTED)
        plan_event = self._required_event(_plan_stream(request.plan_id), _PLAN_ACCEPTED)
        project = _decode_request(project_event, ProjectRequest)
        intent = _decode_request(intent_event, IntentRequest)
        plan = _decode_request(plan_event, PlanRequest)
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

    def inspect_run(self, run_id: str) -> RunResponse:
        _identifier(run_id)
        return _run_response(self._load_run(run_id))

    def query_runs(self, request: RunQueryRequest) -> RunQueryResponse:
        """Search public run projections without appending events or reading artifact content."""

        if not isinstance(request, RunQueryRequest):
            raise RuntimeApiError(RuntimeApiFailureCode.INVALID_REQUEST)
        cursor = request.after_cursor
        scanned_events = 0
        runs: list[RunQueryItem] = []
        exhausted = False
        while scanned_events < MAX_RUN_QUERY_SCAN_EVENTS and len(runs) < request.limit:
            remaining = MAX_RUN_QUERY_SCAN_EVENTS - scanned_events
            page = self._events.read_all(after_position=cursor, limit=min(200, remaining))
            if not page:
                exhausted = True
                break
            stopped_early = False
            for event in page:
                cursor = _global_position(event)
                scanned_events += 1
                if (
                    event.source == RUNTIME_EVENT_SOURCE
                    and event.event_type == RUN_QUEUED
                    and event.stream_id.startswith("run:")
                ):
                    run_id = event.stream_id.removeprefix("run:")
                    loaded = self._load_run(run_id)
                    if _matches_run_query(loaded, request):
                        runs.append(_run_query_item(loaded))
                if scanned_events >= MAX_RUN_QUERY_SCAN_EVENTS or len(runs) >= request.limit:
                    stopped_early = True
                    break
            if stopped_early:
                break
            if len(page) < min(200, remaining):
                exhausted = True
                break
        has_more = (
            False if exhausted else bool(self._events.read_all(after_position=cursor, limit=1))
        )
        return RunQueryResponse(
            query=request,
            scanned_events=scanned_events,
            runs=tuple(runs),
            next_cursor=cursor,
            has_more=has_more,
        )

    def presentation_run_window(self, *, limit: int) -> RunSurfaceWindow:
        """Return the newest run projections from one bounded indexed snapshot."""

        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_RUN_QUERY_PAGE_SIZE
        ):
            raise RuntimeApiError(RuntimeApiFailureCode.INVALID_REQUEST)
        event_cursor = self._events.current_position()
        through_position = event_cursor
        scanned_events = 0
        exhausted = event_cursor == 0
        queued_events: list[EventEnvelope] = []
        while (
            not exhausted
            and scanned_events < MAX_RUN_QUERY_SCAN_EVENTS
            and len(queued_events) <= limit
        ):
            page_limit = min(200, MAX_RUN_QUERY_SCAN_EVENTS - scanned_events)
            page = self._events.read_type_descending(
                RUN_QUEUED,
                through_position=through_position,
                limit=page_limit,
            )
            if not page:
                exhausted = True
                break
            for event in page:
                scanned_events += 1
                if event.source == RUNTIME_EVENT_SOURCE and event.stream_id.startswith("run:"):
                    queued_events.append(event)
                    if len(queued_events) > limit:
                        break
            oldest_position = _global_position(page[-1])
            exhausted = len(page) < page_limit or oldest_position == 1
            through_position = oldest_position - 1

        selected = queued_events[:limit]
        runs = tuple(
            _run_query_item(
                self._load_run(
                    event.stream_id.removeprefix("run:"),
                    through_position=event_cursor,
                )
            )
            for event in reversed(selected)
        )
        return RunSurfaceWindow(
            limit=limit,
            scanned_events=scanned_events,
            runs=runs,
            event_cursor=event_cursor,
            has_older_runs=len(queued_events) > limit or not exhausted,
        )

    def presentation_run_item(self, run_id: str) -> RunQueryItem:
        """Load one run projection directly through its indexed event stream."""

        _identifier(run_id)
        return _run_query_item(self._load_run(run_id))

    def presentation_run_snapshot(self, run_id: str) -> RunSurfaceSnapshot:
        """Project replay and node state from one fixed event-ledger snapshot."""

        _identifier(run_id)
        event_cursor = self._events.current_position()
        loaded = self._load_run(run_id, through_position=event_cursor)
        return RunSurfaceSnapshot(
            event_cursor=event_cursor,
            replay=self._replay_loaded_run(loaded, through_position=event_cursor),
            run_item=_run_query_item(loaded),
        )

    def next_ready_node(self) -> ReadyNode | None:
        """Return the first dependency-ready node in global queued-run order."""

        for run_id in self._run_ids():
            loaded = self._load_run(run_id)
            if (
                loaded.state.status not in {RunLifecycleStatus.QUEUED, RunLifecycleStatus.RUNNING}
                or loaded.state.cancellation_requested
                or loaded.state.active_lease is not None
                or loaded.kernel_state is not None
                or loaded.plan.planning_mode == "generated"
            ):
                continue
            states = {node.node_id: node for node in loaded.state.nodes}
            by_id = {node.node_id: node for node in loaded.plan.nodes}
            for node_id in plan_topological_order(loaded.plan.nodes):
                state = states[node_id]
                node = by_id[node_id]
                if state.status is NodeLifecycleStatus.PENDING and all(
                    states[dependency].status is NodeLifecycleStatus.SUCCEEDED
                    for dependency in node.depends_on
                ):
                    return ReadyNode(run_id=run_id, node=node)
        return None

    def next_generated_run(self) -> GeneratedRun | None:
        """Return the first queued generated-plan run from the public run order."""

        for run_id in self._run_ids():
            loaded = self._load_run(run_id)
            kernel = loaded.kernel_state
            if (
                loaded.plan.planning_mode != "generated"
                or loaded.state.status
                not in {RunLifecycleStatus.QUEUED, RunLifecycleStatus.RUNNING}
                or loaded.state.cancellation_requested
                or loaded.state.active_lease is not None
                or (kernel is not None and kernel.status in _EXECUTION_TERMINAL_STATUSES)
            ):
                continue
            return GeneratedRun(run_id, _generated_goal(loaded), _generated_authority(loaded.plan))
        return None

    def generated_execution_authority(self, run_id: str) -> ExecutionAuthority:
        """Re-read the immutable public-plan bounds for one generated execution attempt."""

        loaded = self._load_run(run_id)
        if loaded.plan.planning_mode != "generated":
            raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
        authority = _generated_authority(loaded.plan)
        kernel = loaded.kernel_state
        if kernel is None:
            return authority
        return ExecutionAuthority(
            budget=authority.budget,
            check_timeout_seconds=authority.check_timeout_seconds,
            max_changed_paths=authority.max_changed_paths,
            consumed_budget=GatewayBudget(
                (
                    kernel.input_tokens
                    if kernel.input_tokens_complete
                    else authority.budget.max_input_tokens
                ),
                (
                    kernel.output_tokens
                    if kernel.output_tokens_complete
                    else authority.budget.max_output_tokens
                ),
                kernel.latency_ms,
                (
                    kernel.cost_microusd
                    if kernel.cost_microusd_complete
                    else authority.budget.max_cost_microusd
                ),
            ),
            input_tokens_complete=kernel.input_tokens_complete,
            output_tokens_complete=kernel.output_tokens_complete,
            cost_microusd_complete=kernel.cost_microusd_complete,
        )

    def generated_execution_goal(self, run_id: str) -> GoalSpec:
        """Re-read the canonical goal derived from one accepted generated plan."""

        loaded = self._load_run(run_id)
        if loaded.plan.planning_mode != "generated":
            raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
        return _generated_goal(loaded)

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

    def should_cancel_generated_run(self, run_id: str) -> bool:
        """Poll the durable public cancellation fence for generated-plan execution."""

        try:
            loaded = self._load_run(run_id)
        except RuntimeApiError:
            return True
        return loaded.state.cancellation_requested or loaded.state.status in {
            RunLifecycleStatus.CANCELED,
            RunLifecycleStatus.FAILED,
            RunLifecycleStatus.RECONCILIATION_REQUIRED,
        }

    def cancel_run(
        self,
        run_id: str,
        request: CancelRunRequest,
        *,
        principal_id: str,
    ) -> RunResponse:
        """Durably request cooperative cancellation without running cleanup code inline."""

        _identifier(run_id)
        _principal(principal_id)
        self._require_storage()
        if not isinstance(request, CancelRunRequest):
            raise RuntimeApiError(RuntimeApiFailureCode.INVALID_REQUEST)
        loaded = self._load_run(run_id)
        if loaded.state.status is RunLifecycleStatus.CANCELED:
            return _run_response(loaded)
        effective_status = _effective_run_status(loaded)
        if effective_status == "canceled":
            return _run_response(loaded)
        if effective_status in {"succeeded", "failed", "reconciliation-required"}:
            raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
        if loaded.state.status in {
            RunLifecycleStatus.SUCCEEDED,
            RunLifecycleStatus.FAILED,
        }:
            raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
        if loaded.state.cancellation_requested:
            return _run_response(loaded)

        request_value = _struct_value(request)
        transitions = [
            _RunTransition(
                RUN_CANCEL_REQUESTED,
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
                    RUN_CANCELED,
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
    ) -> PreparedNode:
        """Persist one fenced claim, then create and record its exact clean worktree."""

        _identifier(run_id)
        _identifier(node_id)
        _principal(worker_id)
        loaded = self._load_run(run_id)
        if (
            loaded.kernel_state is not None
            or loaded.state.status
            not in {
                RunLifecycleStatus.QUEUED,
                RunLifecycleStatus.RUNNING,
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
        if node.status is not NodeLifecycleStatus.PENDING:
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
                    NODE_CLAIMED,
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
                    NODE_WORKTREE_PREPARED,
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
        return PreparedNode(
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
            provider_request_id != provider_request_id_for_lease(spec.lease.digest)
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
                    NODE_PROVIDER_DISPATCH_STARTED,
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
    ) -> RunResponse:
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
                NODE_SUCCEEDED,
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
            node.node_id == spec.lease.node_id or node.status is NodeLifecycleStatus.SUCCEEDED
            for node in loaded.state.nodes
        ):
            transitions.append(
                _RunTransition(
                    RUN_SUCCEEDED,
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
    ) -> RunResponse:
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
    ) -> RunResponse:
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

    def reconcile_startup(self, *, principal_id: str) -> tuple[RunResponse, ...]:
        """Invalidate every surviving active execution lease after exclusive daemon restart."""

        _principal(principal_id)
        reconciled: list[RunResponse] = []
        for run_id in self._run_ids():
            loaded = self._load_run(run_id)
            active = loaded.state.active_lease
            if active is None:
                continue
            try:
                spec = worktree_execution_spec_from_mapping(thaw_worktree_spec(active))
            except (LifecycleError, WorktreeLifecycleError) as error:
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
    ) -> WorktreeMaintenanceReport:
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
            if candidate.cleanup_status is WorktreeCleanupStatus.REQUESTED
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
            if candidate.cleanup_status is WorktreeCleanupStatus.ELIGIBLE
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
        return WorktreeMaintenanceReport(
            pending_recovered=pending_recovered,
            cleanup_requested=requested,
            cleaned=cleaned,
            failed=failed,
            retained=retained,
            quota_satisfied=retained <= max_retained,
        )

    def list_events(self, *, after_cursor: int, limit: int) -> RuntimeEventPageResponse:
        if (
            isinstance(after_cursor, bool)
            or not isinstance(after_cursor, int)
            or after_cursor < 0
            or isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_RUNTIME_EVENT_PAGE_SIZE
        ):
            raise RuntimeApiError(RuntimeApiFailureCode.INVALID_REQUEST)
        scanned = self._events.read_all(after_position=after_cursor, limit=limit + 1)
        window = scanned[:limit]
        runtime_events = tuple(
            _event_response(event) for event in window if event.event_type in _RUNTIME_EVENT_TYPES
        )
        next_cursor = _global_position(window[-1]) if window else after_cursor
        return RuntimeEventPageResponse(
            after_cursor=after_cursor,
            limit=limit,
            scanned_events=len(window),
            events=runtime_events,
            next_cursor=next_cursor,
            has_more=len(scanned) > limit,
        )

    def replay_run(self, run_id: str) -> ReplayResponse:
        _identifier(run_id)
        event_cursor = self._events.current_position()
        loaded = self._load_run(run_id, through_position=event_cursor)
        return self._replay_loaded_run(loaded, through_position=event_cursor)

    def _replay_loaded_run(
        self,
        loaded: _LoadedRun,
        *,
        through_position: int,
    ) -> ReplayResponse:
        run_id = loaded.request.run_id
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
                "lifecycle": run_lifecycle_payload(loaded.state),
                "kernel": ExecutionRunProjection().dump_state(loaded.kernel_state),
            }
        )
        kernel_artifact_report = (
            None if loaded.kernel_state is None else self._verify_kernel_artifacts(loaded)
        )
        artifact_report = (
            verify_run_artifacts(
                self._artifacts,
                run_id=run_id,
                nodes=_artifact_expectations(loaded),
            )
            if kernel_artifact_report is None
            else None
        )
        if kernel_artifact_report is not None:
            artifact_integrity = kernel_artifact_report.status
            artifacts = kernel_artifact_report.artifacts
            findings = kernel_artifact_report.findings
            artifact_evidence_digest = kernel_artifact_report.evidence_digest
        else:
            assert artifact_report is not None
            artifact_integrity = artifact_report.status.value
            artifacts = tuple(
                ReplayArtifactResponse(
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
            )
            findings = tuple(
                ReplayFindingResponse(
                    code=finding.code.value,
                    node_id=finding.node_id,
                    role=None if finding.role is None else finding.role.value,
                    check_id=finding.check_id,
                    artifact_digest=finding.artifact_digest,
                )
                for finding in artifact_report.findings
            )
            artifact_evidence_digest = artifact_report.evidence_digest
        verification_report = replay_verification(
            _EventStoreSnapshot(self._events, through_position),
            self._artifacts,
            run_id=run_id,
        )
        return ReplayResponse(
            run_id=run_id,
            project=project_response,
            intent=intent_response,
            plan=plan_response,
            run=run_response,
            processed_events=3 + len(loaded.events),
            state_digest=state_digest,
            artifact_integrity=artifact_integrity,
            artifacts=artifacts,
            findings=findings,
            artifact_evidence_digest=artifact_evidence_digest,
            verification=VerificationReplayResponse(
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

    def read_run_artifact(self, run_id: str, digest: str) -> RuntimeArtifactPayload:
        """Read one verified artifact only when replay binds it to the requested run."""

        _identifier(run_id)
        replay = self.replay_run(run_id)
        matches = tuple(
            artifact
            for artifact in replay.artifacts
            if artifact.digest == digest and artifact.verified
        )
        if not matches or self._artifacts is None:
            raise RuntimeApiError(RuntimeApiFailureCode.NOT_FOUND)
        reference = matches[0]
        if reference.size_bytes > _MAX_UI_ARTIFACT_BYTES or any(
            artifact.size_bytes != reference.size_bytes
            or artifact.media_type != reference.media_type
            or artifact.encoding != reference.encoding
            for artifact in matches[1:]
        ):
            raise RuntimeApiError(RuntimeApiFailureCode.NOT_FOUND)
        try:
            stored = self._artifacts.stat(digest)
            content = self._artifacts.get_bytes(stored, verify=True)
        except (ArtifactIntegrityError, ArtifactNotFoundError, ValueError) as error:
            raise RuntimeApiError(RuntimeApiFailureCode.NOT_FOUND) from error
        if (
            stored.digest != reference.digest
            or stored.size_bytes != reference.size_bytes
            or stored.media_type != reference.media_type
            or stored.encoding != reference.encoding
            or len(content) != stored.size_bytes
            or bytes_digest(content) != stored.digest
        ):
            raise RuntimeApiError(RuntimeApiFailureCode.NOT_FOUND)
        return RuntimeArtifactPayload(
            digest=stored.digest,
            size_bytes=stored.size_bytes,
            media_type=stored.media_type,
            encoding=stored.encoding,
            content=content,
        )

    def _require_storage(self) -> None:
        if self._storage_quota is not None and not self._storage_quota.has_mutation_capacity():
            raise RuntimeApiError(RuntimeApiFailureCode.STORAGE_QUOTA_EXCEEDED)

    def _verify_kernel_artifacts(self, loaded: _LoadedRun) -> _KernelArtifactReport:
        relationships: list[tuple[str, str]] = []
        for event in loaded.events:
            if event.event_type != EXECUTION_TASK_VERIFIED:
                continue
            payload = _thawed_mapping(event.payload)
            task_id = payload.get("task_id")
            digests = payload.get("artifact_digests")
            if (
                not isinstance(task_id, str)
                or not isinstance(digests, list)
                or not all(isinstance(item, str) for item in digests)
            ):
                raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
            relationships.extend((task_id, digest) for digest in cast("list[str]", digests))
        if not relationships:
            return _kernel_artifact_report("not-applicable", (), ())
        if len(relationships) > _MAX_KERNEL_REPLAY_ARTIFACTS:
            finding = ReplayFindingResponse(
                code="replay-artifact-budget-exceeded",
                node_id=None,
                role=None,
                check_id=None,
                artifact_digest=None,
            )
            return _kernel_artifact_report("inconclusive", (), (finding,))
        if self._artifacts is None:
            finding = ReplayFindingResponse(
                code="replay-artifact-store-unavailable",
                node_id=None,
                role=None,
                check_id=None,
                artifact_digest=None,
            )
            return _kernel_artifact_report("inconclusive", (), (finding,))

        artifacts: list[ReplayArtifactResponse] = []
        findings: list[ReplayFindingResponse] = []
        total_bytes = 0
        status: ReplayArtifactIntegrity = "verified"
        for task_id, digest in relationships:
            try:
                reference = self._artifacts.stat(digest)
                if total_bytes + reference.size_bytes > _MAX_KERNEL_REPLAY_BYTES:
                    findings.append(
                        ReplayFindingResponse(
                            code="replay-artifact-budget-exceeded",
                            node_id=task_id,
                            role="outcome",
                            check_id=None,
                            artifact_digest=digest,
                        )
                    )
                    status = "inconclusive" if status == "verified" else status
                    break
                data = self._artifacts.get_bytes(digest, verify=True)
                verified = len(data) == reference.size_bytes and bytes_digest(data) == digest
                if not verified:
                    raise ArtifactIntegrityError(digest)
                total_bytes += reference.size_bytes
                artifacts.append(
                    ReplayArtifactResponse(
                        node_id=task_id,
                        role="outcome",
                        check_id=None,
                        digest=digest,
                        size_bytes=reference.size_bytes,
                        media_type=reference.media_type,
                        encoding=reference.encoding,
                        verified=True,
                    )
                )
            except ArtifactNotFoundError:
                status = "failed"
                findings.append(
                    ReplayFindingResponse(
                        code="replay-artifact-missing",
                        node_id=task_id,
                        role="outcome",
                        check_id=None,
                        artifact_digest=digest,
                    )
                )
            except ArtifactIntegrityError:
                status = "failed"
                findings.append(
                    ReplayFindingResponse(
                        code="replay-artifact-integrity-failed",
                        node_id=task_id,
                        role="outcome",
                        check_id=None,
                        artifact_digest=digest,
                    )
                )
            except Exception:
                if status != "failed":
                    status = "inconclusive"
                findings.append(
                    ReplayFindingResponse(
                        code="replay-artifact-read-unavailable",
                        node_id=task_id,
                        role="outcome",
                        check_id=None,
                        artifact_digest=digest,
                    )
                )
        return _kernel_artifact_report(status, tuple(artifacts), tuple(findings))

    def review_candidates(self) -> tuple[ReviewCandidate, ...]:
        """Return successful execution snapshots in durable run order."""

        return tuple(self.review_candidate(run_id) for run_id in self.review_run_ids())

    def review_run_ids(self) -> tuple[str, ...]:
        """Return successful execution run IDs without reading their artifact graphs."""

        run_ids: list[str] = []
        for run_id in self._run_ids():
            loaded = self._load_run(run_id)
            if _effective_run_status(loaded) == "succeeded":
                run_ids.append(run_id)
        return tuple(run_ids)

    def review_candidate(self, run_id: str) -> ReviewCandidate:
        """Build one exact terminal execution and artifact-evidence snapshot."""

        _identifier(run_id)
        loaded = self._load_run(run_id)
        if _effective_run_status(loaded) != "succeeded":
            raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
        return self._review_candidate(loaded)

    def prepare_review_context(self, candidate: ReviewCandidate) -> ReviewContext:
        """Revalidate one claimed execution snapshot and build its live-free review context."""

        if not isinstance(candidate, ReviewCandidate):
            raise RuntimeApiError(RuntimeApiFailureCode.INVALID_REQUEST)
        loaded = self._load_run(candidate.run_id)
        if (
            _effective_run_status(loaded) != "succeeded"
            or self._review_candidate(loaded) != candidate
        ):
            raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
        expectations = self._review_artifact_expectations(loaded)
        review_constraints = (
            loaded.intent.constraints
            if loaded.kernel_state is None
            else _kernel_goal(loaded).constraints
        )
        return build_review_context_from_artifacts(
            self._artifacts,
            run_id=candidate.run_id,
            project_id=loaded.request.project_id,
            intent_id=loaded.request.intent_id,
            plan_id=loaded.request.plan_id,
            objective=loaded.intent.objective,
            constraints=review_constraints,
            base_commit=loaded.plan.base_commit,
            state_digest=candidate.state_digest,
            nodes=expectations,
            require_exact_constraints=loaded.kernel_state is None,
        )

    def _review_candidate(self, loaded: _LoadedRun) -> ReviewCandidate:
        terminal_index = _review_terminal_index(loaded)
        if terminal_index is None:
            raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
        terminal = loaded.events[terminal_index]
        terminal_state = fold_run_lifecycle(
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
        state_payload: dict[str, object] = {
            "project": _request_value(project_event),
            "intent": _request_value(intent_event),
            "plan": _request_value(plan_event),
            "run": _request_value(loaded.state.queued_event),
            "lifecycle": run_lifecycle_payload(terminal_state),
        }
        if loaded.kernel_state is not None:
            terminal_kernel = (
                ProjectionRunner()
                .replay(
                    ExecutionRunProjection(),
                    loaded.events[: terminal_index + 1],
                )
                .state
            )
            if (
                terminal_kernel is None
                or terminal_kernel.status is not ExecutionRunStatus.SUCCEEDED
            ):
                raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
            state_payload["kernel"] = ExecutionRunProjection().dump_state(terminal_kernel)
        state_digest = json_digest(cast("Mapping[str, JsonInput]", state_payload))
        expectations = self._review_artifact_expectations(loaded)
        artifact_report = verify_run_artifacts(
            self._artifacts,
            run_id=loaded.request.run_id,
            nodes=expectations,
        )
        return ReviewCandidate(
            run_id=loaded.request.run_id,
            review_id=review_id(loaded.request.run_id, terminal.payload_hash),
            correlation_id=loaded.state.queued_event.correlation_id,
            run_event_id=terminal.event_id,
            run_event_digest=terminal.payload_hash,
            state_digest=state_digest,
            artifact_evidence_digest=artifact_report.evidence_digest,
        )

    def _review_artifact_expectations(
        self,
        loaded: _LoadedRun,
    ) -> tuple[ReplayNodeExpectation, ...]:
        if loaded.kernel_state is None:
            return _artifact_expectations(loaded)
        if self._artifacts is None:
            raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
        plan = _kernel_plan(loaded)
        goal = _kernel_goal(loaded)
        states = {item.task_id: item for item in loaded.kernel_state.tasks}
        authority = _generated_authority(loaded.plan)
        expectations: list[ReplayNodeExpectation] = []
        for task in plan.tasks:
            state = states.get(task.task_id)
            if state is None:
                raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
            outcome_digest, outcome = _kernel_outcome(
                self._artifacts,
                state.last_artifact_digests,
            )
            if (
                state.status is not ExecutionTaskStatus.SUCCEEDED
                or outcome.run_id != loaded.request.run_id
                or outcome.node_id != task.task_id
                or outcome.attempt != state.attempts
                or outcome.status != "succeeded"
            ):
                raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
            base_commit = _kernel_task_base_commit(
                plan,
                states,
                task.task_id,
            )
            constraints = goal.constraints
            max_changed_paths = 0
            provider_context_digest = None
            if task.allowed_paths:
                context = outcome.context_artifact
                if context is None:
                    raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
                raw_context = _artifact_mapping(self._artifacts, context.digest)
                constraints = _kernel_context_constraints(
                    raw_context,
                    accepted=goal.constraints,
                )
                max_changed_paths = _artifact_integer(raw_context, "max_changed_paths")
                if max_changed_paths > authority.max_changed_paths:
                    raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
                provider_context_digest = context.digest
            recorded_checks = {item.check_id: item for item in outcome.checks}
            checks: list[ReplayCheckExpectation] = []
            for check in task.checks:
                recorded = recorded_checks.get(check.check_id)
                if recorded is None:
                    raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
                command = _artifact_mapping(self._artifacts, recorded.command.digest)
                timeout_seconds = _artifact_number(command, "timeout_seconds")
                if timeout_seconds > authority.check_timeout_seconds:
                    raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
                checks.append(
                    ReplayCheckExpectation(
                        check.check_id,
                        check.argv,
                        check.expected_exit_code,
                        timeout_seconds,
                    )
                )
            expectations.append(
                ReplayNodeExpectation(
                    node_id=task.task_id,
                    objective=task.objective,
                    constraints=constraints,
                    depends_on=task.depends_on,
                    repository_write=bool(task.allowed_paths),
                    effects=(
                        ("repository-read", "repository-write", "process")
                        if task.allowed_paths
                        else ("repository-read", "process")
                    ),
                    allowed_paths=task.allowed_paths,
                    max_changed_paths=max_changed_paths,
                    checks=tuple(checks),
                    status=state.status.value,
                    attempt=state.attempts,
                    fencing_token=state.attempts,
                    lease_digest=outcome.lease_digest,
                    worktree_spec_digest=outcome.worktree_spec_digest,
                    base_commit=base_commit,
                    head_commit=state.head_commit,
                    failure_code=(
                        None if state.last_failure_class is None else state.last_failure_class.value
                    ),
                    result_digest=outcome_digest,
                    provider_context_digest=provider_context_digest,
                )
            )
        return tuple(expectations)

    def _record_run_queued(
        self,
        *,
        stream_id: str,
        request: RunRequest,
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
            event_type=RUN_QUEUED,
            actor=principal_id,
            source=RUNTIME_EVENT_SOURCE,
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

    def _load_run(self, run_id: str, *, through_position: int | None = None) -> _LoadedRun:
        events = self._events.read_stream(
            _run_stream(run_id),
            through_position=through_position,
        )
        if not events:
            raise RuntimeApiError(RuntimeApiFailureCode.NOT_FOUND)
        queued = events[0]
        if queued.event_type != RUN_QUEUED:
            raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
        request = _decode_request(queued, RunRequest)
        if request.run_id != run_id:
            raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
        project_event = self._required_event(
            _project_stream(request.project_id), _PROJECT_REGISTERED
        )
        intent_event = self._required_event(_intent_stream(request.intent_id), _INTENT_ACCEPTED)
        plan_event = self._required_event(_plan_stream(request.plan_id), _PLAN_ACCEPTED)
        project = _decode_request(project_event, ProjectRequest)
        intent = _decode_request(intent_event, IntentRequest)
        plan = _decode_request(plan_event, PlanRequest)
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
            state = fold_run_lifecycle(run_id, dependencies, events)
            kernel_state = ProjectionRunner().replay(ExecutionRunProjection(), events).state
        except LifecycleError as error:
            raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT) from error
        except ExecutionRuntimeError as error:
            raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT) from error
        self._validate_worktree_evidence(events, plan)
        loaded = _LoadedRun(
            request=request,
            intent=intent,
            plan=plan,
            events=events,
            state=state,
            kernel_state=kernel_state,
        )
        if plan.planning_mode == "generated" and kernel_state is not None:
            _require_generated_kernel_binding(loaded)
        return loaded

    def _validate_worktree_evidence(
        self,
        events: tuple[EventEnvelope, ...],
        plan: PlanRequest,
    ) -> None:
        specifications: dict[str, WorktreeExecutionSpec] = {}
        successful_heads: dict[str, str] = {}
        plan_nodes = {node.node_id: node for node in plan.nodes}
        try:
            for event in events:
                if event.event_type == NODE_CLAIMED:
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
                if event.event_type == NODE_PROVIDER_DISPATCH_STARTED:
                    lease_digest = event.payload.get("lease_digest")
                    if not isinstance(lease_digest, str):
                        raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_SPEC)
                    spec = specifications[lease_digest]
                    if "repository-write" not in plan_nodes[spec.lease.node_id].effects:
                        raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_SPEC)
                if event.event_type == NODE_WORKTREE_CLEANED:
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
                if event.event_type == NODE_SUCCEEDED:
                    head_commit = _commit_id(event.payload.get("head_commit"))
                    if inspection.head_commit != head_commit:
                        raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_GIT_OUTPUT)
                    successful_heads[spec.lease.node_id] = head_commit
        except (KeyError, LifecycleError, WorktreeLifecycleError) as error:
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
        causation_id = loaded.events[-1].event_id
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
                source=RUNTIME_EVENT_SOURCE,
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
            fold_run_lifecycle(
                loaded.request.run_id,
                dependencies,
                (*loaded.events, *events),
            )
            self._validate_worktree_evidence((*loaded.events, *events), loaded.plan)
        except LifecycleError as error:
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
            fold_run_lifecycle(
                loaded.request.run_id,
                dependencies,
                (*loaded.events, *stored),
            )
        except LifecycleError as error:
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
    ) -> RunResponse:
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
                    NODE_FAILED,
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
                    RUN_FAILED,
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
                    NODE_CANCELED,
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
                    RUN_CANCELED,
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
                    NODE_CANCELED,
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
                    RUN_CANCELED,
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
                    NODE_REQUEUED,
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
                    NODE_RECONCILIATION_REQUIRED,
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
                    RUN_RECONCILIATION_REQUIRED,
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
                if event.event_type == NODE_SUCCEEDED
                and isinstance(event.payload.get("node_id"), str)
            }
            for node in loaded.state.nodes:
                if node.status is not NodeLifecycleStatus.SUCCEEDED or not node.retained_worktree:
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
                except (LifecycleError, KeyError, WorktreeLifecycleError) as error:
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
            expected_status=WorktreeCleanupStatus.ELIGIBLE,
        )
        self._append_run_transitions(
            loaded,
            (
                _RunTransition(
                    NODE_WORKTREE_CLEANUP_REQUESTED,
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
            expected_status=WorktreeCleanupStatus.REQUESTED,
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
                expected_status=WorktreeCleanupStatus.REQUESTED,
            )
            self._append_run_transitions(
                current,
                (
                    _RunTransition(
                        NODE_WORKTREE_CLEANUP_FAILED,
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
            expected_status=WorktreeCleanupStatus.REQUESTED,
        )
        self._append_run_transitions(
            current,
            (
                _RunTransition(
                    NODE_WORKTREE_CLEANED,
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
        expected_status: WorktreeCleanupStatus,
    ) -> None:
        node = next(
            (item for item in loaded.state.nodes if item.node_id == candidate.node_id),
            None,
        )
        if (
            loaded.state.active_lease is not None
            or node is None
            or node.status is not NodeLifecycleStatus.SUCCEEDED
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
                if event.event_type == RUN_QUEUED and event.stream_id.startswith("run:"):
                    run_id = event.stream_id.removeprefix("run:")
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
            source=RUNTIME_EVENT_SOURCE,
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
            or events[0].source != RUNTIME_EVENT_SOURCE
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

    def _retain_plan_base_commit(self, plan_id: str, base_commit: str) -> None:
        try:
            self._worktrees.retain_plan_base_commit(
                self._repository_root,
                plan_id=plan_id,
                base_commit=base_commit,
            )
        except WorktreeLifecycleError as error:
            if error.code is WorktreeFailureCode.BASE_COMMIT_NOT_FOUND:
                raise RuntimeApiError(RuntimeApiFailureCode.INVALID_REQUEST) from error
            raise RuntimeApiError(RuntimeApiFailureCode.NOT_READY) from error


def _project_response(event: EventEnvelope) -> ProjectResponse:
    request = _decode_request(event, ProjectRequest)
    return ProjectResponse(
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


def _intent_response(event: EventEnvelope) -> IntentResponse:
    request = _decode_request(event, IntentRequest)
    return IntentResponse(
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


def _plan_response(event: EventEnvelope) -> PlanResponse:
    request = _decode_request(event, PlanRequest)
    return PlanResponse(
        plan_id=request.plan_id,
        project_id=request.project_id,
        intent_id=request.intent_id,
        base_commit=request.base_commit,
        allowed_effects=request.allowed_effects,
        nodes=request.nodes,
        topological_order=plan_topological_order(request.nodes),
        principal_id=_event_principal(event),
        event_id=event.event_id,
        cursor=_global_position(event),
        event_digest=event.payload_hash,
    )


def _artifact_expectations(loaded: _LoadedRun) -> tuple[ReplayNodeExpectation, ...]:
    plan_nodes = {node.node_id: node for node in loaded.plan.nodes}
    expectations: list[ReplayNodeExpectation] = []
    for state in loaded.state.nodes:
        node = plan_nodes[state.node_id]
        raw_spec = state.worktree_spec
        base_commit_value = None if raw_spec is None else raw_spec.get("base_commit")
        base_commit = base_commit_value if isinstance(base_commit_value, str) else None
        expectations.append(
            ReplayNodeExpectation(
                node_id=node.node_id,
                objective=node.objective,
                constraints=loaded.intent.constraints,
                depends_on=node.depends_on,
                repository_write="repository-write" in node.effects,
                effects=node.effects,
                allowed_paths=node.allowed_paths,
                max_changed_paths=node.budget.max_changed_files,
                checks=tuple(
                    ReplayCheckExpectation(
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
            event.event_type not in {NODE_SUCCEEDED, NODE_FAILED, NODE_CANCELED}
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
            event.event_type == NODE_PROVIDER_DISPATCH_STARTED
            and event.payload.get("node_id") == node_id
            and event.payload.get("lease_digest") == lease_digest
        ):
            digest = event.payload.get("context_artifact_digest")
            return digest if isinstance(digest, str) else None
    return None


def _run_response(loaded: _LoadedRun) -> RunResponse:
    request = loaded.request
    state = loaded.state
    active = state.active_lease
    kernel = loaded.kernel_state
    event = state.latest_event if kernel is None else _kernel_latest_event(loaded, kernel)
    kernel_active = (
        None
        if kernel is None
        else next(
            (
                item
                for item in kernel.tasks
                if item.status
                in {
                    ExecutionTaskStatus.READY,
                    ExecutionTaskStatus.RUNNING,
                    ExecutionTaskStatus.VERIFYING,
                }
            ),
            None,
        )
    )
    kernel_attempt = (
        0 if kernel is None else max((item.attempts for item in kernel.tasks), default=0)
    )
    return RunResponse(
        run_id=request.run_id,
        project_id=request.project_id,
        intent_id=request.intent_id,
        plan_id=request.plan_id,
        status=_effective_run_status(loaded),
        cancellation_requested=(
            state.cancellation_requested
            or (kernel is not None and kernel.status is ExecutionRunStatus.CANCELED)
        ),
        active_node_id=(
            kernel_active.task_id
            if kernel_active is not None
            else None
            if active is None
            else active.node_id
        ),
        attempt=max(max(node.attempts for node in state.nodes), kernel_attempt),
        fencing_token=max(node.fencing_token for node in state.nodes),
        retained_worktree=(
            any(node.retained_worktree for node in state.nodes)
            or (
                kernel is not None
                and (
                    bool(kernel.retained_workspace_ids)
                    or any(node.retained_workspace_id is not None for node in kernel.tasks)
                )
            )
        ),
        principal_id=_event_principal(state.queued_event),
        event_id=event.event_id,
        cursor=_global_position(event),
        event_digest=event.payload_hash,
    )


def _matches_run_query(loaded: _LoadedRun, request: RunQueryRequest) -> bool:
    run = loaded.request
    status = _effective_run_status(loaded)
    return not (
        (request.statuses and status not in request.statuses)
        or (request.project_ids and run.project_id not in request.project_ids)
        or (request.intent_ids and run.intent_id not in request.intent_ids)
        or (request.plan_ids and run.plan_id not in request.plan_ids)
        or (request.run_ids and run.run_id not in request.run_ids)
    )


def _kernel_artifact_report(
    status: ReplayArtifactIntegrity,
    artifacts: tuple[ReplayArtifactResponse, ...],
    findings: tuple[ReplayFindingResponse, ...],
) -> _KernelArtifactReport:
    evidence_digest = json_digest(
        {
            "status": status,
            "artifacts": [
                {
                    "node_id": item.node_id,
                    "role": item.role,
                    "check_id": item.check_id,
                    "digest": item.digest,
                    "size_bytes": item.size_bytes,
                    "media_type": item.media_type,
                    "encoding": item.encoding,
                    "verified": item.verified,
                }
                for item in artifacts
            ],
            "findings": [
                {
                    "code": item.code,
                    "node_id": item.node_id,
                    "role": item.role,
                    "check_id": item.check_id,
                    "artifact_digest": item.artifact_digest,
                }
                for item in findings
            ],
        }
    )
    return _KernelArtifactReport(status, artifacts, findings, evidence_digest)


def _run_query_item(loaded: _LoadedRun) -> RunQueryItem:
    kernel = loaded.kernel_state
    if kernel is None:
        public_nodes = {item.node_id: item for item in loaded.plan.nodes}
        nodes = tuple(
            RunNodeQueryResponse(
                node_id=node.node_id,
                status=node.status.value,
                attempts=node.attempts,
                fencing_token=node.fencing_token,
                failure_code=node.failure_code,
                retained_worktree=node.retained_worktree,
                head_commit=node.head_commit,
                depends_on=public_nodes[node.node_id].depends_on,
            )
            for node in loaded.state.nodes
        )
        usage = None
    else:
        kernel_plan = _kernel_plan(loaded)
        tasks = {item.task_id: item for item in kernel_plan.tasks}
        nodes = tuple(
            RunNodeQueryResponse(
                node_id=node.task_id,
                status=node.status.value,
                attempts=node.attempts,
                fencing_token=0,
                failure_code=(
                    None if node.last_failure_class is None else node.last_failure_class.value
                ),
                retained_worktree=node.retained_workspace_id is not None,
                head_commit=node.head_commit,
                depends_on=tasks[node.task_id].depends_on,
                max_attempts=tasks[node.task_id].max_attempts,
            )
            for node in kernel.tasks
        )
        usage = RunBudgetUsageResponse(
            input_tokens=kernel.input_tokens,
            input_tokens_complete=kernel.input_tokens_complete,
            max_input_tokens=kernel.budget.max_input_tokens,
            output_tokens=kernel.output_tokens,
            output_tokens_complete=kernel.output_tokens_complete,
            max_output_tokens=kernel.budget.max_output_tokens,
            latency_ms=kernel.latency_ms,
            max_latency_ms=kernel.budget.max_latency_ms,
            cost_microusd=kernel.cost_microusd,
            cost_microusd_complete=kernel.cost_microusd_complete,
            max_cost_microusd=kernel.budget.max_cost_microusd,
        )
    return RunQueryItem(
        queued_cursor=_global_position(loaded.state.queued_event),
        run=_run_response(loaded),
        nodes=nodes,
        usage=usage,
    )


def _kernel_plan(loaded: _LoadedRun) -> Plan:
    plans = _kernel_plans(loaded)
    if not plans:
        raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
    return plans[-1]


def _kernel_plans(loaded: _LoadedRun) -> tuple[Plan, ...]:
    plans: list[Plan] = []
    for event in loaded.events:
        if event.event_type != EXECUTION_PLAN_ADMITTED:
            continue
        try:
            plans.append(plan_from_payload(_thawed_mapping(event.payload).get("plan")))
        except (LifecycleError, TypeError, ValueError) as error:
            raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT) from error
    kernel = loaded.kernel_state
    if kernel is None:
        raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
    if not plans:
        if any(
            value is not None
            for value in (kernel.plan_id, kernel.plan_digest, kernel.plan_revision)
        ):
            raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
        return ()
    plan = plans[-1]
    if (
        plan.plan_id != kernel.plan_id
        or plan.plan_digest != kernel.plan_digest
        or plan.plan_revision != kernel.plan_revision
    ):
        raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
    return tuple(plans)


def _kernel_goal(loaded: _LoadedRun) -> GoalSpec:
    event = next(
        (item for item in loaded.events if item.event_type == EXECUTION_GOAL_ADMITTED),
        None,
    )
    if event is None:
        raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
    try:
        goal = goal_from_payload(_thawed_mapping(event.payload).get("goal"))
    except (LifecycleError, TypeError, ValueError) as error:
        raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT) from error
    if (
        loaded.kernel_state is None
        or goal.goal_id != loaded.kernel_state.goal_id
        or goal.digest != loaded.kernel_state.goal_digest
    ):
        raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
    return goal


def _require_generated_kernel_binding(loaded: _LoadedRun) -> None:
    goal = _kernel_goal(loaded)
    required_checks = {check.check_id: check for check in goal.verification_checks}
    if goal != _generated_goal(loaded):
        raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
    for plan in _kernel_plans(loaded):
        internal_checks = tuple(check for task in plan.tasks for check in task.checks)
        if {check.check_id for check in internal_checks} != set(required_checks) or any(
            required_checks.get(check.check_id) != check for check in internal_checks
        ):
            raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)


def _effective_run_status(loaded: _LoadedRun) -> RunStatus:
    kernel = loaded.kernel_state
    if kernel is None:
        return loaded.state.status.value
    statuses: dict[ExecutionRunStatus, RunStatus] = {
        ExecutionRunStatus.ADMITTED: "running",
        ExecutionRunStatus.RUNNING: "running",
        ExecutionRunStatus.REPLANNING: "running",
        ExecutionRunStatus.SUCCEEDED: "succeeded",
        ExecutionRunStatus.REPLAN_REQUIRED: "reconciliation-required",
        ExecutionRunStatus.BLOCKED: "reconciliation-required",
        ExecutionRunStatus.CANCELED: "canceled",
        ExecutionRunStatus.ESCALATED: "reconciliation-required",
        ExecutionRunStatus.TERMINAL_FAILURE: "failed",
    }
    return statuses[kernel.status]


def _review_terminal_index(loaded: _LoadedRun) -> int | None:
    kernel = loaded.kernel_state
    if kernel is not None:
        if kernel.status is not ExecutionRunStatus.SUCCEEDED:
            return None
        return next(
            (
                index
                for index, event in reversed(tuple(enumerate(loaded.events)))
                if event.event_id == kernel.latest_event_id
            ),
            None,
        )
    return next(
        (
            index
            for index, event in reversed(tuple(enumerate(loaded.events)))
            if event.event_type == RUN_SUCCEEDED
        ),
        None,
    )


def _kernel_outcome(
    artifacts: ExecutionArtifactReaderPort,
    digests: tuple[str, ...],
) -> tuple[str, NodeOutcomeManifest]:
    try:
        candidates = tuple(
            digest for digest in digests if artifacts.stat(digest).media_type == OUTCOME_MEDIA_TYPE
        )
        if len(candidates) != 1:
            raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
        digest = candidates[0]
        outcome = node_outcome_from_mapping(_artifact_mapping(artifacts, digest))
    except RuntimeApiError:
        raise
    except Exception as error:
        raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT) from error
    if outcome.digest != digest:
        raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
    return digest, outcome


def _artifact_mapping(
    artifacts: ExecutionArtifactReaderPort,
    digest: str,
) -> Mapping[str, object]:
    try:
        value = msgspec.json.decode(artifacts.get_bytes(digest, verify=True))
    except Exception as error:
        raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT) from error
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
    return cast("Mapping[str, object]", value)


def _artifact_integer(value: Mapping[str, object], field: str) -> int:
    item = value.get(field)
    if isinstance(item, bool) or not isinstance(item, int) or item < 0:
        raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
    return item


def _artifact_number(value: Mapping[str, object], field: str) -> int | float:
    item = value.get(field)
    if (
        isinstance(item, bool)
        or not isinstance(item, int | float)
        or not isfinite(item)
        or item <= 0
    ):
        raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
    return item


def _kernel_context_constraints(
    value: Mapping[str, object],
    *,
    accepted: tuple[str, ...],
) -> tuple[str, ...]:
    raw = value.get("constraints")
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
    constraints = tuple(cast("list[str]", raw))
    if (
        constraints[: len(accepted)] != accepted
        or len(constraints) not in {len(accepted), len(accepted) + 1}
        or (
            len(constraints) == len(accepted) + 1
            and (
                not constraints[-1].startswith("prior-attempt:")
                or len(constraints[-1].encode("utf-8")) > 2 * 1024
            )
        )
    ):
        raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
    return constraints


def _kernel_task_base_commit(
    plan: Plan,
    states: Mapping[str, ExecutionTaskState],
    task_id: str,
) -> str:
    task = next((item for item in plan.tasks if item.task_id == task_id), None)
    if task is None:
        raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
    if not task.depends_on:
        return plan.base_commit
    heads = {states[dependency].head_commit for dependency in task.depends_on}
    if None in heads or len(heads) != 1:
        raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
    return cast("str", next(iter(heads)))


def _kernel_latest_event(loaded: _LoadedRun, state: ExecutionRunState) -> EventEnvelope:
    try:
        return next(
            event for event in reversed(loaded.events) if event.event_id == state.latest_event_id
        )
    except StopIteration as error:
        raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT) from error


def _event_response(event: EventEnvelope) -> RuntimeEventResponse:
    payload = thaw_json(event.payload)
    if (
        not isinstance(payload, dict)
        or event.event_type not in _RUNTIME_EVENT_TYPES
        or event.schema_version != 1
    ):
        raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
    return RuntimeEventResponse(
        event_id=event.event_id,
        cursor=_global_position(event),
        stream_id=event.stream_id,
        stream_sequence=event.stream_sequence,
        event_type=cast("RuntimeEventType", event.event_type),
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
        raise LifecycleError()
    built = thaw_json(cast("JsonValue", value))
    if not isinstance(built, dict) or any(not isinstance(key, str) for key in built):
        raise LifecycleError()
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
        or existing.source != RUNTIME_EVENT_SOURCE
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
        existing.event_type != RUN_QUEUED
        or existing.schema_version != 1
        or existing.stream_sequence != 1
        or existing.source != RUNTIME_EVENT_SOURCE
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
        raise LifecycleError()
    try:
        int(value, 16)
    except ValueError as error:
        raise LifecycleError() from error
    return value


def _node_base_commit(
    plan: PlanRequest,
    node_id: str,
    successful_heads: Mapping[str, str],
) -> str:
    by_id = {node.node_id: node for node in plan.nodes}
    try:
        node = by_id[node_id]
    except KeyError as error:
        raise LifecycleError() from error
    ancestors: set[str] = set()
    pending = list(node.depends_on)
    while pending:
        dependency = pending.pop()
        if dependency in ancestors:
            continue
        try:
            dependency_node = by_id[dependency]
        except KeyError as error:
            raise LifecycleError() from error
        ancestors.add(dependency)
        pending.extend(dependency_node.depends_on)
    writers = tuple(
        candidate
        for candidate in plan_topological_order(plan.nodes)
        if candidate in ancestors and "repository-write" in by_id[candidate].effects
    )
    if not writers:
        return plan.base_commit
    try:
        return _commit_id(successful_heads[writers[-1]])
    except KeyError as error:
        raise LifecycleError() from error


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
    return f"project:{project_id}"


def _intent_stream(intent_id: str) -> str:
    return f"intent:{intent_id}"


def _plan_stream(plan_id: str) -> str:
    return f"plan:{plan_id}"


def _validate_generated_plan(intent: IntentRequest, plan: PlanRequest) -> None:
    if intent.unresolved_questions:
        raise RuntimeApiError(RuntimeApiFailureCode.INVALID_REQUEST)
    _generated_checks(plan)


def _generated_goal(loaded: _LoadedRun) -> GoalSpec:
    identity = json_digest(
        {
            "run_id": loaded.request.run_id,
            "project_id": loaded.request.project_id,
            "intent_id": loaded.request.intent_id,
            "bounds_plan_id": loaded.request.plan_id,
        }
    )
    return GoalSpec(
        goal_id=f"goal-{identity.removeprefix('sha256:')[:32]}",
        project_id=loaded.request.project_id,
        intent_id=loaded.request.intent_id,
        objective=loaded.intent.objective,
        base_commit=loaded.plan.base_commit,
        constraints=tuple(sorted({*loaded.intent.constraints, *loaded.intent.assumptions})),
        allowed_paths=tuple(
            sorted({path for node in loaded.plan.nodes for path in node.allowed_paths})
        ),
        verification_checks=_generated_checks(loaded.plan),
        max_attempts=3,
        same_error_limit=2,
    )


def _generated_authority(plan: PlanRequest) -> ExecutionAuthority:
    return ExecutionAuthority(
        budget=GatewayBudget(
            sum(node.budget.max_input_tokens for node in plan.nodes),
            sum(node.budget.max_output_tokens for node in plan.nodes),
            sum(node.budget.timeout_seconds for node in plan.nodes) * 1_000,
            sum(node.budget.max_cost_microusd for node in plan.nodes),
        ),
        check_timeout_seconds=min(node.budget.timeout_seconds for node in plan.nodes),
        max_changed_paths=min(
            10_000,
            sum(node.budget.max_changed_files for node in plan.nodes),
        ),
    )


def _generated_checks(plan: PlanRequest) -> tuple[VerificationCheck, ...]:
    checks: dict[str, VerificationCheck] = {}
    for node in plan.nodes:
        for check in node.checks:
            converted = VerificationCheck(
                check.check_id,
                check.argv,
                check.expected_exit_code,
            )
            prior = checks.get(check.check_id)
            if prior is not None and prior != converted:
                raise RuntimeApiError(RuntimeApiFailureCode.INVALID_REQUEST)
            checks[check.check_id] = converted
    return tuple(checks[key] for key in sorted(checks))


def _require_review_evidence_capacity(request: PlanRequest) -> None:
    required_items = sum(
        1 + (4 * len(node.checks)) + (3 * node.budget.max_changed_files) for node in request.nodes
    )
    if required_items > MAX_REVIEW_EVIDENCE_ITEMS:
        raise RuntimeApiError(RuntimeApiFailureCode.INVALID_REQUEST)


def _run_stream(run_id: str) -> str:
    return f"run:{run_id}"


__all__ = [
    "GeneratedRun",
    "PreparedNode",
    "ReadyNode",
    "RuntimeService",
    "WorktreeMaintenanceReport",
]
