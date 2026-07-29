"""Pure host-owned projection from canonical runtime contracts to semantic surfaces."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, cast

from pydantic import JsonValue

from blackcell.gateway import ToolingSurfaceCatalog
from blackcell.interfaces.http.contracts import (
    ReplayResponse,
    RunQueryItem,
    RunSurfaceWindow,
    contract_to_json_builtins,
)
from blackcell.interfaces.presentation.fields import (
    ACTION_BINDINGS,
    REQUEST_FIELD_DISPOSITIONS,
)
from blackcell.interfaces.presentation.models import (
    ActionBinding,
    ArtifactComponent,
    ArtifactItem,
    EvidenceMatrixComponent,
    EvidenceRow,
    Finding,
    FindingListComponent,
    FormComponent,
    GraphEdge,
    GraphNode,
    KeyValueComponent,
    KeyValueItem,
    Metric,
    MetricComponent,
    PlanGraphComponent,
    PresentationComponent,
    PresentationSurface,
    SectionComponent,
    SourceBinding,
    SourceComponent,
    StatusComponent,
    SurfaceRevision,
    TableColumn,
    TableComponent,
    TableRow,
    TimelineComponent,
    TimelineItem,
    flatten_mapping,
    source_digest,
)


def workspace_surface(
    runs: RunSurfaceWindow,
    *,
    tooling: ToolingSurfaceCatalog | None = None,
) -> PresentationSurface:
    source_value: dict[str, object] = {
        "runs": contract_to_json_builtins(runs),
        "tooling": None if tooling is None else tooling.model_dump(mode="json"),
    }
    digest = source_digest(source_value)
    source = SourceBinding(kind="run", identity="workspace", digest=digest)
    components: list[PresentationComponent] = [
        SectionComponent(
            component_id="workspace-actions",
            label="Advance a project workflow",
            description="Typed actions are translated to canonical daemon requests.",
            children=tuple(f"form-{operation}" for operation in ACTION_BINDINGS),
            source=source,
        ),
        *(
            FormComponent(
                component_id=f"form-{operation}",
                label=binding.label,
                description=_action_description(binding),
                action=binding,
                source=source,
            )
            for operation, binding in ACTION_BINDINGS.items()
        ),
        SectionComponent(
            component_id="workspace-runs",
            label="Recent runs",
            children=("run-summary", "run-table"),
            source=source,
        ),
        MetricComponent(
            component_id="run-summary",
            label="Run summary",
            metrics=(
                Metric(label="Returned", value=len(runs.runs)),
                Metric(label="Scanned events", value=runs.scanned_events),
                Metric(label="Event cursor", value=runs.event_cursor),
            ),
            source=source,
        ),
        _run_table(runs.runs, source),
    ]
    if tooling is not None:
        components.extend(
            _tooling_components(tooling, source_digest(tooling.model_dump(mode="json")))
        )
    return PresentationSurface(
        surface_id="workspace",
        title="BlackCell project runtime",
        revision=SurfaceRevision(
            number=runs.event_cursor,
            event_cursor=runs.event_cursor,
            source_digest=digest,
        ),
        components=tuple(components),
        field_dispositions=REQUEST_FIELD_DISPOSITIONS,
    )


def run_surface(replay: ReplayResponse, run_item: RunQueryItem | None) -> PresentationSurface:
    replay_value = cast("Mapping[str, JsonValue]", contract_to_json_builtins(replay))
    digest = source_digest(replay_value)
    source = SourceBinding(kind="run", identity=replay.run_id, digest=digest)
    status_tone = cast(
        "Literal['neutral', 'info', 'success', 'warning', 'danger']",
        {
            "succeeded": "success",
            "failed": "danger",
            "canceled": "warning",
            "reconciliation-required": "danger",
            "canceling": "warning",
            "running": "info",
            "queued": "neutral",
        }[replay.run.status],
    )
    node_statuses = (
        {} if run_item is None else {node.node_id: node.status for node in run_item.nodes}
    )
    graph_nodes = tuple(
        GraphNode(
            node_id=node.node_id,
            label=node.objective,
            status=node_statuses.get(node.node_id, "pending"),
            detail=", ".join(node.effects),
        )
        for node in replay.plan.nodes
    )
    graph_edges = tuple(
        GraphEdge(source_id=dependency, target_id=node.node_id)
        for node in replay.plan.nodes
        for dependency in node.depends_on
    )
    timeline = tuple(
        TimelineItem(
            item_id=f"node-{index}-{node.node_id}",
            title=node.node_id,
            detail=node.objective,
            status=node_statuses.get(node.node_id, "pending"),
        )
        for index, node in enumerate(replay.plan.nodes)
    )
    findings = tuple(
        Finding(
            finding_id=f"finding-{index}",
            severity="P1" if "integrity" in finding.code else "P2",
            summary=finding.code,
            evidence=finding.artifact_digest or finding.node_id or "No artifact binding supplied.",
        )
        for index, finding in enumerate(replay.findings)
    )
    evidence_disposition = _verification_disposition(replay)
    components: tuple[PresentationComponent, ...] = (
        SectionComponent(
            component_id="run-overview",
            label=f"Run {replay.run_id}",
            children=("run-status", "run-metrics", "run-identities"),
            source=source,
        ),
        StatusComponent(
            component_id="run-status",
            label="Run status",
            value=replay.run.status,
            tone=status_tone,
            detail=(
                f"Active node: {replay.run.active_node_id}"
                if replay.run.active_node_id is not None
                else "No active node."
            ),
            source=source,
        ),
        MetricComponent(
            component_id="run-metrics",
            label="Run evidence",
            metrics=(
                Metric(label="Processed events", value=replay.processed_events),
                Metric(label="Artifacts", value=len(replay.artifacts)),
                Metric(label="Attempt", value=replay.run.attempt),
                Metric(label="Cursor", value=replay.run.cursor),
            ),
            source=source,
        ),
        KeyValueComponent(
            component_id="run-identities",
            label="Canonical identities",
            items=(
                KeyValueItem(key="project", value=replay.project.project_id),
                KeyValueItem(key="intent", value=replay.intent.intent_id),
                KeyValueItem(key="plan", value=replay.plan.plan_id),
                KeyValueItem(key="base commit", value=replay.plan.base_commit),
                KeyValueItem(key="state digest", value=replay.state_digest),
            ),
            source=source,
        ),
        SectionComponent(
            component_id="run-plan",
            label="Dependency-safe plan",
            description="The SVG renderer must retain the adjacent table representation.",
            children=("plan-graph", "plan-table", "plan-timeline"),
            source=source,
        ),
        PlanGraphComponent(
            component_id="plan-graph",
            label="Plan graph",
            nodes=graph_nodes,
            edges=graph_edges,
            source=source,
        ),
        TableComponent(
            component_id="plan-table",
            label="Plan graph as a table",
            columns=(
                TableColumn(key="node", label="Node"),
                TableColumn(key="objective", label="Objective"),
                TableColumn(key="depends_on", label="Depends on"),
                TableColumn(key="status", label="Status"),
            ),
            rows=tuple(
                TableRow(
                    row_id=f"plan-row-{index}",
                    cells={
                        "node": node.node_id,
                        "objective": node.objective,
                        "depends_on": ", ".join(node.depends_on) or "—",
                        "status": node_statuses.get(node.node_id, "pending"),
                    },
                )
                for index, node in enumerate(replay.plan.nodes)
            ),
            source=source,
        ),
        TimelineComponent(
            component_id="plan-timeline",
            label="Plan progress",
            items=timeline,
            source=source,
        ),
        FindingListComponent(
            component_id="run-findings",
            label="Replay findings",
            findings=findings,
            source=source,
        ),
        EvidenceMatrixComponent(
            component_id="run-evidence",
            label="Verification evidence",
            rows=(
                EvidenceRow(
                    row_id="verification",
                    dimension="deterministic verification",
                    disposition=evidence_disposition,
                    evidence=(
                        replay.verification.verdict
                        or replay.verification.finding_code
                        or replay.verification.lifecycle_status
                    ),
                    source_digest=replay.verification.evidence_digest,
                ),
                EvidenceRow(
                    row_id="artifact-integrity",
                    dimension="artifact integrity",
                    disposition=(
                        "supported"
                        if replay.artifact_integrity in {"verified", "not-applicable"}
                        else "concern"
                        if replay.artifact_integrity == "failed"
                        else "unknown"
                    ),
                    evidence=replay.artifact_integrity,
                    source_digest=replay.artifact_evidence_digest,
                ),
            ),
            source=source,
        ),
        ArtifactComponent(
            component_id="run-artifacts",
            label="Verified artifacts",
            run_id=replay.run_id,
            items=tuple(
                ArtifactItem(
                    digest=artifact.digest,
                    role=artifact.role,
                    node_id=artifact.node_id,
                    media_type=artifact.media_type,
                    size_bytes=artifact.size_bytes,
                    verified=artifact.verified,
                )
                for artifact in replay.artifacts
            ),
            source=source,
        ),
        FormComponent(
            component_id="form-cancel-run",
            label="Cancel run",
            description="Request cooperative cancellation for this run.",
            action=ActionBinding(
                action_id="cancel-run",
                operation="cancel-run",
                label="Cancel run",
                request_schema="execution-cancel-run-request/v1",
                confirmation="Cancel this run? Running work may retain its worktree.",
            ),
            source=source,
        ),
        SourceComponent(
            component_id="run-source",
            label="Canonical replay source",
            operation="replay-run",
            subject_id=replay.run_id,
            summary="Fetch and disclose the canonical replay contract on explicit request.",
            source=source,
        ),
    )
    return PresentationSurface(
        surface_id=f"run:{replay.run_id}",
        title=f"Run {replay.run_id}",
        revision=SurfaceRevision(
            number=replay.run.cursor,
            event_cursor=replay.run.cursor,
            source_digest=digest,
        ),
        components=components,
        field_dispositions=REQUEST_FIELD_DISPOSITIONS,
    )


def _run_table(runs: tuple[RunQueryItem, ...], source: SourceBinding) -> TableComponent:
    return TableComponent(
        component_id="run-table",
        label="Recent runs",
        columns=(
            TableColumn(key="run", label="Run"),
            TableColumn(key="project", label="Project"),
            TableColumn(key="status", label="Status"),
            TableColumn(key="active_node", label="Active node"),
            TableColumn(key="cursor", label="Cursor"),
        ),
        rows=tuple(
            TableRow(
                row_id=f"run-{index}",
                cells={
                    "run": item.run.run_id,
                    "project": item.run.project_id,
                    "status": item.run.status,
                    "active_node": item.run.active_node_id or "—",
                    "cursor": item.run.cursor,
                },
            )
            for index, item in enumerate(runs)
        ),
        source=source,
    )


def _tooling_components(
    tooling: ToolingSurfaceCatalog,
    digest: str,
) -> tuple[PresentationComponent, ...]:
    source = SourceBinding(kind="tooling", identity="model-cli-catalog", digest=digest)
    catalog = tooling.model_dump(mode="json")
    raw_surfaces = cast("list[dict[str, JsonValue]]", catalog["surfaces"])
    components: list[PresentationComponent] = [
        SectionComponent(
            component_id="tooling",
            label="Model CLI boundaries",
            description="Common authority and every tool-specific facet remain explicit.",
            children=("tooling-shared", "tooling-differences", "tooling-codex", "tooling-agy"),
            source=source,
        ),
        KeyValueComponent(
            component_id="tooling-shared",
            label="Shared facets",
            items=tuple(
                KeyValueItem(key=f"shared[{index}]", value=value)
                for index, value in enumerate(tooling.shared_facets)
            ),
            source=source,
        ),
        TableComponent(
            component_id="tooling-differences",
            label="Operational differences",
            columns=(
                TableColumn(key="facet", label="Facet"),
                TableColumn(key="codex", label="Codex CLI"),
                TableColumn(key="agy", label="Agy CLI"),
                TableColumn(key="effect", label="Operational effect"),
            ),
            rows=tuple(
                TableRow(
                    row_id=f"difference-{index}",
                    cells={
                        "facet": difference.facet,
                        "codex": difference.codex_cli,
                        "agy": difference.agy_cli,
                        "effect": difference.operational_effect,
                    },
                )
                for index, difference in enumerate(tooling.differences)
            ),
            source=source,
        ),
        KeyValueComponent(
            component_id="tooling-codex",
            label="Codex CLI complete surface",
            items=flatten_mapping(raw_surfaces[0], prefix="codex"),
            source=source,
        ),
        KeyValueComponent(
            component_id="tooling-agy",
            label="Agy CLI complete surface",
            items=flatten_mapping(raw_surfaces[1], prefix="agy"),
            source=source,
        ),
    ]
    return tuple(components)


def _action_description(binding: ActionBinding) -> str:
    return f"Produces one closed {binding.request_schema} request."


def _verification_disposition(
    replay: ReplayResponse,
) -> Literal["supported", "concern", "unknown", "not-applicable"]:
    if replay.verification.verdict == "pass":
        return "supported"
    if replay.verification.verdict == "fail":
        return "concern"
    if replay.verification.lifecycle_status == "not-started":
        return "not-applicable"
    return "unknown"


__all__ = ["run_surface", "workspace_surface"]
