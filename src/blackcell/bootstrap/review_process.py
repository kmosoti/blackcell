"""Foreground process composition for the opt-in review worker."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from threading import Event
from typing import Protocol

from blackcell.adapters.models import CODEX_CLI_ADAPTER_ID, CodexCliModelAdapter
from blackcell.adapters.models.review_provider import GatewayReviewer
from blackcell.bootstrap.review_runtime import (
    ClaimedReview,
    ReviewReconciliationReport,
    ReviewRuntimeService,
)
from blackcell.bootstrap.review_worker import (
    ReviewWorker,
    ReviewWorkerCycleResult,
    ReviewWorkerPolicy,
)
from blackcell.bootstrap.runtime_service import RuntimeService
from blackcell.bootstrap.worker_process_lock import (
    WorkerProcessLockError,
    WorkerProcessLockFailureCode,
    WorkerProcessRole,
    worker_process_lock,
)
from blackcell.config import ReviewWorkerRuntimeConfig, RuntimeProcessConfig
from blackcell.gateway import GatewayBudget, GatewayProfile, ModelCapability, ModelGateway
from blackcell.kernel import ArtifactRef, ArtifactStore, EventStore
from blackcell.orchestration.review import (
    MAX_REVIEW_CONTEXT_BYTES,
    MAX_REVIEW_PROPOSAL_BYTES,
    ReviewContext,
)
from blackcell.orchestration.review_lifecycle import (
    ReviewCandidate,
    ReviewLease,
    ReviewLifecycleState,
)
from blackcell.runtime import RuntimeStorageQuota, StorageQuotaPort

_CODEX_CONTRACT_OVERHEAD_BYTES = 1024 * 1024
REVIEW_CODEX_MAX_INPUT_BYTES = MAX_REVIEW_CONTEXT_BYTES + _CODEX_CONTRACT_OVERHEAD_BYTES
REVIEW_CODEX_MAX_RESPONSE_BYTES = MAX_REVIEW_PROPOSAL_BYTES + _CODEX_CONTRACT_OVERHEAD_BYTES
REVIEW_CODEX_MAX_STDOUT_BYTES = 2 * REVIEW_CODEX_MAX_RESPONSE_BYTES + _CODEX_CONTRACT_OVERHEAD_BYTES


class ReviewWorkerProcessFailureCode(StrEnum):
    NOT_CONFIGURED = "review-worker-not-configured"
    ALREADY_RUNNING = "review-worker-already-running"
    LOCK_UNAVAILABLE = "review-worker-lock-unavailable"


class ReviewWorkerProcessError(RuntimeError):
    def __init__(self, code: ReviewWorkerProcessFailureCode) -> None:
        self.code = code
        super().__init__(code.value)


class ReviewCycleRunner(Protocol):
    def run_once(self) -> ReviewWorkerCycleResult: ...


class ReviewReconciliationPort(Protocol):
    def reconcile(self, *, principal_id: str) -> ReviewReconciliationReport: ...


@dataclass(frozen=True, slots=True)
class _ReviewExecutionSource:
    """Narrow the full execution runtime to the review worker's read-only source port."""

    _runtime: RuntimeService

    def review_run_ids(self) -> tuple[str, ...]:
        return self._runtime.review_run_ids()

    def review_candidate(self, run_id: str) -> ReviewCandidate:
        return self._runtime.review_candidate(run_id)

    def prepare_review_context(self, candidate: ReviewCandidate) -> ReviewContext:
        return self._runtime.prepare_review_context(candidate)


