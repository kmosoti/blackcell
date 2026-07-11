from __future__ import annotations

from blackcell.features.build_context.command import BuildContext
from blackcell.features.build_context.models import ContextEvidence, ContextFrame
from blackcell.features.build_context.ports import EvidenceCandidateLike, EvidenceSelectionLike
from blackcell.kernel._json import canonical_json


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
        characters = 0
        for candidate in selection.candidates:
            evidence = _context_evidence(candidate)
            size = len(canonical_json(_serialized(evidence)))
            if characters + size > command.max_characters:
                if "required" in candidate.reasons:
                    raise ContextBudgetError("required evidence exceeds the ContextFrame budget")
                continue
            included.append(evidence)
            characters += size
        omitted = selection.omitted_count + len(selection.candidates) - len(included)
        provenance = tuple(dict.fromkeys(item.source_event_id for item in included))
        return ContextFrame(
            task_id=command.task_id,
            objective=command.objective,
            generated_at=command.generated_at,
            state_position=selection.state_position,
            source_packet_id=selection.source_packet_id,
            source_selection_id=selection.selection_id,
            evidence=tuple(included),
            provenance_event_ids=provenance,
            omitted_evidence_count=omitted,
            serialized_characters=characters,
        )


def _context_evidence(candidate: EvidenceCandidateLike) -> ContextEvidence:
    return ContextEvidence(
        candidate.subject,
        candidate.predicate,
        candidate.value,
        candidate.confidence,
        candidate.effective_at,
        candidate.freshness_seconds,
        candidate.stale,
        candidate.source_event_id,
        candidate.score,
        candidate.reasons,
        candidate.conflicted,
    )


def _serialized(evidence: ContextEvidence) -> dict[str, object]:
    return {
        "subject": evidence.subject,
        "predicate": evidence.predicate,
        "value": evidence.value,
        "confidence": evidence.confidence,
        "effective_at": evidence.effective_at.isoformat(),
        "freshness_seconds": evidence.freshness_seconds,
        "stale": evidence.stale,
        "source_event_id": evidence.source_event_id,
        "relevance_score": evidence.relevance_score,
        "selection_reasons": list(evidence.selection_reasons),
        "conflicted": evidence.conflicted,
    }
