from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from blackcell.kernel import EventEnvelope


class ReplayClassification(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CORRUPT = "corrupt"


class ReplayIntegrityStage(StrEnum):
    PROTOCOL = "protocol"
    ARTIFACT = "artifact"
    PROJECTION = "projection"


class ReplayProjectionStage(StrEnum):
    INITIAL = "initial"
    OUTCOME = "outcome"


class ReplayVerificationStatus(StrEnum):
    VERIFIED = "verified"
    NOT_RECORDED = "not-recorded"


@dataclass(frozen=True, slots=True)
class DecodedRunProtocol:
    run_id: str
    run_stream_id: str
    protocol_version: str
    classification: ReplayClassification
    outcome: str | None

    def __post_init__(self) -> None:
        for name in ("run_id", "run_stream_id", "protocol_version"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        if self.classification is ReplayClassification.CORRUPT:
            raise ValueError("decoded protocols cannot be corrupt")
        if self.classification is ReplayClassification.INTERRUPTED:
            if self.outcome is not None:
                raise ValueError("interrupted replay cannot have a recorded outcome")
        elif not self.outcome:
            raise ValueError("terminal replay requires a recorded outcome")
        if self.classification is ReplayClassification.FAILED and self.outcome != "failed":
            raise ValueError("failed replay requires the failed material outcome")


@dataclass(frozen=True, slots=True)
class ReplayArtifactVerification:
    event_id: str
    event_type: str
    stream_sequence: int
    field: str
    digest: str
    verified: bool = True

    def __post_init__(self) -> None:
        for name in ("event_id", "event_type", "field"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        if self.stream_sequence < 1:
            raise ValueError("artifact event sequence must be positive")
        _require_sha256(self.digest)


@dataclass(frozen=True, slots=True)
class ReplayProjectionVerification:
    stage: ReplayProjectionStage
    status: ReplayVerificationStatus
    snapshot_digest: str | None = None
    cutoff_global_position: int | None = None
    effective_time_cutoff: datetime | None = None

    def __post_init__(self) -> None:
        recorded = self.status is ReplayVerificationStatus.VERIFIED
        values = (
            self.snapshot_digest,
            self.cutoff_global_position,
            self.effective_time_cutoff,
        )
        if recorded != all(item is not None for item in values):
            raise ValueError("verified projections require complete recorded identity")
        if not recorded and any(item is not None for item in values):
            raise ValueError("unrecorded projections cannot claim recorded identity")
        if self.snapshot_digest is not None:
            _require_sha256(self.snapshot_digest)
        if self.cutoff_global_position is not None and self.cutoff_global_position < 0:
            raise ValueError("projection cutoff must be non-negative")
        if (
            self.effective_time_cutoff is not None
            and self.effective_time_cutoff.utcoffset() is None
        ):
            raise ValueError("projection effective cutoff must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ReplayFinding:
    stage: ReplayIntegrityStage
    code: str
    message: str

    def __post_init__(self) -> None:
        if not self.code.strip() or not self.message.strip():
            raise ValueError("replay findings require code and message")
        if len(self.code) > 100 or len(self.message) > 500:
            raise ValueError("replay finding exceeds its public bound")


@dataclass(frozen=True, slots=True)
class RunReplayReport:
    run_id: str
    run_stream_id: str
    protocol_version: str | None
    classification: ReplayClassification
    outcome: str | None
    events: tuple[EventEnvelope, ...]
    artifacts: tuple[ReplayArtifactVerification, ...]
    projections: tuple[ReplayProjectionVerification, ...]
    finding: ReplayFinding | None = None
    schema_version: str = "run-replay/v1"

    def __post_init__(self) -> None:
        if not self.run_id.strip() or not self.run_stream_id.strip():
            raise ValueError("replay identity must not be empty")
        if not self.events and self.classification is not ReplayClassification.CORRUPT:
            raise ValueError("replay report requires recorded events")
        if self.classification is ReplayClassification.CORRUPT:
            if self.finding is None or self.outcome is not None:
                raise ValueError("corrupt replay requires one finding and no trusted outcome")
            if self.artifacts or self.projections:
                raise ValueError("corrupt replay cannot expose trusted verification results")
        else:
            if self.finding is not None or not self.protocol_version:
                raise ValueError("valid replay requires a protocol and no corruption finding")
            if self.classification is ReplayClassification.INTERRUPTED:
                if self.outcome is not None:
                    raise ValueError("interrupted replay cannot have an outcome")
            elif not self.outcome:
                raise ValueError("terminal replay requires an outcome")
            stages = tuple(item.stage for item in self.projections)
            if stages != (ReplayProjectionStage.INITIAL, ReplayProjectionStage.OUTCOME):
                raise ValueError("valid replay requires ordered initial and outcome checks")
        if self.schema_version != "run-replay/v1":
            raise ValueError("unsupported replay report schema")

    @property
    def event_count(self) -> int:
        return len(self.events)


class RunNotFoundError(LookupError):
    pass


class ReplayIntegrityError(RuntimeError):
    """A recognized recorded-evidence defect safe to classify as corrupt."""

    def __init__(
        self,
        stage: ReplayIntegrityStage,
        code: str,
        message: str,
        *,
        protocol_version: str | None = None,
        run_stream_id: str | None = None,
    ) -> None:
        self.stage = stage
        self.code = code
        self.public_message = message[:500]
        self.protocol_version = protocol_version
        self.run_stream_id = run_stream_id
        super().__init__(self.public_message)


def _require_sha256(value: str) -> None:
    hexadecimal = value.removeprefix("sha256:")
    if not value.startswith("sha256:") or len(hexadecimal) != 64:
        raise ValueError("artifact identity must be a SHA-256 digest")
    try:
        int(hexadecimal, 16)
    except ValueError as error:
        raise ValueError("artifact identity must be a SHA-256 digest") from error


__all__ = [
    "DecodedRunProtocol",
    "ReplayArtifactVerification",
    "ReplayClassification",
    "ReplayFinding",
    "ReplayIntegrityError",
    "ReplayIntegrityStage",
    "ReplayProjectionStage",
    "ReplayProjectionVerification",
    "ReplayVerificationStatus",
    "RunNotFoundError",
    "RunReplayReport",
]
