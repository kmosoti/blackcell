"""Closed, renderer-neutral contracts for human and agent-readable runtime surfaces."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$")
_DIGEST_PATTERN = re.compile(r"^sha256:[a-f0-9]{64}$")
_MAX_COMPONENTS = 512
_MAX_ITEMS = 4_096
_MAX_TEXT = 32_768

type PresentationScalar = str | int | float | bool | None
type FieldDispositionName = Literal["editable", "displayed", "derived", "hidden"]
type PresentationOperation = Literal[
    "register-project",
    "accept-intent",
    "accept-plan",
    "submit-run",
    "inspect-run",
    "cancel-run",
]

_ACTION_REQUEST_SCHEMAS: Mapping[str, str] = {
    "register-project": "project-request/v1",
    "accept-intent": "intent-request/v1",
    "accept-plan": "plan-request/v1",
    "submit-run": "run-request/v1",
    "inspect-run": "run-lookup/v1",
    "cancel-run": "execution-cancel-run-request/v1",
}


class PresentationModel(BaseModel):
    """Strict immutable base shared by every presentation contract."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class SourceBinding(PresentationModel):
    kind: Literal["contract", "event", "run", "artifact", "tooling"]
    identity: Annotated[str, Field(min_length=1, max_length=240)]
    digest: Annotated[str, Field(pattern=r"^sha256:[a-f0-9]{64}$")]
    json_pointer: Annotated[str, Field(max_length=1_024)] | None = None


class SurfaceRevision(PresentationModel):
    number: Annotated[int, Field(ge=0)]
    event_cursor: Annotated[int, Field(ge=0)]
    source_digest: Annotated[str, Field(pattern=r"^sha256:[a-f0-9]{64}$")]


class FieldDisposition(PresentationModel):
    contract: Annotated[str, Field(min_length=1, max_length=120)]
    json_pointer: Annotated[str, Field(min_length=1, max_length=1_024)]
    disposition: FieldDispositionName
    reason: Annotated[str, Field(min_length=1, max_length=500)]


class FieldOption(PresentationModel):
    value: Annotated[str, Field(max_length=2_048)]
    label: Annotated[str, Field(min_length=1, max_length=240)]


class FieldBinding(PresentationModel):
    field_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$")]
    json_pointer: Annotated[str, Field(min_length=1, max_length=1_024)]
    label: Annotated[str, Field(min_length=1, max_length=240)]
    help: Annotated[str, Field(max_length=1_000)] = ""
    control: Literal[
        "text",
        "textarea",
        "number",
        "checkbox",
        "select",
        "string-list",
        "structured-list",
    ]
    required: bool = True
    sensitive: bool = False
    default: JsonValue = None
    options: tuple[FieldOption, ...] = Field(default=(), max_length=64)

    @model_validator(mode="after")
    def validate_options(self) -> FieldBinding:
        if (self.control == "select") != bool(self.options):
            raise ValueError("select fields require options and other fields forbid them")
        return self


class ActionBinding(PresentationModel):
    action_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$")]
    operation: PresentationOperation
    label: Annotated[str, Field(min_length=1, max_length=120)]
    request_schema: Annotated[str, Field(min_length=1, max_length=120)]
    fields: tuple[FieldBinding, ...] = Field(default=(), max_length=128)
    confirmation: Annotated[str, Field(max_length=500)] | None = None

    @model_validator(mode="after")
    def validate_fields(self) -> ActionBinding:
        field_ids = tuple(field.field_id for field in self.fields)
        pointers = tuple(field.json_pointer for field in self.fields)
        if len(field_ids) != len(set(field_ids)) or len(pointers) != len(set(pointers)):
            raise ValueError("action fields must have unique IDs and pointers")
        if self.request_schema != _ACTION_REQUEST_SCHEMAS[self.operation]:
            raise ValueError("action operation and request schema must agree")
        return self


class ComponentBase(PresentationModel):
    component_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$")]
    label: Annotated[str, Field(min_length=1, max_length=240)]
    source: SourceBinding | None = None


class SectionComponent(ComponentBase):
    kind: Literal["section"] = "section"
    description: Annotated[str, Field(max_length=2_000)] = ""
    children: tuple[Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$")], ...] = (
        Field(default=(), max_length=128)
    )


class StatusComponent(ComponentBase):
    kind: Literal["status"] = "status"
    value: Annotated[str, Field(min_length=1, max_length=120)]
    tone: Literal["neutral", "info", "success", "warning", "danger"] = "neutral"
    detail: Annotated[str, Field(max_length=2_000)] = ""


class Metric(PresentationModel):
    label: Annotated[str, Field(min_length=1, max_length=120)]
    value: PresentationScalar
    unit: Annotated[str, Field(max_length=40)] = ""


class MetricComponent(ComponentBase):
    kind: Literal["metrics"] = "metrics"
    metrics: tuple[Metric, ...] = Field(min_length=1, max_length=32)


