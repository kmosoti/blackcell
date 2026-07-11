from __future__ import annotations

from typing import Protocol

from blackcell.features.build_context.models import ContextFrame


class ContextFrameStorageError(RuntimeError):
    """Base error for durable ContextFrame persistence."""


class ContextFrameConflictError(ContextFrameStorageError):
    """A frame identity was reused for a different serialized artifact."""


class ContextFrameIntegrityError(ContextFrameStorageError):
    """Stored ContextFrame content failed schema or digest verification."""


class ContextFrameSchemaError(ContextFrameStorageError):
    """The store or artifact uses an unsupported schema version."""


class ContextFrameStorage(Protocol):
    """Durable application port for content-addressed ContextFrames."""

    def put(self, frame: ContextFrame) -> ContextFrame: ...

    def get(self, frame_id: str) -> ContextFrame | None: ...

    def list_frames(self) -> tuple[ContextFrame, ...]: ...
