from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path
from typing import cast

from blackcell.adapters.persistence.sqlite import SQLiteOrchestrationScheduler
from blackcell.bootstrap.role_dag import (
    EXECUTE_HANDLER,
    PLAN_HANDLER,
    PLAN_SCHEMA,
    RUN_SCHEMA,
    SUMMARY_SCHEMA,
    repository_operator_role_dag,
)
from blackcell.bootstrap.worker import (
    HandlerOutcome,
    HandlerRegistration,
    RuntimeWorker,
)
from blackcell.config import (
    API_TOKEN_ENV,
    DATA_DIR_ENV,
    REPOSITORY_ROOT_ENV,
    WORKER_ID_ENV,
    RuntimeProcessConfig,
)
from blackcell.kernel import ArtifactStore, EventStore
from blackcell.operator import RepositoryOperator
from blackcell.orchestration import (
    DagDefinition,
    DagNode,
    NodeBudget,
    NodeInputBinding,
    NodeStatus,
    NodeUsage,
    OrchestrationLeaseConflict,
    OrchestrationNodeLease,
    OrchestrationNodeSnapshot,
    OrchestrationRole,
    OrchestrationRunStatus,
    RetryPolicy,
)
from blackcell.workflows.run_protocol import RUN_STARTED

TOKEN = "Runtime-v1_worker-token.0123456789-ABCDEFG"
DEFAULT_TEST_BUDGET = NodeBudget(10, 10, 10_000, 0)


class StaleCompletionScheduler(SQLiteOrchestrationScheduler):
    def complete(
        self,
        lease: OrchestrationNodeLease,
        *,
        result_digest: str,
        output_schema: str,
        usage: NodeUsage,
        completed_at: datetime | None = None,
    ) -> OrchestrationNodeSnapshot:
        del lease, result_digest, output_schema, usage, completed_at
        raise OrchestrationLeaseConflict("stale lease")


def test_repository_role_dag_is_the_canonical_five_role_contract() -> None:
    definition = repository_operator_role_dag()
    nodes = {node.node_id: node for node in definition.nodes}

    assert tuple(nodes) == ("execute", "plan", "review", "synthesize", "verify")
    assert {node.role for node in nodes.values()} == set(OrchestrationRole)
    assert nodes["plan"].depends_on == ()
    assert nodes["execute"].depends_on == ("plan",)
    assert nodes["review"].depends_on == ("execute",)
    assert nodes["verify"].depends_on == ("execute", "review")
    assert nodes["synthesize"].depends_on == ("review", "verify")
    assert nodes["verify"].deterministic_required
    assert all(node.retry.max_attempts == 2 for node in nodes.values())


