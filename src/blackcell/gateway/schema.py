from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from blackcell.kernel import JsonValue


class OutputSchemaError(ValueError):
    pass


def validate_output(
    output: Mapping[str, JsonValue],
    schema: Mapping[str, JsonValue],
) -> None:
    expected_type = schema.get("type")
    if expected_type not in (None, "object"):
        raise OutputSchemaError("gateway output schema root must be an object")
    required = schema.get("required", ())
    if not isinstance(required, tuple) or any(not isinstance(item, str) for item in required):
        raise OutputSchemaError("output schema required must be a string array")
    missing = tuple(name for name in required if name not in output)
    if missing:
        raise OutputSchemaError(f"model output is missing required fields: {missing}")
    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping):
        raise OutputSchemaError("output schema properties must be an object")
    if schema.get("additionalProperties") is False:
        extra = tuple(sorted(set(output) - set(properties)))
        if extra:
            raise OutputSchemaError(f"model output contains undeclared fields: {extra}")
    for name, value in output.items():
        property_schema = properties.get(name)
        if isinstance(property_schema, Mapping):
            _validate_type(name, value, property_schema.get("type"))


def _validate_type(name: str, value: JsonValue, expected: JsonValue | None) -> None:
    if expected is None:
        return
    if isinstance(expected, str):
        names = (expected,)
    elif isinstance(expected, tuple) and all(isinstance(item, str) for item in expected):
        names = cast("tuple[str, ...]", expected)
    else:
        raise OutputSchemaError(f"field {name!r} has an invalid type declaration")
    if not any(_matches(value, item) for item in names):
        raise OutputSchemaError(f"field {name!r} does not match type {names}")


def _matches(value: JsonValue, expected: str) -> bool:
    return {
        "null": value is None,
        "boolean": isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, int | float) and not isinstance(value, bool),
        "string": isinstance(value, str),
        "array": isinstance(value, tuple),
        "object": isinstance(value, Mapping),
    }.get(expected, False)
