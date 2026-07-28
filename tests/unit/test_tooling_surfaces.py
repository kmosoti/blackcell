from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from blackcell.adapters.models import (
    AGY_CLI_REQUIRED_VERSION,
    AgyCliModelAdapter,
    CodexCliModelAdapter,
    tooling_surface_catalog,
)
from blackcell.adapters.models.agy_cli import _command as agy_command
from blackcell.adapters.models.codex_cli import _command as codex_command
from blackcell.cli.app import app
from blackcell.gateway import (
    AgyCliToolingSurface,
    CodexCliToolingSurface,
    ToolingSurface,
    ToolingSurfaceCatalog,
    ToolingSurfaceProvider,
)
from tests.cli_runner import CycloptsCliRunner


def test_cli_tooling_catalog_is_closed_discriminated_and_round_trips() -> None:
    catalog = tooling_surface_catalog()

    assert tuple(item.tool for item in catalog.surfaces) == ("codex-cli", "agy-cli")
    assert isinstance(catalog.surfaces[0], CodexCliToolingSurface)
    assert isinstance(catalog.surfaces[1], AgyCliToolingSurface)
    assert ToolingSurfaceCatalog.model_validate_json(catalog.model_dump_json()) == catalog
    assert {item.facet for item in catalog.differences} == {
        "authority-flags",
        "configuration-and-session",
        "effort-and-provider-timeout",
        "output-schema",
        "response-transport",
        "usage-accounting",
        "version-policy",
    }

    invalid = catalog.model_dump(mode="json")
    invalid["surfaces"][0]["unexpected"] = True
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ToolingSurfaceCatalog.model_validate_json(json.dumps(invalid))

    mismatched = catalog.model_dump(mode="json")
    mismatched["surfaces"][0]["adapter_id"] = "agy-cli"
    with pytest.raises(ValidationError, match="literal_error"):
        ToolingSurfaceCatalog.model_validate_json(json.dumps(mismatched))


def test_tooling_surface_provider_contract_remains_discriminated() -> None:
    schema = TypeAdapter(ToolingSurface).json_schema()

    assert schema["discriminator"] == {
        "mapping": {
            "agy-cli": "#/$defs/AgyCliToolingSurface",
            "codex-cli": "#/$defs/CodexCliToolingSurface",
        },
        "propertyName": "tool",
    }


@pytest.mark.parametrize(
    "surface_tools",
    [
        (),
        ("codex-cli", "codex-cli"),
        ("agy-cli", "codex-cli"),
        ("codex-cli", "agy-cli", "codex-cli"),
    ],
)
def test_cli_tooling_catalog_rejects_invalid_surface_sequences(
    surface_tools: tuple[str, ...],
) -> None:
    payload = tooling_surface_catalog().model_dump(mode="json")
    surfaces_by_tool = {item["tool"]: item for item in payload["surfaces"]}
    payload["surfaces"] = [surfaces_by_tool[tool] for tool in surface_tools]

    with pytest.raises(ValidationError):
        ToolingSurfaceCatalog.model_validate_json(json.dumps(payload))


def test_codex_and_agy_surfaces_expose_shared_and_distinct_authority() -> None:
    codex_adapter = CodexCliModelAdapter(
        timeout_ceiling_seconds=45,
        max_input_bytes=1000,
        max_stdout_bytes=2000,
        max_stderr_bytes=3000,
        max_response_bytes=4000,
    )
    agy_adapter = AgyCliModelAdapter(
        expected_version=AGY_CLI_REQUIRED_VERSION,
        effort="medium",
        timeout_ceiling_seconds=30,
        max_input_bytes=5000,
        max_stdout_bytes=6000,
        max_stderr_bytes=7000,
    )

    assert isinstance(codex_adapter, ToolingSurfaceProvider)
    assert isinstance(agy_adapter, ToolingSurfaceProvider)
    codex = codex_adapter.tooling_surface
    agy = agy_adapter.tooling_surface

    assert codex.prompt.transport == agy.prompt.transport == "stdin"
    assert not codex.prompt.request_content_in_arguments
    assert not agy.prompt.request_content_in_arguments
    assert codex.invocation.working_directory == agy.invocation.working_directory
    assert codex.authority.credentials == agy.authority.credentials == "provider-owned"
    assert codex.structured_output.host_schema_enforcement
    assert agy.structured_output.host_schema_enforcement
    assert codex.structured_output.provider_schema_enforcement
    assert not agy.structured_output.provider_schema_enforcement
    assert codex.accounting.input_tokens == "exact-provider-events"
    assert agy.accounting.input_tokens == "unavailable"
    assert codex.session.persistence == "disabled"
    assert agy.session.persistence == "provider-owned-not-resumed"
    assert codex.version.preflight == "none"
    assert agy.version.required_version == AGY_CLI_REQUIRED_VERSION
    assert agy.configured_effort == "medium"
    assert codex.budgets.max_response_bytes == 4000
    assert agy.budgets.max_response_bytes == 6000
    assert "--ignore-user-config" in codex.invocation.argument_template
    assert "--print-timeout" in agy.invocation.argument_template
    assert "--print" not in agy.invocation.argument_template
    assert codex.invocation.argument_template == tuple(
        codex_command(
            "<codex-executable>",
            Path("<temporary-git-repository>"),
            Path("<private-schema-file>"),
            Path("<private-response-file>"),
            "<model-id>",
        )
    )
    expected_agy = agy_command(
        "<agy-executable>",
        model_id="<model-id>",
        effort="medium",
        timeout_seconds=1,
    )
    expected_agy[-1] = "<remaining-ceiling-seconds>s"
    assert agy.invocation.argument_template == tuple(expected_agy)


def test_adapter_inspection_cli_emits_catalog_and_schema_without_invocation() -> None:
    runner = CycloptsCliRunner()

    inspected = runner.invoke(app, ["adapters", "inspect"], catch_exceptions=False)
    schema = runner.invoke(app, ["adapters", "schema"], catch_exceptions=False)

    assert inspected.exit_code == schema.exit_code == 0
    catalog = ToolingSurfaceCatalog.model_validate_json(inspected.stdout)
    schema_payload = json.loads(schema.stdout)
    assert tuple(item.adapter_id for item in catalog.surfaces) == ("codex-cli", "agy-cli")
    assert schema_payload["additionalProperties"] is False
    surfaces_schema = schema_payload["properties"]["surfaces"]
    assert surfaces_schema["type"] == "array"
    assert surfaces_schema["minItems"] == surfaces_schema["maxItems"] == 2
    assert surfaces_schema["prefixItems"] == [
        {"$ref": "#/$defs/CodexCliToolingSurface"},
        {"$ref": "#/$defs/AgyCliToolingSurface"},
    ]
