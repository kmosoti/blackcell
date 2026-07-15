from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest

from blackcell.adapters.persistence.sqlite import SQLiteOrchestrationScheduler
from blackcell.adapters.persistence.sqlite.session import SQLiteKernelTransaction
from blackcell.kernel import EventStore
from blackcell.kernel._json import json_digest
from blackcell.kernel.database import connect
from blackcell.orchestration import (
    DagDefinition,
    DagNode,
    NodeBudget,
    NodeInputBinding,
    NodeSideEffect,
    NodeStatus,
    NodeUsage,
    OrchestrationApprovalConflict,
    OrchestrationLeaseConflict,
    OrchestrationResultConflict,
    OrchestrationRole,
    OrchestrationRunConflict,
    OrchestrationRunStatus,
    OrchestrationSchedulerIntegrityError,
    RetryPolicy,
    dag_definition_from_payload,
    dag_definition_payload,
)

NOW = datetime(2026, 7, 13, 15, tzinfo=UTC)
BUDGET = NodeBudget(100, 50, 10_000, 1_000)
RETRY = RetryPolicy(2, 5, ("temporary",))


def test_submit_is_content_idempotent_and_reconstructs_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "kernel.sqlite3"
    scheduler = SQLiteOrchestrationScheduler(path)
    definition = _dag()

    submitted = scheduler.submit(
        "run:restart",
        definition,
        submitted_by="operator:test",
        submitted_at=NOW,
    )
    exact_retry = scheduler.submit(
        "run:restart",
        definition,
        submitted_by="operator:retry",
        submitted_at=NOW + timedelta(seconds=1),
    )
    restarted = SQLiteOrchestrationScheduler(path).inspect("run:restart")

    assert submitted == exact_retry == restarted
    assert submitted.status is OrchestrationRunStatus.PENDING
    assert dag_definition_from_payload(dag_definition_payload(definition)) == definition
    changed = DagDefinition(
        definition.dag_id,
        tuple(
            replace(node, retry=RetryPolicy(3, 5, ("temporary",)))
            if node.node_id == "plan"
            else node
            for node in definition.nodes
        ),
    )
    with pytest.raises(OrchestrationRunConflict, match="different DAG"):
        scheduler.submit(
            "run:restart",
            changed,
            submitted_by="operator:test",
            submitted_at=NOW,
        )
    events = EventStore(path).read_stream("orchestration-run:run:restart")
    assert tuple(event.event_type for event in events) == ("OrchestrationRunSubmitted",)


def test_dependencies_approvals_and_terminal_success_are_durable(tmp_path: Path) -> None:
    path = tmp_path / "kernel.sqlite3"
    scheduler = SQLiteOrchestrationScheduler(path)
    scheduler.submit("run:success", _dag(), submitted_by="operator:test", submitted_at=NOW)

    plan = scheduler.acquire(
        "worker:plan",
        lease_seconds=30,
        acquired_at=NOW + timedelta(seconds=1),
    )
    assert plan is not None and plan.node.node_id == "plan"
    assert (
        scheduler.acquire(
            "worker:blocked",
            lease_seconds=30,
            acquired_at=NOW + timedelta(seconds=2),
        )
        is None
    )
    scheduler.complete(
        plan,
        result_digest=_digest("plan"),
        output_schema="plan/v1",
        usage=NodeUsage(5, 2, 10, 1),
        completed_at=NOW + timedelta(seconds=3),
    )
    assert (
        scheduler.acquire(
            "worker:unapproved",
            lease_seconds=30,
            acquired_at=NOW + timedelta(seconds=4),
        )
        is None
    )
    approval = scheduler.record_approval(
        "run:success",
        "execute",
        OrchestrationRole.REVIEWER,
        principal_id="agent:independent-review",
        approved=True,
        decided_at=NOW + timedelta(seconds=5),
    )
    execute = scheduler.acquire(
        "worker:execute",
        lease_seconds=30,
        acquired_at=NOW + timedelta(seconds=6),
    )
    assert approval.approved
    assert execute is not None and execute.node.node_id == "execute"
    terminal = scheduler.complete(
        execute,
        result_digest=_digest("execute"),
        output_schema="change/v1",
        usage=NodeUsage(10, 4, 20, 2),
        completed_at=NOW + timedelta(seconds=7),
    )

    assert terminal.status is NodeStatus.SUCCEEDED
    snapshot = SQLiteOrchestrationScheduler(path).inspect("run:success")
    assert snapshot.status is OrchestrationRunStatus.SUCCEEDED
    assert tuple(node.status for node in snapshot.nodes) == (
        NodeStatus.SUCCEEDED,
        NodeStatus.SUCCEEDED,
    )
    events = EventStore(path).read_stream("orchestration-run:run:success")
    assert tuple(event.stream_sequence for event in events) == tuple(range(1, len(events) + 1))
    assert tuple(event.causation_id for event in events[1:]) == tuple(
        event.event_id for event in events[:-1]
    )
    assert events[-1].event_type == "OrchestrationRunStatusChanged"
    assert events[-1].payload["status"] == "succeeded"
    with connect(path) as connection:
        assert connection.execute("pragma integrity_check").fetchone()[0] == "ok"
        assert connection.execute("pragma foreign_key_check").fetchall() == []


