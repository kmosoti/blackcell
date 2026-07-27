"""Foreground process composition for the opt-in alpha execution worker."""

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
from blackcell.adapters.execution.evidence import AlphaEvidenceCollector
from blackcell.adapters.execution.text_changes import TextChangeExecutor
from blackcell.adapters.execution.worktree import GitWorktreeLifecycle
from blackcell.adapters.models import (
    AGY_CLI_ADAPTER_ID,
    CODEX_CLI_ADAPTER_ID,
    AgyCliModelAdapter,
    CodexCliModelAdapter,
    GatewayAlphaPlanner,
)
from blackcell.adapters.models.agy_cli import AgyEffort
from blackcell.adapters.models.alpha_change_provider import GatewayAlphaChangeProvider
from blackcell.adapters.telemetry import AlphaV2TraceObserver, RuntimeTelemetry
from blackcell.bootstrap.alpha_runtime import (
    AlphaGeneratedRun,
    AlphaRuntimeApiService,
    AlphaWorktreeMaintenanceReport,
)
from blackcell.bootstrap.alpha_v2_kernel import (
    AlphaV2ExecutionPolicy,
    AlphaV2KernelRunResult,
    ProductionAlphaV2AttemptExecutor,
    ProductionAlphaV2Kernel,
)
from blackcell.bootstrap.alpha_worker import (
    AlphaRuntimeWorker,
    AlphaWorkerCycleResult,
    AlphaWorkerPolicy,
)
from blackcell.bootstrap.worker_process_lock import (
    WorkerProcessLockError,
    WorkerProcessLockFailureCode,
    WorkerProcessRole,
    worker_process_lock,
)
from blackcell.config import (
    AlphaExecutionProviderAdapter,
    AlphaWorkerRuntimeConfig,
    RuntimeProcessConfig,
)
from blackcell.gateway import GatewayBudget, GatewayProfile, ModelCapability, ModelGateway
from blackcell.kernel import ArtifactStore, CheckpointStore, EventStore
from blackcell.kernel._json import canonical_json_bytes
from blackcell.orchestration.alpha_changes import (
    MAX_ALPHA_CHANGE_CONTEXT_BYTES,
    MAX_ALPHA_CHANGE_PROPOSAL_BYTES,
)
from blackcell.orchestration.alpha_v2 import (
    AlphaPlanningRequest,
    AlphaV2PolicyKernel,
    RunLifecycleStatus,
    alpha_planning_payload,
)
from blackcell.orchestration.alpha_v2_runtime import (
    AlphaV2Coordinator,
    EventBackedAlphaV2RunJournal,
)
from blackcell.runtime import RuntimeStorageQuota, StorageQuotaPort

_CODEX_CONTRACT_OVERHEAD_BYTES = 1024 * 1024
ALPHA_CHANGE_CODEX_MAX_INPUT_BYTES = MAX_ALPHA_CHANGE_CONTEXT_BYTES + _CODEX_CONTRACT_OVERHEAD_BYTES
ALPHA_CHANGE_CODEX_MAX_RESPONSE_BYTES = (
    MAX_ALPHA_CHANGE_PROPOSAL_BYTES + _CODEX_CONTRACT_OVERHEAD_BYTES
)
ALPHA_CHANGE_CODEX_MAX_STDOUT_BYTES = (
    2 * ALPHA_CHANGE_CODEX_MAX_RESPONSE_BYTES + _CODEX_CONTRACT_OVERHEAD_BYTES
)
ALPHA_CHANGE_AGY_MAX_INPUT_BYTES = MAX_ALPHA_CHANGE_CONTEXT_BYTES + _CODEX_CONTRACT_OVERHEAD_BYTES
ALPHA_CHANGE_AGY_MAX_STDOUT_BYTES = MAX_ALPHA_CHANGE_PROPOSAL_BYTES + _CODEX_CONTRACT_OVERHEAD_BYTES


