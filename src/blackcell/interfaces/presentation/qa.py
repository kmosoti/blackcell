"""Strict shared scenarios for browser and terminal semantic verification."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, JsonValue, model_validator

from blackcell.interfaces.presentation.models import (
    FormComponent,
    PresentationModel,
    PresentationSurface,
)


class SemanticComponentExpectation(PresentationModel):
    component_id: Annotated[str, Field(min_length=1, max_length=120)]
    kind: Annotated[str, Field(min_length=1, max_length=120)]
    label: Annotated[str, Field(min_length=1, max_length=240)]
    source_digest: Annotated[str, Field(pattern=r"^sha256:[a-f0-9]{64}$")] | None
    action: Annotated[str, Field(min_length=1, max_length=120)] | None


class SurfaceExpectation(PresentationModel):
    surface_id: Annotated[str, Field(min_length=1, max_length=120)]
    manifest: tuple[SemanticComponentExpectation, ...]


class ActionFidelityExpectation(PresentationModel):
    surface_id: Annotated[str, Field(min_length=1, max_length=120)]
    action_id: Annotated[str, Field(min_length=1, max_length=120)]
    submitted_values: dict[str, JsonValue]
    expected_request_subset: dict[str, JsonValue]


class AccessibilityExpectation(PresentationModel):
    surface_id: Annotated[str, Field(min_length=1, max_length=120)]
    role: Literal[
        "button",
        "figure",
        "form",
        "heading",
        "main",
        "navigation",
        "status",
        "table",
    ]
    name: Annotated[str, Field(min_length=1, max_length=240)]


class FaultExpectation(PresentationModel):
    fault: Literal[
        "authorization-failure",
        "oversized-response",
        "unknown-component",
    ]
    expected_code: Annotated[str, Field(pattern=r"^[a-z0-9-]{1,100}$")]


class UiScenario(PresentationModel):
    """Synthetic scenario shared by projection, browser, and terminal gates."""

    schema_version: Literal["ui-scenario/v1"] = "ui-scenario/v1"
    scenario_id: Annotated[str, Field(min_length=1, max_length=120)]
    title: Annotated[str, Field(min_length=1, max_length=240)]
    surfaces: tuple[PresentationSurface, ...] = Field(min_length=1, max_length=8)
    surface_expectations: tuple[SurfaceExpectation, ...] = Field(min_length=1, max_length=8)
    action_expectations: tuple[ActionFidelityExpectation, ...] = Field(max_length=16)
    accessibility_expectations: tuple[AccessibilityExpectation, ...] = Field(max_length=32)
    fault_expectations: tuple[FaultExpectation, ...] = Field(max_length=16)

    @model_validator(mode="after")
    def validate_scenario(self) -> UiScenario:
        by_id = {surface.surface_id: surface for surface in self.surfaces}
        if len(by_id) != len(self.surfaces):
            raise ValueError("scenario surface IDs must be unique")
        expectations = {item.surface_id: item for item in self.surface_expectations}
        if set(expectations) != set(by_id) or len(expectations) != len(self.surface_expectations):
            raise ValueError("every scenario surface requires one semantic expectation")
        for surface_id, expected in expectations.items():
            if expected.manifest != semantic_manifest(by_id[surface_id]):
                raise ValueError("scenario semantic manifest does not match its surface")
        for expectation in self.action_expectations:
            surface = by_id.get(expectation.surface_id)
            if surface is None:
                raise ValueError("action expectation references an unknown surface")
            actions = {
                component.action.action_id: component.action
                for component in surface.components
                if isinstance(component, FormComponent)
            }
            action = actions.get(expectation.action_id)
            if action is None:
                raise ValueError("action expectation references an unknown action")
            pointers = {field.json_pointer.removeprefix("/") for field in action.fields}
            if not set(expectation.submitted_values).issubset(pointers):
                raise ValueError("action expectation submits an undeclared field")
            schema = expectation.expected_request_subset.get("schema_version")
            if schema != action.request_schema:
                raise ValueError("action expectation uses the wrong request schema")
            allowed_request_fields = {"schema_version", *pointers}
            if action.operation == "cancel-run":
                allowed_request_fields.add("idempotency_key")
            if not set(expectation.expected_request_subset).issubset(allowed_request_fields):
                raise ValueError("action expectation checks an undeclared request field")
            if any(
                expectation.expected_request_subset.get(key) != value
                for key, value in expectation.submitted_values.items()
            ):
                raise ValueError("action expectation changes a submitted value")
        if any(item.surface_id not in by_id for item in self.accessibility_expectations):
            raise ValueError("accessibility expectation references an unknown surface")
        return self


def semantic_manifest(
    surface: PresentationSurface,
) -> tuple[SemanticComponentExpectation, ...]:
    """Return renderer-independent meaning for exact cross-surface comparison."""

    return tuple(
        SemanticComponentExpectation(
            component_id=component.component_id,
            kind=component.kind,
            label=component.label,
            source_digest=None if component.source is None else component.source.digest,
            action=(component.action.operation if isinstance(component, FormComponent) else None),
        )
        for component in surface.components
    )


__all__ = [
    "AccessibilityExpectation",
    "ActionFidelityExpectation",
    "FaultExpectation",
    "SemanticComponentExpectation",
    "SurfaceExpectation",
    "UiScenario",
    "semantic_manifest",
]
