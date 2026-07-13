from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

from blackcell.gateway import DataClassification, LocalityPolicy, ModelCapability
from blackcell.orchestration import (
    ROLE_PROFILES,
    DagDefinition,
    DagNode,
    DagValidationError,
    NodeBudget,
    NodeInputBinding,
    NodeSideEffect,
    OrchestrationRole,
    RetryPolicy,
    RolePolicyError,
    dag_definition_payload,
    topological_order,
)

BUDGET = NodeBudget(1_000, 200, 10_000, 1_000)
RETRY = RetryPolicy(2, 5, ("temporary",))


def test_valid_role_separated_dag_has_stable_identity_and_topological_order() -> None:
    first = _valid_dag()
    reordered = DagDefinition(first.dag_id, tuple(reversed(first.nodes)))

    assert first == reordered
    assert first.dag_digest == reordered.dag_digest
    assert topological_order(first) == ("plan", "execute", "review", "verify", "synthesize")
    assert dag_definition_payload(first)["schema_version"] == "orchestration-dag/v1"
    assert all(item.node_digest.startswith("sha256:") for item in first.nodes)


def test_dag_rejects_missing_dependencies_cycles_and_schema_drift() -> None:
    with pytest.raises(DagValidationError, match="missing dependencies"):
        DagDefinition("dag:missing", (_node("execute", depends_on=("absent",)),))

    left = _node("left", depends_on=("right",))
    right = _node("right", depends_on=("left",))
    with pytest.raises(DagValidationError, match="cycle"):
        DagDefinition("dag:cycle", (left, right))

    source = _node("source", output_schema="schema/source-v1")
    target = _node(
        "target",
        depends_on=("source",),
        inputs=(NodeInputBinding("source", "source", "schema/source-v2"),),
    )
    with pytest.raises(DagValidationError, match="schema differs"):
        DagDefinition("dag:schema", (source, target))


def test_role_policy_binds_gateway_capability_and_side_effect_authority() -> None:
    with pytest.raises(RolePolicyError, match="cannot request capability"):
        DagDefinition(
            "dag:planner-code",
            (_node("plan", role=OrchestrationRole.PLANNER, capability=ModelCapability.CODE),),
        )
    with pytest.raises(RolePolicyError, match="only executor"):
        DagDefinition(
            "dag:planner-effect",
            (_node("plan", role=OrchestrationRole.PLANNER, side_effect=NodeSideEffect.READ_ONLY),),
        )
    with pytest.raises(RolePolicyError, match="requires reviewer or verifier"):
        DagDefinition(
            "dag:unapproved",
            (
                _node(
                    "execute",
                    role=OrchestrationRole.EXECUTOR,
                    side_effect=NodeSideEffect.REVERSIBLE,
                    capability=ModelCapability.CODE,
                ),
            ),
        )
    with pytest.raises(RolePolicyError, match="cannot approve its own"):
        DagDefinition(
            "dag:self-approval",
            (
                _node(
                    "review",
                    role=OrchestrationRole.REVIEWER,
                    approvals=(OrchestrationRole.REVIEWER,),
                    capability=ModelCapability.REVIEW,
                ),
            ),
        )
    with pytest.raises(RolePolicyError, match="outside the scheduler"):
        DagDefinition(
            "dag:irreversible",
            (
                _node(
                    "execute",
                    role=OrchestrationRole.EXECUTOR,
                    side_effect=NodeSideEffect.IRREVERSIBLE,
                    approvals=(OrchestrationRole.VERIFIER,),
                ),
            ),
        )
    with pytest.raises(RolePolicyError, match="locality"):
        DagDefinition(
            "dag:remote-verifier",
            (
                _node(
                    "verify",
                    role=OrchestrationRole.VERIFIER,
                    capability=ModelCapability.VERIFY,
                    deterministic=True,
                    locality=LocalityPolicy.REMOTE_ALLOWED,
                ),
            ),
        )
    with pytest.raises(RolePolicyError, match="requires deterministic"):
        DagDefinition(
            "dag:nondeterministic-verifier",
            (
                _node(
                    "verify",
                    role=OrchestrationRole.VERIFIER,
                    capability=ModelCapability.VERIFY,
                ),
            ),
        )
    with pytest.raises(RolePolicyError, match="classification"):
        DagDefinition(
            "dag:secret-planner",
            (
                _node(
                    "plan",
                    role=OrchestrationRole.PLANNER,
                    capability=ModelCapability.REASON,
                    classification=DataClassification.SECRET,
                ),
            ),
        )


