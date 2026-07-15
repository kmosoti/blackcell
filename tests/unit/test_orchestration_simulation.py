from __future__ import annotations

import pytest

from blackcell.orchestration import (
    DagDefinition,
    DagNode,
    NodeBudget,
    NodeInputBinding,
    NodeSideEffect,
    NodeSimulationPlan,
    NodeStatus,
    NodeUsage,
    OrchestrationRole,
    OrchestrationRunStatus,
    RetryPolicy,
    SimulatedApproval,
    SimulatedAttempt,
    SimulatedAttemptOutcome,
    SimulationInvariantError,
    SimulationScenario,
    simulate_dag,
)

BUDGET = NodeBudget(100, 50, 1_000, 500)
RETRY = RetryPolicy(3, 1, ("temporary",))


def test_success_report_is_deterministic_and_aggregates_usage() -> None:
    dag = _dag()
    scenario = SimulationScenario(
        plans=(
            NodeSimulationPlan(
                "plan",
                (SimulatedAttempt(SimulatedAttemptOutcome.SUCCEEDED, NodeUsage(10, 2, 5, 1)),),
            ),
            NodeSimulationPlan(
                "execute",
                (SimulatedAttempt(SimulatedAttemptOutcome.SUCCEEDED, NodeUsage(20, 4, 7, 2)),),
            ),
        ),
        approvals=(SimulatedApproval("execute", OrchestrationRole.REVIEWER, "agent:review", True),),
    )

    first = simulate_dag(dag, scenario)
    second = simulate_dag(dag, scenario)

    assert first == second
    assert first.report_id == second.report_id
    assert first.run_status is OrchestrationRunStatus.SUCCEEDED
    assert first.total_usage == NodeUsage(30, 6, 12, 3)
    assert all(item.commit_count == 1 for item in first.nodes)


def test_transient_retry_and_duplicate_delivery_commit_once_with_new_fence() -> None:
    dag = DagDefinition("dag:retry", (_node("execute"),))
    report = simulate_dag(
        dag,
        SimulationScenario(
            (
                NodeSimulationPlan(
                    "execute",
                    (
                        SimulatedAttempt(
                            SimulatedAttemptOutcome.TRANSIENT_FAILURE,
                            NodeUsage(3, 0, 10, 0),
                            "temporary",
                        ),
                        SimulatedAttempt(
                            SimulatedAttemptOutcome.DUPLICATE_DELIVERY,
                            NodeUsage(4, 1, 20, 1),
                        ),
                    ),
                ),
            )
        ),
    )
    node = report.nodes[0]

    assert node.status is NodeStatus.SUCCEEDED
    assert tuple(item.fencing_token for item in node.attempts) == (1, 2)
    assert tuple(item.committed for item in node.attempts) == (False, True)
    assert node.commit_count == 1
    assert node.duplicate_deliveries == 1
    assert node.usage == NodeUsage(7, 1, 30, 1)


def test_worker_loss_and_stale_completion_never_commit_and_block_descendants() -> None:
    first = _node("first")
    dependent = _node(
        "dependent",
        depends_on=("first",),
        inputs=(NodeInputBinding("first", "first", "output/v1"),),
    )
    report = simulate_dag(
        DagDefinition("dag:loss", (dependent, first)),
        SimulationScenario(
            (
                NodeSimulationPlan(
                    "first",
                    (
                        SimulatedAttempt(SimulatedAttemptOutcome.WORKER_LOST),
                        SimulatedAttempt(SimulatedAttemptOutcome.STALE_COMPLETION),
                        SimulatedAttempt(SimulatedAttemptOutcome.WORKER_LOST),
                    ),
                ),
            )
        ),
    )

    assert report.run_status is OrchestrationRunStatus.FAILED
    assert report.nodes[0].status is NodeStatus.FAILED
    assert report.nodes[0].commit_count == 0
    assert report.nodes[0].stale_rejections == 1
    assert all(not item.committed for item in report.nodes[0].attempts)
    assert report.nodes[1].status is NodeStatus.BLOCKED
    assert report.nodes[1].terminal_code == "dependency-not-satisfied"


