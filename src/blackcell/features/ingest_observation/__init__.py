"""Typed, provenance-aware observation ingestion."""

from blackcell.features.ingest_observation.command import (
    CorrectionInput,
    EvidencePointer,
    IngestCorrection,
    IngestObservation,
    ObservationInput,
    ObservedClaim,
)
from blackcell.features.ingest_observation.handler import (
    IngestCorrectionHandler,
    IngestObservationHandler,
)

__all__ = [
    "CorrectionInput",
    "EvidencePointer",
    "IngestCorrection",
    "IngestCorrectionHandler",
    "IngestObservation",
    "IngestObservationHandler",
    "ObservationInput",
    "ObservedClaim",
]