def test_profiles_keep_planning_execution_review_verification_and_synthesis_distinct() -> None:
    assert ROLE_PROFILES[OrchestrationRole.PLANNER].allowed_capabilities == (
        ModelCapability.REASON,
    )
    assert ROLE_PROFILES[OrchestrationRole.EXECUTOR].may_execute
    assert not ROLE_PROFILES[OrchestrationRole.EXECUTOR].may_approve
    assert ROLE_PROFILES[OrchestrationRole.REVIEWER].may_approve
    assert ROLE_PROFILES[OrchestrationRole.VERIFIER].allowed_capabilities == (
        ModelCapability.VERIFY,
    )
    assert ROLE_PROFILES[OrchestrationRole.VERIFIER].deterministic_required
    assert ROLE_PROFILES[OrchestrationRole.VERIFIER].allowed_localities == (
        LocalityPolicy.LOCAL_ONLY,
    )
    assert ROLE_PROFILES[OrchestrationRole.SYNTHESIZER].may_synthesize


def test_contracts_are_immutable_and_identity_changes_with_policy() -> None:
    node = _node("execute", role=OrchestrationRole.EXECUTOR)
    with pytest.raises(FrozenInstanceError):
        node_id_field = "node_id"
        setattr(node, node_id_field, "changed")

    changed = replace(node, retry=RetryPolicy(3, 5, ("temporary",)))
    assert changed.node_digest != node.node_digest
    assert (
        DagDefinition("dag:one", (node,)).dag_digest
        != DagDefinition("dag:one", (changed,)).dag_digest
    )


def _valid_dag() -> DagDefinition:
    plan = _node(
        "plan",
        role=OrchestrationRole.PLANNER,
        output_schema="plan/v1",
        capability=ModelCapability.REASON,
    )
    execute = _node(
        "execute",
        role=OrchestrationRole.EXECUTOR,
        output_schema="change/v1",
        depends_on=("plan",),
        inputs=(NodeInputBinding("plan", "plan", "plan/v1"),),
        side_effect=NodeSideEffect.REVERSIBLE,
        approvals=(OrchestrationRole.REVIEWER,),
        capability=ModelCapability.CODE,
    )
    review = _node(
        "review",
        role=OrchestrationRole.REVIEWER,
        output_schema="review/v1",
        depends_on=("execute",),
        inputs=(NodeInputBinding("change", "execute", "change/v1"),),
        capability=ModelCapability.REVIEW,
    )
    verify = _node(
        "verify",
        role=OrchestrationRole.VERIFIER,
        output_schema="verification/v1",
        depends_on=("review",),
        inputs=(NodeInputBinding("review", "review", "review/v1"),),
        capability=ModelCapability.VERIFY,
        deterministic=True,
    )
    synthesize = _node(
        "synthesize",
        role=OrchestrationRole.SYNTHESIZER,
        output_schema="result/v1",
        depends_on=("verify",),
        inputs=(NodeInputBinding("verification", "verify", "verification/v1"),),
        capability=ModelCapability.REASON,
    )
    return DagDefinition("dag:runtime", (synthesize, review, plan, verify, execute))


def _node(
    node_id: str,
    *,
    role: OrchestrationRole = OrchestrationRole.EXECUTOR,
    output_schema: str = "output/v1",
    depends_on: tuple[str, ...] = (),
    inputs: tuple[NodeInputBinding, ...] = (),
    side_effect: NodeSideEffect = NodeSideEffect.NONE,
    approvals: tuple[OrchestrationRole, ...] = (),
    capability: ModelCapability | None = None,
    deterministic: bool = False,
    classification: DataClassification = DataClassification.INTERNAL,
    locality: LocalityPolicy = LocalityPolicy.LOCAL_ONLY,
) -> DagNode:
    return DagNode(
        node_id=node_id,
        role=role,
        principal_id=f"agent:{node_id}",
        handler=f"handler:{node_id}",
        output_schema=output_schema,
        depends_on=depends_on,
        inputs=inputs,
        retry=RETRY,
        timeout_seconds=30,
        budget=BUDGET,
        side_effect=side_effect,
        required_approvals=approvals,
        model_capability=capability,
        classification=classification,
        locality=locality,
        deterministic_required=deterministic,
    )