def test_worker_survives_restarts_and_verification_replays_with_repository_offline(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    first = RuntimeWorker.from_config(config)
    scheduler = SQLiteOrchestrationScheduler(config.security.paths.database_path)
    scheduler.submit(
        "orchestration:repository-1",
        repository_operator_role_dag(),
        submitted_by="service:test",
    )

    assert first.run_once()  # plan
    assert RuntimeWorker.from_config(config).run_once()  # execute after restart
    assert RuntimeWorker.from_config(config).run_once()  # review after restart
    replay_worker = RuntimeWorker.from_config(config)
    config.repository_root.rename(tmp_path / "repository-offline")
    assert replay_worker.run_once()  # verify without the live repository
    assert replay_worker.run_once()  # synthesize from artifacts

    snapshot = scheduler.inspect("orchestration:repository-1")
    assert snapshot.status is OrchestrationRunStatus.SUCCEEDED
    assert all(node.status is NodeStatus.SUCCEEDED for node in snapshot.nodes)
    execute = next(node for node in snapshot.nodes if node.node_id == "execute")
    summary = next(node for node in snapshot.nodes if node.node_id == "synthesize")
    assert execute.usage.input_tokens > 0
    assert execute.usage.output_tokens > 0
    assert execute.usage.cost_microusd == 0
    assert summary.result_digest is not None
    payload = cast(
        "dict[str, object]",
        ArtifactStore(
            config.security.paths.artifact_root,
            database_path=config.security.paths.database_path,
        ).get_json(summary.result_digest),
    )
    assert isinstance(payload, dict)
    assert payload["schema_version"] == SUMMARY_SCHEMA
    assert payload["accepted"] is True
    events = EventStore(config.security.paths.database_path).read_all(
        after_position=0,
        limit=1_000,
    )
    assert sum(event.event_type == RUN_STARTED for event in events) == 1
    assert not replay_worker.run_once()


def test_worker_rejects_unknown_handlers_and_budget_overruns(tmp_path: Path) -> None:
    config, operator, scheduler = _runtime(tmp_path)
    scheduler.submit(
        "orchestration:unknown",
        DagDefinition(
            "dag:unknown-handler",
            (_node("work", handler="customer.unknown/v1", schema="unknown/v1"),),
        ),
        submitted_by="service:test",
    )
    worker = RuntimeWorker(operator, scheduler, config)

    assert worker.run_once()
    unknown = scheduler.inspect("orchestration:unknown").nodes[0]
    assert unknown.status is NodeStatus.FAILED
    assert unknown.failure_code == "handler-unavailable"

    def expensive(_work: object) -> HandlerOutcome:
        artifact = operator.artifacts.put_json(
            {"schema_version": "expensive-result/v1", "accepted": True}
        )
        return HandlerOutcome(artifact.digest, output_tokens=1)

    scheduler.submit(
        "orchestration:budget",
        DagDefinition(
            "dag:budget-overrun",
            (
                _node(
                    "work",
                    handler="test.expensive/v1",
                    schema="expensive-result/v1",
                    budget=NodeBudget(0, 0, 10_000, 0),
                ),
            ),
        ),
        submitted_by="service:test",
    )
    bounded = RuntimeWorker(
        operator,
        scheduler,
        config,
        handlers={"test.expensive/v1": HandlerRegistration("expensive-result/v1", expensive)},
    )

    assert bounded.run_once()
    overrun = scheduler.inspect("orchestration:budget").nodes[0]
    assert overrun.status is NodeStatus.FAILED
    assert overrun.failure_code == "budget-exceeded"


def test_worker_rejects_malformed_dependency_artifacts(tmp_path: Path) -> None:
    config, operator, scheduler = _runtime(tmp_path)
    standard = RuntimeWorker(operator, scheduler, config)

    def malformed_plan(_work: object) -> HandlerOutcome:
        artifact = operator.artifacts.put_json(
            {"schema_version": "unexpected-plan/v1", "objective": "ignored"}
        )
        return HandlerOutcome(artifact.digest)

    worker = RuntimeWorker(
        operator,
        scheduler,
        config,
        handlers={
            PLAN_HANDLER: HandlerRegistration(PLAN_SCHEMA, malformed_plan),
            EXECUTE_HANDLER: HandlerRegistration(RUN_SCHEMA, standard._execute),
        },
    )
    scheduler.submit(
        "orchestration:malformed",
        _plan_execute_dag(),
        submitted_by="service:test",
    )

    assert worker.run_once()
    assert worker.run_once()
    snapshot = scheduler.inspect("orchestration:malformed")
    execute = next(node for node in snapshot.nodes if node.node_id == "execute")
    assert execute.status is NodeStatus.FAILED
    assert execute.failure_code == "invalid-input-artifact"


def test_stale_worker_completion_cannot_escape_scheduler_fencing(tmp_path: Path) -> None:
    config = _config(tmp_path)
    database = config.security.paths.ensure_database_file()
    operator = RepositoryOperator(
        config.repository_root,
        database_path=database,
        artifact_root=config.security.paths.artifact_root,
    )
    scheduler = StaleCompletionScheduler(database)
    scheduler.submit(
        "orchestration:stale",
        DagDefinition("dag:stale", (_node("plan", handler=PLAN_HANDLER, schema=PLAN_SCHEMA),)),
        submitted_by="service:test",
    )

    assert RuntimeWorker(operator, scheduler, config).run_once()
    node = scheduler.inspect("orchestration:stale").nodes[0]
    assert node.status is NodeStatus.RUNNING
    assert node.result_digest is None


def _runtime(
    tmp_path: Path,
) -> tuple[RuntimeProcessConfig, RepositoryOperator, SQLiteOrchestrationScheduler]:
    config = _config(tmp_path)
    database = config.security.paths.ensure_database_file()
    operator = RepositoryOperator(
        config.repository_root,
        database_path=database,
        artifact_root=config.security.paths.artifact_root,
    )
    return config, operator, SQLiteOrchestrationScheduler(database)


def _config(tmp_path: Path) -> RuntimeProcessConfig:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(
        ["git", "init", "--quiet", str(repository)],
        check=True,
        capture_output=True,
        text=True,
    )
    return RuntimeProcessConfig.from_environment(
        {
            DATA_DIR_ENV: str(tmp_path / "data"),
            API_TOKEN_ENV: TOKEN,
            REPOSITORY_ROOT_ENV: str(repository),
            WORKER_ID_ENV: "worker:test",
        }
    )


def _node(
    node_id: str,
    *,
    handler: str,
    schema: str,
    budget: NodeBudget = DEFAULT_TEST_BUDGET,
) -> DagNode:
    return DagNode(
        node_id=node_id,
        role=OrchestrationRole.PLANNER,
        principal_id=f"runtime:{node_id}",
        handler=handler,
        output_schema=schema,
        depends_on=(),
        inputs=(),
        retry=RetryPolicy(),
        timeout_seconds=10,
        budget=budget,
    )


def _plan_execute_dag() -> DagDefinition:
    return DagDefinition(
        "dag:malformed-plan",
        (
            _node("plan", handler=PLAN_HANDLER, schema=PLAN_SCHEMA),
            DagNode(
                node_id="execute",
                role=OrchestrationRole.EXECUTOR,
                principal_id="runtime:execute",
                handler=EXECUTE_HANDLER,
                output_schema=RUN_SCHEMA,
                depends_on=("plan",),
                inputs=(NodeInputBinding("plan", "plan", PLAN_SCHEMA),),
                retry=RetryPolicy(),
                timeout_seconds=120,
                budget=NodeBudget(2_000, 512, 120_000, 0),
            ),
        ),
    )
