"""Live-free, artifact-validating source for deterministic execution verification."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from blackcell.bootstrap.runtime_service import RuntimeService
from blackcell.interfaces.http.ports import RuntimeApiError
from blackcell.kernel import ArtifactStore, EventStore, KernelError
from blackcell.kernel._json import canonical_json_bytes
from blackcell.orchestration.execution_artifacts import (
    ADMITTED_REVIEW_MEDIA_TYPE,
    REVIEW_CONTEXT_MEDIA_TYPE,
    REVIEW_PROPOSAL_MEDIA_TYPE,
    REVIEW_PROVIDER_MEDIA_TYPE,
)
from blackcell.orchestration.replay import ReviewEvidenceError
from blackcell.orchestration.review import (
    AdmittedReview,
    ReviewContext,
    ReviewContractError,
    ReviewProposal,
    admit_review,
    admitted_review_from_mapping,
    admitted_review_payload,
    review_context_payload,
    review_proposal_from_mapping,
    review_proposal_payload,
    review_provider_result_from_mapping,
    review_provider_result_payload,
)
from blackcell.orchestration.review_lifecycle import (
    REVIEW_SUCCEEDED,
    ReviewCandidate,
    ReviewLifecycleError,
    ReviewLifecycleStatus,
    fold_review_lifecycle,
    review_stream,
)
from blackcell.orchestration.verification_lifecycle import (
    VerificationCandidate,
    verification_id,
)


class VerificationSourceFailureCode(StrEnum):
    NOT_FOUND = "verification-source-not-found"
    SNAPSHOT_MISMATCH = "verification-source-snapshot-mismatch"
    EXECUTION_EVIDENCE_INVALID = "verification-execution-evidence-invalid"
    REVIEW_ARTIFACT_INVALID = "verification-review-artifact-invalid"


class VerificationSourceError(RuntimeError):
    """Content-free failure while reconstructing verifier input."""

    def __init__(self, code: VerificationSourceFailureCode) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True, slots=True)
class PreparedVerification:
    candidate: VerificationCandidate
    context: ReviewContext
    admitted_review: AdmittedReview


@dataclass(frozen=True, slots=True)
class VerificationSourceService:
    events: EventStore
    execution: RuntimeService
    artifacts: ArtifactStore

    def __post_init__(self) -> None:
        if self.events.path.resolve() != self.artifacts.database_path.resolve():
            raise ValueError("execution verification source stores do not match")

    def verification_run_ids(self) -> tuple[str, ...]:
        """Return successful-review run IDs in durable global event order."""

        run_ids: list[str] = []
        seen: set[str] = set()
        cursor = 0
        while True:
            events = self.events.read_all(after_position=cursor, limit=200)
            if not events:
                break
            for event in events:
                if event.event_type == REVIEW_SUCCEEDED and event.stream_id.startswith("review:"):
                    run_id = event.stream_id.removeprefix("review:")
                    if run_id not in seen:
                        seen.add(run_id)
                        run_ids.append(run_id)
            position = events[-1].global_position
            if position is None:
                raise VerificationSourceError(VerificationSourceFailureCode.SNAPSHOT_MISMATCH)
            cursor = position
        return tuple(run_ids)

    def verification_candidate(self, run_id: str) -> VerificationCandidate:
        try:
            review_events = self.events.read_stream(review_stream(run_id))
        except (RuntimeError, ValueError) as error:
            raise VerificationSourceError(VerificationSourceFailureCode.NOT_FOUND) from error
        if not review_events:
            raise VerificationSourceError(VerificationSourceFailureCode.NOT_FOUND)
        try:
            state = fold_review_lifecycle(run_id, review_events)
        except ReviewLifecycleError as error:
            raise VerificationSourceError(
                VerificationSourceFailureCode.SNAPSHOT_MISMATCH
            ) from error
        if state.status is not ReviewLifecycleStatus.SUCCEEDED:
            raise VerificationSourceError(VerificationSourceFailureCode.SNAPSHOT_MISMATCH)
        terminal = state.latest_event
        lease = state.lease
        required = (
            state.acceptance_digest,
            state.context_digest,
            state.proposal_artifact_digest,
            state.provider_result_artifact_digest,
            state.admitted_artifact_digest,
            state.finding_count,
        )
        if any(value is None for value in required):
            raise VerificationSourceError(VerificationSourceFailureCode.SNAPSHOT_MISMATCH)
        return VerificationCandidate(
            run_id=run_id,
            verification_id=verification_id(run_id, terminal.payload_hash),
            correlation_id=terminal.correlation_id,
            run_event_id=lease.run_event_id,
            run_event_digest=lease.run_event_digest,
            state_digest=lease.state_digest,
            artifact_evidence_digest=lease.artifact_evidence_digest,
            review_id=state.review_id,
            review_event_id=terminal.event_id,
            review_event_digest=terminal.payload_hash,
            acceptance_digest=_required_text(state.acceptance_digest),
            context_digest=_required_text(state.context_digest),
            proposal_artifact_digest=_required_text(state.proposal_artifact_digest),
            provider_result_artifact_digest=_required_text(state.provider_result_artifact_digest),
            admitted_review_digest=_required_text(state.admitted_artifact_digest),
            finding_count=_required_integer(state.finding_count),
        )

    def prepare_verification(
        self,
        candidate: VerificationCandidate,
    ) -> PreparedVerification:
        if not isinstance(candidate, VerificationCandidate):
            raise VerificationSourceError(VerificationSourceFailureCode.SNAPSHOT_MISMATCH)
        if self.verification_candidate(candidate.run_id) != candidate:
            raise VerificationSourceError(VerificationSourceFailureCode.SNAPSHOT_MISMATCH)
        try:
            context = self.execution.prepare_review_context(
                ReviewCandidate(
                    run_id=candidate.run_id,
                    review_id=candidate.review_id,
                    correlation_id=candidate.correlation_id,
                    run_event_id=candidate.run_event_id,
                    run_event_digest=candidate.run_event_digest,
                    state_digest=candidate.state_digest,
                    artifact_evidence_digest=candidate.artifact_evidence_digest,
                )
            )
        except (RuntimeApiError, ReviewEvidenceError, ReviewContractError) as error:
            raise VerificationSourceError(
                VerificationSourceFailureCode.EXECUTION_EVIDENCE_INVALID
            ) from error
        if (
            context.digest != candidate.context_digest
            or context.acceptance.digest != candidate.acceptance_digest
        ):
            raise VerificationSourceError(VerificationSourceFailureCode.SNAPSHOT_MISMATCH)
        try:
            self._require_exact_artifact(
                candidate.context_digest,
                REVIEW_CONTEXT_MEDIA_TYPE,
                review_context_payload(context),
            )
            proposal_raw = self._load_canonical_json(
                candidate.proposal_artifact_digest,
                REVIEW_PROPOSAL_MEDIA_TYPE,
            )
            proposal = review_proposal_from_mapping(proposal_raw)
            self._require_serialized_proposal(candidate, proposal)
            provider_raw = self._load_canonical_json(
                candidate.provider_result_artifact_digest,
                REVIEW_PROVIDER_MEDIA_TYPE,
            )
            provider = review_provider_result_from_mapping(
                provider_raw,
                proposal=proposal,
            )
            self._require_exact_artifact(
                candidate.provider_result_artifact_digest,
                REVIEW_PROVIDER_MEDIA_TYPE,
                review_provider_result_payload(provider),
            )
            admitted_raw = self._load_canonical_json(
                candidate.admitted_review_digest,
                ADMITTED_REVIEW_MEDIA_TYPE,
            )
            admitted = admitted_review_from_mapping(admitted_raw)
            expected_admitted = admit_review(context, proposal)
            if admitted != expected_admitted or len(admitted.findings) != candidate.finding_count:
                raise ValueError
            self._require_exact_artifact(
                candidate.admitted_review_digest,
                ADMITTED_REVIEW_MEDIA_TYPE,
                admitted_review_payload(admitted),
            )
        except (
            ReviewContractError,
            KernelError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as error:
            raise VerificationSourceError(
                VerificationSourceFailureCode.REVIEW_ARTIFACT_INVALID
            ) from error
        return PreparedVerification(candidate, context, admitted)

    def _require_serialized_proposal(
        self,
        candidate: VerificationCandidate,
        proposal: ReviewProposal,
    ) -> None:
        if proposal.context_digest != candidate.context_digest:
            raise ValueError
        self._require_exact_artifact(
            candidate.proposal_artifact_digest,
            REVIEW_PROPOSAL_MEDIA_TYPE,
            review_proposal_payload(proposal),
        )

    def _load_canonical_json(self, digest: str, media_type: str) -> dict[str, object]:
        reference = self.artifacts.stat(digest)
        if reference.media_type != media_type or reference.encoding != "utf-8":
            raise ValueError
        data = self.artifacts.get_bytes(reference)
        value = json.loads(data.decode("utf-8"))
        if not isinstance(value, dict) or canonical_json_bytes(value) != data:
            raise ValueError
        return value

    def _require_exact_artifact(
        self,
        digest: str,
        media_type: str,
        payload: Mapping[str, object],
    ) -> None:
        raw = self._load_canonical_json(digest, media_type)
        if raw != payload:
            raise ValueError


def _required_text(value: str | None) -> str:
    if value is None:
        raise VerificationSourceError(VerificationSourceFailureCode.SNAPSHOT_MISMATCH)
    return value


def _required_integer(value: int | None) -> int:
    if value is None:
        raise VerificationSourceError(VerificationSourceFailureCode.SNAPSHOT_MISMATCH)
    return value


__all__ = [
    "PreparedVerification",
    "VerificationSourceError",
    "VerificationSourceFailureCode",
    "VerificationSourceService",
]
