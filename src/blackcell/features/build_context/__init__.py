"""Task-specific ContextFrame construction."""

from blackcell.features.build_context.artifacts import (
    CONTEXT_FRAME_MEDIA_TYPE,
    CONTEXT_FRAME_SCHEMA_VERSION_V3,
    CONTEXT_FRAME_SCHEMA_VERSION_V4,
    CONTEXT_FRAME_SCHEMA_VERSIONS,
    CONTEXT_OMISSION_SCHEMA_VERSION_V2,
    CONTEXT_OMISSION_SCHEMA_VERSION_V3,
    CONTEXT_OMISSION_SCHEMA_VERSIONS,
    decode_context_frame,
    encode_context_frame,
)
from blackcell.features.build_context.command import BuildContext
from blackcell.features.build_context.handler import (
    ContextBudgetError,
    ContextSelectionMismatchError,
    build_context_frame,
)
from blackcell.features.build_context.models import (
    ContextClaimIdentity,
    ContextEpistemicStatus,
    ContextEvidence,
    ContextFrame,
    ContextOmission,
    ContextOmissionReason,
    ContextOmissionStage,
    ContextUnknownReason,
    serialize_context_evidence,
    serialize_context_frame,
)
from blackcell.features.build_context.storage import (
    ContextFrameConflictError,
    ContextFrameIntegrityError,
    ContextFrameSchemaError,
    ContextFrameStorage,
    ContextFrameStorageError,
)

__all__ = [
    "CONTEXT_FRAME_MEDIA_TYPE",
    "CONTEXT_FRAME_SCHEMA_VERSIONS",
    "CONTEXT_FRAME_SCHEMA_VERSION_V3",
    "CONTEXT_FRAME_SCHEMA_VERSION_V4",
    "CONTEXT_OMISSION_SCHEMA_VERSIONS",
    "CONTEXT_OMISSION_SCHEMA_VERSION_V2",
    "CONTEXT_OMISSION_SCHEMA_VERSION_V3",
    "BuildContext",
    "ContextBudgetError",
    "ContextClaimIdentity",
    "ContextEpistemicStatus",
    "ContextEvidence",
    "ContextFrame",
    "ContextFrameConflictError",
    "ContextFrameIntegrityError",
    "ContextFrameSchemaError",
    "ContextFrameStorage",
    "ContextFrameStorageError",
    "ContextOmission",
    "ContextOmissionReason",
    "ContextOmissionStage",
    "ContextSelectionMismatchError",
    "ContextUnknownReason",
    "build_context_frame",
    "decode_context_frame",
    "encode_context_frame",
    "serialize_context_evidence",
    "serialize_context_frame",
]
