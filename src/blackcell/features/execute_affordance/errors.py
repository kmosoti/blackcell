class ExecutionJournalError(RuntimeError):
    """Base error for durable affordance execution state."""


class ExecutionIdentityConflict(ExecutionJournalError):
    """An execution identity was reused across an incompatible binding."""

    def __init__(self, message: str, *, fields: tuple[str, ...] = ()) -> None:
        self.fields = fields
        super().__init__(message)


class IdempotencyKeyConflict(ExecutionIdentityConflict):
    """Compatibility name for an incompatible idempotency-key retry."""


class AuthorizationBindingConflict(ExecutionIdentityConflict):
    """An authorization decision was reused for another execution."""


class ExecutionInProgress(ExecutionJournalError):
    """An execution has an active claim and requires explicit recovery."""


class StaleExecutionClaim(ExecutionJournalError):
    """A fenced execution worker attempted to commit a result."""


class ExecutionRecoveryError(ExecutionJournalError):
    """An execution cannot enter explicit recovery from its current state."""


class ExecutionJournalIntegrityError(ExecutionJournalError):
    """Journal metadata or an owned result artifact failed verification."""


class ExecutionJournalSchemaError(ExecutionJournalError):
    """The execution journal or result artifact uses an unsupported schema."""
