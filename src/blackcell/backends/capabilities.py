"""Schema-backed capability matrix for narrow planning protocols."""

from dataclasses import dataclass
from enum import StrEnum
from functools import cache
from typing import Any

from blackcell.backends.planning import (
    MaterializationPlanningBackend,
    PlanningAssignmentReader,
    PlanningAssignmentWriter,
    PlanningBackend,
    PlanningIdentityReader,
    PlanningIntegrationReader,
    PlanningIssueWorkflowReader,
    PlanningProjectIssueReader,
    PlanningProjectLabelReader,
    PlanningProjectLabelWriter,
    PlanningProjectLocator,
    PlanningProjectReader,
    PlanningProjectStatusReader,
    PlanningProjectWriter,
    PlanWorkflowBackend,
)
from blackcell.schema.linear import LinearSchema, load_linear_schema
from blackcell.sdk.operations import OperationId


class CapabilityType(StrEnum):
    OBJECT = "object"
    INPUT = "input"
    QUERY = "query"
    MUTATION = "mutation"


@dataclass(frozen=True, slots=True, order=True)
class LinearCapability:
    kind: CapabilityType
    owner: str
    field: str

    @property
    def label(self) -> str:
        return f"{self.kind}:{self.owner}.{self.field}"

    def supported_by(self, schema: LinearSchema) -> bool:
        if self.kind is CapabilityType.QUERY:
            return schema.lookup_query_field(self.field) is not None
        if self.kind is CapabilityType.MUTATION:
            return schema.lookup_mutation_field(self.field) is not None
        if self.kind is CapabilityType.INPUT:
            return schema.lookup_input_field(self.owner, self.field) is not None
        return schema.lookup_type_field(self.owner, self.field) is not None


def object_field(owner: str, field: str) -> LinearCapability:
    return LinearCapability(CapabilityType.OBJECT, owner, field)


def input_field(owner: str, field: str) -> LinearCapability:
    return LinearCapability(CapabilityType.INPUT, owner, field)


def query_field(field: str) -> LinearCapability:
    return LinearCapability(CapabilityType.QUERY, "Query", field)


def mutation_field(field: str) -> LinearCapability:
    return LinearCapability(CapabilityType.MUTATION, "Mutation", field)


PLANNING_PROTOCOL_CAPABILITIES: dict[type[Any], frozenset[LinearCapability]] = {
    PlanningAssignmentReader: frozenset(
        {
            query_field("issues"),
            object_field("Project", "issues"),
            object_field("Issue", "relations"),
        }
    ),
    PlanningAssignmentWriter: frozenset(
        {
            mutation_field("issueCreate"),
            mutation_field("issueUpdate"),
            mutation_field("issueRelationCreate"),
            input_field("IssueCreateInput", "assigneeId"),
            input_field("IssueCreateInput", "delegateId"),
            input_field("IssueCreateInput", "description"),
            input_field("IssueCreateInput", "labelIds"),
            input_field("IssueCreateInput", "parentId"),
            input_field("IssueCreateInput", "priority"),
            input_field("IssueCreateInput", "projectId"),
            input_field("IssueCreateInput", "stateId"),
            input_field("IssueCreateInput", "title"),
            input_field("IssueUpdateInput", "assigneeId"),
            input_field("IssueUpdateInput", "delegateId"),
            input_field("IssueUpdateInput", "description"),
            input_field("IssueUpdateInput", "labelIds"),
            input_field("IssueUpdateInput", "parentId"),
            input_field("IssueUpdateInput", "priority"),
            input_field("IssueUpdateInput", "projectId"),
            input_field("IssueUpdateInput", "stateId"),
            input_field("IssueUpdateInput", "title"),
        }
    ),
    PlanningIdentityReader: frozenset(
        {
            query_field("viewer"),
            query_field("team"),
            object_field("Team", "id"),
            object_field("User", "id"),
        }
    ),
    PlanningIntegrationReader: frozenset({query_field("integrations")}),
    PlanningIssueWorkflowReader: frozenset(
        {
            query_field("workflowStates"),
            query_field("issueLabels"),
            object_field("IssueLabel", "name"),
            object_field("WorkflowState", "name"),
        }
    ),
    PlanningProjectIssueReader: frozenset(
        {
            query_field("project"),
            object_field("Project", "issues"),
            object_field("Issue", "id"),
        }
    ),
    PlanningProjectLabelReader: frozenset(
        {
            query_field("projectLabels"),
            object_field("ProjectLabel", "id"),
            object_field("ProjectLabel", "name"),
        }
    ),
    PlanningProjectLabelWriter: frozenset(
        {
            mutation_field("projectLabelCreate"),
            mutation_field("projectAddLabel"),
            mutation_field("projectRemoveLabel"),
        }
    ),
    PlanningProjectLocator: frozenset(
        {
            query_field("projects"),
            object_field("Project", "id"),
            object_field("Project", "labels"),
            object_field("Project", "lead"),
            object_field("Project", "members"),
            object_field("Project", "name"),
            object_field("Project", "priority"),
            object_field("Project", "status"),
        }
    ),
    PlanningProjectStatusReader: frozenset(
        {
            query_field("projectStatuses"),
            object_field("ProjectStatus", "name"),
            object_field("ProjectStatus", "type"),
        }
    ),
    PlanningProjectWriter: frozenset(
        {
            mutation_field("entityExternalLinkCreate"),
            mutation_field("entityExternalLinkUpdate"),
            mutation_field("projectCreate"),
            mutation_field("projectUpdate"),
            input_field("ProjectCreateInput", "color"),
            input_field("ProjectCreateInput", "content"),
            input_field("ProjectCreateInput", "description"),
            input_field("ProjectCreateInput", "icon"),
            input_field("ProjectCreateInput", "labelIds"),
            input_field("ProjectCreateInput", "leadId"),
            input_field("ProjectCreateInput", "memberIds"),
            input_field("ProjectCreateInput", "name"),
            input_field("ProjectCreateInput", "priority"),
            input_field("ProjectCreateInput", "statusId"),
            input_field("ProjectCreateInput", "teamIds"),
            input_field("ProjectUpdateInput", "color"),
            input_field("ProjectUpdateInput", "content"),
            input_field("ProjectUpdateInput", "description"),
            input_field("ProjectUpdateInput", "icon"),
            input_field("ProjectUpdateInput", "labelIds"),
            input_field("ProjectUpdateInput", "leadId"),
            input_field("ProjectUpdateInput", "memberIds"),
            input_field("ProjectUpdateInput", "priority"),
            input_field("ProjectUpdateInput", "statusId"),
            input_field("ProjectUpdateInput", "teamIds"),
        }
    ),
}