class AlphaWorkerProcessFailureCode(StrEnum):
    NOT_CONFIGURED = "alpha-worker-not-configured"
    ALREADY_RUNNING = "alpha-worker-already-running"
    LOCK_UNAVAILABLE = "alpha-worker-lock-unavailable"


class AlphaWorkerProcessError(RuntimeError):
    def __init__(self, code: AlphaWorkerProcessFailureCode) -> None:
        self.code = code
        super().__init__(code.value)


class AlphaCycleRunner(Protocol):
    def run_once(self) -> AlphaWorkerCycleResult: ...


class AlphaReconciliationPort(Protocol):
    def next_generated_run(self) -> AlphaGeneratedRun | None: ...

    def should_cancel_generated_run(self, run_id: str) -> bool: ...

    def reconcile_startup(self, *, principal_id: str) -> tuple[object, ...]: ...

    def maintain_successful_worktrees(
        self,
        *,
        max_retained: int,
        principal_id: str,
    ) -> AlphaWorktreeMaintenanceReport: ...


@dataclass(frozen=True, slots=True)
class _AlphaExecutionBoundaries:
    worktrees: GitWorktreeLifecycle
    provider: GatewayAlphaChangeProvider
    planner: GatewayAlphaPlanner
    acceptance: BubblewrapAcceptanceRunner