class KeyValueItem(PresentationModel):
    key: Annotated[str, Field(min_length=1, max_length=240)]
    value: PresentationScalar
    provenance: Annotated[str, Field(max_length=500)] = ""


class KeyValueComponent(ComponentBase):
    kind: Literal["key-value"] = "key-value"
    items: tuple[KeyValueItem, ...] = Field(max_length=_MAX_ITEMS)


class TableColumn(PresentationModel):
    key: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$")]
    label: Annotated[str, Field(min_length=1, max_length=240)]


class TableRow(PresentationModel):
    row_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$")]
    cells: dict[str, PresentationScalar] = Field(max_length=64)


class TableComponent(ComponentBase):
    kind: Literal["table"] = "table"
    columns: tuple[TableColumn, ...] = Field(min_length=1, max_length=64)
    rows: tuple[TableRow, ...] = Field(max_length=_MAX_ITEMS)
    empty_message: Annotated[str, Field(min_length=1, max_length=500)] = "No records."

    @model_validator(mode="after")
    def validate_rows(self) -> TableComponent:
        keys = tuple(column.key for column in self.columns)
        if len(keys) != len(set(keys)):
            raise ValueError("table columns must be unique")
        if any(set(row.cells) != set(keys) for row in self.rows):
            raise ValueError("every table row must exactly match the columns")
        row_ids = tuple(row.row_id for row in self.rows)
        if len(row_ids) != len(set(row_ids)):
            raise ValueError("table row IDs must be unique")
        return self


class FormComponent(ComponentBase):
    kind: Literal["form"] = "form"
    description: Annotated[str, Field(max_length=2_000)] = ""
    action: ActionBinding


class GraphNode(PresentationModel):
    node_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$")]
    label: Annotated[str, Field(min_length=1, max_length=240)]
    status: Annotated[str, Field(max_length=120)] = ""
    detail: Annotated[str, Field(max_length=2_000)] = ""


class GraphEdge(PresentationModel):
    source_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$")]
    target_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$")]
    label: Annotated[str, Field(max_length=120)] = "depends on"


class PlanGraphComponent(ComponentBase):
    kind: Literal["plan-graph"] = "plan-graph"
    nodes: tuple[GraphNode, ...] = Field(max_length=64)
    edges: tuple[GraphEdge, ...] = Field(max_length=4_096)

    @model_validator(mode="after")
    def validate_graph(self) -> PlanGraphComponent:
        identifiers = tuple(node.node_id for node in self.nodes)
        known = set(identifiers)
        if len(identifiers) != len(known):
            raise ValueError("graph node IDs must be unique")
        if any(edge.source_id not in known or edge.target_id not in known for edge in self.edges):
            raise ValueError("graph edges must reference known nodes")
        return self


class TimelineItem(PresentationModel):
    item_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$")]
    title: Annotated[str, Field(min_length=1, max_length=240)]
    detail: Annotated[str, Field(max_length=2_000)] = ""
    cursor: Annotated[int, Field(ge=0)] | None = None
    status: Annotated[str, Field(max_length=120)] = ""


class TimelineComponent(ComponentBase):
    kind: Literal["timeline"] = "timeline"
    items: tuple[TimelineItem, ...] = Field(max_length=_MAX_ITEMS)


class Finding(PresentationModel):
    finding_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$")]
    severity: Literal["P1", "P2", "P3", "info"]
    summary: Annotated[str, Field(min_length=1, max_length=2_000)]
    evidence: Annotated[str, Field(max_length=2_000)] = ""


class FindingListComponent(ComponentBase):
    kind: Literal["findings"] = "findings"
    findings: tuple[Finding, ...] = Field(max_length=_MAX_ITEMS)


class EvidenceRow(PresentationModel):
    row_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$")]
    dimension: Annotated[str, Field(min_length=1, max_length=240)]
    disposition: Literal["supported", "concern", "unknown", "not-applicable"]
    evidence: Annotated[str, Field(max_length=2_000)]
    source_digest: Annotated[str, Field(pattern=r"^sha256:[a-f0-9]{64}$")]


class EvidenceMatrixComponent(ComponentBase):
    kind: Literal["evidence-matrix"] = "evidence-matrix"
    rows: tuple[EvidenceRow, ...] = Field(max_length=256)


class ArtifactItem(PresentationModel):
    digest: Annotated[str, Field(pattern=r"^sha256:[a-f0-9]{64}$")]
    role: Annotated[str, Field(min_length=1, max_length=120)]
    node_id: Annotated[str, Field(min_length=1, max_length=120)]
    media_type: Annotated[str, Field(min_length=1, max_length=240)]
    size_bytes: Annotated[int, Field(ge=0)]
    verified: bool


class ArtifactComponent(ComponentBase):
    kind: Literal["artifacts"] = "artifacts"
    run_id: Annotated[str, Field(min_length=1, max_length=120)]
    items: tuple[ArtifactItem, ...] = Field(max_length=_MAX_ITEMS)


