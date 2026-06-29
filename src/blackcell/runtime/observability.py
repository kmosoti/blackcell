"""Secret-safe structured event and output primitives."""

import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, TextIO

_SENSITIVE_KEYS = ("authorization", "api_key", "password", "secret", "token")
_AUTHORIZATION_PATTERN = re.compile(r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+")
_LINEAR_TOKEN_PATTERN = re.compile(r"\blin_api_[A-Za-z0-9_-]+\b")
_GITHUB_TOKEN_PATTERN = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)\b")


def redact(value: Any, *, key: str | None = None) -> Any:
    """Recursively remove credential-shaped values before serialization."""
    if key is not None and any(part in key.casefold() for part in _SENSITIVE_KEYS):
        return "[redacted]"
    if isinstance(value, Mapping):
        return {str(item_key): redact(item, key=str(item_key)) for item_key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        value = _AUTHORIZATION_PATTERN.sub(r"\1[redacted]", value)
        value = _LINEAR_TOKEN_PATTERN.sub("[redacted]", value)
        value = _GITHUB_TOKEN_PATTERN.sub("[redacted]", value)
        for variable in ("LINEAR_API_KEY", "GITHUB_TOKEN", "GH_TOKEN"):
            secret = os.environ.get(variable)
            if secret:
                value = value.replace(secret, "[redacted]")
        return value
    return value


class EventSink(Protocol):
    def emit(self, event: Mapping[str, Any]) -> None: ...


class NullEventSink:
    def emit(self, event: Mapping[str, Any]) -> None:
        del event


@dataclass(slots=True)
class JsonLineEventSink:
    """Write one redacted structured event per line."""

    stream: TextIO = sys.stderr

    def emit(self, event: Mapping[str, Any]) -> None:
        payload = redact(event)
        try:
            self.stream.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
            self.stream.flush()
        except OSError:
            return


def event_sink_from_environment() -> EventSink:
    if os.environ.get("BLACKCELL_EVENTS", "").casefold() == "jsonl":
        return JsonLineEventSink()
    return NullEventSink()
