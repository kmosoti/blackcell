"""Foreground process composition for the execution worker."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from threading import Event
from typing import Protocol, cast

from blackcell.adapters.execution.bubblewrap import (
    BubblewrapAcceptanceRunner,
    BubblewrapExecutable,
    BubblewrapIsolationPolicy,
)
from blackcell.adapters.execution.evidence import ExecutionEvidenceCollector
from blackcell.adapters.execution.text_changes import TextChangeExecutor
from blackcell.adapters.execution.worktree import GitWorktreeLifecycle
from blackcell.adapters.models import (
    AGY_CLI_ADAPTER_ID,
    CODEX_CLI_ADAPTER_ID,
    AgyCliModelAdapter,
    CodexCliModelAdapter,
    GatewayPlanner,
)
from blackcell.adapters.models.agy_cli import AgyEffort
from blackcell.adapters.models.change_provider import GatewayChangeProvider
from blackcell.adapters.telemetry import ExecutionTraceObserver, RuntimeTelemetry
from blackcell.bootstrap.execution_plan import (
    ExecutionPolicy,
    ExecutionRunResult,
    ProductionAttemptExecutor,
    ProductionExecution,
)
from blackcell.bootstrap.execution_worker import (
    ExecutionWorker,
    ExecutionWorkerCycleResult,
    ExecutionWorkerPolicy,
)
from blackcell.bootstrap.runtime_service import (
    GeneratedRun,
    RuntimeService,
    WorktreeMaintenanceReport,
)
from blackcell.bootstrap.worker_process_lock import (
    WorkerProcessLockError,
    WorkerProcessLockFailureCode,
    WorkerProcessRole,
    worker_process_lock,
)
from blackcell.config import (
    ExecutionProviderAdapter,
    ExecutionWorkerRuntimeConfig,
    RuntimeProcessConfig,
)
from blackcell.gateway import GatewayBudget, GatewayProfile, ModelCapability, ModelGateway
from blackcell.kernel import ArtifactStore, CheckpointStore, EventStore
from blackcell.kernel._json import canonical_json_bytes
from blackcell.orchestration.changes import (
    MAX_CHANGE_CONTEXT_BYTES,
    MAX_CHANGE_PROPOSAL_BYTES,
)
from blackcell.orchestration.execution_plan import (
    ExecutionAuthority,
    ExecutionPolicyKernel,
    GoalSpec,
    PlanningRequest,
    RunLifecycleStatus,
    planning_payload,
)
from blackcell.orchestration.execution_runtime import (
    EventBackedExecutionRunJournal,
    ExecutionCoordinator,
)
from blackcell.runtime import RuntimeStorageQuota, StorageQuotaPort

_CODEX_CONTRACT_OVERHEAD_BYTES = 1024 * 1024
CHANGE_CODEX_MAX_INPUT_BYTES = MAX_CHANGE_CONTEXT_BYTES + _CODEX_CONTRACT_OVERHEAD_BYTES
CHANGE_CODEX_MAX_RESPONSE_BYTES = MAX_CHANGE_PROPOSAL_BYTES + _CODEX_CONTRACT_OVERHEAD_BYTES
CHANGE_CODEX_MAX_STDOUT_BYTES = 2 * CHANGE_CODEX_MAX_RESPONSE_BYTES + _CODEX_CONTRACT_OVERHEAD_BYTES
CHANGE_AGY_MAX_INPUT_BYTES = MAX_CHANGE_CONTEXT_BYTES + _CODEX_CONTRACT_OVERHEAD_BYTES
CHANGE_AGY_MAX_STDOUT_BYTES = MAX_CHANGE_PROPOSAL_BYTES + _CODEX_CONTRACT_OVERHEAD_BYTES
_IDLE_POLL_SECONDS = 0.25


class ExecutionWorkerProcessFailureCode(StrEnum):
    NOT_CONFIGURED = "execution-worker-not-configured"
    ALREADY_RUNNING = "execution-worker-already-running"
    LOCK_UNAVAILABLE = "execution-worker-lock-unavailable"


class ExecutionWorkerProcessError(RuntimeError):
    def __init__(self, code: ExecutionWorkerProcessFailureCode) -> None:
        self.code = code
        super().__init__(code.value)


class ExecutionCycleRunner(Protocol):
    def run_once(self) -> ExecutionWorkerCycleResult: ...


class ExecutionReconciliationPort(Protocol):
    def next_generated_run(self) -> GeneratedRun | None: ...

    def generated_execution_goal(self, run_id: str) -> GoalSpec: ...

    def generated_execution_authority(self, run_id: str) -> ExecutionAuthority: ...

    def should_cancel_generated_run(self, run_id: str) -> bool: ...

    def reconcile_startup(self, *, principal_id: str) -> tuple[object, ...]: ...

    def maintain_successful_worktrees(
        self,
        *,
        max_retained: int,
        principal_id: str,
    ) -> WorktreeMaintenanceReport: ...


@dataclass(frozen=True, slots=True)
class _ExecutionBoundaries:
    worktrees: GitWorktreeLifecycle
    provider: GatewayChangeProvider
    planner: GatewayPlanner
    acceptance: BubblewrapAcceptanceRunner


@dataclass(slots=True)
class ExecutionWorkerProcess:
    """Run one execution coordinator at a time against the canonical local ledger."""

    coordinator: ExecutionCycleRunner
    runtime: ExecutionReconciliationPort
    config: RuntimeProcessConfig
    stop_event: Event = field(default_factory=Event)
    storage_quota: StorageQuotaPort | None = None
    execution: ProductionExecution | None = None
    shutdown: Callable[[], None] = field(default=lambda: None, repr=False)

    @classmethod
    def from_config(
        cls,
        config: RuntimeProcessConfig,
        *,
        stop_event: Event | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> ExecutionWorkerProcess:
        execution_config = _required_execution_config(config)
        telemetry = RuntimeTelemetry.from_config(config)
        try:
            boundaries = _execution_boundaries(execution_config, environment=environment)
            database_path = config.security.paths.ensure_database_file()
            events = EventStore(database_path)
            artifacts = ArtifactStore(
                config.security.paths.artifact_root,
                database_path=database_path,
                max_total_bytes=config.quota.artifact_max_total_bytes,
            )
            runtime = RuntimeService(
                events,
                config.repository_root,
                isolation_root=execution_config.isolation.root,
                worktrees=boundaries.worktrees,
                artifacts=artifacts,
            )
            coordinator = ExecutionWorker(
                runtime=runtime,
                artifacts=artifacts,
                provider=boundaries.provider,
                change_executor=TextChangeExecutor(boundaries.worktrees),
                acceptance=boundaries.acceptance,
                worktrees=boundaries.worktrees,
                evidence=ExecutionEvidenceCollector(boundaries.worktrees),
                policy=ExecutionWorkerPolicy(
                    worker_id=execution_config.worker.worker_id,
                    classification=execution_config.provider.classification,
                    locality=execution_config.provider.locality,
                    stdout_limit_bytes=execution_config.worker.stdout_limit_bytes,
                    stderr_limit_bytes=execution_config.worker.stderr_limit_bytes,
                    lease_grace_seconds=execution_config.worker.lease_grace_seconds,
                ),
            )
            execution_executor = ProductionAttemptExecutor(
                repository_root=config.repository_root,
                isolation_root=execution_config.isolation.root,
                artifacts=artifacts,
                change_provider=boundaries.provider,
                acceptance=boundaries.acceptance,
                policy=ExecutionPolicy(
                    worker_id=execution_config.worker.worker_id,
                    classification=execution_config.provider.classification,
                    locality=execution_config.provider.locality,
                    provider_budget=GatewayBudget(
                        execution_config.provider.max_input_tokens,
                        execution_config.provider.max_output_tokens,
                        execution_config.provider.timeout_ceiling_seconds * 1_000,
                        execution_config.provider.max_cost_microusd,
                    ),
                    check_timeout_seconds=min(
                        execution_config.provider.timeout_ceiling_seconds, 600
                    ),
                    stdout_limit_bytes=execution_config.worker.stdout_limit_bytes,
                    stderr_limit_bytes=execution_config.worker.stderr_limit_bytes,
                ),
                worktrees=boundaries.worktrees,
                evidence=ExecutionEvidenceCollector(boundaries.worktrees),
                changes=TextChangeExecutor(boundaries.worktrees),
                cancel_requested=runtime.should_cancel_generated_run,
                authority_for_run=runtime.generated_execution_authority,
            )
            observer = (
                None if telemetry.recorder is None else ExecutionTraceObserver(telemetry.recorder)
            )
            execution = ProductionExecution(
                ExecutionCoordinator(
                    EventBackedExecutionRunJournal(
                        events,
                        CheckpointStore(database_path),
                        observer=observer,
                    ),
                    boundaries.planner,
                    execution_executor,
                    ExecutionPolicyKernel(),
                ),
                authority_for_run=runtime.generated_execution_authority,
                goal_for_run=runtime.generated_execution_goal,
            )
        except Exception:
            telemetry.shutdown()
            raise
        return cls(
            coordinator,
            runtime,
            config,
            stop_event or Event(),
            storage_quota=RuntimeStorageQuota(
                config.security.paths,
                max_active_bytes=config.quota.active_storage_max_bytes,
                mutation_reserve_bytes=config.quota.mutation_reserve_bytes,
            ),
            execution=execution,
            shutdown=telemetry.shutdown,
        )

    def run_kernel(self, request: PlanningRequest, *, actor: str) -> ExecutionRunResult:
        """Run one admitted execution goal through the production-composed kernel."""

        if self.execution is None:
            raise ExecutionWorkerProcessError(ExecutionWorkerProcessFailureCode.NOT_CONFIGURED)
        return self.execution.run(request, actor=actor)

    def _run_generated_once(
        self,
        config: ExecutionWorkerRuntimeConfig,
        *,
        actor: str,
    ) -> ExecutionWorkerCycleResult | None:
        if self.execution is None:
            return None
        generated = self.runtime.next_generated_run()
        if generated is None:
            return None
        budget = _intersect_budget(
            GatewayBudget(
                config.provider.max_input_tokens,
                config.provider.max_output_tokens,
                config.provider.timeout_ceiling_seconds * 1_000,
                config.provider.max_cost_microusd,
            ),
            generated.authority.budget,
        )
        payload_size = len(canonical_json_bytes(planning_payload(generated.goal)))
        request = PlanningRequest(
            goal=generated.goal,
            classification=config.provider.classification,
            locality=config.provider.locality,
            budget=budget,
            estimated_input_tokens=(payload_size + 3) // 4,
            correlation_id=generated.run_id,
            run_id=generated.run_id,
        )
        state = self.execution.process(request, actor=actor)
        if state.status is RunLifecycleStatus.SUCCEEDED:
            status = "node-succeeded"
            failure_code = None
        elif state.status is RunLifecycleStatus.CANCELED:
            status = "node-canceled"
            failure_code = None
        else:
            status = "node-failed"
            failure_code = state.status.value
        return ExecutionWorkerCycleResult(
            status=status,
            run_id=generated.run_id,
            run_status=state.status.value,
            failure_code=failure_code,
        )

    def serve(self, *, once: bool = False) -> int:
        execution_config = _required_execution_config(self.config)
        worker_id = execution_config.worker.worker_id
        try:
            with worker_process_lock(
                self.config.security.paths,
                WorkerProcessRole.EXECUTION,
            ):
                self.runtime.reconcile_startup(principal_id=worker_id)
                maintenance = self.runtime.maintain_successful_worktrees(
                    max_retained=execution_config.worker.max_retained_successful_worktrees,
                    principal_id=worker_id,
                )
                while not self.stop_event.is_set():
                    if not maintenance.quota_satisfied or (
                        self.storage_quota is not None
                        and not self.storage_quota.has_mutation_capacity()
                    ):
                        cycle: ExecutionWorkerCycleResult | None = None
                    else:
                        cycle = self._run_generated_once(execution_config, actor=worker_id)
                        if cycle is None:
                            cycle = self.coordinator.run_once()
                    if cycle is not None and cycle.status in {
                        "node-succeeded",
                        "node-failed",
                        "node-canceled",
                    }:
                        maintenance = self.runtime.maintain_successful_worktrees(
                            max_retained=(
                                execution_config.worker.max_retained_successful_worktrees
                            ),
                            principal_id=worker_id,
                        )
                    if once:
                        return 3 if cycle is None or cycle.status == "idle" else 0
                    if cycle is None or cycle.status in {"idle", "claim-conflict"}:
                        self.stop_event.wait(_IDLE_POLL_SECONDS)
                return 0
        except WorkerProcessLockError as error:
            code = (
                ExecutionWorkerProcessFailureCode.ALREADY_RUNNING
                if error.code is WorkerProcessLockFailureCode.ALREADY_RUNNING
                else ExecutionWorkerProcessFailureCode.LOCK_UNAVAILABLE
            )
            raise ExecutionWorkerProcessError(code) from error
        finally:
            self.shutdown()


def _intersect_budget(left: GatewayBudget, right: GatewayBudget) -> GatewayBudget:
    return GatewayBudget(
        min(left.max_input_tokens, right.max_input_tokens),
        min(left.max_output_tokens, right.max_output_tokens),
        min(left.max_latency_ms, right.max_latency_ms),
        min(left.max_cost_microusd, right.max_cost_microusd),
    )


def validate_execution_worker_runtime_config(
    config: RuntimeProcessConfig,
    *,
    environment: Mapping[str, str] | None = None,
) -> None:
    """Resolve every executable and isolation invariant before daemon children start."""

    _execution_boundaries(_required_execution_config(config), environment=environment)


def _required_execution_config(config: RuntimeProcessConfig) -> ExecutionWorkerRuntimeConfig:
    if not isinstance(config, RuntimeProcessConfig):
        raise TypeError("execution worker requires runtime process configuration")
    if config.execution_worker is None:
        raise ExecutionWorkerProcessError(ExecutionWorkerProcessFailureCode.NOT_CONFIGURED)
    return config.execution_worker


def _execution_boundaries(
    config: ExecutionWorkerRuntimeConfig,
    *,
    environment: Mapping[str, str] | None,
) -> _ExecutionBoundaries:
    provider_config = config.provider
    values = os.environ if environment is None else environment
    try:
        provider_environment = {
            name: values[name] for name in provider_config.environment_variables
        }
    except KeyError as error:
        raise ValueError("execution provider environment is incomplete") from error
    if provider_config.adapter is ExecutionProviderAdapter.CODEX_CLI:
        adapter = CodexCliModelAdapter(
            executable=provider_config.executable,
            git_executable=provider_config.git_executable,
            environment=provider_environment,
            timeout_ceiling_seconds=provider_config.timeout_ceiling_seconds,
            max_input_bytes=CHANGE_CODEX_MAX_INPUT_BYTES,
            max_stdout_bytes=CHANGE_CODEX_MAX_STDOUT_BYTES,
            max_response_bytes=CHANGE_CODEX_MAX_RESPONSE_BYTES,
        )
        adapter_id = CODEX_CLI_ADAPTER_ID
    else:
        if provider_config.effort is None:
            raise ValueError("AGY execution provider configuration is incomplete")
        adapter = AgyCliModelAdapter(
            executable=provider_config.executable,
            git_executable=provider_config.git_executable,
            effort=cast(AgyEffort, provider_config.effort),
            environment=provider_environment,
            timeout_ceiling_seconds=provider_config.timeout_ceiling_seconds,
            max_input_bytes=CHANGE_AGY_MAX_INPUT_BYTES,
            max_stdout_bytes=CHANGE_AGY_MAX_STDOUT_BYTES,
            max_stderr_bytes=config.worker.stderr_limit_bytes,
        )
        adapter_id = AGY_CLI_ADAPTER_ID
    profile = GatewayProfile(
        profile_id=provider_config.profile_id,
        capability=ModelCapability.CODE,
        adapter_id=adapter_id,
        model_id=provider_config.model_id,
        priority=0,
        local=False,
        deterministic=False,
        maximum_classification=provider_config.classification,
        max_input_tokens=provider_config.max_input_tokens,
        max_output_tokens=provider_config.max_output_tokens,
        max_cost_microusd=provider_config.max_cost_microusd,
    )
    planning_profile = GatewayProfile(
        profile_id=f"{provider_config.profile_id[:120]}.plan",
        capability=ModelCapability.REASON,
        adapter_id=adapter_id,
        model_id=provider_config.model_id,
        priority=0,
        local=False,
        deterministic=False,
        maximum_classification=provider_config.classification,
        max_input_tokens=provider_config.max_input_tokens,
        max_output_tokens=provider_config.max_output_tokens,
        max_cost_microusd=provider_config.max_cost_microusd,
    )
    gateway = ModelGateway((profile, planning_profile), {adapter.adapter_id: adapter})
    provider = GatewayChangeProvider(gateway)
    planner = GatewayPlanner(gateway)
    worktrees = GitWorktreeLifecycle(git_executable=provider_config.git_executable)
    isolation = config.isolation
    policy = BubblewrapIsolationPolicy(
        executables=tuple(
            BubblewrapExecutable(item.alias, item.path) for item in isolation.executables
        ),
        runtime_roots=isolation.runtime_roots,
        address_space_limit_bytes=isolation.address_space_limit_bytes,
        cpu_limit_seconds=isolation.cpu_limit_seconds,
        process_limit=isolation.process_limit,
        open_file_limit=isolation.open_file_limit,
        file_size_limit_bytes=isolation.file_size_limit_bytes,
        tmpfs_limit_bytes=isolation.tmpfs_limit_bytes,
    )
    acceptance = BubblewrapAcceptanceRunner(
        policy,
        worktrees,
        bubblewrap_executable=isolation.bubblewrap_executable,
        prlimit_executable=isolation.prlimit_executable,
        probe_executable=isolation.probe_executable,
    )
    return _ExecutionBoundaries(worktrees, provider, planner, acceptance)


__all__ = [
    "CHANGE_CODEX_MAX_INPUT_BYTES",
    "CHANGE_CODEX_MAX_RESPONSE_BYTES",
    "CHANGE_CODEX_MAX_STDOUT_BYTES",
    "ExecutionCycleRunner",
    "ExecutionReconciliationPort",
    "ExecutionWorkerProcess",
    "ExecutionWorkerProcessError",
    "ExecutionWorkerProcessFailureCode",
    "validate_execution_worker_runtime_config",
]