class SourceComponent(ComponentBase):
    kind: Literal["source"] = "source"
    operation: Literal["inspect-run", "replay-run"]
    subject_id: Annotated[str, Field(min_length=1, max_length=120)]
    summary: Annotated[str, Field(max_length=_MAX_TEXT)]


PresentationComponent = Annotated[
    SectionComponent
    | StatusComponent
    | MetricComponent
    | KeyValueComponent
    | TableComponent
    | FormComponent
    | PlanGraphComponent
    | TimelineComponent
    | FindingListComponent
    | EvidenceMatrixComponent
    | ArtifactComponent
    | SourceComponent,
    Field(discriminator="kind"),
]


class PresentationSurface(PresentationModel):
    schema_version: Literal["presentation-surface/v1"] = "presentation-surface/v1"
    surface_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,123}$")]
    title: Annotated[str, Field(min_length=1, max_length=240)]
    revision: SurfaceRevision
    components: tuple[PresentationComponent, ...] = Field(max_length=_MAX_COMPONENTS)
    field_dispositions: tuple[FieldDisposition, ...] = Field(max_length=512)

    @model_validator(mode="after")
    def validate_surface(self) -> PresentationSurface:
        if len(self.surface_id) > 120:
            run_id = self.surface_id.removeprefix("run:")
            if run_id == self.surface_id or _ID_PATTERN.fullmatch(run_id) is None:
                raise ValueError("long surface IDs must reserve a canonical run prefix")
        component_ids = tuple(component.component_id for component in self.components)
        if len(component_ids) != len(set(component_ids)):
            raise ValueError("surface component IDs must be unique")
        known = set(component_ids)
        for component in self.components:
            if isinstance(component, SectionComponent):
                if len(component.children) != len(set(component.children)):
                    raise ValueError("section children must be unique")
                if component.component_id in component.children or any(
                    child not in known for child in component.children
                ):
                    raise ValueError("section children must reference other known components")
        sections = {
            component.component_id: component.children
            for component in self.components
            if isinstance(component, SectionComponent)
        }
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(component_id: str) -> None:
            if component_id in visiting:
                raise ValueError("section references must be acyclic")
            if component_id in visited:
                return
            visiting.add(component_id)
            for child_id in sections.get(component_id, ()):
                visit(child_id)
            visiting.remove(component_id)
            visited.add(component_id)

        for component_id in component_ids:
            visit(component_id)
        action_ids = tuple(
            component.action.action_id
            for component in self.components
            if isinstance(component, FormComponent)
        )
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("surface action IDs must be unique")
        dispositions = tuple((item.contract, item.json_pointer) for item in self.field_dispositions)
        if len(dispositions) != len(set(dispositions)):
            raise ValueError("field dispositions must be unique")
        return self


class SurfaceProjector(Protocol):
    def workspace(self) -> PresentationSurface: ...

    def run(self, run_id: str) -> PresentationSurface: ...


def canonical_surface_bytes(surface: PresentationSurface) -> bytes:
    """Encode a surface deterministically for ETags, fixtures, and cross-client parity."""

    return json.dumps(
        surface.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def source_digest(value: object) -> str:
    """Digest JSON-compatible host evidence without accepting renderer-owned state."""

    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def flatten_mapping(
    value: Mapping[str, JsonValue], *, prefix: str = ""
) -> tuple[KeyValueItem, ...]:
    """Flatten every tooling leaf into a stable, lossless human-readable row."""

    rows: list[KeyValueItem] = []

    def visit(item: JsonValue, path: str) -> None:
        if isinstance(item, dict):
            for key in sorted(item):
                visit(item[key], f"{path}.{key}" if path else key)
        elif isinstance(item, list):
            for index, child in enumerate(item):
                visit(child, f"{path}[{index}]")
        else:
            rows.append(KeyValueItem(key=path, value=item))

    visit(dict(value), prefix)
    return tuple(rows)


__all__ = [
    "ActionBinding",
    "ArtifactComponent",
    "ArtifactItem",
    "EvidenceMatrixComponent",
    "EvidenceRow",
    "FieldBinding",
    "FieldDisposition",
    "FieldDispositionName",
    "FieldOption",
    "Finding",
    "FindingListComponent",
    "FormComponent",
    "GraphEdge",
    "GraphNode",
    "KeyValueComponent",
    "KeyValueItem",
    "Metric",
    "MetricComponent",
    "PlanGraphComponent",
    "PresentationComponent",
    "PresentationModel",
    "PresentationOperation",
    "PresentationScalar",
    "PresentationSurface",
    "SectionComponent",
    "SourceBinding",
    "SourceComponent",
    "StatusComponent",
    "SurfaceProjector",
    "SurfaceRevision",
    "TableColumn",
    "TableComponent",
    "TableRow",
    "TimelineComponent",
    "TimelineItem",
    "canonical_surface_bytes",
    "flatten_mapping",
    "source_digest",
]
