from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from blackcell.kernel import JsonValue


@dataclass(frozen=True, slots=True)
class RepositoryStatusSnapshot:
    """Bounded repository facts admitted by the operator use case."""

    valid: bool
    clean: bool
    entry_count: int
    output_digest: str
    observed_at: datetime

    def __post_init__(self) -> None:
        if self.entry_count < 0:
            raise ValueError("repository status entry count must be non-negative")
        if not self.output_digest.startswith("sha256:"):
            raise ValueError("repository status output digest must be SHA-256")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("repository status observation time must be timezone-aware")

    def value_for(self, subject: str, predicate: str) -> bool:
        key = (subject, predicate)
        if key == ("repository", "git.valid"):
            return self.valid
        if key == ("repository", "git.clean"):
            return self.clean
        raise LookupError(f"repository status does not observe target {key!r}")

    def manifest(self, *, schema_version: str) -> dict[str, JsonValue]:
        if not schema_version.strip():
            raise ValueError("repository status schema version must be non-empty")
        return {
            "schema_version": schema_version,
            "valid": self.valid,
            "clean": self.clean,
            "entry_count": self.entry_count,
            "output_digest": self.output_digest,
            "observed_at": self.observed_at.astimezone(UTC).isoformat(),
        }


class RepositoryStatusPort(Protocol):
    """Read the bounded repository facts needed by the operator use case."""

    def read(self) -> RepositoryStatusSnapshot: ...


__all__ = ["RepositoryStatusPort", "RepositoryStatusSnapshot"]
