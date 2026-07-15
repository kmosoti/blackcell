"""Task-relevant evidence retrieval with Blackcell-owned policy semantics."""

from blackcell.features.retrieve_evidence.command import RetrieveEvidence
from blackcell.features.retrieve_evidence.handler import (
    DeterministicEvidenceRetriever,
    DeterministicObjectiveMatcher,
    RankedEvidenceRetriever,
)
from blackcell.features.retrieve_evidence.models import (
    EvidenceCandidate,
    EvidenceClaimIdentity,
    EvidenceEpistemicStatus,
    EvidenceKey,
    EvidenceObjectiveMatch,
    EvidenceOmission,
    EvidenceOmissionReason,
    EvidenceSelection,
    EvidenceUnknownReason,
    MissingRequiredEvidenceError,
    RequiredEvidenceGap,
    RequiredEvidenceGapReason,
    UnknownEvidenceSupport,
)
from blackcell.features.retrieve_evidence.ports import (
    EvidenceObjectiveMatcher,
    EvidenceRetriever,
)

__all__ = [
    "DeterministicEvidenceRetriever",
    "DeterministicObjectiveMatcher",
    "EvidenceCandidate",
    "EvidenceClaimIdentity",
    "EvidenceEpistemicStatus",
    "EvidenceKey",
    "EvidenceObjectiveMatch",
    "EvidenceObjectiveMatcher",
    "EvidenceOmission",
    "EvidenceOmissionReason",
    "EvidenceRetriever",
    "EvidenceSelection",
    "EvidenceUnknownReason",
    "MissingRequiredEvidenceError",
    "RankedEvidenceRetriever",
    "RequiredEvidenceGap",
    "RequiredEvidenceGapReason",
    "RetrieveEvidence",
    "UnknownEvidenceSupport",
]
