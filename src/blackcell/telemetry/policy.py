from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from blackcell.models import JsonObject, JsonValue

_MAX_KEY_CHARS = 256


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
    sensitive_values: tuple[str, ...] = field(default=(), repr=False, compare=False)
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
        if not self.replacement or len(self.replacement) > 64:
            raise ValueError("replacement must be bounded non-empty text")
        if self.max_string_chars <= 0:
            raise ValueError("max_string_chars must be positive")
        if self.max_collection_items <= 0:
            raise ValueError("max_collection_items must be positive")
        if any(
            not isinstance(item, str) or not 8 <= len(item) <= 4_096
            for item in self.sensitive_values
        ):
            raise ValueError("sensitive_values must contain bounded non-empty secrets")

    def sanitize(self, attributes: Mapping[str, Any] | None) -> JsonObject:
        if not attributes:
            return {}
        return {
            self._sanitize_key(str(key)): self._sanitize_value(str(key), value)
            for key, value in list(attributes.items())[: self.max_collection_items]
        }

    def sanitize_text(self, value: str) -> str:
        sanitized = self._sanitize_value("", value)
        if not isinstance(sanitized, str):  # pragma: no cover - string input invariant
            raise TypeError("text sanitization must produce text")
        return sanitized

    def sanitize_text_mapping(self, value: Mapping[str, str]) -> dict[str, str]:
        result: dict[str, str] = {}
        for key, item in list(value.items())[: self.max_collection_items]:
            sanitized = self._sanitize_value(str(key), item)
            if not isinstance(sanitized, str):  # pragma: no cover - string input invariant
                raise TypeError("text mapping sanitization must produce text")
            result[self._sanitize_key(str(key))] = sanitized
        return result

    def _sanitize_key(self, key: str) -> str:
        if any(secret in key for secret in self.sensitive_values) or _looks_like_credential(key):
            return self.replacement
        return key[:_MAX_KEY_CHARS]

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
                self._sanitize_key(str(child_key)): self._sanitize_value(
                    str(child_key), child_value
                )
                for child_key, child_value in list(value.items())[: self.max_collection_items]
            }
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return [
                self._sanitize_value(key, item) for item in list(value)[: self.max_collection_items]
            ]
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            if any(secret in value for secret in self.sensitive_values):
                return self.replacement
            if _looks_like_credential(value):
                return self.replacement
            return value[: self.max_string_chars]
        return f"<{type(value).__name__}>"


def _matches_sensitive_key(normalized: str, candidates: tuple[str, ...]) -> bool:
    return any(
        normalized == candidate
        or normalized.startswith(f"{candidate}_")
        or normalized.endswith(f"_{candidate}")
        or f"_{candidate}_" in normalized
        for candidate in candidates
    )


def _matches_content_key(normalized: str, candidates: tuple[str, ...]) -> bool:
    return any(
        normalized == candidate or normalized.endswith(f"_{candidate}") for candidate in candidates
    )


def _looks_like_credential(value: str) -> bool:
    lowered = value.casefold().strip()
    if lowered.startswith(
        (
            "bearer ",
            "basic ",
            "gho_",
            "ghp_",
            "ghs_",
            "github_pat_",
            "sk-",
        )
    ):
        return True
    if "-----begin " in lowered and " private key-----" in lowered:
        return True
    return bool(
        re.fullmatch(r"AKIA[0-9A-Z]{16}", value.strip())
        or re.fullmatch(
            r"[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}",
            value.strip(),
        )
        or re.search(r"[a-z][a-z0-9+.-]*://[^/@\s:]+:[^/@\s]+@", value, re.IGNORECASE)
    )
