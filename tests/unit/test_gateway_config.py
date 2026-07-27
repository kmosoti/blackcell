from dataclasses import replace
from pathlib import Path

import pytest

from blackcell.adapters.config import GatewayConfigurationError, load_gateway_config
from blackcell.gateway import (
    DataClassification,
    GatewayConfiguration,
    ModelCapability,
)


def test_local_first_example_loads_blackcell_owned_profiles() -> None:
    config = load_gateway_config("examples/gateway/local-first.yaml")

    assert config.schema_version == "gateway-config/v1"
    assert tuple(profile.capability for profile in config.profiles) == (
        ModelCapability.REASON,
        ModelCapability.EMBED,
        ModelCapability.REVIEW,
    )
    assert config.profiles[0].maximum_classification == DataClassification.SECRET
    assert config.profiles[2].maximum_classification == DataClassification.INTERNAL


def test_gateway_config_rejects_credentials_and_unknown_fields(tmp_path: Path) -> None:
    credentials = tmp_path / "credentials.yaml"
    credentials.write_text(
        "schema_version: gateway-config/v1\nprofiles: []\napi_key: forbidden\n",
        encoding="utf-8",
    )
    with pytest.raises(GatewayConfigurationError, match="credentials"):
        load_gateway_config(credentials)

    unknown = tmp_path / "unknown.yaml"
    unknown.write_text(
        "schema_version: gateway-config/v1\nprofiles: []\nprovider: direct\n",
        encoding="utf-8",
    )
    with pytest.raises(GatewayConfigurationError, match="unknown fields"):
        load_gateway_config(unknown)


def test_gateway_config_rejects_unsupported_versions(tmp_path: Path) -> None:
    path = tmp_path / "future.yaml"
    path.write_text("schema_version: gateway-config/v99\nprofiles: []\n", encoding="utf-8")

    with pytest.raises(GatewayConfigurationError, match="unsupported"):
        load_gateway_config(path)


def test_gateway_capability_profiles_reject_ambiguous_authority() -> None:
    loaded = load_gateway_config("examples/gateway/local-first.yaml")
    profile = loaded.profiles[0]

    with pytest.raises(ValueError, match="at least one profile"):
        GatewayConfiguration(loaded.schema_version, ())
    with pytest.raises(ValueError, match="must be unique"):
        GatewayConfiguration(loaded.schema_version, (profile, profile))
    for field in ("profile_id", "adapter_id", "model_id"):
        with pytest.raises(ValueError, match=field):
            replace(profile, **{field: " "})
    with pytest.raises(ValueError, match="non-negative"):
        replace(profile, max_input_tokens=-1)