def test_approval_is_independent_idempotent_and_denial_blocks_descendants(
    tmp_path: Path,
) -> None:
    scheduler = SQLiteOrchestrationScheduler(tmp_path / "kernel.sqlite3")
    scheduler.submit("run:denied", _dag(), submitted_by="operator:test", submitted_at=NOW)

    with pytest.raises(OrchestrationApprovalConflict, match="cannot approve"):
        scheduler.record_approval(
            "run:denied",
            "execute",
            OrchestrationRole.REVIEWER,
            principal_id="agent:execute",
            approved=True,
            decided_at=NOW,
        )
    denied = scheduler.record_approval(
        "run:denied",
        "execute",
        OrchestrationRole.REVIEWER,
        principal_id="agent:review",
        approved=False,
        decided_at=NOW + timedelta(seconds=1),
    )
    exact = scheduler.record_approval(
        "run:denied",
        "execute",
        OrchestrationRole.REVIEWER,
        principal_id="agent:review",
        approved=False,
        decided_at=NOW + timedelta(seconds=9),
    )

    assert denied == exact
    snapshot = scheduler.inspect("run:denied")
    assert snapshot.status is OrchestrationRunStatus.DENIED
    assert snapshot.nodes[0].status is NodeStatus.DENIED
    with pytest.raises(OrchestrationApprovalConflict, match="already decided"):
        scheduler.record_approval(
            "run:denied",
            "execute",
            OrchestrationRole.REVIEWER,
            principal_id="agent:review",
            approved=True,
            decided_at=NOW + timedelta(seconds=10),
        )


def test_retry_backoff_fencing_and_exact_failure_are_enforced(tmp_path: Path) -> None:
    scheduler = SQLiteOrchestrationScheduler(tmp_path / "kernel.sqlite3")
    scheduler.submit(
        "run:retry",
        DagDefinition("dag:retry", (_node("work"),)),
        submitted_by="operator:test",
        submitted_at=NOW,
    )
    first = scheduler.acquire(
        "worker:first",
        lease_seconds=30,
        acquired_at=NOW + timedelta(seconds=1),
    )
    assert first is not None
    failed = scheduler.fail(
        first,
        failure_code="temporary",
        usage=NodeUsage(3, 0, 10, 0),
        failed_at=NOW + timedelta(seconds=2),
    )
    exact = scheduler.fail(
        first,
        failure_code="temporary",
        usage=NodeUsage(3, 0, 10, 0),
        failed_at=NOW + timedelta(seconds=3),
    )

    assert failed == exact
    assert failed.status is NodeStatus.PENDING
    assert (
        scheduler.acquire(
            "worker:early",
            lease_seconds=30,
            acquired_at=NOW + timedelta(seconds=6),
        )
        is None
    )
    second = scheduler.acquire(
        "worker:second",
        lease_seconds=30,
        acquired_at=NOW + timedelta(seconds=7),
    )
    assert second is not None
    assert (second.attempt, second.fencing_token) == (2, 2)
    with pytest.raises(OrchestrationResultConflict, match="different terminal outcome"):
        scheduler.complete(
            first,
            result_digest=_digest("stale"),
            output_schema="output/v1",
            usage=NodeUsage(),
            completed_at=NOW + timedelta(seconds=8),
        )
    completed = scheduler.complete(
        second,
        result_digest=_digest("retry-success"),
        output_schema="output/v1",
        usage=NodeUsage(4, 1, 20, 1),
        completed_at=NOW + timedelta(seconds=9),
    )
    assert completed.usage == NodeUsage(7, 1, 30, 1)