@dataclass(frozen=True, slots=True)
class _ReviewWorkerScheduler:
    """Expose reviewer transitions without exposing supervisor reconciliation."""

    _scheduler: ReviewRuntimeService

    def inspect(self, run_id: str) -> ReviewLifecycleState | None:
        return self._scheduler.inspect(run_id)

    def claim(
        self,
        candidate: ReviewCandidate,
        *,
        worker_id: str,
        lease_expires_at: datetime,
        claimed_at: datetime | None = None,
    ) -> ClaimedReview:
        return self._scheduler.claim(
            candidate,
            worker_id=worker_id,
            lease_expires_at=lease_expires_at,
            claimed_at=claimed_at,
        )

    def renew_lease(
        self,
        lease: ReviewLease,
        *,
        lease_expires_at: datetime,
        principal_id: str,
        renewed_at: datetime | None = None,
    ) -> ReviewLease:
        return self._scheduler.renew_lease(
            lease,
            lease_expires_at=lease_expires_at,
            principal_id=principal_id,
            renewed_at=renewed_at,
        )

    def record_provider_dispatch(
        self,
        lease: ReviewLease,
        *,
        acceptance_digest: str,
        context_digest: str,
        context_artifact_digest: str,
        principal_id: str,
        dispatched_at: datetime | None = None,
    ) -> str:
        return self._scheduler.record_provider_dispatch(
            lease,
            acceptance_digest=acceptance_digest,
            context_digest=context_digest,
            context_artifact_digest=context_artifact_digest,
            principal_id=principal_id,
            dispatched_at=dispatched_at,
        )

    def record_success(
        self,
        lease: ReviewLease,
        *,
        context_digest: str,
        proposal_artifact_digest: str,
        provider_result_artifact_digest: str,
        admitted_artifact_digest: str,
        finding_count: int,
        principal_id: str,
        completed_at: datetime | None = None,
    ) -> ReviewLifecycleState:
        return self._scheduler.record_success(
            lease,
            context_digest=context_digest,
            proposal_artifact_digest=proposal_artifact_digest,
            provider_result_artifact_digest=provider_result_artifact_digest,
            admitted_artifact_digest=admitted_artifact_digest,
            finding_count=finding_count,
            principal_id=principal_id,
            completed_at=completed_at,
        )

    def record_failure(
        self,
        lease: ReviewLease,
        *,
        failure_code: str,
        result_artifact_digest: str | None,
        principal_id: str,
        failed_at: datetime | None = None,
    ) -> ReviewLifecycleState:
        return self._scheduler.record_failure(
            lease,
            failure_code=failure_code,
            result_artifact_digest=result_artifact_digest,
            principal_id=principal_id,
            failed_at=failed_at,
        )


@dataclass(frozen=True, slots=True)
class _ReviewArtifactWriter:
    """Expose immutable artifact writes without read or maintenance methods."""

    _artifacts: ArtifactStore

    def put_bytes(
        self,
        data: bytes,
        *,
        media_type: str = "application/octet-stream",
        encoding: str | None = None,
    ) -> ArtifactRef:
        return self._artifacts.put_bytes(data, media_type=media_type, encoding=encoding)


@dataclass(slots=True)
class ReviewWorkerProcess:
    """Run one reviewer against shared evidence without execution authority."""

    coordinator: ReviewCycleRunner
    scheduler: ReviewReconciliationPort
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
    ) -> ReviewWorkerProcess:
        review_config = _required_review_config(config)
        reviewer = _reviewer(review_config, environment=environment)
        policy = _review_policy(review_config)
        database_path = config.security.paths.ensure_database_file()
        events = EventStore(database_path)
        artifacts = ArtifactStore(
            config.security.paths.artifact_root,
            database_path=database_path,
            max_total_bytes=config.quota.artifact_max_total_bytes,
        )
        scheduler = ReviewRuntimeService(events)
        execution_source = _ReviewExecutionSource(
            RuntimeService(
                events,
                config.repository_root,
                artifacts=artifacts,
            )
        )
        coordinator = ReviewWorker(
            execution=execution_source,
            scheduler=_ReviewWorkerScheduler(scheduler),
            artifacts=_ReviewArtifactWriter(artifacts),
            reviewer=reviewer,
            policy=policy,
        )
        return cls(
            coordinator,
            scheduler,
            config,
            stop_event or Event(),
            RuntimeStorageQuota(
                config.security.paths,
                max_active_bytes=config.quota.active_storage_max_bytes,
                mutation_reserve_bytes=config.quota.mutation_reserve_bytes,
            ),
        )

    def serve(self, *, once: bool = False) -> int:
        review_config = _required_review_config(self.config)
        try:
            with worker_process_lock(
                self.config.security.paths,
                WorkerProcessRole.REVIEW,
            ):
                self.scheduler.reconcile(principal_id=review_config.worker.supervisor_id)
                while not self.stop_event.is_set():
                    cycle = (
                        None
                        if self.storage_quota is not None
                        and not self.storage_quota.has_mutation_capacity()
                        else self.coordinator.run_once()
                    )
                    if once:
                        return 3 if cycle is None or cycle.status == "idle" else 0
                    if cycle is None or cycle.status in {"idle", "claim-conflict"}:
                        self.stop_event.wait(review_config.worker.poll_milliseconds / 1_000)
                return 0
        except WorkerProcessLockError as error:
            code = (
                ReviewWorkerProcessFailureCode.ALREADY_RUNNING
                if error.code is WorkerProcessLockFailureCode.ALREADY_RUNNING
                else ReviewWorkerProcessFailureCode.LOCK_UNAVAILABLE
            )
            raise ReviewWorkerProcessError(code) from error


