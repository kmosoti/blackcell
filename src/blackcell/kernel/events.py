from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Self, cast

from blackcell.kernel._json import JsonInput, JsonValue, freeze_json, json_digest
from blackcell.kernel.errors import EventIntegrityError


def new_event_id() -> str:
    """Return a UUIDv7 on Python versions that provide it, otherwise a UUIDv4."""

    uuid7 = getattr(uuid, "uuid7", None)
    return str(uuid7() if uuid7 is not None else uuid.uuid4())


def utc_now() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _required_text(value: str, field_name: str) -> str:
    if not value or not value.strip():
        raise ValueError(f"{field_name} must not be empty")
    return value


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    """Immutable occurrence record stored by the Blackcell event ledger.

    ``stream_sequence`` is one-based. ``global_position`` is assigned by the
    store and is deliberately distinct from the domain stream sequence.
    """

    event_id: str
    stream_id: str
    stream_sequence: int
    event_type: str
    schema_version: int
    recorded_at: datetime
    effective_at: datetime
    correlation_id: str
    causation_id: str | None
    actor: str
    source: str
    payload: Mapping[str, JsonValue]
    payload_hash: str
    idempotency_key: str | None = None
    global_position: int | None = None

    def __post_init__(self) -> None:
        required_fields = (
            "event_id",
            "stream_id",
            "event_type",
            "correlation_id",
            "actor",
            "source",
        )
        for field_name in required_fields:
            _required_text(getattr(self, field_name), field_name)
        if self.causation_id is not None:
            _required_text(self.causation_id, "causation_id")
        if self.idempotency_key is not None:
            _required_text(self.idempotency_key, "idempotency_key")
        if self.stream_sequence < 1:
            raise ValueError("stream_sequence must be at least 1")
        if self.schema_version < 1:
            raise ValueError("schema_version must be at least 1")
        if self.global_position is not None and self.global_position < 1:
            raise ValueError("global_position must be at least 1")

        object.__setattr__(self, "recorded_at", _timestamp(self.recorded_at, "recorded_at"))
        object.__setattr__(self, "effective_at", _timestamp(self.effective_at, "effective_at"))
        frozen_payload = freeze_json(self.payload, path="$.payload")
        if not isinstance(frozen_payload, Mapping):
            raise TypeError("event payload must be a JSON object")
        object.__setattr__(self, "payload", frozen_payload)

        actual_hash = json_digest(frozen_payload)
        if self.payload_hash != actual_hash:
            raise EventIntegrityError(
                f"payload hash mismatch for event {self.event_id}: "
                f"expected {self.payload_hash}, calculated {actual_hash}"
            )

    @classmethod
    def create(
        cls,
        *,
        stream_id: str,
        stream_sequence: int,
        event_type: str,
        actor: str,
        source: str,
        payload: Mapping[str, JsonInput] | None = None,
        schema_version: int = 1,
        recorded_at: datetime | None = None,
        effective_at: datetime | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        idempotency_key: str | None = None,
        event_id: str | None = None,
    ) -> Self:
        observed_at = _timestamp(recorded_at or utc_now(), "recorded_at")
        effective = _timestamp(effective_at or observed_at, "effective_at")
        resolved_event_id = event_id or new_event_id()
        frozen_payload = freeze_json(payload or {}, path="$.payload")
        if not isinstance(frozen_payload, Mapping):  # pragma: no cover - constrained by type/API
            raise TypeError("event payload must be a JSON object")
        event_payload = cast("Mapping[str, JsonValue]", frozen_payload)
        return cls(
            event_id=resolved_event_id,
            stream_id=stream_id,
            stream_sequence=stream_sequence,
            event_type=event_type,
            schema_version=schema_version,
            recorded_at=observed_at,
            effective_at=effective,
            correlation_id=correlation_id or resolved_event_id,
            causation_id=causation_id,
            actor=actor,
            source=source,
            payload=event_payload,
            payload_hash=json_digest(event_payload),
            idempotency_key=idempotency_key,
        )

    @property
    def idempotency_hash(self) -> str:
        """Hash semantic request content, excluding occurrence/ledger metadata."""

        return json_digest(
            {
                "actor": self.actor,
                "causation_id": self.causation_id,
                "correlation_id": self.correlation_id,
                "effective_at": self.effective_at.isoformat(),
                "event_type": self.event_type,
                "payload_hash": self.payload_hash,
                "schema_version": self.schema_version,
                "source": self.source,
                "stream_id": self.stream_id,
            }
        )

    @property
    def sequence(self) -> int:
        """Compatibility alias for stream-oriented projectors."""

        return self.stream_sequence

    @property
    def kind(self) -> str:
        """Compatibility alias for event consumers using domain terminology."""

        return self.event_type

    @property
    def occurred_at(self) -> datetime:
        """Compatibility alias; the canonical ingestion time is ``recorded_at``."""

        return self.recorded_at
