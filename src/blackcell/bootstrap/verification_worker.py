"""Verification worker joining durable review evidence and host policy."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal, Protocol

from blackcell.bootstrap.verification_runtime import (
    ClaimedVerification,
    VerificationRuntimeError,
)
from blackcell.bootstrap.verification_source import (
    PreparedVerification,
    VerificationSourceError,
    VerificationSourceFailureCode,
)
from blackcell.kernel import ArtifactRef, utc_now
from blackcell.kernel._json import canonical_json_bytes
from blackcell.orchestration.execution_artifacts import VERIFICATION_REPORT_MEDIA_TYPE
from blackcell.orchestration.review import AdmittedReview, ReviewContext
from blackcell.orchestration.verification import (
    VerificationError,
    VerificationFailureCode,
    VerificationReport,
    VerificationStatus,
    verification_report_payload,
    verify_review,
)
from blackcell.orchestration.verification_lifecycle import (
    VerificationCandidate,
    VerificationLease,
    VerificationLifecycleState,
    VerificationLifecycleStatus,
)


class VerificationSourcePort(Protocol):
    def verification_run_ids(self) -> tuple[str, ...]: ...

    def verification_candidate(self, run_id: str) -> VerificationCandidate: ...

    def prepare_verification(
        self,
        candidate: VerificationCandidate,
    ) -> PreparedVerification: ...


class VerificationSchedulerPort(Protocol):
    def inspect(self, run_id: str) -> VerificationLifecycleState | None: ...

    def claim(
        self,
        candidate: VerificationCandidate,
        *,
        worker_id: str,
        lease_expires_at: datetime,
        claimed_at: datetime | None = None,
    ) -> ClaimedVerification: ...

    def record_completed(
        self,
        lease: VerificationLease,
        *,
        verdict: VerificationStatus,
        report_artifact_digest: str,
        matrix_digest: str,
        principal_id: str,
        completed_at: datetime | None = None,
    ) -> VerificationLifecycleState: ...

    def record_failure(
        self,
        lease: VerificationLease,
        *,
        failure_code: str,
        result_artifact_digest: str | None,
        principal_id: str,
        failed_at: datetime | None = None,
    ) -> VerificationLifecycleState: ...


class VerificationArtifactStorePort(Protocol):
    def put_bytes(
        self,
        data: bytes,
        *,
        media_type: str = "application/octet-stream",
        encoding: str | None = None,
    ) -> ArtifactRef: ...


class VerifierPort(Protocol):
    def verify(
        self,
        context: ReviewContext,
        admitted_review: AdmittedReview,
    ) -> VerificationReport: ...


@dataclass(frozen=True, slots=True)
class DeterministicVerifier:
    def verify(
        self,
        context: ReviewContext,
        admitted_review: AdmittedReview,
    ) -> VerificationReport:
        return verify_review(context, admitted_review)


@dataclass(frozen=True, slots=True)
class VerificationWorkerPolicy:
    worker_id: str
    lease_seconds: int = 300

    def __post_init__(self) -> None:
        if (
            not isinstance(self.worker_id, str)
            or not self.worker_id
            or len(self.worker_id) > 120
            or any(not 0x21 <= ord(character) <= 0x7E for character in self.worker_id)
            or isinstance(self.lease_seconds, bool)
            or not isinstance(self.lease_seconds, int)
            or not 1 <= self.lease_seconds <= 86_400
        ):
            raise ValueError("invalid verification worker policy")


@dataclass(frozen=True, slots=True)
class VerificationWorkerCycleResult:
    status: Literal[
        "idle",
        "verification-completed",
        "verification-error",
        "claim-conflict",
    ]
    run_id: str | None = None
    verification_id: str | None = None
    verdict: VerificationStatus | None = None
    report_artifact_digest: str | None = None
    failure_code: str | None = None


@dataclass(frozen=True, slots=True)
class VerificationWorker:
    source: VerificationSourcePort
    scheduler: VerificationSchedulerPort
    artifacts: VerificationArtifactStorePort
    verifier: VerifierPort
    policy: VerificationWorkerPolicy
    clock: Callable[[], datetime] = field(default=utc_now, repr=False)

    def run_once(self) -> VerificationWorkerCycleResult:
        candidate = self._next_candidate()
        if candidate is None:
            return VerificationWorkerCycleResult(status="idle")
        claimed_at = self.clock()
        try:
            claimed = self.scheduler.claim(
                candidate,
                worker_id=self.policy.worker_id,
                lease_expires_at=claimed_at + timedelta(seconds=self.policy.lease_seconds),
                claimed_at=claimed_at,
            )
        except VerificationRuntimeError:
            return VerificationWorkerCycleResult(
                status="claim-conflict",
                run_id=candidate.run_id,
                verification_id=candidate.verification_id,
            )
        return self._execute(candidate, claimed)

    def _next_candidate(self) -> VerificationCandidate | None:
        for run_id in self.source.verification_run_ids():
            state = self.scheduler.inspect(run_id)
            if state is None or state.status is VerificationLifecycleStatus.REQUEUED:
                return self.source.verification_candidate(run_id)
        return None

    def _execute(
        self,
        candidate: VerificationCandidate,
        claimed: ClaimedVerification,
    ) -> VerificationWorkerCycleResult:
        lease = claimed.lease
        phase = "source"
        result_artifact_digest: str | None = None
        try:
            prepared = self.source.prepare_verification(candidate)
            if prepared.candidate != candidate or _lease_identity(lease) != _candidate_identity(
                candidate
            ):
                raise VerificationSourceError(VerificationSourceFailureCode.SNAPSHOT_MISMATCH)
            phase = "verifier"
            report = self.verifier.verify(prepared.context, prepared.admitted_review)
            _require_report_bindings(report, candidate)
            phase = "artifact"
            reference = self.artifacts.put_bytes(
                canonical_json_bytes(verification_report_payload(report)),
                media_type=VERIFICATION_REPORT_MEDIA_TYPE,
                encoding="utf-8",
            )
            result_artifact_digest = reference.digest
            if reference.digest != report.digest:
                raise ValueError("execution verification report digest mismatch")
            phase = "persistence"
            state = self.scheduler.record_completed(
                lease,
                verdict=report.status,
                report_artifact_digest=reference.digest,
                matrix_digest=report.matrix_digest,
                principal_id=self.policy.worker_id,
                completed_at=self.clock(),
            )
            return VerificationWorkerCycleResult(
                status="verification-completed",
                run_id=candidate.run_id,
                verification_id=candidate.verification_id,
                verdict=state.verdict,
                report_artifact_digest=reference.digest,
            )
        except Exception as error:
            failure_code = _verification_failure_code(error, phase=phase)
            try:
                self.scheduler.record_failure(
                    lease,
                    failure_code=failure_code,
                    result_artifact_digest=result_artifact_digest,
                    principal_id=self.policy.worker_id,
                    failed_at=self.clock(),
                )
            except VerificationRuntimeError:
                return VerificationWorkerCycleResult(
                    status="claim-conflict",
                    run_id=candidate.run_id,
                    verification_id=candidate.verification_id,
                    failure_code=failure_code,
                )
            return VerificationWorkerCycleResult(
                status="verification-error",
                run_id=candidate.run_id,
                verification_id=candidate.verification_id,
                failure_code=failure_code,
            )


def _require_report_bindings(
    report: VerificationReport,
    candidate: VerificationCandidate,
) -> None:
    if (
        not isinstance(report, VerificationReport)
        or report.run_id != candidate.run_id
        or report.context_digest != candidate.context_digest
        or report.acceptance_digest != candidate.acceptance_digest
        or report.state_digest != candidate.state_digest
        or report.artifact_evidence_digest != candidate.artifact_evidence_digest
        or report.admitted_review_digest != candidate.admitted_review_digest
    ):
        raise VerificationError(VerificationFailureCode.BINDING_MISMATCH)


def _verification_failure_code(error: Exception, *, phase: str) -> str:
    if isinstance(error, VerificationSourceError):
        return error.code.value
    if isinstance(error, VerificationError):
        return error.code.value
    if phase == "verifier":
        return "verifier-failed"
    if phase == "artifact":
        return "verification-report-artifact-failed"
    if phase == "persistence":
        return "verification-persistence-failed"
    return "verification-worker-failed"


def _candidate_identity(value: VerificationCandidate) -> tuple[object, ...]:
    return (
        value.run_id,
        value.verification_id,
        value.run_event_id,
        value.run_event_digest,
        value.state_digest,
        value.artifact_evidence_digest,
        value.review_id,
        value.review_event_id,
        value.review_event_digest,
        value.acceptance_digest,
        value.context_digest,
        value.proposal_artifact_digest,
        value.provider_result_artifact_digest,
        value.admitted_review_digest,
        value.finding_count,
    )


def _lease_identity(value: VerificationLease) -> tuple[object, ...]:
    return (
        value.run_id,
        value.verification_id,
        value.run_event_id,
        value.run_event_digest,
        value.state_digest,
        value.artifact_evidence_digest,
        value.review_id,
        value.review_event_id,
        value.review_event_digest,
        value.acceptance_digest,
        value.context_digest,
        value.proposal_artifact_digest,
        value.provider_result_artifact_digest,
        value.admitted_review_digest,
        value.finding_count,
    )


__all__ = [
    "DeterministicVerifier",
    "VerificationArtifactStorePort",
    "VerificationSchedulerPort",
    "VerificationSourcePort",
    "VerificationWorker",
    "VerificationWorkerCycleResult",
    "VerificationWorkerPolicy",
    "VerifierPort",
]
