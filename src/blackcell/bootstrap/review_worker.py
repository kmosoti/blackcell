"""Review worker joining durable execution evidence, REVIEW, and admission."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal, Protocol

from blackcell.bootstrap.review_runtime import (
    ClaimedReview,
    ReviewRuntimeError,
)
from blackcell.gateway import DataClassification, GatewayBudget, LocalityPolicy
from blackcell.interfaces.http.ports import RuntimeApiError, RuntimeApiFailureCode
from blackcell.kernel import ArtifactRef, JsonInput, utc_now
from blackcell.kernel._json import canonical_json_bytes
from blackcell.orchestration.execution_artifacts import (
    ADMITTED_REVIEW_MEDIA_TYPE,
    REVIEW_CONTEXT_MEDIA_TYPE,
    REVIEW_PROPOSAL_MEDIA_TYPE,
    REVIEW_PROVIDER_MEDIA_TYPE,
)
from blackcell.orchestration.replay import ReviewEvidenceError
from blackcell.orchestration.review import (
    ReviewContext,
    ReviewContractError,
    ReviewProviderCall,
    ReviewProviderResult,
    admit_review,
    admitted_review_payload,
    review_context_payload,
    review_proposal_payload,
    review_provider_result_payload,
)
from blackcell.orchestration.review_lifecycle import (
    ReviewCandidate,
    ReviewLease,
    ReviewLifecycleState,
    ReviewLifecycleStatus,
    review_provider_request_id,
)


class ReviewExecutionSourcePort(Protocol):
    def review_run_ids(self) -> tuple[str, ...]: ...

    def review_candidate(self, run_id: str) -> ReviewCandidate: ...

    def prepare_review_context(self, candidate: ReviewCandidate) -> ReviewContext: ...


class ReviewSchedulerPort(Protocol):
    def inspect(self, run_id: str) -> ReviewLifecycleState | None: ...

    def claim(
        self,
        candidate: ReviewCandidate,
        *,
        worker_id: str,
        lease_expires_at: datetime,
        claimed_at: datetime | None = None,
    ) -> ClaimedReview: ...

    def renew_lease(
        self,
        lease: ReviewLease,
        *,
        lease_expires_at: datetime,
        principal_id: str,
        renewed_at: datetime | None = None,
    ) -> ReviewLease: ...

    def record_provider_dispatch(
        self,
        lease: ReviewLease,
        *,
        acceptance_digest: str,
        context_digest: str,
        context_artifact_digest: str,
        principal_id: str,
        dispatched_at: datetime | None = None,
    ) -> str: ...

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
    ) -> ReviewLifecycleState: ...

    def record_failure(
        self,
        lease: ReviewLease,
        *,
        failure_code: str,
        result_artifact_digest: str | None,
        principal_id: str,
        failed_at: datetime | None = None,
    ) -> ReviewLifecycleState: ...


class ReviewArtifactStorePort(Protocol):
    def put_bytes(
        self,
        data: bytes,
        *,
        media_type: str = "application/octet-stream",
        encoding: str | None = None,
    ) -> ArtifactRef: ...


class ReviewerPort(Protocol):
    def review(self, call: ReviewProviderCall) -> ReviewProviderResult: ...


@dataclass(frozen=True, slots=True)
class ReviewWorkerPolicy:
    worker_id: str
    budget: GatewayBudget
    classification: DataClassification = DataClassification.PRIVATE
    locality: LocalityPolicy = LocalityPolicy.LOCAL_ONLY
    lease_seconds: int = 300

    def __post_init__(self) -> None:
        if (
            not isinstance(self.worker_id, str)
            or not self.worker_id
            or len(self.worker_id) > 120
            or any(not 0x21 <= ord(character) <= 0x7E for character in self.worker_id)
            or not isinstance(self.budget, GatewayBudget)
            or not isinstance(self.classification, DataClassification)
            or not isinstance(self.locality, LocalityPolicy)
            or isinstance(self.lease_seconds, bool)
            or not isinstance(self.lease_seconds, int)
            or not 1 <= self.lease_seconds <= 86_400
            or self.lease_seconds * 1_000 <= self.budget.max_latency_ms
        ):
            raise ValueError("invalid review worker policy")


@dataclass(frozen=True, slots=True)
class ReviewWorkerCycleResult:
    status: Literal[
        "idle",
        "review-succeeded",
        "review-failed",
        "claim-conflict",
    ]
    run_id: str | None = None
    review_id: str | None = None
    finding_count: int | None = None
    admitted_artifact_digest: str | None = None
    failure_code: str | None = None


@dataclass(frozen=True, slots=True)
class ReviewWorker:
    execution: ReviewExecutionSourcePort
    scheduler: ReviewSchedulerPort
    artifacts: ReviewArtifactStorePort
    reviewer: ReviewerPort
    policy: ReviewWorkerPolicy
    clock: Callable[[], datetime] = field(default=utc_now, repr=False)

    def run_once(self) -> ReviewWorkerCycleResult:
        candidate = self._next_candidate()
        if candidate is None:
            return ReviewWorkerCycleResult(status="idle")
        claimed_at = self.clock()
        try:
            claimed = self.scheduler.claim(
                candidate,
                worker_id=self.policy.worker_id,
                lease_expires_at=claimed_at + timedelta(seconds=self.policy.lease_seconds),
                claimed_at=claimed_at,
            )
        except ReviewRuntimeError:
            return ReviewWorkerCycleResult(
                status="claim-conflict",
                run_id=candidate.run_id,
                review_id=candidate.review_id,
            )
        return self._execute(candidate, claimed)

    def _next_candidate(self) -> ReviewCandidate | None:
        for run_id in self.execution.review_run_ids():
            state = self.scheduler.inspect(run_id)
            if state is None or state.status is ReviewLifecycleStatus.REQUEUED:
                return self.execution.review_candidate(run_id)
        return None

    def _execute(
        self,
        candidate: ReviewCandidate,
        claimed: ClaimedReview,
    ) -> ReviewWorkerCycleResult:
        lease = claimed.lease
        phase = "preparation"
        last_artifact_digest: str | None = None
        try:
            context = self.execution.prepare_review_context(candidate)
            if (
                context.acceptance.run_id != lease.run_id
                or context.state_digest != lease.state_digest
                or context.artifact_evidence_digest != lease.artifact_evidence_digest
            ):
                raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
            phase = "artifact"
            context_ref = self._store_json(
                review_context_payload(context),
                media_type=REVIEW_CONTEXT_MEDIA_TYPE,
                expected_digest=context.digest,
            )
            last_artifact_digest = context_ref.digest
            phase = "provider"
            dispatched_at = self.clock()
            renewed_expires_at = dispatched_at + timedelta(seconds=self.policy.lease_seconds)
            if renewed_expires_at > lease.expires_at:
                lease = self.scheduler.renew_lease(
                    lease,
                    lease_expires_at=renewed_expires_at,
                    principal_id=self.policy.worker_id,
                    renewed_at=dispatched_at,
                )
            provider_budget = self._provider_budget(lease, dispatched_at)
            dispatch_event_id = self.scheduler.record_provider_dispatch(
                lease,
                acceptance_digest=context.acceptance.digest,
                context_digest=context.digest,
                context_artifact_digest=context_ref.digest,
                principal_id=self.policy.worker_id,
                dispatched_at=dispatched_at,
            )
            provider_result = self.reviewer.review(
                ReviewProviderCall(
                    request_id=review_provider_request_id(lease.digest),
                    correlation_id=candidate.correlation_id,
                    review_id=candidate.review_id,
                    context=context,
                    classification=self.policy.classification,
                    locality=self.policy.locality,
                    budget=provider_budget,
                    estimated_input_tokens=(
                        len(canonical_json_bytes(review_context_payload(context))) + 3
                    )
                    // 4,
                    causation_id=dispatch_event_id,
                )
            )
            phase = "artifact"
            proposal_ref = self._store_json(
                review_proposal_payload(provider_result.proposal),
                media_type=REVIEW_PROPOSAL_MEDIA_TYPE,
                expected_digest=provider_result.proposal.digest,
            )
            last_artifact_digest = proposal_ref.digest
            provider_ref = self._store_json(
                review_provider_result_payload(provider_result),
                media_type=REVIEW_PROVIDER_MEDIA_TYPE,
            )
            last_artifact_digest = provider_ref.digest
            phase = "admission"
            admitted = admit_review(context, provider_result.proposal)
            phase = "artifact"
            admitted_ref = self._store_json(
                admitted_review_payload(admitted),
                media_type=ADMITTED_REVIEW_MEDIA_TYPE,
                expected_digest=admitted.digest,
            )
            last_artifact_digest = admitted_ref.digest
            phase = "persistence"
            state = self.scheduler.record_success(
                lease,
                context_digest=context.digest,
                proposal_artifact_digest=proposal_ref.digest,
                provider_result_artifact_digest=provider_ref.digest,
                admitted_artifact_digest=admitted_ref.digest,
                finding_count=len(admitted.findings),
                principal_id=self.policy.worker_id,
                completed_at=self.clock(),
            )
            return ReviewWorkerCycleResult(
                status="review-succeeded",
                run_id=candidate.run_id,
                review_id=candidate.review_id,
                finding_count=state.finding_count,
                admitted_artifact_digest=admitted_ref.digest,
            )
        except Exception as error:
            failure_code = _review_failure_code(error, phase=phase)
            try:
                self.scheduler.record_failure(
                    lease,
                    failure_code=failure_code,
                    result_artifact_digest=last_artifact_digest,
                    principal_id=self.policy.worker_id,
                    failed_at=self.clock(),
                )
            except ReviewRuntimeError:
                return ReviewWorkerCycleResult(
                    status="claim-conflict",
                    run_id=candidate.run_id,
                    review_id=candidate.review_id,
                    failure_code=failure_code,
                )
            return ReviewWorkerCycleResult(
                status="review-failed",
                run_id=candidate.run_id,
                review_id=candidate.review_id,
                failure_code=failure_code,
            )

    def _provider_budget(self, lease: ReviewLease, dispatched_at: datetime) -> GatewayBudget:
        configured = self.policy.budget
        completion_reserve_ms = self.policy.lease_seconds * 1_000 - configured.max_latency_ms
        remaining_lease_ms = int((lease.expires_at - dispatched_at).total_seconds() * 1_000)
        provider_latency_ms = min(
            configured.max_latency_ms,
            remaining_lease_ms - completion_reserve_ms,
        )
        if provider_latency_ms < 1:
            raise ValueError("execution review lease has no safe provider window")
        return GatewayBudget(
            configured.max_input_tokens,
            configured.max_output_tokens,
            provider_latency_ms,
            configured.max_cost_microusd,
        )

    def _store_json(
        self,
        payload: dict[str, JsonInput],
        *,
        media_type: str,
        expected_digest: str | None = None,
    ) -> ArtifactRef:
        reference = self.artifacts.put_bytes(
            canonical_json_bytes(payload),
            media_type=media_type,
            encoding="utf-8",
        )
        if expected_digest is not None and reference.digest != expected_digest:
            raise ValueError("execution review artifact digest mismatch")
        return reference


def _review_failure_code(error: Exception, *, phase: str) -> str:
    if isinstance(error, ReviewEvidenceError):
        return error.code.value
    if isinstance(error, RuntimeApiError):
        return "review-preparation-failed"
    if isinstance(error, ReviewContractError):
        return "review-admission-rejected"
    if phase == "provider":
        return "review-provider-failed"
    if phase == "artifact":
        return "review-artifact-failed"
    if phase == "persistence":
        return "review-persistence-failed"
    return "review-worker-failed"


__all__ = [
    "ReviewArtifactStorePort",
    "ReviewExecutionSourcePort",
    "ReviewSchedulerPort",
    "ReviewWorker",
    "ReviewWorkerCycleResult",
    "ReviewWorkerPolicy",
    "ReviewerPort",
]
