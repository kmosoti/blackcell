"""Blackcell's immutable event, artifact, and deterministic replay kernel."""

from blackcell.kernel._json import JsonInput, JsonScalar, JsonValue
from blackcell.kernel.artifacts import ArtifactRef, ArtifactStore
from blackcell.kernel.database import SCHEMA_VERSION
from blackcell.kernel.errors import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactQuotaExceededError,
    ConcurrencyError,
    EventConflictError,
    EventIntegrityError,
    EventSequenceError,
    IdempotencyConflict,
    KernelError,
    ProjectionConflict,
    SchemaVersionError,
)
from blackcell.kernel.events import EventEnvelope, new_event_id, utc_now
from blackcell.kernel.projections import (
    CheckpointStore,
    Projection,
    ProjectionCheckpoint,
    ProjectionRunner,
    ReplayResult,
)
from blackcell.kernel.store import EventStore

__all__ = [
    "SCHEMA_VERSION",
    "ArtifactIntegrityError",
    "ArtifactNotFoundError",
    "ArtifactQuotaExceededError",
    "ArtifactRef",
    "ArtifactStore",
    "CheckpointStore",
    "ConcurrencyError",
    "EventConflictError",
    "EventEnvelope",
    "EventIntegrityError",
    "EventSequenceError",
    "EventStore",
    "IdempotencyConflict",
    "JsonInput",
    "JsonScalar",
    "JsonValue",
    "KernelError",
    "Projection",
    "ProjectionCheckpoint",
    "ProjectionConflict",
    "ProjectionRunner",
    "ReplayResult",
    "SchemaVersionError",
    "new_event_id",
    "utc_now",
]
