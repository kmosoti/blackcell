"""Typed, provenance-aware observation ingestion."""

from blackcell.features.ingest_observation.command import (
    EvidencePointer,
    IngestObservation,
    ObservationInput,
    ObservedClaim,
)
from blackcell.features.ingest_observation.handler import IngestObservationHandler

__all__ = [
    "EvidencePointer",
    "IngestObservation",
    "IngestObservationHandler",
    "ObservationInput",
    "ObservedClaim",
]
