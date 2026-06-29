"""Linear protocol capability matrix and public operation coverage."""

from blackcell.backends.capabilities import (
    PLANNING_PROTOCOL_CAPABILITIES,
    PLANNING_PROTOCOL_COMPOSITIONS,
    PLANNING_PROTOCOL_OPERATIONS,
    CapabilityType,
    capability_is_schema_backed,
    planning_capabilities_for_operation,
    planning_capabilities_for_protocol,
)
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
from blackcell.contracts.facade import Authority
from blackcell.sdk.operations import OPERATIONS, OperationId


def test_planning_protocol_matrix_documents_narrow_protocols() -> None:
    required_protocols = {
        PlanningIdentityReader,
        PlanningProjectStatusReader,
        PlanningIssueWorkflowReader,
        PlanningIntegrationReader,
        PlanningProjectLocator,
        PlanningProjectIssueReader,
        PlanningProjectLabelReader,
        PlanningProjectLabelWriter,
        PlanningProjectWriter,
        PlanningAssignmentReader,
        PlanningAssignmentWriter,
    }
    composed_protocols = {
        MaterializationPlanningBackend,
        PlanWorkflowBackend,
        PlanningBackend,
        PlanningProjectReader,
    }

    assert required_protocols <= set(PLANNING_PROTOCOL_CAPABILITIES)
    assert composed_protocols <= set(PLANNING_PROTOCOL_COMPOSITIONS)
    for protocol in required_protocols:
        assert PLANNING_PROTOCOL_CAPABILITIES[protocol], (
            f"Missing capability mapping for protocol {protocol.__name__}"
        )
    for protocol in composed_protocols:
        assert planning_capabilities_for_protocol(protocol), (
            f"Missing capability mapping for protocol {protocol.__name__}"
        )


def test_planning_protocol_capabilities_are_schema_backed() -> None:
    for protocol, capabilities in PLANNING_PROTOCOL_CAPABILITIES.items():
        for capability in capabilities:
            assert capability_is_schema_backed(capability), (
                f"{protocol.__name__} needs schema-backed Linear capability {capability}"
            )


def test_composed_protocols_accumulate_expected_capabilities() -> None:
    assert (
        planning_capabilities_for_protocol(PlanWorkflowBackend)
        == PLANNING_PROTOCOL_CAPABILITIES[PlanningIdentityReader]
        | PLANNING_PROTOCOL_CAPABILITIES[PlanningProjectStatusReader]
        | PLANNING_PROTOCOL_CAPABILITIES[PlanningProjectLocator]
        | PLANNING_PROTOCOL_CAPABILITIES[PlanningProjectLabelReader]
        | PLANNING_PROTOCOL_CAPABILITIES[PlanningProjectLabelWriter]
        | PLANNING_PROTOCOL_CAPABILITIES[PlanningProjectWriter]
    )
    assert (
        planning_capabilities_for_protocol(MaterializationPlanningBackend)
        == PLANNING_PROTOCOL_CAPABILITIES[PlanningIdentityReader]
        | PLANNING_PROTOCOL_CAPABILITIES[PlanningIssueWorkflowReader]
        | PLANNING_PROTOCOL_CAPABILITIES[PlanningProjectLocator]
        | PLANNING_PROTOCOL_CAPABILITIES[PlanningAssignmentReader]
        | PLANNING_PROTOCOL_CAPABILITIES[PlanningAssignmentWriter]
    )


def test_planning_matrix_covers_linear_public_operations() -> None:
    expected_operations = {
        OperationId.DIRECTIVE_PROPOSE,
        OperationId.DIRECTIVE_STATUS,
        OperationId.DIRECTIVE_MATERIALIZE,
        OperationId.DIRECTIVE_RECONCILE,
        OperationId.OPERATION_INSPECT,
        OperationId.OPERATION_RECONCILE,
        OperationId.OPERATION_VERIFY,
        OperationId.ASSIGNMENT_LIST,
        OperationId.ASSIGNMENT_VERIFY,
        OperationId.RECON_STATUS,
        OperationId.WORKFLOW_RUN,
        OperationId.WORKFLOW_RESUME,
        OperationId.PULSE,
    }

    public_linear_operations = {
        operation_id
        for operation_id, spec in OPERATIONS.items()
        if spec.authority in {Authority.LINEAR, Authority.CROSS_SYSTEM}
    }

    assert public_linear_operations == expected_operations
    assert set(PLANNING_PROTOCOL_OPERATIONS) == expected_operations
    for operation_id in expected_operations:
        assert PLANNING_PROTOCOL_OPERATIONS[operation_id]


def test_matrix_projection_is_schema_backed_for_key_operations() -> None:
    for operation_id in (
        OperationId.DIRECTIVE_PROPOSE,
        OperationId.DIRECTIVE_MATERIALIZE,
        OperationId.OPERATION_VERIFY,
        OperationId.ASSIGNMENT_LIST,
        OperationId.PULSE,
    ):
        operation_caps = planning_capabilities_for_operation(operation_id)
        assert operation_caps
        assert all(capability_is_schema_backed(capability) for capability in operation_caps)
        assert any(capability.kind is CapabilityType.QUERY for capability in operation_caps)
        assert any(capability.kind is CapabilityType.MUTATION for capability in operation_caps) == (
            operation_id
            in {
                OperationId.DIRECTIVE_PROPOSE,
                OperationId.DIRECTIVE_MATERIALIZE,
                OperationId.DIRECTIVE_RECONCILE,
                OperationId.OPERATION_RECONCILE,
                OperationId.WORKFLOW_RUN,
                OperationId.WORKFLOW_RESUME,
            }
        )