def test_completion_is_exactly_idempotent_and_validates_schema_budget_and_lease(
    tmp_path: Path,
) -> None:
    scheduler = SQLiteOrchestrationScheduler(tmp_path / "kernel.sqlite3")
    scheduler.submit(
        "run:completion",
        DagDefinition("dag:completion", (_node("work"),)),
        submitted_by="operator:test",
        submitted_at=NOW,
    )
    lease = scheduler.acquire(
        "worker:one",
        lease_seconds=30,
        acquired_at=NOW + timedelta(seconds=1),
    )
    assert lease is not None
    with pytest.raises(OrchestrationResultConflict, match="output schema"):
        scheduler.complete(
            lease,
            result_digest=_digest("wrong-schema"),
            output_schema="other/v1",
            usage=NodeUsage(),
            completed_at=NOW + timedelta(seconds=2),
        )
    with pytest.raises(OrchestrationResultConflict, match="usage budget"):
        scheduler.complete(
            lease,
            result_digest=_digest("over-budget"),
            output_schema="output/v1",
            usage=NodeUsage(input_tokens=101),
            completed_at=NOW + timedelta(seconds=3),
        )
    completed = scheduler.complete(
        lease,
        result_digest=_digest("accepted"),
        output_schema="output/v1",
        usage=NodeUsage(10, 2, 20, 1),
        completed_at=NOW + timedelta(seconds=4),
    )
    exact = scheduler.complete(
        lease,
        result_digest=_digest("accepted"),
        output_schema="output/v1",
        usage=NodeUsage(10, 2, 20, 1),
        completed_at=NOW + timedelta(seconds=5),
    )

    assert completed == exact
    assert completed.status is NodeStatus.SUCCEEDED
    forged_exact = replace(lease, expires_at=lease.expires_at + timedelta(seconds=1))
    with pytest.raises(OrchestrationResultConflict, match="different terminal outcome"):
        scheduler.complete(
            forged_exact,
            result_digest=_digest("accepted"),
            output_schema="output/v1",
            usage=NodeUsage(10, 2, 20, 1),
            completed_at=NOW + timedelta(seconds=5),
        )
    with pytest.raises(OrchestrationResultConflict, match="different terminal outcome"):
        scheduler.complete(
            lease,
            result_digest=_digest("changed"),
            output_schema="output/v1",
            usage=NodeUsage(10, 2, 20, 1),
            completed_at=NOW + timedelta(seconds=6),
        )


def test_expired_leases_retry_then_fail_and_block_descendants(tmp_path: Path) -> None:
    scheduler = SQLiteOrchestrationScheduler(tmp_path / "kernel.sqlite3")
    child = _node(
        "child",
        depends_on=("root",),
        inputs=(NodeInputBinding("root", "root", "output/v1"),),
    )
    scheduler.submit(
        "run:recovery",
        DagDefinition("dag:recovery", (_node("root"), child)),
        submitted_by="operator:test",
        submitted_at=NOW,
    )
    first = scheduler.acquire("worker:lost", lease_seconds=10, acquired_at=NOW)
    assert first is not None
    recovered = scheduler.recover_expired(recovered_at=NOW + timedelta(seconds=10))
    assert len(recovered) == 1 and recovered[0].status is NodeStatus.PENDING
    assert scheduler.recover_expired(recovered_at=NOW + timedelta(seconds=11)) == ()
    second = scheduler.acquire(
        "worker:lost-again",
        lease_seconds=10,
        acquired_at=NOW + timedelta(seconds=15),
    )
    assert second is not None and second.fencing_token == 2
    terminal = scheduler.recover_expired(recovered_at=NOW + timedelta(seconds=25))

    assert len(terminal) == 1 and terminal[0].status is NodeStatus.FAILED
    snapshot = SQLiteOrchestrationScheduler(scheduler.path).inspect("run:recovery")
    assert snapshot.status is OrchestrationRunStatus.FAILED
    assert tuple(node.status for node in snapshot.nodes) == (
        NodeStatus.BLOCKED,
        NodeStatus.FAILED,
    )
    with pytest.raises(OrchestrationResultConflict, match="different terminal outcome"):
        scheduler.complete(
            second,
            result_digest=_digest("late"),
            output_schema="output/v1",
            usage=NodeUsage(),
            completed_at=NOW + timedelta(seconds=26),
        )


