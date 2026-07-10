from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any


def jsonable(value: object) -> Any:
    """Convert immutable runtime values without deepcopying mapping proxies."""

    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: jsonable(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [jsonable(item) for item in value]
    if isinstance(value, set | frozenset):
        return sorted((jsonable(item) for item in value), key=str)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    return value
