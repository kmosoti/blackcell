from __future__ import annotations

import json
import re
from pathlib import Path
from typing import cast

import msgspec
import pytest
from pydantic import ValidationError

import blackcell.interfaces.presentation as presentation
from blackcell.adapters.models import tooling_surface_catalog
from blackcell.interfaces.http import (
    AcceptanceCheck,
    CancelRunRequest,
    IntentRequest,
    NodeBudget,
    PlanNode,
    PlanRequest,
    ProjectRequest,
    RunQueryRequest,
    RunQueryResponse,
    RunRequest,
)
from blackcell.interfaces.presentation import (
    ACTION_BINDINGS,
    REQUEST_FIELD_DISPOSITIONS,
    KeyValueComponent,
    PresentationSurface,
    UiScenario,
    canonical_surface_bytes,
    export_a2ui,
    workspace_surface,
)
from blackcell.interfaces.presentation import models as presentation_models

ROOT = Path(__file__).parents[2]


def test_public_presentation_package_exports_every_contract_model() -> None:
    assert set(presentation_models.__all__) <= set(presentation.__all__)


def test_design_tokens_use_stable_typed_value_shapes() -> None:
    tokens = json.loads(
        (ROOT / "src" / "blackcell" / "interfaces" / "presentation" / "tokens.json").read_text()
    )

    assert "$schema" not in tokens
    for token in tokens["color"].values():
        assert token["$type"] == "color"
        value = token["$value"]
        assert value["colorSpace"] == "srgb"
        assert len(value["components"]) == 3
        assert all(0 <= component <= 1 for component in value["components"])
        assert re.fullmatch(r"#[0-9a-f]{6}", value["hex"])
    for group in ("space", "radius"):
        for token in tokens[group].values():
            assert token["$type"] == "dimension"
            assert isinstance(token["$value"]["value"], int | float)
            assert token["$value"]["unit"] in {"px", "rem"}


def test_every_canonical_request_field_has_one_explicit_ui_disposition() -> None:
    contracts = {
        "project-request/v1": ProjectRequest,
        "intent-request/v1": IntentRequest,
        "plan-request/v1": PlanRequest,
        "run-request/v1": RunRequest,
        "execution-cancel-run-request/v1": CancelRunRequest,
    }
    dispositions = {
        contract: {
            item.json_pointer for item in REQUEST_FIELD_DISPOSITIONS if item.contract == contract
        }
        for contract in contracts
    }

    for schema, contract in contracts.items():
        expected = {f"/{field.name}" for field in msgspec.structs.fields(contract)}
        assert dispositions[schema] == expected

    assert "/planning_mode" in dispositions["plan-request/v1"]
    assert ACTION_BINDINGS["accept-plan"].request_schema == "plan-request/v1"


def test_workspace_surface_is_deterministic_closed_and_a2ui_compatible() -> None:
    query = RunQueryResponse(
        query=RunQueryRequest(schema_version="run-query-request/v1"),
        scanned_events=0,
        runs=(),
        next_cursor=0,
        has_more=False,
    )
    surface = workspace_surface(query, tooling=tooling_surface_catalog())

    assert canonical_surface_bytes(surface) == canonical_surface_bytes(surface)
    assert PresentationSurface.model_validate_json(canonical_surface_bytes(surface)) == surface
    assert surface.revision.source_digest.startswith("sha256:")
    assert {component.kind for component in surface.components} >= {
        "form",
        "table",
        "metrics",
        "key-value",
    }
    exported = export_a2ui(surface)
    assert exported["surfaceId"] == "workspace"
    assert len(cast("list[object]", exported["components"])) == len(surface.components)

    invalid = surface.model_dump(mode="json")
    invalid["unexpected"] = True
    with pytest.raises(ValidationError, match="extra_forbidden"):
        PresentationSurface.model_validate_json(json.dumps(invalid))