def test_terminal_failure_fences_other_active_branches(tmp_path: Path) -> None:
    scheduler = SQLiteOrchestrationScheduler(tmp_path / "kernel.sqlite3")
    scheduler.submit(
        "run:fail-fast",
        DagDefinition("dag:fail-fast", (_node("left"), _node("right"))),
        submitted_by="operator:test",
        submitted_at=NOW,
    )
    left = scheduler.acquire("worker:left", lease_seconds=30, acquired_at=NOW)
    right = scheduler.acquire(
        "worker:right",
        lease_seconds=30,
        acquired_at=NOW + timedelta(seconds=1),
    )
    assert left is not None and left.node.node_id == "left"
    assert right is not None and right.node.node_id == "right"

    scheduler.fail(
        left,
        failure_code="fatal",
        usage=NodeUsage(),
        failed_at=NOW + timedelta(seconds=2),
    )
    snapshot = scheduler.inspect("run:fail-fast")

    assert snapshot.status is OrchestrationRunStatus.FAILED
    assert tuple(node.status for node in snapshot.nodes) == (
        NodeStatus.FAILED,
        NodeStatus.BLOCKED,
    )
    assert snapshot.nodes[1].failure_code == "run-terminal"
    with pytest.raises(OrchestrationLeaseConflict, match="no longer"):
        scheduler.complete(
            right,
            result_digest=_digest("fenced"),
            output_schema="output/v1",
            usage=NodeUsage(),
            completed_at=NOW + timedelta(seconds=3),
        )


def test_concurrent_schedulers_issue_only_one_current_lease(tmp_path: Path) -> None:
    path = tmp_path / "kernel.sqlite3"
    first = SQLiteOrchestrationScheduler(path)
    second = SQLiteOrchestrationScheduler(path)
    first.submit(
        "run:concurrent",
        DagDefinition("dag:concurrent", (_node("work"),)),
        submitted_by="operator:test",
        submitted_at=NOW,
    )
    barrier = Barrier(2)

    def acquire(scheduler: SQLiteOrchestrationScheduler, worker_id: str):
        barrier.wait()
        return scheduler.acquire(worker_id, lease_seconds=30, acquired_at=NOW)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(
            future.result()
            for future in (
                pool.submit(acquire, first, "worker:first"),
                pool.submit(acquire, second, "worker:second"),
            )
        )

    leases = tuple(result for result in results if result is not None)
    assert len(leases) == 1
    snapshot = first.inspect("run:concurrent")
    assert snapshot.nodes[0].status is NodeStatus.RUNNING
    assert snapshot.nodes[0].lease_worker_id == leases[0].worker_id


def test_scheduler_state_rolls_back_when_kernel_event_append_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "kernel.sqlite3"
    scheduler = SQLiteOrchestrationScheduler(path)

    def reject_append(
        transaction: SQLiteKernelTransaction,
        events,
        *,
        expected_sequences,
    ):
        del transaction, events, expected_sequences
        raise RuntimeError("event append failed")

    monkeypatch.setattr(SQLiteKernelTransaction, "append_events", reject_append)
    with pytest.raises(RuntimeError, match="event append failed"):
        scheduler.submit(
            "run:rollback",
            DagDefinition("dag:rollback", (_node("work"),)),
            submitted_by="operator:test",
            submitted_at=NOW,
        )

    with pytest.raises(LookupError, match="does not exist"):
        scheduler.inspect("run:rollback")
    assert EventStore(path).read_stream("orchestration-run:run:rollback") == ()


