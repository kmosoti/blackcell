from __future__ import annotations

import hashlib
import hmac
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

API_TOKEN_ENV = "BLACKCELL_API_TOKEN"
API_TOKEN_FILE_ENV = "BLACKCELL_API_TOKEN_FILE"
_MIN_SECRET_CHARS = 32
_MAX_SECRET_BYTES = 4_096
_PLACEHOLDERS = frozenset(
    {
        "change-me",
        "changeme",
        "default",
        "password",
        "replace-me",
        "secret",
    }
)


class SecurityConfigFailureCode(StrEnum):
    MISSING_SECRET = "missing-secret"
    AMBIGUOUS_SECRET_SOURCE = "ambiguous-secret-source"
    INVALID_SECRET = "invalid-secret"
    UNSAFE_SECRET_FILE = "unsafe-secret-file"
    INVALID_DATA_DIRECTORY = "invalid-data-directory"
    UNSAFE_DATA_DIRECTORY = "unsafe-data-directory"
    INVALID_BIND = "invalid-bind"
    INVALID_PROXY_TRUST = "invalid-proxy-trust"


class SecurityConfigError(RuntimeError):
    def __init__(self, code: SecurityConfigFailureCode) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True, slots=True, eq=False)
class SecretValue:
    """Opaque credential with constant-time verification and redacted display."""

    _value: str = field(repr=False)
    _digest: bytes = field(init=False, repr=False)

    def __post_init__(self) -> None:
        _validate_secret(self._value)
        object.__setattr__(self, "_digest", hashlib.sha256(self._value.encode()).digest())

    def __repr__(self) -> str:
        return "SecretValue([REDACTED])"

    def __str__(self) -> str:
        return "[REDACTED]"

    def verify(self, candidate: str) -> bool:
        if not isinstance(candidate, str):
            return False
        candidate_digest = hashlib.sha256(candidate.encode()).digest()
        return hmac.compare_digest(self._digest, candidate_digest)

    def redaction_value(self) -> str:
        """Return the value only for constructing a pre-storage redaction policy."""

        return self._value


def load_service_token(
    environment: Mapping[str, str],
    *,
    expected_uid: int | None = None,
) -> SecretValue:
    has_value = API_TOKEN_ENV in environment
    has_file = API_TOKEN_FILE_ENV in environment
    if has_value and has_file:
        raise SecurityConfigError(SecurityConfigFailureCode.AMBIGUOUS_SECRET_SOURCE)
    if not has_value and not has_file:
        raise SecurityConfigError(SecurityConfigFailureCode.MISSING_SECRET)
    if has_value:
        raw = environment[API_TOKEN_ENV]
        if not isinstance(raw, str):
            raise SecurityConfigError(SecurityConfigFailureCode.INVALID_SECRET)
        return _secret_value(raw)
    raw_path = environment[API_TOKEN_FILE_ENV]
    if not isinstance(raw_path, str) or not raw_path:
        raise SecurityConfigError(SecurityConfigFailureCode.UNSAFE_SECRET_FILE)
    path = Path(raw_path)
    if not path.is_absolute():
        raise SecurityConfigError(SecurityConfigFailureCode.UNSAFE_SECRET_FILE)
    return _secret_value(_read_owner_only_file(path, expected_uid=expected_uid))


def _secret_value(raw: str) -> SecretValue:
    try:
        return SecretValue(raw)
    except (TypeError, ValueError) as error:
        raise SecurityConfigError(SecurityConfigFailureCode.INVALID_SECRET) from error


def _validate_secret(value: str) -> None:
    if not isinstance(value, str):
        raise TypeError("secret must be text")
    encoded = value.encode("utf-8")
    if not len(value) >= _MIN_SECRET_CHARS or len(encoded) > _MAX_SECRET_BYTES:
        raise ValueError("secret length is outside the accepted range")
    if any(not 0x21 <= ord(character) <= 0x7E for character in value) or "," in value:
        raise ValueError("secret must use visible ASCII without delimiters")
    if len(set(value)) < 8 or value.casefold() in _PLACEHOLDERS:
        raise ValueError("secret lacks minimum diversity")


def _read_owner_only_file(path: Path, *, expected_uid: int | None) -> str:
    try:
        before = path.lstat()
    except OSError as error:
        raise SecurityConfigError(SecurityConfigFailureCode.UNSAFE_SECRET_FILE) from error
    if stat.S_ISLNK(before.st_mode):
        raise SecurityConfigError(SecurityConfigFailureCode.UNSAFE_SECRET_FILE)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise SecurityConfigError(SecurityConfigFailureCode.UNSAFE_SECRET_FILE) from error
    try:
        metadata = os.fstat(descriptor)
        uid = _current_uid() if expected_uid is None else expected_uid
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != uid
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size > _MAX_SECRET_BYTES + 1
        ):
            raise SecurityConfigError(SecurityConfigFailureCode.UNSAFE_SECRET_FILE)
        chunks: list[bytes] = []
        remaining = _MAX_SECRET_BYTES + 2
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
    finally:
        os.close(descriptor)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SecurityConfigError(SecurityConfigFailureCode.INVALID_SECRET) from error
    if text.endswith("\n"):
        text = text[:-1]
    if "\n" in text or "\r" in text:
        raise SecurityConfigError(SecurityConfigFailureCode.INVALID_SECRET)
    return text


def _current_uid() -> int:
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        raise SecurityConfigError(SecurityConfigFailureCode.UNSAFE_SECRET_FILE)
    return int(getuid())


__all__ = [
    "API_TOKEN_ENV",
    "API_TOKEN_FILE_ENV",
    "SecretValue",
    "SecurityConfigError",
    "SecurityConfigFailureCode",
    "load_service_token",
]
