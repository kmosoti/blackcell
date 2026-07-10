from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import cast

type JsonScalar = None | bool | int | float | str
type JsonValue = JsonScalar | Mapping[str, JsonValue] | tuple[JsonValue, ...]
type JsonInput = JsonScalar | Mapping[str, JsonInput] | Sequence[JsonInput]


def freeze_json(value: object, *, path: str = "$") -> JsonValue:
    """Validate and recursively freeze a value from the JSON data model."""

    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"non-finite JSON number at {path}")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"JSON object key at {path} must be a string")
            frozen[key] = freeze_json(item, path=f"{path}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return tuple(freeze_json(item, path=f"{path}[{index}]") for index, item in enumerate(value))
    raise TypeError(f"unsupported JSON value at {path}: {type(value).__name__}")


def thaw_json(value: JsonValue) -> object:
    """Return mutable, standard-library JSON containers for serialization/consumers."""

    if isinstance(value, Mapping):
        mapping = cast("Mapping[str, JsonValue]", value)
        return {key: thaw_json(item) for key, item in mapping.items()}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    return value


def canonical_json_bytes(value: object) -> bytes:
    frozen = freeze_json(value)
    return json.dumps(
        thaw_json(frozen),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_json(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def json_digest(value: object) -> str:
    return bytes_digest(canonical_json_bytes(value))


def bytes_digest(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"
