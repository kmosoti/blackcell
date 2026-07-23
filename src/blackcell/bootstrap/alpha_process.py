"""Foreground process composition for the opt-in alpha execution worker."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from threading import Event
from typing import Protocol

from blackcell.adapters.execution.bubblewrap import (
    BubblewrapAcceptanceRunner,
    BubblewrapExecutable,
    BubblewrapIsolationPolicy,
)
from blackcell.adapters.execution.evidence import AlphaEvidenceCollector
from blackcell.adapters.execution.text_changes import TextChangeExecutor
from blackcell.adapters.execution.worktree import GitWorktreeLifecycle
from blackcell.adapters.models import CODEX_CLI_ADAPTER_ID, CodexCliModelAdapter
from blackcell.adapters.models.alpha_change_provider import GatewayAlphaChangeProvider
from blackcell.bootstrap.alpha_runtime import (
    AlphaRuntimeApiService,
    AlphaWorktreeMaintenanceReport,
)
from blackcell.bootstrap.alpha_worker import (
    AlphaRuntimeWorker,
    AlphaWorkerCycleResult,
    AlphaWorkerPolicy,
)
from blackcell.config import AlphaWorkerRuntimeConfig, RuntimeProcessConfig
from blackcell.gateway import GatewayProfile, ModelCapability, ModelGateway
from blackcell.kernel import ArtifactStore, EventStore
from blackcell.orchestration.alpha_changes import (
    MAX_ALPHA_CHANGE_CONTEXT_BYTES,
    MAX_ALPHA_CHANGE_PROPOSAL_BYTES,
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


class AlphaWorkerProcessFailureCode(StrEnum):
    NOT_CONFIGURED = "alpha-worker-not-configured"


class AlphaWorkerProcessError(RuntimeError):
    def __init__(self, code: AlphaWorkerProcessFailureCode) -> None:
        self.code = code
        super().__init__(code.value)


class AlphaCycleRunner(Protocol):
    def run_once(self) -> AlphaWorkerCycleResult: ...


class AlphaReconciliationPort(Protocol):
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
    acceptance: BubblewrapAcceptanceRunner


@dataclass(slots=True)
class AlphaWorkerProcess:
    """Run one alpha coordinator at a time against the canonical local ledger."""

    coordinator: AlphaCycleRunner
    runtime: AlphaReconciliationPort
    config: RuntimeProcessConfig
    stop_event: Event = field(default_factory=Event)
    storage_quota: StorageQuotaPort | None = None

    @classmethod
    def from_config(
        cls,
        config: RuntimeProcessConfig,
        *,
        stop_event: Event | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> AlphaWorkerProcess:
        alpha = _required_alpha_config(config)
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
        return cls(
            coordinator,
            runtime,
            config,
            stop_event or Event(),
            RuntimeStorageQuota(
                config.security.paths,
                max_active_bytes=config.quota.active_storage_max_bytes,
                mutation_reserve_bytes=config.quota.mutation_reserve_bytes,
            ),
        )

    def serve(self, *, once: bool = False) -> int:
        alpha = _required_alpha_config(self.config)
        worker_id = alpha.worker.worker_id
        self.runtime.reconcile_startup(principal_id=worker_id)
        maintenance = self.runtime.maintain_successful_worktrees(
            max_retained=alpha.worker.max_retained_successful_worktrees,
            principal_id=worker_id,
        )
        while not self.stop_event.is_set():
            if not maintenance.quota_satisfied or (
                self.storage_quota is not None and not self.storage_quota.has_mutation_capacity()
            ):
                cycle: AlphaWorkerCycleResult | None = None
            else:
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
    adapter = CodexCliModelAdapter(
        executable=provider_config.codex_executable,
        git_executable=provider_config.git_executable,
        environment=provider_environment,
        timeout_ceiling_seconds=provider_config.timeout_ceiling_seconds,
        max_input_bytes=ALPHA_CHANGE_CODEX_MAX_INPUT_BYTES,
        max_stdout_bytes=ALPHA_CHANGE_CODEX_MAX_STDOUT_BYTES,
        max_response_bytes=ALPHA_CHANGE_CODEX_MAX_RESPONSE_BYTES,
    )
    profile = GatewayProfile(
        profile_id=provider_config.profile_id,
        capability=ModelCapability.CODE,
        adapter_id=CODEX_CLI_ADAPTER_ID,
        model_id=provider_config.model_id,
        priority=0,
        local=False,
        deterministic=False,
        maximum_classification=provider_config.classification,
        max_input_tokens=provider_config.max_input_tokens,
        max_output_tokens=provider_config.max_output_tokens,
        max_cost_microusd=provider_config.max_cost_microusd,
    )
    provider = GatewayAlphaChangeProvider(ModelGateway((profile,), {adapter.adapter_id: adapter}))
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
    return _AlphaExecutionBoundaries(worktrees, provider, acceptance)


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
