from __future__ import annotations

from blackcell.orchestration import (
    DagDefinition,
    DagNode,
    NodeBudget,
    NodeInputBinding,
    NodeSideEffect,
    OrchestrationRole,
    RetryPolicy,
)

REPOSITORY_ROLE_DAG_ID = "dag:repository-operator/v1"
PLAN_HANDLER = "blackcell.operator.plan/v1"
EXECUTE_HANDLER = "blackcell.operator.execute/v1"
REVIEW_HANDLER = "blackcell.operator.review/v1"
VERIFY_HANDLER = "blackcell.operator.verify/v1"
SYNTHESIZE_HANDLER = "blackcell.operator.synthesize/v1"

PLAN_SCHEMA = "operator-plan/v1"
RUN_SCHEMA = "canonical-operator-run-result/v1"
REVIEW_SCHEMA = "operator-review/v1"
VERIFICATION_SCHEMA = "operator-verification/v1"
SUMMARY_SCHEMA = "operator-summary/v1"

_DETERMINISTIC_RETRY = RetryPolicy(2, 1, ("handler-failed", "worker-lost"))


def repository_operator_role_dag(
    dag_id: str = REPOSITORY_ROLE_DAG_ID,
) -> DagDefinition:
    """Return the immutable reviewed role DAG executed by the local worker."""

    plan = DagNode(
        node_id="plan",
        role=OrchestrationRole.PLANNER,
        principal_id="runtime:planner",
        handler=PLAN_HANDLER,
        output_schema=PLAN_SCHEMA,
        depends_on=(),
        inputs=(),
        retry=_DETERMINISTIC_RETRY,
        timeout_seconds=10,
        budget=NodeBudget(0, 0, 10_000, 0),
    )
    execute = DagNode(
        node_id="execute",
        role=OrchestrationRole.EXECUTOR,
        principal_id="runtime:executor",
        handler=EXECUTE_HANDLER,
        output_schema=RUN_SCHEMA,
        depends_on=("plan",),
        inputs=(NodeInputBinding("plan", "plan", PLAN_SCHEMA),),
        retry=_DETERMINISTIC_RETRY,
        timeout_seconds=120,
        budget=NodeBudget(2_000, 512, 120_000, 0),
        side_effect=NodeSideEffect.READ_ONLY,
    )
    review = DagNode(
        node_id="review",
        role=OrchestrationRole.REVIEWER,
        principal_id="runtime:reviewer",
        handler=REVIEW_HANDLER,
        output_schema=REVIEW_SCHEMA,
        depends_on=("execute",),
        inputs=(NodeInputBinding("run", "execute", RUN_SCHEMA),),
        retry=_DETERMINISTIC_RETRY,
        timeout_seconds=10,
        budget=NodeBudget(0, 0, 10_000, 0),
    )
    verify = DagNode(
        node_id="verify",
        role=OrchestrationRole.VERIFIER,
        principal_id="runtime:verifier",
        handler=VERIFY_HANDLER,
        output_schema=VERIFICATION_SCHEMA,
        depends_on=("execute", "review"),
        inputs=(
            NodeInputBinding("review", "review", REVIEW_SCHEMA),
            NodeInputBinding("run", "execute", RUN_SCHEMA),
        ),
        retry=_DETERMINISTIC_RETRY,
        timeout_seconds=30,
        budget=NodeBudget(0, 0, 30_000, 0),
        deterministic_required=True,
    )
    synthesize = DagNode(
        node_id="synthesize",
        role=OrchestrationRole.SYNTHESIZER,
        principal_id="runtime:synthesizer",
        handler=SYNTHESIZE_HANDLER,
        output_schema=SUMMARY_SCHEMA,
        depends_on=("review", "verify"),
        inputs=(
            NodeInputBinding("review", "review", REVIEW_SCHEMA),
            NodeInputBinding("verification", "verify", VERIFICATION_SCHEMA),
        ),
        retry=_DETERMINISTIC_RETRY,
        timeout_seconds=10,
        budget=NodeBudget(0, 0, 10_000, 0),
    )
    return DagDefinition(dag_id, (plan, execute, review, verify, synthesize))


__all__ = [
    "EXECUTE_HANDLER",
    "PLAN_HANDLER",
    "PLAN_SCHEMA",
    "REPOSITORY_ROLE_DAG_ID",
    "REVIEW_HANDLER",
    "REVIEW_SCHEMA",
    "RUN_SCHEMA",
    "SUMMARY_SCHEMA",
    "SYNTHESIZE_HANDLER",
    "VERIFICATION_SCHEMA",
    "VERIFY_HANDLER",
    "repository_operator_role_dag",
]