def test_tooling_projection_preserves_every_codex_and_agy_leaf() -> None:
    tooling = tooling_surface_catalog()
    query = RunQueryResponse(
        query=RunQueryRequest(schema_version="run-query-request/v1"),
        scanned_events=0,
        runs=(),
        next_cursor=0,
        has_more=False,
    )
    surface = workspace_surface(query, tooling=tooling)
    components = {
        component.component_id: component
        for component in surface.components
        if isinstance(component, KeyValueComponent)
    }
    codex_keys = {item.key for item in components["tooling-codex"].items}
    agy_keys = {item.key for item in components["tooling-agy"].items}

    def leaves(value: object, prefix: str) -> set[str]:
        if isinstance(value, dict):
            mapping = cast("dict[str, object]", value)
            return {
                leaf
                for key, item in mapping.items()
                for leaf in leaves(item, f"{prefix}.{key}" if prefix else key)
            }
        if isinstance(value, list):
            return {
                leaf
                for index, item in enumerate(value)
                for leaf in leaves(item, f"{prefix}[{index}]")
            }
        return {prefix}

    payload = tooling.model_dump(mode="json")
    surfaces = cast("list[dict[str, object]]", payload["surfaces"])
    assert codex_keys == leaves(surfaces[0], "codex")
    assert agy_keys == leaves(surfaces[1], "agy")


def test_surface_rejects_dangling_section_and_graph_references() -> None:
    query = RunQueryResponse(
        query=RunQueryRequest(schema_version="run-query-request/v1"),
        scanned_events=0,
        runs=(),
        next_cursor=0,
        has_more=False,
    )
    invalid = workspace_surface(query).model_dump(mode="json")
    section = cast("dict[str, object]", invalid["components"][0])
    section["children"] = ["missing"]
    with pytest.raises(ValidationError, match="section children"):
        PresentationSurface.model_validate_json(json.dumps(invalid))


def test_nested_plan_contract_remains_representable_by_structured_field() -> None:
    plan = PlanRequest(
        schema_version="plan-request/v1",
        plan_id="plan",
        project_id="project",
        intent_id="intent",
        base_commit="a" * 40,
        allowed_effects=("repository-read", "process"),
        nodes=(
            PlanNode(
                node_id="node",
                objective="Inspect",
                depends_on=(),
                budget=NodeBudget(
                    max_input_tokens=0,
                    max_output_tokens=0,
                    timeout_seconds=30,
                    max_cost_microusd=0,
                    max_changed_files=0,
                ),
                effects=("repository-read", "process"),
                allowed_paths=(),
                checks=(AcceptanceCheck(check_id="check", argv=("python", "-V")),),
            ),
        ),
        idempotency_key="plan-key",
        planning_mode="declared",
    )
    nodes_field = next(
        field for field in ACTION_BINDINGS["accept-plan"].fields if field.json_pointer == "/nodes"
    )
    assert nodes_field.control == "structured-list"
    assert msgspec.json.decode(msgspec.json.encode(plan))["planning_mode"] == "declared"


def test_action_operation_and_request_schema_are_one_closed_pair() -> None:
    invalid = ACTION_BINDINGS["accept-plan"].model_dump()
    invalid["request_schema"] = "intent-request/v1"

    with pytest.raises(ValidationError, match="operation and request schema"):
        type(ACTION_BINDINGS["accept-plan"]).model_validate(invalid)


def test_shared_review_scenario_is_strict_and_semantically_self_consistent() -> None:
    scenario = UiScenario.model_validate_json(
        (ROOT / "tests" / "ui" / "review-workflow.json").read_bytes()
    )

    assert tuple(surface.surface_id for surface in scenario.surfaces) == (
        "workspace",
        "run:run-1",
    )
    assert {item.action_id for item in scenario.action_expectations} == {
        "accept-plan",
        "cancel-run",
    }
    assert {item.fault for item in scenario.fault_expectations} == {
        "authorization-failure",
        "oversized-response",
        "unknown-component",
    }
