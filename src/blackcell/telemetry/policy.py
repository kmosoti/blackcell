from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from blackcell.models import JsonObject, JsonValue


class ContentMode(StrEnum):
    METADATA_ONLY = "metadata-only"
    REDACT_SENSITIVE = "redact-sensitive"
    ALLOW = "allow"


@dataclass(frozen=True, slots=True)
class ContentPolicy:
    """Sanitization policy applied before a span enters recorder storage."""

    mode: ContentMode = ContentMode.METADATA_ONLY
    replacement: str = "[REDACTED]"
    max_string_chars: int = 256
    max_collection_items: int = 32
    sensitive_keys: tuple[str, ...] = (
        "api_key",
        "authorization",
        "cookie",
        "credential",
        "password",
        "secret",
        "token",
    )
    content_keys: tuple[str, ...] = (
        "body",
        "completion",
        "content",
        "context",
        "document",
        "input",
        "message",
        "output",
        "prompt",
        "response",
        "tool_result",
    )

    def __post_init__(self) -> None:
        if self.max_string_chars <= 0:
            raise ValueError("max_string_chars must be positive")
        if self.max_collection_items <= 0:
            raise ValueError("max_collection_items must be positive")

    def sanitize(self, attributes: Mapping[str, Any] | None) -> JsonObject:
        if not attributes:
            return {}
        return {
            str(key): self._sanitize_value(str(key), value)
            for key, value in list(attributes.items())[: self.max_collection_items]
        }

    def _sanitize_value(self, key: str, value: Any) -> JsonValue:
        normalized = key.casefold().replace("-", "_").replace(".", "_")
        if _matches_sensitive_key(normalized, self.sensitive_keys):
            return self.replacement
        if self.mode is ContentMode.METADATA_ONLY and _matches_content_key(
            normalized, self.content_keys
        ):
            return self.replacement
        if isinstance(value, Mapping):
            return {
                str(child_key): self._sanitize_value(str(child_key), child_value)
                for child_key, child_value in list(value.items())[: self.max_collection_items]
            }
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return [
                self._sanitize_value(key, item) for item in list(value)[: self.max_collection_items]
            ]
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            if _looks_like_bearer(value):
                return self.replacement
            return value[: self.max_string_chars]
        return f"<{type(value).__name__}>"


def _matches_sensitive_key(normalized: str, candidates: tuple[str, ...]) -> bool:
    return any(
        normalized == candidate or normalized.endswith(f"_{candidate}")
        for candidate in candidates
    )


def _matches_content_key(normalized: str, candidates: tuple[str, ...]) -> bool:
    return any(
        normalized == candidate or normalized.endswith(f"_{candidate}")
        for candidate in candidates
    )


def _looks_like_bearer(value: str) -> bool:
    lowered = value.casefold().strip()
    return lowered.startswith(("bearer ", "basic ", "ghp_", "sk-"))
