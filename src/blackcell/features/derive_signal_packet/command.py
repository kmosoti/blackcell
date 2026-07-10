from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class DeriveSignalPacket:
    scope: str
    generated_at: datetime
    stale_after_seconds: int = 86_400

    def __post_init__(self) -> None:
        if not self.scope.strip():
            raise ValueError("scope must not be empty")
        if self.generated_at.tzinfo is None or self.generated_at.utcoffset() is None:
            raise ValueError("generated_at must be timezone-aware")
        if self.stale_after_seconds < 0:
            raise ValueError("stale_after_seconds must be non-negative")