def validate_review_worker_runtime_config(
    config: RuntimeProcessConfig,
    *,
    environment: Mapping[str, str] | None = None,
) -> None:
    """Resolve the explicit REVIEW route before daemon children start."""

    execution = _required_review_config(config)
    _reviewer(execution, environment=environment)
    _review_policy(execution)


def _required_review_config(config: RuntimeProcessConfig) -> ReviewWorkerRuntimeConfig:
    if not isinstance(config, RuntimeProcessConfig):
        raise TypeError("review worker requires runtime process configuration")
    if config.review_worker is None:
        raise ReviewWorkerProcessError(ReviewWorkerProcessFailureCode.NOT_CONFIGURED)
    return config.review_worker


def _reviewer(
    config: ReviewWorkerRuntimeConfig,
    *,
    environment: Mapping[str, str] | None,
) -> GatewayReviewer:
    provider = config.provider
    values = os.environ if environment is None else environment
    try:
        provider_environment = {name: values[name] for name in provider.environment_variables}
    except KeyError as error:
        raise ValueError("execution review provider environment is incomplete") from error
    adapter = CodexCliModelAdapter(
        executable=provider.codex_executable,
        git_executable=provider.git_executable,
        environment=provider_environment,
        timeout_ceiling_seconds=provider.timeout_ceiling_seconds,
        max_input_bytes=REVIEW_CODEX_MAX_INPUT_BYTES,
        max_stdout_bytes=REVIEW_CODEX_MAX_STDOUT_BYTES,
        max_response_bytes=REVIEW_CODEX_MAX_RESPONSE_BYTES,
    )
    profile = GatewayProfile(
        profile_id=provider.profile_id,
        capability=ModelCapability.REVIEW,
        adapter_id=CODEX_CLI_ADAPTER_ID,
        model_id=provider.model_id,
        priority=0,
        local=False,
        deterministic=False,
        maximum_classification=provider.classification,
        max_input_tokens=provider.max_input_tokens,
        max_output_tokens=provider.max_output_tokens,
        max_cost_microusd=provider.max_cost_microusd,
    )
    return GatewayReviewer(ModelGateway((profile,), {adapter.adapter_id: adapter}))


def _review_policy(config: ReviewWorkerRuntimeConfig) -> ReviewWorkerPolicy:
    provider = config.provider
    return ReviewWorkerPolicy(
        worker_id=config.worker.worker_id,
        classification=provider.classification,
        locality=provider.locality,
        budget=GatewayBudget(
            provider.max_input_tokens,
            provider.max_output_tokens,
            provider.timeout_ceiling_seconds * 1_000,
            provider.max_cost_microusd,
        ),
        lease_seconds=config.worker.lease_seconds,
    )


__all__ = [
    "REVIEW_CODEX_MAX_INPUT_BYTES",
    "REVIEW_CODEX_MAX_RESPONSE_BYTES",
    "REVIEW_CODEX_MAX_STDOUT_BYTES",
    "ReviewCycleRunner",
    "ReviewReconciliationPort",
    "ReviewWorkerProcess",
    "ReviewWorkerProcessError",
    "ReviewWorkerProcessFailureCode",
    "validate_review_worker_runtime_config",
]
