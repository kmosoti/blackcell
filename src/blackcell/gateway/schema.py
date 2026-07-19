"""Closed, recursive schema validation for model-gateway outputs.

The gateway intentionally supports only a small JSON-Schema-like vocabulary:
``type``, ``const``, ``enum``, object
``properties``/``required``/``additionalProperties``, array
``items``/``maxItems``/``uniqueItems``, numeric bounds, and string length bounds.
``$schema`` is accepted as root metadata. Every other keyword is rejected,
including inside an optional property, so unsupported schema semantics can never
be mistaken for enforcement.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import cast

from blackcell.gateway.models import GatewayCompletion
from blackcell.kernel import JsonValue


class OutputSchemaError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        completion: GatewayCompletion | None = None,
    ) -> None:
        if completion is not None and not isinstance(completion, GatewayCompletion):
            raise TypeError("output schema completion has an invalid type")
        super().__init__(message)
        self.completion = completion


_TYPE_NAMES = frozenset(
    {
        "null",
        "boolean",
        "integer",
        "number",
        "string",
        "array",
        "object",
    }
)
_SUPPORTED_KEYWORDS = frozenset(
    {
        "type",
        "const",
        "enum",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "maxItems",
        "uniqueItems",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minLength",
        "maxLength",
    }
)
_ROOT_METADATA_KEYWORDS = frozenset({"$schema"})
_NUMERIC_BOUNDARIES = (
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
)
_STRING_BOUNDARIES = ("minLength", "maxLength")


def validate_output(
    output: Mapping[str, JsonValue],
    schema: Mapping[str, JsonValue],
) -> None:
    """Validate an object output against Blackcell's bounded schema subset."""

    _validate_schema(schema, path="$schema", root=True)
    declared_types = _declared_types(schema, path="$schema")
    if declared_types is not None and "object" not in declared_types:
        raise OutputSchemaError("gateway output schema root must allow an object")
    _validate_value(output, schema, path="$")


def _validate_schema(
    schema: Mapping[str, JsonValue],
    *,
    path: str,
    root: bool,
) -> None:
    allowed = _SUPPORTED_KEYWORDS | (_ROOT_METADATA_KEYWORDS if root else frozenset())
    unsupported = tuple(sorted(set(schema) - allowed))
    if unsupported:
        raise OutputSchemaError(f"unsupported output schema keywords at {path}: {unsupported}")

    _declared_types(schema, path=path)

    if "enum" in schema:
        enum = schema["enum"]
        if not isinstance(enum, tuple) or not enum:
            raise OutputSchemaError(f"{path}.enum must be a non-empty array")
        enum_values = cast("tuple[JsonValue, ...]", enum)
        if any(
            _json_equal(left, right)
            for index, left in enumerate(enum_values)
            for right in enum_values[index + 1 :]
        ):
            raise OutputSchemaError(f"{path}.enum must not contain duplicates")

    if "$schema" in schema and not isinstance(schema["$schema"], str):
        raise OutputSchemaError(f"{path}.$schema must be a string")

    if "properties" in schema:
        raw_properties = schema["properties"]
        if not isinstance(raw_properties, Mapping):
            raise OutputSchemaError(f"{path}.properties must be an object")
        properties = cast("Mapping[str, JsonValue]", raw_properties)
        for name, property_schema in properties.items():
            if not isinstance(property_schema, Mapping):
                raise OutputSchemaError(f"{_property_path(path, name)} must be a schema object")
            nested_schema = cast("Mapping[str, JsonValue]", property_schema)
            _validate_schema(
                nested_schema,
                path=_property_path(path, name),
                root=False,
            )

    if "required" in schema:
        required = schema["required"]
        if not isinstance(required, tuple) or any(not isinstance(item, str) for item in required):
            raise OutputSchemaError(f"{path}.required must be a string array")
        if len(required) != len(set(required)):
            raise OutputSchemaError(f"{path}.required must not contain duplicates")

    if "additionalProperties" in schema and not isinstance(schema["additionalProperties"], bool):
        raise OutputSchemaError(f"{path}.additionalProperties must be a boolean")

    if "items" in schema:
        raw_items = schema["items"]
        if not isinstance(raw_items, Mapping):
            raise OutputSchemaError(f"{path}.items must be a schema object")
        items = cast("Mapping[str, JsonValue]", raw_items)
        _validate_schema(items, path=f"{path}.items", root=False)

    if "maxItems" in schema:
        maximum_items = schema["maxItems"]
        if (
            not isinstance(maximum_items, int)
            or isinstance(maximum_items, bool)
            or maximum_items < 0
        ):
            raise OutputSchemaError(f"{path}.maxItems must be a non-negative integer")

    if "uniqueItems" in schema and not isinstance(schema["uniqueItems"], bool):
        raise OutputSchemaError(f"{path}.uniqueItems must be a boolean")

    for keyword in _NUMERIC_BOUNDARIES:
        if keyword not in schema:
            continue
        boundary = _as_number(schema[keyword])
        if boundary is None:
            raise OutputSchemaError(f"{path}.{keyword} must be a finite number")

    for keyword in _STRING_BOUNDARIES:
        if keyword not in schema:
            continue
        boundary = schema[keyword]
        if not isinstance(boundary, int) or isinstance(boundary, bool) or boundary < 0:
            raise OutputSchemaError(f"{path}.{keyword} must be a non-negative integer")

    minimum = _as_number(schema.get("minimum"))
    maximum = _as_number(schema.get("maximum"))
    if minimum is not None and maximum is not None and minimum > maximum:
        raise OutputSchemaError(f"{path}.minimum must not exceed maximum")

    exclusive_minimum = _as_number(schema.get("exclusiveMinimum"))
    exclusive_maximum = _as_number(schema.get("exclusiveMaximum"))
    if (
        exclusive_minimum is not None
        and exclusive_maximum is not None
        and exclusive_minimum >= exclusive_maximum
    ):
        raise OutputSchemaError(f"{path}.exclusiveMinimum must be less than exclusiveMaximum")

    min_length = schema.get("minLength")
    max_length = schema.get("maxLength")
    if (
        isinstance(min_length, int)
        and not isinstance(min_length, bool)
        and isinstance(max_length, int)
        and not isinstance(max_length, bool)
        and min_length > max_length
    ):
        raise OutputSchemaError(f"{path}.minLength must not exceed maxLength")


