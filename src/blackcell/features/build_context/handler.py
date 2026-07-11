from __future__ import annotations

from blackcell.features.build_context.command import BuildContext
from blackcell.features.build_context.models import (
    ContextClaimIdentity,
    ContextEvidence,
    ContextFrame,
    ContextOmission,
    ContextOmissionReason,
    ContextOmissionStage,
    serialize_context_evidence,
)
from blackcell.features.build_context.ports import (
    EvidenceCandidateLike,
    EvidenceOmissionLike,
    EvidenceSelectionLike,
)


class ContextBudgetError(ValueError):
    pass


class ContextSelectionMismatchError(ValueError):
    pass


class ContextFrameBuilder:
    def handle(self, command: BuildContext, selection: EvidenceSelectionLike) -> ContextFrame:
        if selection.objective != command.objective:
            raise ContextSelectionMismatchError(
                "evidence selection objective does not match the ContextFrame objective"
            )
        included: list[ContextEvidence] = []
        omissions = [_retrieval_omission(item) for item in selection.omissions]
        characters = 0
        for candidate in selection.candidates:
            evidence = _context_evidence(candidate)
            size = len(serialize_context_evidence(evidence)) + int(bool(included))
            if characters + size > command.max_characters:
                if "required" in candidate.reasons:
                    raise ContextBudgetError(
                        "required evidence exceeds the model-facing evidence-payload budget"
                    )
                omissions.append(_character_budget_omission(candidate, size))
                continue
            included.append(evidence)
            characters += size
        provenance = tuple(dict.fromkeys(item.source_event_id for item in included))
        return ContextFrame(
            task_id=command.task_id,
            objective=command.objective,
            generated_at=command.generated_at,
            source_packet_id=selection.source_packet_id,
            source_packet_purpose=selection.source_packet_purpose,
            source_selection_id=selection.selection_id,
            state_domain=selection.state_domain,
            state_stream_id=selection.state_stream_id,
            state_global_position=selection.state_global_position,
            state_stream_position=selection.state_stream_position,
            source_claim_identities=tuple(
                ContextClaimIdentity(item.source_event_id, item.claim_id)
                for item in selection.source_claim_identities
            ),
            evidence=tuple(included),
            provenance_event_ids=provenance,
            omissions=tuple(omissions),
            model_payload_characters=characters,
        )


def _context_evidence(candidate: EvidenceCandidateLike) -> ContextEvidence:
    return ContextEvidence(
        candidate.claim_id,
        candidate.subject,
        candidate.predicate,
        candidate.value,
        candidate.confidence,
        candidate.effective_at,
        candidate.freshness_seconds,
        candidate.stale,
        candidate.source_event_id,
        candidate.domain,
        candidate.stream_id,
        candidate.stream_sequence,
        candidate.global_position,
        candidate.score,
        candidate.reasons,
        candidate.conflicted,
    )


def _retrieval_omission(omission: EvidenceOmissionLike) -> ContextOmission:
    return ContextOmission(
        subject=omission.subject,
        claim_id=omission.claim_id,
        predicate=omission.predicate,
        value=omission.value,
        confidence=omission.confidence,
        effective_at=omission.effective_at,
        freshness_seconds=omission.freshness_seconds,
        stale=omission.stale,
        source_event_id=omission.source_event_id,
        domain=omission.domain,
        stream_id=omission.stream_id,
        stream_sequence=omission.stream_sequence,
        global_position=omission.global_position,
        relevance_score=omission.score,
        selection_reasons=omission.reasons,
        conflicted=omission.conflicted,
        stage=ContextOmissionStage.RETRIEVAL,
        reason=ContextOmissionReason(omission.reason),
        source_omission_id=omission.omission_id,
        source_omission_schema_version=omission.schema_version,
    )


def _character_budget_omission(
    candidate: EvidenceCandidateLike,
    serialized_characters: int,
) -> ContextOmission:
    return ContextOmission(
        subject=candidate.subject,
        claim_id=candidate.claim_id,
        predicate=candidate.predicate,
        value=candidate.value,
        confidence=candidate.confidence,
        effective_at=candidate.effective_at,
        freshness_seconds=candidate.freshness_seconds,
        stale=candidate.stale,
        source_event_id=candidate.source_event_id,
        domain=candidate.domain,
        stream_id=candidate.stream_id,
        stream_sequence=candidate.stream_sequence,
        global_position=candidate.global_position,
        relevance_score=candidate.score,
        selection_reasons=candidate.reasons,
        conflicted=candidate.conflicted,
        stage=ContextOmissionStage.CONTEXT_PROJECTION,
        reason=ContextOmissionReason.CHARACTER_BUDGET,
        model_payload_characters=serialized_characters,
    )
