"""Foreground process composition for deterministic execution verification."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from threading import Event
from typing import Protocol

from blackcell.bootstrap.runtime_service import RuntimeService
from blackcell.bootstrap.verification_runtime import (
    ClaimedVerification,
    VerificationReconciliationReport,
    VerificationRuntimeService,
)
from blackcell.bootstrap.verification_source import (
    PreparedVerification,
    VerificationSourceService,
)
from blackcell.bootstrap.verification_worker import (
    DeterministicVerifier,
    VerificationWorker,
    VerificationWorkerCycleResult,
    VerificationWorkerPolicy,
)
from blackcell.bootstrap.worker_process_lock import (
    WorkerProcessLockError,
    WorkerProcessLockFailureCode,
    WorkerProcessRole,
    worker_process_lock,
)
from blackcell.config import RuntimeProcessConfig, VerificationWorkerRuntimeConfig
from blackcell.kernel import ArtifactRef, ArtifactStore, EventStore
from blackcell.orchestration.verification import VerificationStatus
from blackcell.orchestration.verification_lifecycle import (
    VerificationCandidate,
    VerificationLease,
    VerificationLifecycleState,
)
from blackcell.runtime import RuntimeStorageQuota, StorageQuotaPort


class VerificationWorkerProcessFailureCode(StrEnum):
    NOT_CONFIGURED = "verification-worker-not-configured"
    ALREADY_RUNNING = "verification-worker-already-running"
    LOCK_UNAVAILABLE = "verification-worker-lock-unavailable"


class VerificationWorkerProcessError(RuntimeError):
    def __init__(self, code: VerificationWorkerProcessFailureCode) -> None:
        self.code = code
        super().__init__(code.value)


class VerificationCycleRunner(Protocol):
    def run_once(self) -> VerificationWorkerCycleResult: ...


class VerificationReconciliationPort(Protocol):
    def reconcile(self, *, principal_id: str) -> VerificationReconciliationReport: ...


@dataclass(frozen=True, slots=True)
class _VerificationSource:
    """Expose immutable verifier input reconstruction without execution mutation methods."""

    _source: VerificationSourceService

    def verification_run_ids(self) -> tuple[str, ...]:
        return self._source.verification_run_ids()

    def verification_candidate(self, run_id: str) -> VerificationCandidate:
        return self._source.verification_candidate(run_id)

    def prepare_verification(
        self,
        candidate: VerificationCandidate,
    ) -> PreparedVerification:
        return self._source.prepare_verification(candidate)


@dataclass(frozen=True, slots=True)
class _VerificationWorkerScheduler:
    """Expose verifier transitions without supervisor reconciliation."""

    _scheduler: VerificationRuntimeService

    def inspect(self, run_id: str) -> VerificationLifecycleState | None:
        return self._scheduler.inspect(run_id)

    def claim(
        self,
        candidate: VerificationCandidate,
        *,
        worker_id: str,
        lease_expires_at: datetime,
        claimed_at: datetime | None = None,
    ) -> ClaimedVerification:
        return self._scheduler.claim(
            candidate,
            worker_id=worker_id,
            lease_expires_at=lease_expires_at,
            claimed_at=claimed_at,
        )

    def record_completed(
        self,
        lease: VerificationLease,
        *,
        verdict: VerificationStatus,
        report_artifact_digest: str,
        matrix_digest: str,
        principal_id: str,
        completed_at: datetime | None = None,
    ) -> VerificationLifecycleState:
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
        lease: VerificationLease,
        *,
        failure_code: str,
        result_artifact_digest: str | None,
        principal_id: str,
        failed_at: datetime | None = None,
    ) -> VerificationLifecycleState:
        return self._scheduler.record_failure(
            lease,
            failure_code=failure_code,
            result_artifact_digest=result_artifact_digest,
            principal_id=principal_id,
            failed_at=failed_at,
        )


@dataclass(frozen=True, slots=True)
class _VerificationArtifactWriter:
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
class VerificationWorkerProcess:
    """Run deterministic verification against shared immutable evidence."""

    coordinator: VerificationCycleRunner
    scheduler: VerificationReconciliationPort
    config: RuntimeProcessConfig
    stop_event: Event = field(default_factory=Event)
    storage_quota: StorageQuotaPort | None = None

    @classmethod
    def from_config(
        cls,
        config: RuntimeProcessConfig,
        *,
        stop_event: Event | None = None,
    ) -> VerificationWorkerProcess:
        verification_config = _required_verify_config(config)
        database_path = config.security.paths.ensure_database_file()
        events = EventStore(database_path)
        artifacts = ArtifactStore(
            config.security.paths.artifact_root,
            database_path=database_path,
            max_total_bytes=config.quota.artifact_max_total_bytes,
        )
        scheduler = VerificationRuntimeService(events)
        source = VerificationSourceService(
            events,
            RuntimeService(
                events,
                config.repository_root,
                artifacts=artifacts,
            ),
            artifacts,
        )
        coordinator = VerificationWorker(
            source=_VerificationSource(source),
            scheduler=_VerificationWorkerScheduler(scheduler),
            artifacts=_VerificationArtifactWriter(artifacts),
            verifier=DeterministicVerifier(),
            policy=VerificationWorkerPolicy(
                worker_id=verification_config.worker.worker_id,
                lease_seconds=verification_config.worker.lease_seconds,
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
        verification_config = _required_verify_config(self.config)
        try:
            with worker_process_lock(
                self.config.security.paths,
                WorkerProcessRole.VERIFICATION,
            ):
                self.scheduler.reconcile(principal_id=verification_config.worker.supervisor_id)
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
                        self.stop_event.wait(verification_config.worker.poll_milliseconds / 1_000)
                return 0
        except WorkerProcessLockError as error:
            code = (
                VerificationWorkerProcessFailureCode.ALREADY_RUNNING
                if error.code is WorkerProcessLockFailureCode.ALREADY_RUNNING
                else VerificationWorkerProcessFailureCode.LOCK_UNAVAILABLE
            )
            raise VerificationWorkerProcessError(code) from error


def validate_verification_worker_runtime_config(config: RuntimeProcessConfig) -> None:
    """Require explicit deterministic-verifier authority before daemon spawn."""

    _required_verify_config(config)


def _required_verify_config(config: RuntimeProcessConfig) -> VerificationWorkerRuntimeConfig:
    if not isinstance(config, RuntimeProcessConfig):
        raise TypeError("verification worker requires runtime process configuration")
    if config.verification_worker is None:
        raise VerificationWorkerProcessError(VerificationWorkerProcessFailureCode.NOT_CONFIGURED)
    return config.verification_worker


__all__ = [
    "VerificationCycleRunner",
    "VerificationReconciliationPort",
    "VerificationWorkerProcess",
    "VerificationWorkerProcessError",
    "VerificationWorkerProcessFailureCode",
    "validate_verification_worker_runtime_config",
]
