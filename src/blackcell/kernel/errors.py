class KernelError(Exception):
    """Base class for kernel contract and persistence errors."""


class SchemaVersionError(KernelError):
    """The database schema is newer than this runtime understands."""


class ConcurrencyError(KernelError):
    def __init__(self, stream_id: str, expected: int, actual: int) -> None:
        self.stream_id = stream_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"stream {stream_id!r} expected sequence {expected}, current sequence is {actual}"
        )


class EventSequenceError(KernelError):
    """An event's declared stream sequence is not the next expected sequence."""


class EventConflictError(KernelError):
    """An event identifier or causal reference violates ledger integrity."""


class IdempotencyConflict(KernelError):
    """An idempotency key was reused for semantically different content."""


class EventIntegrityError(KernelError):
    """A persisted event no longer matches its content hash."""


class ArtifactNotFoundError(KernelError):
    """No artifact metadata exists for a requested digest."""


class ArtifactIntegrityError(KernelError):
    """Artifact bytes do not match their content address."""


class ProjectionConflict(KernelError):
    """A projection checkpoint was concurrently changed or would regress."""
