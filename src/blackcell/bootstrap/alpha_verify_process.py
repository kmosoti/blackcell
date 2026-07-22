"""Foreground process composition for deterministic alpha verification."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from threading import Event
from typing import Protocol

from blackcell.bootstrap.alpha_runtime import AlphaRuntimeApiService
from blackcell.bootstrap.alpha_verify_runtime import (
    AlphaClaimedVerification,
    AlphaVerificationReconciliationReport,
    AlphaVerificationRuntimeService,
)
from blackcell.bootstrap.alpha_verify_source import (
    AlphaPreparedVerification,
    AlphaVerificationSourceService,
)
from blackcell.bootstrap.alpha_verify_worker import (
    AlphaVerificationWorker,
    AlphaVerificationWorkerCycleResult,
    AlphaVerificationWorkerPolicy,
    DeterministicAlphaVerifier,
)
from blackcell.config import AlphaVerifyWorkerRuntimeConfig, RuntimeProcessConfig
from blackcell.kernel import ArtifactRef, ArtifactStore, EventStore
from blackcell.orchestration.alpha_verify import AlphaVerificationStatus
from blackcell.orchestration.alpha_verify_lifecycle import (
    AlphaVerificationCandidate,
    AlphaVerificationLease,
    AlphaVerificationLifecycleState,
)
from blackcell.runtime import RuntimeStorageQuota, StorageQuotaPort


class AlphaVerifyWorkerProcessFailureCode(StrEnum):
    NOT_CONFIGURED = "alpha-verify-worker-not-configured"


class AlphaVerifyWorkerProcessError(RuntimeError):
    def __init__(self, code: AlphaVerifyWorkerProcessFailureCode) -> None:
        self.code = code
        super().__init__(code.value)


class AlphaVerifyCycleRunner(Protocol):
    def run_once(self) -> AlphaVerificationWorkerCycleResult: ...


class AlphaVerifyReconciliationPort(Protocol):
    def reconcile(self, *, principal_id: str) -> AlphaVerificationReconciliationReport: ...


@dataclass(frozen=True, slots=True)
class _AlphaVerificationSource:
    """Expose immutable verifier input reconstruction without execution mutation methods."""

    _source: AlphaVerificationSourceService

    def verification_run_ids(self) -> tuple[str, ...]:
        return self._source.verification_run_ids()

    def verification_candidate(self, run_id: str) -> AlphaVerificationCandidate:
        return self._source.verification_candidate(run_id)

    def prepare_verification(
        self,
        candidate: AlphaVerificationCandidate,
    ) -> AlphaPreparedVerification:
        return self._source.prepare_verification(candidate)


@dataclass(frozen=True, slots=True)
class _AlphaVerificationWorkerScheduler:
    """Expose verifier transitions without supervisor reconciliation."""

    _scheduler: AlphaVerificationRuntimeService

    def inspect(self, run_id: str) -> AlphaVerificationLifecycleState | None:
        return self._scheduler.inspect(run_id)

    def claim(
        self,
        candidate: AlphaVerificationCandidate,
        *,
        worker_id: str,
        lease_expires_at: datetime,
        claimed_at: datetime | None = None,
    ) -> AlphaClaimedVerification:
        return self._scheduler.claim(
            candidate,
            worker_id=worker_id,
            lease_expires_at=lease_expires_at,
            claimed_at=claimed_at,
        )

    def record_completed(
        self,
        lease: AlphaVerificationLease,
        *,
        verdict: AlphaVerificationStatus,
        report_artifact_digest: str,
        matrix_digest: str,
        principal_id: str,
        completed_at: datetime | None = None,
    ) -> AlphaVerificationLifecycleState:
        return self._scheduler.record_completed(
            lease,
            verdict=verdict,
            report_artifact_digest=report_artifact_digest,
            matrix_digest=matrix_digest,
            principal_id=principal_id,
            completed_at=completed_at,
        )

    def record_failure(
        self,
        lease: AlphaVerificationLease,
        *,
        failure_code: str,
        result_artifact_digest: str | None,
        principal_id: str,
        failed_at: datetime | None = None,
    ) -> AlphaVerificationLifecycleState:
        return self._scheduler.record_failure(
            lease,
            failure_code=failure_code,
            result_artifact_digest=result_artifact_digest,
            principal_id=principal_id,
            failed_at=failed_at,
        )


@dataclass(frozen=True, slots=True)
class _AlphaVerificationArtifactWriter:
    """Expose immutable report writes without artifact reads or maintenance."""

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
class AlphaVerifyWorkerProcess:
    """Run deterministic verification against shared immutable evidence."""

    coordinator: AlphaVerifyCycleRunner
    scheduler: AlphaVerifyReconciliationPort
    config: RuntimeProcessConfig
    stop_event: Event = field(default_factory=Event)
    storage_quota: StorageQuotaPort | None = None

    @classmethod
    def from_config(
        cls,
        config: RuntimeProcessConfig,
        *,
        stop_event: Event | None = None,
    ) -> AlphaVerifyWorkerProcess:
        alpha = _required_verify_config(config)
        database_path = config.security.paths.ensure_database_file()
        events = EventStore(database_path)
        artifacts = ArtifactStore(
            config.security.paths.artifact_root,
            database_path=database_path,
            max_total_bytes=config.quota.artifact_max_total_bytes,
        )
        scheduler = AlphaVerificationRuntimeService(events)
        source = AlphaVerificationSourceService(
            events,
            AlphaRuntimeApiService(
                events,
                config.repository_root,
                artifacts=artifacts,
            ),
            artifacts,
        )
        coordinator = AlphaVerificationWorker(
            source=_AlphaVerificationSource(source),
            scheduler=_AlphaVerificationWorkerScheduler(scheduler),
            artifacts=_AlphaVerificationArtifactWriter(artifacts),
            verifier=DeterministicAlphaVerifier(),
            policy=AlphaVerificationWorkerPolicy(
                worker_id=alpha.worker.worker_id,
                lease_seconds=alpha.worker.lease_seconds,
            ),
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
        alpha = _required_verify_config(self.config)
        self.scheduler.reconcile(principal_id=alpha.worker.supervisor_id)
        while not self.stop_event.is_set():
            cycle = (
                None
                if self.storage_quota is not None and not self.storage_quota.has_mutation_capacity()
                else self.coordinator.run_once()
            )
            if once:
                return 3 if cycle is None or cycle.status == "idle" else 0
            if cycle is None or cycle.status in {"idle", "claim-conflict"}:
                self.stop_event.wait(alpha.worker.poll_milliseconds / 1_000)
        return 0


def validate_alpha_verify_worker_runtime_config(config: RuntimeProcessConfig) -> None:
    """Require explicit deterministic-verifier authority before daemon spawn."""

    _required_verify_config(config)


def _required_verify_config(config: RuntimeProcessConfig) -> AlphaVerifyWorkerRuntimeConfig:
    if not isinstance(config, RuntimeProcessConfig):
        raise TypeError("alpha verify worker requires runtime process configuration")
    if config.alpha_verify_worker is None:
        raise AlphaVerifyWorkerProcessError(AlphaVerifyWorkerProcessFailureCode.NOT_CONFIGURED)
    return config.alpha_verify_worker


__all__ = [
    "AlphaVerifyCycleRunner",
    "AlphaVerifyReconciliationPort",
    "AlphaVerifyWorkerProcess",
    "AlphaVerifyWorkerProcessError",
    "AlphaVerifyWorkerProcessFailureCode",
    "validate_alpha_verify_worker_runtime_config",
]