def _declared_types(
    schema: Mapping[str, JsonValue],
    *,
    path: str,
) -> tuple[str, ...] | None:
    if "type" not in schema:
        return None
    declaration = schema["type"]
    if isinstance(declaration, str):
        names = (declaration,)
    elif (
        isinstance(declaration, tuple)
        and declaration
        and all(isinstance(item, str) for item in declaration)
    ):
        names = tuple(item for item in declaration if isinstance(item, str))
    else:
        raise OutputSchemaError(f"{path}.type must be a type name or non-empty string array")
    unsupported = tuple(name for name in names if name not in _TYPE_NAMES)
    if unsupported:
        raise OutputSchemaError(f"unsupported type names at {path}: {unsupported}")
    if len(names) != len(set(names)):
        raise OutputSchemaError(f"{path}.type must not contain duplicates")
    return names


def _validate_value(
    value: JsonValue,
    schema: Mapping[str, JsonValue],
    *,
    path: str,
) -> None:
    declared_types = _declared_types(schema, path=path)
    if declared_types is not None and not any(
        _matches_type(value, expected) for expected in declared_types
    ):
        raise OutputSchemaError(f"{path} does not match type {declared_types}")

    if "const" in schema and not _json_equal(value, schema["const"]):
        raise OutputSchemaError(f"{path} does not match its const value")

    if "enum" in schema:
        enum = schema["enum"]
        if not isinstance(enum, tuple):  # pragma: no cover - schema checked first
            raise OutputSchemaError(f"{path}.enum must be an array")
        if not any(_json_equal(value, item) for item in enum):
            raise OutputSchemaError(f"{path} does not match an enum value")

    if isinstance(value, Mapping):
        _validate_object(cast("Mapping[str, JsonValue]", value), schema, path=path)
    elif isinstance(value, tuple):
        _validate_array(cast("tuple[JsonValue, ...]", value), schema, path=path)
    elif (number := _as_number(value)) is not None:
        _validate_number(number, schema, path=path)
    elif isinstance(value, str):
        _validate_string(value, schema, path=path)