PLANNING_PROTOCOL_COMPOSITIONS: dict[type[Any], tuple[type[Any], ...]] = {
    PlanningProjectReader: (PlanningProjectLocator, PlanningProjectIssueReader),
    PlanWorkflowBackend: (
        PlanningIdentityReader,
        PlanningProjectStatusReader,
        PlanningProjectLocator,
        PlanningProjectLabelReader,
        PlanningProjectLabelWriter,
        PlanningProjectWriter,
    ),
    MaterializationPlanningBackend: (
        PlanningIdentityReader,
        PlanningIssueWorkflowReader,
        PlanningProjectLocator,
        PlanningAssignmentReader,
        PlanningAssignmentWriter,
    ),
    PlanningBackend: (
        PlanningIdentityReader,
        PlanningProjectStatusReader,
        PlanningIssueWorkflowReader,
        PlanningIntegrationReader,
        PlanningProjectLabelReader,
        PlanningProjectLabelWriter,
        PlanningProjectReader,
        PlanningProjectWriter,
        PlanningAssignmentReader,
        PlanningAssignmentWriter,
    ),
}


PLANNING_PROTOCOL_OPERATIONS: dict[OperationId, tuple[type[Any], ...]] = {
    OperationId.ASSIGNMENT_LIST: (
        PlanningIdentityReader,
        PlanningProjectLocator,
        PlanningProjectIssueReader,
    ),
    OperationId.ASSIGNMENT_VERIFY: (
        PlanningIdentityReader,
        PlanningIssueWorkflowReader,
        PlanningProjectLocator,
        PlanningProjectIssueReader,
    ),
    OperationId.DIRECTIVE_MATERIALIZE: (MaterializationPlanningBackend,),
    OperationId.DIRECTIVE_PROPOSE: (PlanWorkflowBackend,),
    OperationId.DIRECTIVE_RECONCILE: (MaterializationPlanningBackend,),
    OperationId.DIRECTIVE_STATUS: (PlanningIdentityReader, PlanningProjectLocator),
    OperationId.OPERATION_INSPECT: (PlanningIdentityReader, PlanningProjectLocator),
    OperationId.OPERATION_RECONCILE: (PlanWorkflowBackend,),
    OperationId.OPERATION_VERIFY: (PlanningIdentityReader, PlanningProjectLocator),
    OperationId.PULSE: (
        PlanningIdentityReader,
        PlanningProjectStatusReader,
        PlanningIssueWorkflowReader,
        PlanningIntegrationReader,
    ),
    OperationId.RECON_STATUS: (
        PlanningIdentityReader,
        PlanningProjectLocator,
    ),
    OperationId.WORKFLOW_RESUME: (
        PlanWorkflowBackend,
        MaterializationPlanningBackend,
        PlanningIntegrationReader,
    ),
    OperationId.WORKFLOW_RUN: (
        PlanWorkflowBackend,
        MaterializationPlanningBackend,
        PlanningIntegrationReader,
    ),
}


def planning_capabilities_for_protocol(protocol: type[Any]) -> frozenset[LinearCapability]:
    if protocol in PLANNING_PROTOCOL_CAPABILITIES:
        return PLANNING_PROTOCOL_CAPABILITIES[protocol]
    capabilities: set[LinearCapability] = set()
    for component in PLANNING_PROTOCOL_COMPOSITIONS[protocol]:
        capabilities.update(planning_capabilities_for_protocol(component))
    return frozenset(capabilities)


def planning_capabilities_for_operation(operation_id: OperationId) -> frozenset[LinearCapability]:
    capabilities: set[LinearCapability] = set()
    for protocol in PLANNING_PROTOCOL_OPERATIONS[operation_id]:
        capabilities.update(planning_capabilities_for_protocol(protocol))
    return frozenset(capabilities)


def capability_is_schema_backed(
    capability: LinearCapability,
    schema: LinearSchema | None = None,
) -> bool:
    return capability.supported_by(schema or _cached_linear_schema())


def missing_capabilities(
    schema: LinearSchema,
    protocol: type[Any],
) -> tuple[LinearCapability, ...]:
    return tuple(
        sorted(
            capability
            for capability in planning_capabilities_for_protocol(protocol)
            if not capability.supported_by(schema)
        )
    )


@cache
def _cached_linear_schema() -> LinearSchema:
    return load_linear_schema()