def test_corrupt_persisted_definition_and_unbounded_failure_codes_fail_closed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "kernel.sqlite3"
    scheduler = SQLiteOrchestrationScheduler(path)
    scheduler.submit(
        "run:integrity",
        DagDefinition("dag:integrity", (_node("work"),)),
        submitted_by="operator:test",
        submitted_at=NOW,
    )
    lease = scheduler.acquire("worker:test", lease_seconds=30, acquired_at=NOW)
    assert lease is not None
    with pytest.raises(ValueError, match="content-free"):
        scheduler.fail(
            lease,
            failure_code="secret details leaked",
            usage=NodeUsage(),
            failed_at=NOW + timedelta(seconds=1),
        )
    with connect(path) as connection:
        connection.execute(
            "update orchestration_runs set definition_json = '{}' where run_id = ?",
            ("run:integrity",),
        )
    with pytest.raises(OrchestrationSchedulerIntegrityError, match="failed validation"):
        scheduler.inspect("run:integrity")


def test_restart_rejects_incomplete_scheduler_schema(tmp_path: Path) -> None:
    path = tmp_path / "kernel.sqlite3"
    SQLiteOrchestrationScheduler(path)
    with connect(path) as connection:
        connection.execute("drop table orchestration_attempt_outcomes")

    with pytest.raises(OrchestrationSchedulerIntegrityError, match="missing tables"):
        SQLiteOrchestrationScheduler(path)


def test_expired_or_forged_lease_cannot_complete(tmp_path: Path) -> None:
    scheduler = SQLiteOrchestrationScheduler(tmp_path / "kernel.sqlite3")
    scheduler.submit(
        "run:lease",
        DagDefinition("dag:lease", (_node("work"),)),
        submitted_by="operator:test",
        submitted_at=NOW,
    )
    lease = scheduler.acquire("worker:test", lease_seconds=5, acquired_at=NOW)
    assert lease is not None
    forged = replace(lease, worker_id="worker:forged")
    with pytest.raises(OrchestrationLeaseConflict, match="stale"):
        scheduler.complete(
            forged,
            result_digest=_digest("forged"),
            output_schema="output/v1",
            usage=NodeUsage(),
            completed_at=NOW + timedelta(seconds=1),
        )
    with pytest.raises(OrchestrationLeaseConflict, match="expired"):
        scheduler.complete(
            lease,
            result_digest=_digest("expired"),
            output_schema="output/v1",
            usage=NodeUsage(),
            completed_at=NOW + timedelta(seconds=5),
        )


def _dag() -> DagDefinition:
    plan = _node(
        "plan",
        role=OrchestrationRole.PLANNER,
        output_schema="plan/v1",
        principal="agent:plan",
    )
    execute = _node(
        "execute",
        output_schema="change/v1",
        principal="agent:execute",
        depends_on=("plan",),
        inputs=(NodeInputBinding("plan", "plan", "plan/v1"),),
        side_effect=NodeSideEffect.REVERSIBLE,
        approvals=(OrchestrationRole.REVIEWER,),
    )
    return DagDefinition("dag:scheduler", (execute, plan))


def _node(
    node_id: str,
    *,
    role: OrchestrationRole = OrchestrationRole.EXECUTOR,
    output_schema: str = "output/v1",
    principal: str | None = None,
    depends_on: tuple[str, ...] = (),
    inputs: tuple[NodeInputBinding, ...] = (),
    side_effect: NodeSideEffect = NodeSideEffect.NONE,
    approvals: tuple[OrchestrationRole, ...] = (),
) -> DagNode:
    return DagNode(
        node_id=node_id,
        role=role,
        principal_id=principal or f"agent:{node_id}",
        handler=f"handler:{node_id}",
        output_schema=output_schema,
        depends_on=depends_on,
        inputs=inputs,
        retry=RETRY,
        timeout_seconds=30,
        budget=BUDGET,
        side_effect=side_effect,
        required_approvals=approvals,
    )


def _digest(value: str) -> str:
    return json_digest({"value": value})
