import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date, datetime
from enum import Enum, StrEnum
from pathlib import Path
from typing import Any

from rich.console import Console


class OutputMode(StrEnum):
    JSON = "json"
    JSONL = "jsonl"
    RICH = "rich"


@dataclass(slots=True)
class OutputRenderer:
    mode: OutputMode = OutputMode.JSON
    console: Console = field(default_factory=Console)
    error_console: Console = field(default_factory=lambda: Console(stderr=True))

    @classmethod
    def from_flags(
        cls,
        *,
        rich: bool = False,
        jsonl: bool = False,
        output_format: str | None = None,
    ) -> OutputRenderer:
        requested_modes: list[OutputMode] = []

        if rich:
            requested_modes.append(OutputMode.RICH)
        if jsonl:
            requested_modes.append(OutputMode.JSONL)
        if output_format:
            try:
                requested_modes.append(OutputMode(output_format))
            except ValueError as error:
                raise ValueError("--format must be one of: json, jsonl, rich") from error

        if len(set(requested_modes)) > 1:
            raise ValueError("--rich, --jsonl, and --format cannot request different modes")

        if requested_modes:
            return cls(mode=requested_modes[0])

        return cls(mode=OutputMode.JSON)

    def emit(self, value: object, *, rich: object | None = None) -> None:
        if self.mode is OutputMode.RICH and rich is not None:
            self.console.print(rich)
            return

        if self.mode is OutputMode.JSONL:
            for record in _records(value):
                self.console.print(_json(record), markup=False, soft_wrap=True)
            return

        self.console.print(_json(value, indent=2), markup=False, soft_wrap=True)

    def emit_collection(
        self,
        key: str,
        records: Sequence[object],
        *,
        rich: object | None = None,
    ) -> None:
        if self.mode is OutputMode.JSONL:
            self.emit(records, rich=rich)
            return
        self.emit({key: records}, rich=rich)

    def emit_error(self, message: str) -> None:
        if self.mode is OutputMode.RICH:
            self.error_console.print(f"[red]error:[/red] {message}")
            return

        payload = {"error": {"message": message}}
        if self.mode is OutputMode.JSONL:
            self.error_console.print(_json(payload), markup=False, soft_wrap=True)
            return
        self.error_console.print(_json(payload, indent=2), markup=False, soft_wrap=True)


def _records(value: object) -> list[object]:
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return list(value)
    return [value]


def _json(value: object, *, indent: int | None = None) -> str:
    return json.dumps(_jsonable(value), indent=indent, sort_keys=True)


def _jsonable(value: object) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _jsonable(getattr(value, item.name)) for item in fields(value)}

    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}

    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_jsonable(item) for item in value]

    if isinstance(value, Path):
        return str(value)

    if isinstance(value, datetime | date):
        return value.isoformat()

    if isinstance(value, Enum):
        return value.value

    if isinstance(value, set | frozenset):
        return sorted((_jsonable(item) for item in value), key=str)

    return value