def test_budget_exhaustion_and_nonretryable_failure_are_terminal() -> None:
    dag = DagDefinition("dag:budget", (_node("execute"),))
    over_budget = simulate_dag(
        dag,
        SimulationScenario(
            (
                NodeSimulationPlan(
                    "execute",
                    (
                        SimulatedAttempt(
                            SimulatedAttemptOutcome.SUCCEEDED,
                            NodeUsage(input_tokens=101),
                        ),
                        SimulatedAttempt(SimulatedAttemptOutcome.SUCCEEDED),
                    ),
                ),
            )
        ),
    )
    permanent = simulate_dag(
        dag,
        SimulationScenario(
            (
                NodeSimulationPlan(
                    "execute",
                    (
                        SimulatedAttempt(
                            SimulatedAttemptOutcome.TRANSIENT_FAILURE,
                            failure_code="not-retryable",
                        ),
                        SimulatedAttempt(SimulatedAttemptOutcome.SUCCEEDED),
                    ),
                ),
            )
        ),
    )

    assert len(over_budget.nodes[0].attempts) == 1
    assert over_budget.nodes[0].terminal_code == "budget-exceeded"
    assert len(permanent.nodes[0].attempts) == 1
    assert permanent.nodes[0].terminal_code == "not-retryable"


def test_approval_denial_missing_approval_and_self_approval_are_distinct() -> None:
    dag = _dag()
    denied = simulate_dag(
        dag,
        SimulationScenario(
            approvals=(
                SimulatedApproval(
                    "execute",
                    OrchestrationRole.REVIEWER,
                    "agent:review",
                    False,
                ),
            )
        ),
    )
    missing = simulate_dag(dag)

    assert denied.run_status is OrchestrationRunStatus.DENIED
    assert denied.nodes[1].status is NodeStatus.DENIED
    assert denied.nodes[1].terminal_code == "approval-denied"
    assert missing.run_status is OrchestrationRunStatus.FAILED
    assert missing.nodes[1].status is NodeStatus.BLOCKED
    assert missing.nodes[1].terminal_code == "approval-missing"
    with pytest.raises(SimulationInvariantError, match="cannot approve"):
        simulate_dag(
            dag,
            SimulationScenario(
                approvals=(
                    SimulatedApproval(
                        "execute",
                        OrchestrationRole.REVIEWER,
                        "agent:execute",
                        True,
                    ),
                )
            ),
        )


def test_scenario_rejects_unknown_or_duplicate_node_evidence() -> None:
    with pytest.raises(ValueError, match="outside"):
        simulate_dag(
            DagDefinition("dag:one", (_node("one"),)),
            SimulationScenario((NodeSimulationPlan("other", (_success(),)),)),
        )
    with pytest.raises(ValueError, match="unique"):
        SimulationScenario(
            (
                NodeSimulationPlan("one", (_success(),)),
                NodeSimulationPlan("one", (_success(),)),
            )
        )


def _dag() -> DagDefinition:
    plan = _node("plan", role=OrchestrationRole.PLANNER, principal="agent:plan")
    execute = _node(
        "execute",
        principal="agent:execute",
        depends_on=("plan",),
        inputs=(NodeInputBinding("plan", "plan", "output/v1"),),
        side_effect=NodeSideEffect.REVERSIBLE,
        approvals=(OrchestrationRole.REVIEWER,),
    )
    return DagDefinition("dag:simulation", (execute, plan))


def _node(
    node_id: str,
    *,
    role: OrchestrationRole = OrchestrationRole.EXECUTOR,
    principal: str | None = None,
    depends_on: tuple[str, ...] = (),
    inputs: tuple[NodeInputBinding, ...] = (),
    side_effect: NodeSideEffect = NodeSideEffect.NONE,
    approvals: tuple[OrchestrationRole, ...] = (),
) -> DagNode:
    return DagNode(
        node_id,
        role,
        principal or f"agent:{node_id}",
        f"handler:{node_id}",
        "output/v1",
        depends_on,
        inputs,
        RETRY,
        5,
        BUDGET,
        side_effect,
        approvals,
    )


def _success() -> SimulatedAttempt:
    return SimulatedAttempt(SimulatedAttemptOutcome.SUCCEEDED)
