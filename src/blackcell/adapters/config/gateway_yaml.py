from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

import yaml

from blackcell.gateway import (
    DataClassification,
    GatewayConfiguration,
    GatewayProfile,
    ModelCapability,
)

_ROOT_KEYS = frozenset({"schema_version", "profiles"})
_PROFILE_KEYS = frozenset(
    {
        "profile_id",
        "capability",
        "adapter_id",
        "model_id",
        "priority",
        "local",
        "deterministic",
        "maximum_classification",
        "max_input_tokens",
        "max_output_tokens",
        "max_cost_microusd",
        "enabled",
    }
)
_CREDENTIAL_KEYS = frozenset({"api_key", "apikey", "credential", "password", "secret", "token"})


class GatewayConfigurationError(ValueError):
    pass


def load_gateway_config(path: Path | str) -> GatewayConfiguration:
    source = Path(path)
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise GatewayConfigurationError(f"cannot read gateway configuration: {error}") from error
    if not isinstance(raw, Mapping):
        raise GatewayConfigurationError("gateway configuration root must be an object")
    _reject_credentials(raw)
    _reject_unknown(raw, _ROOT_KEYS, "gateway configuration")
    schema_version = _text(raw, "schema_version")
    profiles_value = raw.get("profiles")
    if not isinstance(profiles_value, Sequence) or isinstance(profiles_value, str | bytes):
        raise GatewayConfigurationError("profiles must be an array")
    profiles = tuple(_profile(value, index) for index, value in enumerate(profiles_value))
    try:
        return GatewayConfiguration(schema_version, profiles)
    except ValueError as error:
        raise GatewayConfigurationError(str(error)) from error


def _profile(value: object, index: int) -> GatewayProfile:
    if not isinstance(value, Mapping):
        raise GatewayConfigurationError(f"profile {index} must be an object")
    mapping = cast("Mapping[object, object]", value)
    _reject_unknown(mapping, _PROFILE_KEYS, f"profile {index}")
    try:
        return GatewayProfile(
            profile_id=_text(mapping, "profile_id"),
            capability=ModelCapability(_text(mapping, "capability")),
            adapter_id=_text(mapping, "adapter_id"),
            model_id=_text(mapping, "model_id"),
            priority=_integer(mapping, "priority"),
            local=_boolean(mapping, "local"),
            deterministic=_boolean(mapping, "deterministic"),
            maximum_classification=DataClassification[
                _text(mapping, "maximum_classification").upper()
            ],
            max_input_tokens=_integer(mapping, "max_input_tokens"),
            max_output_tokens=_integer(mapping, "max_output_tokens"),
            max_cost_microusd=_integer(mapping, "max_cost_microusd"),
            enabled=_boolean(mapping, "enabled", default=True),
        )
    except (KeyError, ValueError) as error:
        raise GatewayConfigurationError(f"invalid profile {index}: {error}") from error


def _reject_credentials(value: object, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).casefold().replace("-", "_")
            if normalized in _CREDENTIAL_KEYS:
                raise GatewayConfigurationError(
                    f"credentials are not allowed in gateway configuration at {path}.{key}"
                )
            _reject_credentials(item, f"{path}.{key}")
    elif isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        for index, item in enumerate(value):
            _reject_credentials(item, f"{path}[{index}]")


def _reject_unknown(value: Mapping[object, object], allowed: frozenset[str], path: str) -> None:
    unknown = tuple(sorted(str(key) for key in value if key not in allowed))
    if unknown:
        raise GatewayConfigurationError(f"unknown fields in {path}: {unknown}")


def _text(value: Mapping[object, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise GatewayConfigurationError(f"{key} must be a non-empty string")
    return item


def _integer(value: Mapping[object, object], key: str) -> int:
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, int):
        raise GatewayConfigurationError(f"{key} must be an integer")
    return item


def _boolean(value: Mapping[object, object], key: str, *, default: bool | None = None) -> bool:
    item = value.get(key, default)
    if not isinstance(item, bool):
        raise GatewayConfigurationError(f"{key} must be a boolean")
    return item
