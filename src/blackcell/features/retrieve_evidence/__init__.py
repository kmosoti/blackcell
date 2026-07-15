"""Deterministic task-relevant evidence retrieval."""

from blackcell.features.retrieve_evidence.command import RetrieveEvidence
from blackcell.features.retrieve_evidence.handler import DeterministicEvidenceRetriever
from blackcell.features.retrieve_evidence.models import (
    EvidenceCandidate,
    EvidenceClaimIdentity,
    EvidenceEpistemicStatus,
    EvidenceKey,
    EvidenceOmission,
    EvidenceOmissionReason,
    EvidenceSelection,
    EvidenceUnknownReason,
    MissingRequiredEvidenceError,
    RequiredEvidenceGap,
    RequiredEvidenceGapReason,
    UnknownEvidenceSupport,
)

__all__ = [
    "DeterministicEvidenceRetriever",
    "EvidenceCandidate",
    "EvidenceClaimIdentity",
    "EvidenceEpistemicStatus",
    "EvidenceKey",
    "EvidenceOmission",
    "EvidenceOmissionReason",
    "EvidenceSelection",
    "EvidenceUnknownReason",
    "MissingRequiredEvidenceError",
    "RequiredEvidenceGap",
    "RequiredEvidenceGapReason",
    "RetrieveEvidence",
    "UnknownEvidenceSupport",
]