@dataclass(slots=True)
class AlphaWorkerProcess:
    """Run one alpha coordinator at a time against the canonical local ledger."""

    coordinator: AlphaCycleRunner
    runtime: AlphaReconciliationPort
    config: RuntimeProcessConfig
    stop_event: Event = field(default_factory=Event)
    storage_quota: StorageQuotaPort | None = None
    alpha_v2: ProductionAlphaV2Kernel | None = None
    shutdown: Callable[[], None] = field(default=lambda: None, repr=False)

    @classmethod
    def from_config(
        cls,
        config: RuntimeProcessConfig,
        *,
        stop_event: Event | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> AlphaWorkerProcess:
        alpha = _required_alpha_config(config)
        telemetry = RuntimeTelemetry.from_config(config)
        try:
            boundaries = _execution_boundaries(alpha, environment=environment)
            database_path = config.security.paths.ensure_database_file()
            events = EventStore(database_path)
            artifacts = ArtifactStore(
                config.security.paths.artifact_root,
                database_path=database_path,
                max_total_bytes=config.quota.artifact_max_total_bytes,
            )
            runtime = AlphaRuntimeApiService(
                events,
                config.repository_root,
                isolation_root=alpha.isolation.root,
                worktrees=boundaries.worktrees,
                artifacts=artifacts,
            )
            coordinator = AlphaRuntimeWorker(
                runtime=runtime,
                artifacts=artifacts,
                provider=boundaries.provider,
                change_executor=TextChangeExecutor(boundaries.worktrees),
                acceptance=boundaries.acceptance,
                worktrees=boundaries.worktrees,
                evidence=AlphaEvidenceCollector(boundaries.worktrees),
                policy=AlphaWorkerPolicy(
                    worker_id=alpha.worker.worker_id,
                    classification=alpha.provider.classification,
                    locality=alpha.provider.locality,
                    stdout_limit_bytes=alpha.worker.stdout_limit_bytes,
                    stderr_limit_bytes=alpha.worker.stderr_limit_bytes,
                    lease_grace_seconds=alpha.worker.lease_grace_seconds,
                ),
            )
            alpha_v2_executor = ProductionAlphaV2AttemptExecutor(
                repository_root=config.repository_root,
                isolation_root=alpha.isolation.root,
                artifacts=artifacts,
                change_provider=boundaries.provider,
                acceptance=boundaries.acceptance,
                policy=AlphaV2ExecutionPolicy(
                    worker_id=alpha.worker.worker_id,
                    classification=alpha.provider.classification,
                    locality=alpha.provider.locality,
                    provider_budget=GatewayBudget(
                        alpha.provider.max_input_tokens,
                        alpha.provider.max_output_tokens,
                        alpha.provider.timeout_ceiling_seconds * 1_000,
                        alpha.provider.max_cost_microusd,
                    ),
                    check_timeout_seconds=min(alpha.provider.timeout_ceiling_seconds, 600),
                    stdout_limit_bytes=alpha.worker.stdout_limit_bytes,
                    stderr_limit_bytes=alpha.worker.stderr_limit_bytes,
                ),
                worktrees=boundaries.worktrees,
                evidence=AlphaEvidenceCollector(boundaries.worktrees),
                changes=TextChangeExecutor(boundaries.worktrees),
                cancel_requested=runtime.should_cancel_generated_run,
            )
            observer = (
                None if telemetry.recorder is None else AlphaV2TraceObserver(telemetry.recorder)
            )
            alpha_v2 = ProductionAlphaV2Kernel(
                AlphaV2Coordinator(
                    EventBackedAlphaV2RunJournal(
                        events,
                        CheckpointStore(database_path),
                        observer=observer,
                    ),
                    boundaries.planner,
                    alpha_v2_executor,
                    AlphaV2PolicyKernel(),
                )
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
            alpha_v2=alpha_v2,
            shutdown=telemetry.shutdown,
        )

    def run_v2(self, request: AlphaPlanningRequest, *, actor: str) -> AlphaV2KernelRunResult:
        """Run one admitted alpha-v2 goal through the production-composed kernel."""

        if self.alpha_v2 is None:
            raise AlphaWorkerProcessError(AlphaWorkerProcessFailureCode.NOT_CONFIGURED)
        return self.alpha_v2.run(request, actor=actor)

    def _run_generated_once(
        self,
        alpha: AlphaWorkerRuntimeConfig,
        *,
        actor: str,
    ) -> AlphaWorkerCycleResult | None:
        if self.alpha_v2 is None:
            return None
        generated = self.runtime.next_generated_run()
        if generated is None:
            return None
        budget = GatewayBudget(
            alpha.provider.max_input_tokens,
            alpha.provider.max_output_tokens,
            alpha.provider.timeout_ceiling_seconds * 1_000,
            alpha.provider.max_cost_microusd,
        )
        payload_size = len(canonical_json_bytes(alpha_planning_payload(generated.goal)))
        request = AlphaPlanningRequest(
            goal=generated.goal,
            classification=alpha.provider.classification,
            locality=alpha.provider.locality,
            budget=budget,
            estimated_input_tokens=(payload_size + 3) // 4,
            correlation_id=generated.run_id,
            run_id=generated.run_id,
        )
        state = self.alpha_v2.process(request, actor=actor)
        if state.status is RunLifecycleStatus.SUCCEEDED:
            status = "node-succeeded"
            failure_code = None
        elif state.status is RunLifecycleStatus.CANCELED:
            status = "node-canceled"
            failure_code = None
        else:
            status = "node-failed"
            failure_code = state.status.value
        return AlphaWorkerCycleResult(
            status=status,
            run_id=generated.run_id,
            run_status=state.status.value,
            failure_code=failure_code,
        )

    def serve(self, *, once: bool = False) -> int:
        alpha = _required_alpha_config(self.config)
        worker_id = alpha.worker.worker_id
        try:
            with worker_process_lock(
                self.config.security.paths,
                WorkerProcessRole.ALPHA_EXECUTION,
            ):
                self.runtime.reconcile_startup(principal_id=worker_id)
                maintenance = self.runtime.maintain_successful_worktrees(
                    max_retained=alpha.worker.max_retained_successful_worktrees,
                    principal_id=worker_id,
                )
                while not self.stop_event.is_set():
                    if not maintenance.quota_satisfied or (
                        self.storage_quota is not None
                        and not self.storage_quota.has_mutation_capacity()
                    ):
                        cycle: AlphaWorkerCycleResult | None = None
                    else:
                        cycle = self._run_generated_once(alpha, actor=worker_id)
                        if cycle is None:
                            cycle = self.coordinator.run_once()
                    if cycle is not None and cycle.status in {
                        "node-succeeded",
                        "node-failed",
                        "node-canceled",
                    }:
                        maintenance = self.runtime.maintain_successful_worktrees(
                            max_retained=alpha.worker.max_retained_successful_worktrees,
                            principal_id=worker_id,
                        )
                    if once:
                        return 3 if cycle is None or cycle.status == "idle" else 0
                    if cycle is None or cycle.status in {"idle", "claim-conflict"}:
                        self.stop_event.wait(self.config.worker_poll_milliseconds / 1_000)
                return 0
        except WorkerProcessLockError as error:
            code = (
                AlphaWorkerProcessFailureCode.ALREADY_RUNNING
                if error.code is WorkerProcessLockFailureCode.ALREADY_RUNNING
                else AlphaWorkerProcessFailureCode.LOCK_UNAVAILABLE
            )
            raise AlphaWorkerProcessError(code) from error
        finally:
            self.shutdown()


def validate_alpha_worker_runtime_config(
    config: RuntimeProcessConfig,
    *,
    environment: Mapping[str, str] | None = None,
) -> None:
    """Resolve every executable and isolation invariant before daemon children start."""

    _execution_boundaries(_required_alpha_config(config), environment=environment)


def _required_alpha_config(config: RuntimeProcessConfig) -> AlphaWorkerRuntimeConfig:
    if not isinstance(config, RuntimeProcessConfig):
        raise TypeError("alpha worker requires runtime process configuration")
    if config.alpha_worker is None:
        raise AlphaWorkerProcessError(AlphaWorkerProcessFailureCode.NOT_CONFIGURED)
    return config.alpha_worker


def _execution_boundaries(
    config: AlphaWorkerRuntimeConfig,
    *,
    environment: Mapping[str, str] | None,
) -> _AlphaExecutionBoundaries:
    provider_config = config.provider
    values = os.environ if environment is None else environment
    try:
        provider_environment = {
            name: values[name] for name in provider_config.environment_variables
        }
    except KeyError as error:
        raise ValueError("alpha provider environment is incomplete") from error
    if provider_config.adapter is AlphaExecutionProviderAdapter.CODEX_CLI:
        adapter = CodexCliModelAdapter(
            executable=provider_config.executable,
            git_executable=provider_config.git_executable,
            environment=provider_environment,
            timeout_ceiling_seconds=provider_config.timeout_ceiling_seconds,
            max_input_bytes=ALPHA_CHANGE_CODEX_MAX_INPUT_BYTES,
            max_stdout_bytes=ALPHA_CHANGE_CODEX_MAX_STDOUT_BYTES,
            max_response_bytes=ALPHA_CHANGE_CODEX_MAX_RESPONSE_BYTES,
        )
        adapter_id = CODEX_CLI_ADAPTER_ID
    else:
        if provider_config.effort is None:
            raise ValueError("AGY alpha provider configuration is incomplete")
        adapter = AgyCliModelAdapter(
            executable=provider_config.executable,
            git_executable=provider_config.git_executable,
            effort=cast(AgyEffort, provider_config.effort),
            environment=provider_environment,
            timeout_ceiling_seconds=provider_config.timeout_ceiling_seconds,
            max_input_bytes=ALPHA_CHANGE_AGY_MAX_INPUT_BYTES,
            max_stdout_bytes=ALPHA_CHANGE_AGY_MAX_STDOUT_BYTES,
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
    provider = GatewayAlphaChangeProvider(gateway)
    planner = GatewayAlphaPlanner(gateway)
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
    return _AlphaExecutionBoundaries(worktrees, provider, planner, acceptance)


__all__ = [
    "ALPHA_CHANGE_CODEX_MAX_INPUT_BYTES",
    "ALPHA_CHANGE_CODEX_MAX_RESPONSE_BYTES",
    "ALPHA_CHANGE_CODEX_MAX_STDOUT_BYTES",
    "AlphaCycleRunner",
    "AlphaReconciliationPort",
    "AlphaWorkerProcess",
    "AlphaWorkerProcessError",
    "AlphaWorkerProcessFailureCode",
    "validate_alpha_worker_runtime_config",
]