def _validate_object(
    value: Mapping[str, JsonValue],
    schema: Mapping[str, JsonValue],
    *,
    path: str,
) -> None:
    required = schema.get("required", ())
    if not isinstance(required, tuple):  # pragma: no cover - schema checked first
        raise OutputSchemaError(f"{path}.required must be a string array")
    missing = tuple(name for name in required if isinstance(name, str) and name not in value)
    if missing:
        raise OutputSchemaError(f"{path} is missing required fields: {missing}")

    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping):  # pragma: no cover - schema checked first
        raise OutputSchemaError(f"{path}.properties must be an object")
    if schema.get("additionalProperties") is False:
        extra = tuple(sorted(set(value) - set(properties)))
        if extra:
            raise OutputSchemaError(f"{path} contains undeclared fields: {extra}")

    for name, item in value.items():
        property_schema = properties.get(name)
        if isinstance(property_schema, Mapping):
            _validate_value(item, property_schema, path=_property_path(path, name))


def _validate_array(
    value: tuple[JsonValue, ...],
    schema: Mapping[str, JsonValue],
    *,
    path: str,
) -> None:
    maximum_items = schema.get("maxItems")
    if isinstance(maximum_items, int) and len(value) > maximum_items:
        raise OutputSchemaError(f"{path} contains more than maxItems {maximum_items}")
    if schema.get("uniqueItems") is True and any(
        _json_equal(left, right) for index, left in enumerate(value) for right in value[index + 1 :]
    ):
        raise OutputSchemaError(f"{path} contains duplicate items")
    items = schema.get("items")
    if not isinstance(items, Mapping):
        return
    item_schema = cast("Mapping[str, JsonValue]", items)
    for index, item in enumerate(value):
        _validate_value(item, item_schema, path=f"{path}[{index}]")


def _validate_number(
    value: int | float,
    schema: Mapping[str, JsonValue],
    *,
    path: str,
) -> None:
    minimum = _as_number(schema.get("minimum"))
    if minimum is not None and value < minimum:
        raise OutputSchemaError(f"{path} is less than minimum {minimum}")
    maximum = _as_number(schema.get("maximum"))
    if maximum is not None and value > maximum:
        raise OutputSchemaError(f"{path} exceeds maximum {maximum}")
    exclusive_minimum = _as_number(schema.get("exclusiveMinimum"))
    if exclusive_minimum is not None and value <= exclusive_minimum:
        raise OutputSchemaError(f"{path} must be greater than exclusiveMinimum {exclusive_minimum}")
    exclusive_maximum = _as_number(schema.get("exclusiveMaximum"))
    if exclusive_maximum is not None and value >= exclusive_maximum:
        raise OutputSchemaError(f"{path} must be less than exclusiveMaximum {exclusive_maximum}")


def _validate_string(
    value: str,
    schema: Mapping[str, JsonValue],
    *,
    path: str,
) -> None:
    min_length = schema.get("minLength")
    if isinstance(min_length, int) and not isinstance(min_length, bool) and len(value) < min_length:
        raise OutputSchemaError(f"{path} is shorter than minLength {min_length}")
    max_length = schema.get("maxLength")
    if isinstance(max_length, int) and not isinstance(max_length, bool) and len(value) > max_length:
        raise OutputSchemaError(f"{path} is longer than maxLength {max_length}")


def _matches_type(value: JsonValue, expected: str) -> bool:
    return {
        "null": value is None,
        "boolean": isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": _as_number(value) is not None,
        "string": isinstance(value, str),
        "array": isinstance(value, tuple),
        "object": isinstance(value, Mapping),
    }[expected]


def _as_number(value: JsonValue | None) -> int | float | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    return None


def _json_equal(left: JsonValue, right: JsonValue) -> bool:
    if left is None or right is None:
        return left is right
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left is right
    left_number = _as_number(left)
    right_number = _as_number(right)
    if left_number is not None or right_number is not None:
        return left_number is not None and right_number is not None and left_number == right_number
    if isinstance(left, str) or isinstance(right, str):
        return isinstance(left, str) and isinstance(right, str) and left == right
    if isinstance(left, tuple) or isinstance(right, tuple):
        if not isinstance(left, tuple) or not isinstance(right, tuple):
            return False
        left_items = cast("tuple[JsonValue, ...]", left)
        right_items = cast("tuple[JsonValue, ...]", right)
        return len(left_items) == len(right_items) and all(
            _json_equal(a, b) for a, b in zip(left_items, right_items, strict=True)
        )
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        left_object = cast("Mapping[str, JsonValue]", left)
        right_object = cast("Mapping[str, JsonValue]", right)
        return set(left_object) == set(right_object) and all(
            _json_equal(left_object[key], right_object[key]) for key in left_object
        )
    return False


def _property_path(path: str, name: str) -> str:
    if name.isidentifier():
        return f"{path}.{name}"
    return f"{path}[{name!r}]"
