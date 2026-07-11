"""Deterministic task-relevant evidence retrieval."""

from blackcell.features.retrieve_evidence.command import RetrieveEvidence
from blackcell.features.retrieve_evidence.handler import DeterministicEvidenceRetriever
from blackcell.features.retrieve_evidence.models import (
    EvidenceCandidate,
    EvidenceKey,
    EvidenceOmission,
    EvidenceOmissionReason,
    EvidenceSelection,
    MissingRequiredEvidenceError,
    RequiredEvidenceGap,
    RequiredEvidenceGapReason,
)

__all__ = [
    "DeterministicEvidenceRetriever",
    "EvidenceCandidate",
    "EvidenceKey",
    "EvidenceOmission",
    "EvidenceOmissionReason",
    "EvidenceSelection",
    "MissingRequiredEvidenceError",
    "RequiredEvidenceGap",
    "RequiredEvidenceGapReason",
    "RetrieveEvidence",
]
