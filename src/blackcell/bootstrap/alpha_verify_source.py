"""Live-free, artifact-validating source for deterministic alpha verification."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from blackcell.bootstrap.alpha_runtime import AlphaRuntimeApiService
from blackcell.interfaces.http.ports import RuntimeApiError
from blackcell.kernel import ArtifactStore, EventStore, KernelError
from blackcell.kernel._json import canonical_json_bytes
from blackcell.orchestration.alpha_artifacts import (
    ALPHA_ADMITTED_REVIEW_MEDIA_TYPE,
    ALPHA_REVIEW_CONTEXT_MEDIA_TYPE,
    ALPHA_REVIEW_PROPOSAL_MEDIA_TYPE,
    ALPHA_REVIEW_PROVIDER_MEDIA_TYPE,
)
from blackcell.orchestration.alpha_replay import AlphaReviewEvidenceError
from blackcell.orchestration.alpha_review import (
    AlphaAdmittedReview,
    AlphaReviewContext,
    AlphaReviewContractError,
    AlphaReviewProposal,
    admit_alpha_review,
    alpha_admitted_review_from_mapping,
    alpha_admitted_review_payload,
    alpha_review_context_payload,
    alpha_review_proposal_from_mapping,
    alpha_review_proposal_payload,
    alpha_review_provider_result_from_mapping,
    alpha_review_provider_result_payload,
)
from blackcell.orchestration.alpha_review_lifecycle import (
    ALPHA_REVIEW_SUCCEEDED,
    AlphaReviewCandidate,
    AlphaReviewLifecycleError,
    AlphaReviewLifecycleStatus,
    alpha_review_stream,
    fold_alpha_review_lifecycle,
)
from blackcell.orchestration.alpha_verify_lifecycle import (
    AlphaVerificationCandidate,
    alpha_verification_id,
)


class AlphaVerificationSourceFailureCode(StrEnum):
    NOT_FOUND = "alpha-verification-source-not-found"
    SNAPSHOT_MISMATCH = "alpha-verification-source-snapshot-mismatch"
    EXECUTION_EVIDENCE_INVALID = "alpha-verification-execution-evidence-invalid"
    REVIEW_ARTIFACT_INVALID = "alpha-verification-review-artifact-invalid"


class AlphaVerificationSourceError(RuntimeError):
    """Content-free failure while reconstructing verifier input."""

    def __init__(self, code: AlphaVerificationSourceFailureCode) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True, slots=True)
class AlphaPreparedVerification:
    candidate: AlphaVerificationCandidate
    context: AlphaReviewContext
    admitted_review: AlphaAdmittedReview


@dataclass(frozen=True, slots=True)
class AlphaVerificationSourceService:
    events: EventStore
    execution: AlphaRuntimeApiService
    artifacts: ArtifactStore

    def __post_init__(self) -> None:
        if self.events.path.resolve() != self.artifacts.database_path.resolve():
            raise ValueError("alpha verification source stores do not match")

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
                if event.event_type == ALPHA_REVIEW_SUCCEEDED and event.stream_id.startswith(
                    "alpha:review:"
                ):
                    run_id = event.stream_id.removeprefix("alpha:review:")
                    if run_id not in seen:
                        seen.add(run_id)
                        run_ids.append(run_id)
            position = events[-1].global_position
            if position is None:
                raise AlphaVerificationSourceError(
                    AlphaVerificationSourceFailureCode.SNAPSHOT_MISMATCH
                )
            cursor = position
        return tuple(run_ids)

    def verification_candidate(self, run_id: str) -> AlphaVerificationCandidate:
        try:
            review_events = self.events.read_stream(alpha_review_stream(run_id))
        except (RuntimeError, ValueError) as error:
            raise AlphaVerificationSourceError(
                AlphaVerificationSourceFailureCode.NOT_FOUND
            ) from error
        if not review_events:
            raise AlphaVerificationSourceError(AlphaVerificationSourceFailureCode.NOT_FOUND)
        try:
            state = fold_alpha_review_lifecycle(run_id, review_events)
        except AlphaReviewLifecycleError as error:
            raise AlphaVerificationSourceError(
                AlphaVerificationSourceFailureCode.SNAPSHOT_MISMATCH
            ) from error
        if state.status is not AlphaReviewLifecycleStatus.SUCCEEDED:
            raise AlphaVerificationSourceError(AlphaVerificationSourceFailureCode.SNAPSHOT_MISMATCH)
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
            raise AlphaVerificationSourceError(AlphaVerificationSourceFailureCode.SNAPSHOT_MISMATCH)
        return AlphaVerificationCandidate(
            run_id=run_id,
            verification_id=alpha_verification_id(run_id, terminal.payload_hash),
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
        candidate: AlphaVerificationCandidate,
    ) -> AlphaPreparedVerification:
        if not isinstance(candidate, AlphaVerificationCandidate):
            raise AlphaVerificationSourceError(AlphaVerificationSourceFailureCode.SNAPSHOT_MISMATCH)
        if self.verification_candidate(candidate.run_id) != candidate:
            raise AlphaVerificationSourceError(AlphaVerificationSourceFailureCode.SNAPSHOT_MISMATCH)
        try:
            context = self.execution.prepare_review_context(
                AlphaReviewCandidate(
                    run_id=candidate.run_id,
                    review_id=candidate.review_id,
                    correlation_id=candidate.correlation_id,
                    run_event_id=candidate.run_event_id,
                    run_event_digest=candidate.run_event_digest,
                    state_digest=candidate.state_digest,
                    artifact_evidence_digest=candidate.artifact_evidence_digest,
                )
            )
        except (RuntimeApiError, AlphaReviewEvidenceError, AlphaReviewContractError) as error:
            raise AlphaVerificationSourceError(
                AlphaVerificationSourceFailureCode.EXECUTION_EVIDENCE_INVALID
            ) from error
        if (
            context.digest != candidate.context_digest
            or context.acceptance.digest != candidate.acceptance_digest
        ):
            raise AlphaVerificationSourceError(AlphaVerificationSourceFailureCode.SNAPSHOT_MISMATCH)
        try:
            self._require_exact_artifact(
                candidate.context_digest,
                ALPHA_REVIEW_CONTEXT_MEDIA_TYPE,
                alpha_review_context_payload(context),
            )
            proposal_raw = self._load_canonical_json(
                candidate.proposal_artifact_digest,
                ALPHA_REVIEW_PROPOSAL_MEDIA_TYPE,
            )
            proposal = alpha_review_proposal_from_mapping(proposal_raw)
            self._require_serialized_proposal(candidate, proposal)
            provider_raw = self._load_canonical_json(
                candidate.provider_result_artifact_digest,
                ALPHA_REVIEW_PROVIDER_MEDIA_TYPE,
            )
            provider = alpha_review_provider_result_from_mapping(
                provider_raw,
                proposal=proposal,
            )
            self._require_exact_artifact(
                candidate.provider_result_artifact_digest,
                ALPHA_REVIEW_PROVIDER_MEDIA_TYPE,
                alpha_review_provider_result_payload(provider),
            )
            admitted_raw = self._load_canonical_json(
                candidate.admitted_review_digest,
                ALPHA_ADMITTED_REVIEW_MEDIA_TYPE,
            )
            admitted = alpha_admitted_review_from_mapping(admitted_raw)
            expected_admitted = admit_alpha_review(context, proposal)
            if admitted != expected_admitted or len(admitted.findings) != candidate.finding_count:
                raise ValueError
            self._require_exact_artifact(
                candidate.admitted_review_digest,
                ALPHA_ADMITTED_REVIEW_MEDIA_TYPE,
                alpha_admitted_review_payload(admitted),
            )
        except (
            AlphaReviewContractError,
            KernelError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as error:
            raise AlphaVerificationSourceError(
                AlphaVerificationSourceFailureCode.REVIEW_ARTIFACT_INVALID
            ) from error
        return AlphaPreparedVerification(candidate, context, admitted)

    def _require_serialized_proposal(
        self,
        candidate: AlphaVerificationCandidate,
        proposal: AlphaReviewProposal,
    ) -> None:
        if proposal.context_digest != candidate.context_digest:
            raise ValueError
        self._require_exact_artifact(
            candidate.proposal_artifact_digest,
            ALPHA_REVIEW_PROPOSAL_MEDIA_TYPE,
            alpha_review_proposal_payload(proposal),
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
        raise AlphaVerificationSourceError(AlphaVerificationSourceFailureCode.SNAPSHOT_MISMATCH)
    return value


def _required_integer(value: int | None) -> int:
    if value is None:
        raise AlphaVerificationSourceError(AlphaVerificationSourceFailureCode.SNAPSHOT_MISMATCH)
    return value


__all__ = [
    "AlphaPreparedVerification",
    "AlphaVerificationSourceError",
    "AlphaVerificationSourceFailureCode",
    "AlphaVerificationSourceService",
]
