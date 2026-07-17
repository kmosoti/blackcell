from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from blackcell.kernel import EventEnvelope
from blackcell.kernel._json import bytes_digest

from ._state_transition_errors import StateTransitionBindingError, StateTransitionNotReady

if TYPE_CHECKING:
    from .state_transition import StateTransitionArtifacts, StateTransitionHistory

_ARTIFACT_KEYS = frozenset(
    {"digest", "media_type", "encoding", "size_bytes", "schema_version", "logical_id"}
)


@dataclass(frozen=True, slots=True)
class _Artifact:
    digest: str
    media_type: str
    encoding: str | None
    size_bytes: int
    schema_version: str
    logical_id: str
    data: bytes


def _artifact(
    event: EventEnvelope,
    *,
    artifacts: StateTransitionArtifacts,
    media_type: str,
    schema_version: str | None,
) -> _Artifact:
    return _named_artifact(
        event,
        "artifact",
        artifacts=artifacts,
        media_type=media_type,
        schema_version=schema_version,
    )


def _named_artifact(
    event: EventEnvelope,
    name: str,
    *,
    artifacts: StateTransitionArtifacts,
    media_type: str,
    schema_version: str | None,
) -> _Artifact:
    value = event.payload.get(name)
    if not isinstance(value, Mapping):
        label = "owner artifact" if name == "artifact" else f"{name} owner artifact"
        raise StateTransitionBindingError(f"{event.event_type} lacks its {label} link")
    link = _artifact_from_mapping(
        cast("Mapping[str, object]", value),
        artifacts=artifacts,
        label=f"{event.event_type}.{name}",
    )
    if link.media_type != media_type or link.encoding != "utf-8":
        raise StateTransitionBindingError(f"{event.event_type} artifact type is incompatible")
    if schema_version is not None and link.schema_version != schema_version:
        raise StateTransitionBindingError(f"{event.event_type} artifact schema is incompatible")
    return link


def _artifact_from_mapping(
    value: Mapping[str, object],
    *,
    artifacts: StateTransitionArtifacts,
    label: str,
) -> _Artifact:
    if frozenset(value) != _ARTIFACT_KEYS:
        raise StateTransitionBindingError(f"{label} fields are not exact")
    digest = _text(value, "digest")
    media_type = _text(value, "media_type")
    encoding_value = value.get("encoding")
    if encoding_value is not None and (
        not isinstance(encoding_value, str) or not encoding_value.strip()
    ):
        raise StateTransitionBindingError(f"{label} encoding is invalid")
    size = value.get("size_bytes")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise StateTransitionBindingError(f"{label} size is invalid")
    schema_version = _text(value, "schema_version")
    logical_id = _text(value, "logical_id")
    reference = artifacts.stat(digest)
    if (
        reference.digest != digest
        or reference.size_bytes != size
        or reference.media_type != media_type
        or reference.encoding != encoding_value
    ):
        raise StateTransitionBindingError(f"{label} differs from persisted artifact metadata")
    data = artifacts.get_bytes(digest, verify=True)
    if len(data) != size or bytes_digest(data) != digest:
        raise StateTransitionBindingError(f"{label} bytes do not match their content address")
    return _Artifact(
        digest,
        media_type,
        encoding_value,
        size,
        schema_version,
        logical_id,
        data,
    )


def _event(events: Mapping[str, EventEnvelope], event_type: str) -> EventEnvelope:
    try:
        return events[event_type]
    except KeyError as error:
        raise StateTransitionNotReady(f"run has not recorded {event_type}") from error


def _prove_occurrence(event: EventEnvelope, history: StateTransitionHistory) -> None:
    if event.global_position is None:
        raise StateTransitionBindingError("transition evidence is not a stored ledger occurrence")
    loaded = history.get(event.event_id)
    if loaded != event:
        raise StateTransitionBindingError("event lookup does not prove the selected occurrence")
    slot = tuple(history.read_all(after_position=event.global_position - 1, limit=1))
    if len(slot) != 1 or slot[0] != event:
        raise StateTransitionBindingError("event does not occupy its claimed global position")


def _required_occurrence(
    history: StateTransitionHistory,
    event_id: str,
) -> EventEnvelope:
    event = history.get(event_id)
    if event is None:
        raise StateTransitionBindingError(f"event {event_id!r} is absent from the ledger")
    _prove_occurrence(event, history)
    return event


def _matches(event: EventEnvelope, expected: Mapping[str, object]) -> None:
    mismatches = tuple(key for key, value in expected.items() if event.payload.get(key) != value)
    if mismatches:
        raise StateTransitionBindingError(
            f"{event.event_type} payload differs from owner evidence: {', '.join(mismatches)}"
        )


def _text(value: Mapping[str, object], field: str) -> str:
    item = value.get(field)
    if not isinstance(item, str) or not item.strip():
        raise StateTransitionBindingError(f"{field} must be a non-empty string")
    return item


def _strings(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, tuple | list):
        raise StateTransitionBindingError(f"{label} must be an array")
    result = tuple(value)
    if any(not isinstance(item, str) or not item.strip() for item in result):
        raise StateTransitionBindingError(f"{label} values must be non-empty strings")
    if len(result) != len(set(result)):
        raise StateTransitionBindingError(f"{label} values must be unique")
    return cast("tuple[str, ...]", result)
