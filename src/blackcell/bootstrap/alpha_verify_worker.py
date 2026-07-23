"""Verifier-only alpha worker joining durable review evidence and host policy."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal, Protocol

from blackcell.bootstrap.alpha_verify_runtime import (
    AlphaClaimedVerification,
    AlphaVerificationRuntimeError,
)
from blackcell.bootstrap.alpha_verify_source import (
    AlphaPreparedVerification,
    AlphaVerificationSourceError,
    AlphaVerificationSourceFailureCode,
)
from blackcell.kernel import ArtifactRef, utc_now
from blackcell.kernel._json import canonical_json_bytes
from blackcell.orchestration.alpha_artifacts import ALPHA_VERIFICATION_REPORT_MEDIA_TYPE
from blackcell.orchestration.alpha_review import AlphaAdmittedReview, AlphaReviewContext
from blackcell.orchestration.alpha_verify import (
    AlphaVerificationError,
    AlphaVerificationFailureCode,
    AlphaVerificationReport,
    AlphaVerificationStatus,
    alpha_verification_report_payload,
    verify_alpha_review,
)
from blackcell.orchestration.alpha_verify_lifecycle import (
    AlphaVerificationCandidate,
    AlphaVerificationLease,
    AlphaVerificationLifecycleState,
    AlphaVerificationLifecycleStatus,
)


class AlphaVerificationSourcePort(Protocol):
    def verification_run_ids(self) -> tuple[str, ...]: ...

    def verification_candidate(self, run_id: str) -> AlphaVerificationCandidate: ...

    def prepare_verification(
        self,
        candidate: AlphaVerificationCandidate,
    ) -> AlphaPreparedVerification: ...


class AlphaVerificationSchedulerPort(Protocol):
    def inspect(self, run_id: str) -> AlphaVerificationLifecycleState | None: ...

    def claim(
        self,
        candidate: AlphaVerificationCandidate,
        *,
        worker_id: str,
        lease_expires_at: datetime,
        claimed_at: datetime | None = None,
    ) -> AlphaClaimedVerification: ...

    def record_completed(
        self,
        lease: AlphaVerificationLease,
        *,
        verdict: AlphaVerificationStatus,
        report_artifact_digest: str,
        matrix_digest: str,
        principal_id: str,
        completed_at: datetime | None = None,
    ) -> AlphaVerificationLifecycleState: ...

    def record_failure(
        self,
        lease: AlphaVerificationLease,
        *,
        failure_code: str,
        result_artifact_digest: str | None,
        principal_id: str,
        failed_at: datetime | None = None,
    ) -> AlphaVerificationLifecycleState: ...


class AlphaVerificationArtifactStorePort(Protocol):
    def put_bytes(
        self,
        data: bytes,
        *,
        media_type: str = "application/octet-stream",
        encoding: str | None = None,
    ) -> ArtifactRef: ...


class AlphaVerifierPort(Protocol):
    def verify(
        self,
        context: AlphaReviewContext,
        admitted_review: AlphaAdmittedReview,
    ) -> AlphaVerificationReport: ...


@dataclass(frozen=True, slots=True)
class DeterministicAlphaVerifier:
    def verify(
        self,
        context: AlphaReviewContext,
        admitted_review: AlphaAdmittedReview,
    ) -> AlphaVerificationReport:
        return verify_alpha_review(context, admitted_review)


@dataclass(frozen=True, slots=True)
class AlphaVerificationWorkerPolicy:
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
            raise ValueError("invalid alpha verification worker policy")


@dataclass(frozen=True, slots=True)
class AlphaVerificationWorkerCycleResult:
    status: Literal[
        "idle",
        "verification-completed",
        "verification-error",
        "claim-conflict",
    ]
    run_id: str | None = None
    verification_id: str | None = None
    verdict: AlphaVerificationStatus | None = None
    report_artifact_digest: str | None = None
    failure_code: str | None = None


@dataclass(frozen=True, slots=True)
class AlphaVerificationWorker:
    source: AlphaVerificationSourcePort
    scheduler: AlphaVerificationSchedulerPort
    artifacts: AlphaVerificationArtifactStorePort
    verifier: AlphaVerifierPort
    policy: AlphaVerificationWorkerPolicy
    clock: Callable[[], datetime] = field(default=utc_now, repr=False)

    def run_once(self) -> AlphaVerificationWorkerCycleResult:
        candidate = self._next_candidate()
        if candidate is None:
            return AlphaVerificationWorkerCycleResult(status="idle")
        claimed_at = self.clock()
        try:
            claimed = self.scheduler.claim(
                candidate,
                worker_id=self.policy.worker_id,
                lease_expires_at=claimed_at + timedelta(seconds=self.policy.lease_seconds),
                claimed_at=claimed_at,
            )
        except AlphaVerificationRuntimeError:
            return AlphaVerificationWorkerCycleResult(
                status="claim-conflict",
                run_id=candidate.run_id,
                verification_id=candidate.verification_id,
            )
        return self._execute(candidate, claimed)

    def _next_candidate(self) -> AlphaVerificationCandidate | None:
        for run_id in self.source.verification_run_ids():
            state = self.scheduler.inspect(run_id)
            if state is None or state.status is AlphaVerificationLifecycleStatus.REQUEUED:
                return self.source.verification_candidate(run_id)
        return None

    def _execute(
        self,
        candidate: AlphaVerificationCandidate,
        claimed: AlphaClaimedVerification,
    ) -> AlphaVerificationWorkerCycleResult:
        lease = claimed.lease
        phase = "source"
        result_artifact_digest: str | None = None
        try:
            prepared = self.source.prepare_verification(candidate)
            if prepared.candidate != candidate or _lease_identity(lease) != _candidate_identity(
                candidate
            ):
                raise AlphaVerificationSourceError(
                    AlphaVerificationSourceFailureCode.SNAPSHOT_MISMATCH
                )
            phase = "verifier"
            report = self.verifier.verify(prepared.context, prepared.admitted_review)
            _require_report_bindings(report, candidate)
            phase = "artifact"
            reference = self.artifacts.put_bytes(
                canonical_json_bytes(alpha_verification_report_payload(report)),
                media_type=ALPHA_VERIFICATION_REPORT_MEDIA_TYPE,
                encoding="utf-8",
            )
            result_artifact_digest = reference.digest
            if reference.digest != report.digest:
                raise ValueError("alpha verification report digest mismatch")
            phase = "persistence"
            state = self.scheduler.record_completed(
                lease,
                verdict=report.status,
                report_artifact_digest=reference.digest,
                matrix_digest=report.matrix_digest,
                principal_id=self.policy.worker_id,
                completed_at=self.clock(),
            )
            return AlphaVerificationWorkerCycleResult(
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
            except AlphaVerificationRuntimeError:
                return AlphaVerificationWorkerCycleResult(
                    status="claim-conflict",
                    run_id=candidate.run_id,
                    verification_id=candidate.verification_id,
                    failure_code=failure_code,
                )
            return AlphaVerificationWorkerCycleResult(
                status="verification-error",
                run_id=candidate.run_id,
                verification_id=candidate.verification_id,
                failure_code=failure_code,
            )


def _require_report_bindings(
    report: AlphaVerificationReport,
    candidate: AlphaVerificationCandidate,
) -> None:
    if (
        not isinstance(report, AlphaVerificationReport)
        or report.run_id != candidate.run_id
        or report.context_digest != candidate.context_digest
        or report.acceptance_digest != candidate.acceptance_digest
        or report.state_digest != candidate.state_digest
        or report.artifact_evidence_digest != candidate.artifact_evidence_digest
        or report.admitted_review_digest != candidate.admitted_review_digest
    ):
        raise AlphaVerificationError(AlphaVerificationFailureCode.BINDING_MISMATCH)


def _verification_failure_code(error: Exception, *, phase: str) -> str:
    if isinstance(error, AlphaVerificationSourceError):
        return error.code.value
    if isinstance(error, AlphaVerificationError):
        return error.code.value
    if phase == "verifier":
        return "alpha-verifier-failed"
    if phase == "artifact":
        return "alpha-verification-report-artifact-failed"
    if phase == "persistence":
        return "alpha-verification-persistence-failed"
    return "alpha-verification-worker-failed"


def _candidate_identity(value: AlphaVerificationCandidate) -> tuple[object, ...]:
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


def _lease_identity(value: AlphaVerificationLease) -> tuple[object, ...]:
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
    "AlphaVerificationArtifactStorePort",
    "AlphaVerificationSchedulerPort",
    "AlphaVerificationSourcePort",
    "AlphaVerificationWorker",
    "AlphaVerificationWorkerCycleResult",
    "AlphaVerificationWorkerPolicy",
    "AlphaVerifierPort",
    "DeterministicAlphaVerifier",
]
