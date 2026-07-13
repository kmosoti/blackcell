from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from blackcell.adapters.persistence.sqlite.session import (
    SQLiteKernelSession,
    SQLiteKernelTransaction,
)
from blackcell.kernel import EventEnvelope, JsonInput, utc_now
from blackcell.kernel._json import canonical_json, json_digest
from blackcell.kernel.database import connect
from blackcell.orchestration.models import (
    DagDefinition,
    DagNode,
    NodeStatus,
    NodeUsage,
    OrchestrationRole,
    OrchestrationRunStatus,
    dag_definition_from_payload,
    dag_definition_payload,
)
from blackcell.orchestration.scheduler import (
    OrchestrationApproval,
    OrchestrationApprovalConflict,
    OrchestrationLeaseConflict,
    OrchestrationNodeLease,
    OrchestrationNodeSnapshot,
    OrchestrationResultConflict,
    OrchestrationRunConflict,
    OrchestrationRunSnapshot,
    OrchestrationSchedulerIntegrityError,
)

_SCHEMA_VERSION = 1
_SOURCE = "blackcell.orchestration.scheduler"
_MAX_LEASE_SECONDS = 86_400
_FAILURE_CODE = re.compile(r"[a-z0-9][a-z0-9._-]{0,99}\Z")
_REQUIRED_TABLES = frozenset(
    {
        "orchestration_approvals",
        "orchestration_attempt_outcomes",
        "orchestration_dependencies",
        "orchestration_nodes",
        "orchestration_required_approvals",
        "orchestration_runs",
        "orchestration_schema_migrations",
    }
)

_MIGRATION_SCHEMA = """
create table if not exists orchestration_schema_migrations (
    version integer primary key,
    applied_at text not null
)
"""

_SCHEMA = """
create table if not exists orchestration_runs (
    run_id text primary key check(length(run_id) > 0),
    dag_id text not null check(length(dag_id) > 0),
    dag_digest text not null check(length(dag_digest) > 0),
    definition_json text not null,
    status text not null check(status in ('pending', 'running', 'succeeded', 'failed', 'denied')),
    submitted_by text not null check(length(submitted_by) > 0),
    submitted_at text not null,
    updated_at text not null
);

create index if not exists idx_orchestration_runs_status
    on orchestration_runs(status, submitted_at, run_id);

create table if not exists orchestration_nodes (
    run_id text not null,
    node_id text not null check(length(node_id) > 0),
    node_digest text not null check(length(node_digest) > 0),
    status text not null check(
        status in ('pending', 'running', 'succeeded', 'failed', 'blocked', 'denied')
    ),
    attempts integer not null default 0 check(attempts >= 0),
    fencing_token integer not null default 0 check(fencing_token >= 0),
    available_at text not null,
    lease_worker_id text,
    lease_acquired_at text,
    lease_expires_at text,
    completion_digest text,
    result_digest text,
    failure_code text,
    input_tokens integer not null default 0 check(input_tokens >= 0),
    output_tokens integer not null default 0 check(output_tokens >= 0),
    latency_ms integer not null default 0 check(latency_ms >= 0),
    cost_microusd integer not null default 0 check(cost_microusd >= 0),
    updated_at text not null,
    primary key(run_id, node_id),
    foreign key(run_id) references orchestration_runs(run_id) on delete cascade,
    check(
        (status = 'running') =
        (lease_worker_id is not null
         and lease_acquired_at is not null
         and lease_expires_at is not null)
    ),
    check(
        (status = 'succeeded') =
        (completion_digest is not null and result_digest is not null)
    )
);

create index if not exists idx_orchestration_nodes_ready
    on orchestration_nodes(status, available_at, run_id, node_id);
create index if not exists idx_orchestration_nodes_lease
    on orchestration_nodes(status, lease_expires_at, run_id, node_id);

create table if not exists orchestration_dependencies (
    run_id text not null,
    node_id text not null,
    dependency_node_id text not null,
    primary key(run_id, node_id, dependency_node_id),
    foreign key(run_id, node_id) references orchestration_nodes(run_id, node_id) on delete cascade,
    foreign key(run_id, dependency_node_id)
        references orchestration_nodes(run_id, node_id) on delete cascade
);

create table if not exists orchestration_required_approvals (
    run_id text not null,
    node_id text not null,
    role text not null check(role in ('reviewer', 'verifier')),
    primary key(run_id, node_id, role),
    foreign key(run_id, node_id) references orchestration_nodes(run_id, node_id) on delete cascade
);

create table if not exists orchestration_approvals (
    run_id text not null,
    node_id text not null,
    role text not null check(role in ('reviewer', 'verifier')),
    principal_id text not null check(length(principal_id) > 0),
    approved integer not null check(approved in (0, 1)),
    decided_at text not null,
    decision_digest text not null check(length(decision_digest) > 0),
    primary key(run_id, node_id, role),
    foreign key(run_id, node_id, role)
        references orchestration_required_approvals(run_id, node_id, role) on delete cascade
);

create table if not exists orchestration_attempt_outcomes (
    run_id text not null,
    node_id text not null,
    fencing_token integer not null check(fencing_token >= 1),
    outcome text not null check(outcome in ('succeeded', 'failed', 'lease-expired')),
    outcome_digest text not null check(length(outcome_digest) > 0),
    result_digest text,
    failure_code text,
    input_tokens integer not null check(input_tokens >= 0),
    output_tokens integer not null check(output_tokens >= 0),
    latency_ms integer not null check(latency_ms >= 0),
    cost_microusd integer not null check(cost_microusd >= 0),
    recorded_at text not null,
    primary key(run_id, node_id, fencing_token),
    foreign key(run_id, node_id) references orchestration_nodes(run_id, node_id) on delete cascade,
    check((outcome = 'succeeded') = (result_digest is not null)),
    check((outcome != 'succeeded') = (failure_code is not null))
);
"""

Clock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class _Transition:
    event_type: str
    idempotency_key: str
    payload: Mapping[str, JsonInput]


class _SqlExecutor(Protocol):
    def execute(
        self,
        statement: str,
        parameters: tuple[object, ...] = (),
        /,
    ) -> sqlite3.Cursor: ...


class SQLiteOrchestrationScheduler:
    """Restart-safe local DAG scheduler with leases, fencing, and kernel events."""

    def __init__(self, path: Path | str, *, clock: Clock = utc_now) -> None:
        self.path = Path(path)
        self._clock = clock
        self._session = SQLiteKernelSession(self.path)
        self._initialize_schema()

    def submit(
        self,
        run_id: str,
        definition: DagDefinition,
        *,
        submitted_by: str,
        submitted_at: datetime | None = None,
    ) -> OrchestrationRunSnapshot:
        _required_text(run_id, "run_id")
        _required_text(submitted_by, "submitted_by")
        at = _timestamp(submitted_at or self._clock(), "submitted_at")
        definition_json = canonical_json(dag_definition_payload(definition))
        with self._session.transaction() as transaction:
            existing = transaction.execute(
                "select dag_digest from orchestration_runs where run_id = ?",
                (run_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["dag_digest"]) != definition.dag_digest:
                    raise OrchestrationRunConflict(
                        f"run {run_id!r} already owns a different DAG definition"
                    )
                return self._snapshot(transaction, run_id)

            transaction.execute(
                """
                insert into orchestration_runs(
                    run_id, dag_id, dag_digest, definition_json, status,
                    submitted_by, submitted_at, updated_at
                ) values (?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    run_id,
                    definition.dag_id,
                    definition.dag_digest,
                    definition_json,
                    submitted_by,
                    at.isoformat(),
                    at.isoformat(),
                ),
            )
            for node in definition.nodes:
                transaction.execute(
                    """
                    insert into orchestration_nodes(
                        run_id, node_id, node_digest, status, available_at, updated_at
                    ) values (?, ?, ?, 'pending', ?, ?)
                    """,
                    (run_id, node.node_id, node.node_digest, at.isoformat(), at.isoformat()),
                )
            for node in definition.nodes:
                for dependency in node.depends_on:
                    transaction.execute(
                        """
                        insert into orchestration_dependencies(
                            run_id, node_id, dependency_node_id
                        ) values (?, ?, ?)
                        """,
                        (run_id, node.node_id, dependency),
                    )
                for role in node.required_approvals:
                    transaction.execute(
                        """
                        insert into orchestration_required_approvals(run_id, node_id, role)
                        values (?, ?, ?)
                        """,
                        (run_id, node.node_id, role.value),
                    )
            self._append_transitions(
                transaction,
                run_id,
                submitted_by,
                at,
                (
                    _Transition(
                        "OrchestrationRunSubmitted",
                        f"orchestration:{run_id}:submitted",
                        {
                            "run_id": run_id,
                            "dag_id": definition.dag_id,
                            "dag_digest": definition.dag_digest,
                            "node_count": len(definition.nodes),
                        },
                    ),
                ),
            )
            return self._snapshot(transaction, run_id)

    def record_approval(
        self,
        run_id: str,
        node_id: str,
        role: OrchestrationRole,
        *,
        principal_id: str,
        approved: bool,
        decided_at: datetime | None = None,
    ) -> OrchestrationApproval:
        _required_text(run_id, "run_id")
        _required_text(node_id, "node_id")
        _required_text(principal_id, "principal_id")
        if not isinstance(role, OrchestrationRole):
            raise TypeError("approval role must be recognized")
        if not isinstance(approved, bool):
            raise TypeError("approved must be a boolean")
        at = _timestamp(decided_at or self._clock(), "decided_at")
        decision_digest = json_digest(
            {
                "approved": approved,
                "node_id": node_id,
                "principal_id": principal_id,
                "role": role.value,
                "run_id": run_id,
            }
        )
        with self._session.transaction() as transaction:
            definition = self._definition(transaction, run_id)
            node = _definition_node(definition, node_id)
            if role not in node.required_approvals:
                raise OrchestrationApprovalConflict(
                    f"node {node_id!r} does not require {role.value!r} approval"
                )
            if principal_id == node.principal_id:
                raise OrchestrationApprovalConflict("a node principal cannot approve its own work")
            existing = transaction.execute(
                """
                select principal_id, approved, decided_at, decision_digest
                from orchestration_approvals
                where run_id = ? and node_id = ? and role = ?
                """,
                (run_id, node_id, role.value),
            ).fetchone()
            if existing is not None:
                if str(existing["decision_digest"]) != decision_digest:
                    raise OrchestrationApprovalConflict(
                        f"approval for {node_id!r}/{role.value!r} is already decided"
                    )
                return _approval_from_row(node_id, role, existing)
            state = self._node_row(transaction, run_id, node_id)
            if NodeStatus(str(state["status"])) is not NodeStatus.PENDING:
                raise OrchestrationApprovalConflict("only pending nodes may receive approvals")
            transaction.execute(
                """
                insert into orchestration_approvals(
                    run_id, node_id, role, principal_id, approved, decided_at, decision_digest
                ) values (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    node_id,
                    role.value,
                    principal_id,
                    int(approved),
                    at.isoformat(),
                    decision_digest,
                ),
            )
            transitions: list[_Transition] = [
                _Transition(
                    "OrchestrationApprovalRecorded",
                    f"orchestration:{run_id}:node:{node_id}:approval:{role.value}",
                    {
                        "approved": approved,
                        "node_id": node_id,
                        "principal_id": principal_id,
                        "role": role.value,
                    },
                )
            ]
            if not approved:
                transaction.execute(
                    """
                    update orchestration_nodes
                    set status = 'denied', failure_code = 'approval-denied', updated_at = ?
                    where run_id = ? and node_id = ? and status = 'pending'
                    """,
                    (at.isoformat(), run_id, node_id),
                )
                transitions.append(
                    _Transition(
                        "OrchestrationNodeDenied",
                        f"orchestration:{run_id}:node:{node_id}:denied",
                        {"failure_code": "approval-denied", "node_id": node_id},
                    )
                )
                transitions.extend(self._block_descendants(transaction, run_id, at))
                transitions.extend(self._block_remaining(transaction, run_id, at))
            transitions.extend(self._update_run_status(transaction, run_id, at))
            self._append_transitions(transaction, run_id, principal_id, at, transitions)
            row = transaction.execute(
                """
                select principal_id, approved, decided_at, decision_digest
                from orchestration_approvals
                where run_id = ? and node_id = ? and role = ?
                """,
                (run_id, node_id, role.value),
            ).fetchone()
            if row is None:  # pragma: no cover - same-transaction insert invariant
                raise OrchestrationSchedulerIntegrityError("approval insert disappeared")
            return _approval_from_row(node_id, role, row)

    def acquire(
        self,
        worker_id: str,
        *,
        lease_seconds: int,
        acquired_at: datetime | None = None,
    ) -> OrchestrationNodeLease | None:
        _required_text(worker_id, "worker_id")
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int):
            raise TypeError("lease_seconds must be an integer")
        if not 1 <= lease_seconds <= _MAX_LEASE_SECONDS:
            raise ValueError(f"lease_seconds must be between 1 and {_MAX_LEASE_SECONDS}")
        at = _timestamp(acquired_at or self._clock(), "acquired_at")
        with self._session.transaction() as transaction:
            candidates = transaction.execute(
                """
                select n.run_id, n.node_id
                from orchestration_nodes n
                join orchestration_runs r on r.run_id = n.run_id
                where n.status = 'pending'
                  and n.available_at <= ?
                  and r.status in ('pending', 'running')
                  and not exists (
                      select 1
                      from orchestration_dependencies d
                      join orchestration_nodes parent
                        on parent.run_id = d.run_id
                       and parent.node_id = d.dependency_node_id
                      where d.run_id = n.run_id
                        and d.node_id = n.node_id
                        and parent.status != 'succeeded'
                  )
                  and not exists (
                      select 1
                      from orchestration_required_approvals required
                      where required.run_id = n.run_id
                        and required.node_id = n.node_id
                        and not exists (
                            select 1
                            from orchestration_approvals approval
                            where approval.run_id = required.run_id
                              and approval.node_id = required.node_id
                              and approval.role = required.role
                              and approval.approved = 1
                        )
                  )
                order by r.submitted_at, n.run_id, n.node_id
                """,
                (at.isoformat(),),
            ).fetchall()
            for candidate in candidates:
                run_id = str(candidate["run_id"])
                node_id = str(candidate["node_id"])
                definition = self._definition(transaction, run_id)
                node = _definition_node(definition, node_id)
                state = self._node_row(transaction, run_id, node_id)
                attempts = int(state["attempts"]) + 1
                if attempts > node.retry.max_attempts:
                    raise OrchestrationSchedulerIntegrityError(
                        f"pending node {node_id!r} exhausted its attempts"
                    )
                fencing_token = int(state["fencing_token"]) + 1
                effective_seconds = min(lease_seconds, node.timeout_seconds)
                expires_at = at + timedelta(seconds=effective_seconds)
                changed = transaction.execute(
                    """
                    update orchestration_nodes
                    set status = 'running', attempts = ?, fencing_token = ?,
                        lease_worker_id = ?, lease_acquired_at = ?, lease_expires_at = ?,
                        failure_code = null, updated_at = ?
                    where run_id = ? and node_id = ? and status = 'pending'
                    """,
                    (
                        attempts,
                        fencing_token,
                        worker_id,
                        at.isoformat(),
                        expires_at.isoformat(),
                        at.isoformat(),
                        run_id,
                        node_id,
                    ),
                ).rowcount
                if changed != 1:  # pragma: no cover - begin-immediate serialization invariant
                    continue
                transitions = [
                    _Transition(
                        "OrchestrationNodeLeased",
                        f"orchestration:{run_id}:node:{node_id}:lease:{fencing_token}",
                        {
                            "attempt": attempts,
                            "expires_at": expires_at.isoformat(),
                            "fencing_token": fencing_token,
                            "node_id": node_id,
                            "worker_id": worker_id,
                        },
                    )
                ]
                transitions.extend(self._update_run_status(transaction, run_id, at))
                self._append_transitions(transaction, run_id, worker_id, at, transitions)
                return OrchestrationNodeLease(
                    run_id,
                    node,
                    attempts,
                    fencing_token,
                    worker_id,
                    at,
                    expires_at,
                )
            return None

    def complete(
        self,
        lease: OrchestrationNodeLease,
        *,
        result_digest: str,
        output_schema: str,
        usage: NodeUsage,
        completed_at: datetime | None = None,
    ) -> OrchestrationNodeSnapshot:
        _content_digest(result_digest, "result_digest")
        _required_text(output_schema, "output_schema")
        if not isinstance(usage, NodeUsage):
            raise TypeError("usage must be NodeUsage")
        at = _timestamp(completed_at or self._clock(), "completed_at")
        outcome_digest = json_digest(
            {
                "lease": _lease_identity_payload(lease),
                "output_schema": output_schema,
                "result_digest": result_digest,
                "usage": _usage_payload(usage),
            }
        )
        with self._session.transaction() as transaction:
            existing = self._idempotent_outcome(
                transaction,
                lease,
                outcome_digest,
                OrchestrationResultConflict,
            )
            if existing:
                return self._node_snapshot(transaction, lease.run_id, lease.node.node_id)
            definition = self._definition(transaction, lease.run_id)
            node = _definition_node(definition, lease.node.node_id)
            state = self._validate_active_lease(transaction, lease, node, at)
            if output_schema != node.output_schema:
                raise OrchestrationResultConflict(
                    "completion output schema does not match the node"
                )
            cumulative = _usage_from_row(state) + usage
            if cumulative.exceeds(node.budget):
                raise OrchestrationResultConflict("completion exceeds the node usage budget")
            transaction.execute(
                """
                update orchestration_nodes
                set status = 'succeeded', completion_digest = ?, result_digest = ?,
                    failure_code = null, input_tokens = ?, output_tokens = ?, latency_ms = ?,
                    cost_microusd = ?, lease_worker_id = null, lease_acquired_at = null,
                    lease_expires_at = null, updated_at = ?
                where run_id = ? and node_id = ?
                """,
                (
                    outcome_digest,
                    result_digest,
                    *_usage_values(cumulative),
                    at.isoformat(),
                    lease.run_id,
                    node.node_id,
                ),
            )
            self._insert_outcome(
                transaction,
                lease,
                "succeeded",
                outcome_digest,
                usage,
                at,
                result_digest=result_digest,
            )
            transitions = [
                _Transition(
                    "OrchestrationNodeSucceeded",
                    f"orchestration:{lease.run_id}:node:{node.node_id}:outcome:{lease.fencing_token}",
                    {
                        "attempt": lease.attempt,
                        "fencing_token": lease.fencing_token,
                        "node_id": node.node_id,
                        "result_digest": result_digest,
                        "usage": _usage_payload(usage),
                    },
                )
            ]
            transitions.extend(self._update_run_status(transaction, lease.run_id, at))
            self._append_transitions(
                transaction,
                lease.run_id,
                lease.worker_id,
                at,
                transitions,
            )
            return self._node_snapshot(transaction, lease.run_id, node.node_id)

    def fail(
        self,
        lease: OrchestrationNodeLease,
        *,
        failure_code: str,
        usage: NodeUsage,
        failed_at: datetime | None = None,
    ) -> OrchestrationNodeSnapshot:
        _bounded_failure_code(failure_code)
        if not isinstance(usage, NodeUsage):
            raise TypeError("usage must be NodeUsage")
        at = _timestamp(failed_at or self._clock(), "failed_at")
        outcome_digest = json_digest(
            {
                "failure_code": failure_code,
                "lease": _lease_identity_payload(lease),
                "usage": _usage_payload(usage),
            }
        )
        with self._session.transaction() as transaction:
            existing = self._idempotent_outcome(
                transaction,
                lease,
                outcome_digest,
                OrchestrationResultConflict,
            )
            if existing:
                return self._node_snapshot(transaction, lease.run_id, lease.node.node_id)
            definition = self._definition(transaction, lease.run_id)
            node = _definition_node(definition, lease.node.node_id)
            state = self._validate_active_lease(transaction, lease, node, at)
            cumulative = _usage_from_row(state) + usage
            effective_code = "budget-exceeded" if cumulative.exceeds(node.budget) else failure_code
            retryable = (
                effective_code in node.retry.retryable_codes
                and lease.attempt < node.retry.max_attempts
            )
            status = NodeStatus.PENDING if retryable else NodeStatus.FAILED
            available_at = at + timedelta(seconds=node.retry.backoff_seconds)
            transaction.execute(
                """
                update orchestration_nodes
                set status = ?, available_at = ?, failure_code = ?,
                    input_tokens = ?, output_tokens = ?, latency_ms = ?, cost_microusd = ?,
                    lease_worker_id = null, lease_acquired_at = null, lease_expires_at = null,
                    updated_at = ?
                where run_id = ? and node_id = ?
                """,
                (
                    status.value,
                    available_at.isoformat(),
                    effective_code,
                    *_usage_values(cumulative),
                    at.isoformat(),
                    lease.run_id,
                    node.node_id,
                ),
            )
            self._insert_outcome(
                transaction,
                lease,
                "failed",
                outcome_digest,
                usage,
                at,
                failure_code=effective_code,
            )
            event_type = (
                "OrchestrationNodeRetryScheduled" if retryable else "OrchestrationNodeFailed"
            )
            transitions: list[_Transition] = [
                _Transition(
                    event_type,
                    f"orchestration:{lease.run_id}:node:{node.node_id}:outcome:{lease.fencing_token}",
                    {
                        "attempt": lease.attempt,
                        "available_at": available_at.isoformat(),
                        "failure_code": effective_code,
                        "fencing_token": lease.fencing_token,
                        "node_id": node.node_id,
                        "retryable": retryable,
                        "usage": _usage_payload(usage),
                    },
                )
            ]
            if not retryable:
                transitions.extend(self._block_descendants(transaction, lease.run_id, at))
                transitions.extend(self._block_remaining(transaction, lease.run_id, at))
            transitions.extend(self._update_run_status(transaction, lease.run_id, at))
            self._append_transitions(
                transaction,
                lease.run_id,
                lease.worker_id,
                at,
                transitions,
            )
            return self._node_snapshot(transaction, lease.run_id, node.node_id)

    def recover_expired(
        self,
        *,
        recovered_at: datetime | None = None,
    ) -> tuple[OrchestrationNodeSnapshot, ...]:
        at = _timestamp(recovered_at or self._clock(), "recovered_at")
        with connect(self.path) as connection:
            expired = connection.execute(
                """
                select run_id, node_id
                from orchestration_nodes
                where status = 'running' and lease_expires_at <= ?
                order by run_id, node_id
                """,
                (at.isoformat(),),
            ).fetchall()
        recovered: list[OrchestrationNodeSnapshot] = []
        for candidate in expired:
            run_id = str(candidate["run_id"])
            node_id = str(candidate["node_id"])
            result = self._recover_one(run_id, node_id, at)
            if result is not None:
                recovered.append(result)
        return tuple(recovered)

    def inspect(self, run_id: str) -> OrchestrationRunSnapshot:
        _required_text(run_id, "run_id")
        with connect(self.path) as connection:
            return self._snapshot(connection, run_id)

    def _recover_one(
        self,
        run_id: str,
        node_id: str,
        at: datetime,
    ) -> OrchestrationNodeSnapshot | None:
        with self._session.transaction() as transaction:
            state = self._node_row(transaction, run_id, node_id)
            if NodeStatus(str(state["status"])) is not NodeStatus.RUNNING:
                return None
            expires_at = datetime.fromisoformat(str(state["lease_expires_at"]))
            if expires_at > at:
                return None
            definition = self._definition(transaction, run_id)
            node = _definition_node(definition, node_id)
            attempt = int(state["attempts"])
            fencing_token = int(state["fencing_token"])
            worker_id = str(state["lease_worker_id"])
            lease = OrchestrationNodeLease(
                run_id,
                node,
                attempt,
                fencing_token,
                worker_id,
                datetime.fromisoformat(str(state["lease_acquired_at"])),
                expires_at,
            )
            outcome_digest = json_digest(
                {
                    "failure_code": "lease-expired",
                    "lease": _lease_identity_payload(lease),
                }
            )
            existing = transaction.execute(
                """
                select outcome_digest from orchestration_attempt_outcomes
                where run_id = ? and node_id = ? and fencing_token = ?
                """,
                (run_id, node_id, fencing_token),
            ).fetchone()
            if existing is not None:
                raise OrchestrationSchedulerIntegrityError(
                    "a running expired lease already has a terminal attempt outcome"
                )
            retryable = attempt < node.retry.max_attempts
            status = NodeStatus.PENDING if retryable else NodeStatus.FAILED
            available_at = at + timedelta(seconds=node.retry.backoff_seconds)
            transaction.execute(
                """
                update orchestration_nodes
                set status = ?, available_at = ?, failure_code = 'lease-expired',
                    lease_worker_id = null, lease_acquired_at = null, lease_expires_at = null,
                    updated_at = ?
                where run_id = ? and node_id = ? and status = 'running'
                """,
                (status.value, available_at.isoformat(), at.isoformat(), run_id, node_id),
            )
            self._insert_outcome(
                transaction,
                lease,
                "lease-expired",
                outcome_digest,
                NodeUsage(),
                at,
                failure_code="lease-expired",
            )
            event_type = (
                "OrchestrationLeaseExpiredRetryScheduled"
                if retryable
                else "OrchestrationLeaseExpiredTerminal"
            )
            transitions: list[_Transition] = [
                _Transition(
                    event_type,
                    f"orchestration:{run_id}:node:{node_id}:outcome:{fencing_token}",
                    {
                        "attempt": attempt,
                        "available_at": available_at.isoformat(),
                        "failure_code": "lease-expired",
                        "fencing_token": fencing_token,
                        "node_id": node_id,
                        "retryable": retryable,
                    },
                )
            ]
            if not retryable:
                transitions.extend(self._block_descendants(transaction, run_id, at))
                transitions.extend(self._block_remaining(transaction, run_id, at))
            transitions.extend(self._update_run_status(transaction, run_id, at))
            self._append_transitions(
                transaction,
                run_id,
                "scheduler:recovery",
                at,
                transitions,
            )
            return self._node_snapshot(transaction, run_id, node_id)

    def _initialize_schema(self) -> None:
        with connect(self.path) as connection:
            connection.execute("begin immediate")
            try:
                connection.execute(_MIGRATION_SCHEMA)
                row = connection.execute(
                    "select coalesce(max(version), 0) from orchestration_schema_migrations"
                ).fetchone()
                current = int(row[0]) if row is not None else 0
                if current > _SCHEMA_VERSION:
                    raise OrchestrationSchedulerIntegrityError(
                        f"orchestration schema {current} is newer than supported schema "
                        f"{_SCHEMA_VERSION}"
                    )
                if current < 1:
                    _execute_schema(connection, _SCHEMA)
                    connection.execute(
                        """
                        insert into orchestration_schema_migrations(version, applied_at)
                        values (1, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                        """
                    )
                tables = {
                    str(item["name"])
                    for item in connection.execute(
                        "select name from sqlite_master where type = 'table'"
                    ).fetchall()
                }
                missing = _REQUIRED_TABLES.difference(tables)
                if missing:
                    raise OrchestrationSchedulerIntegrityError(
                        f"orchestration schema is missing tables: {sorted(missing)}"
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def _definition(self, executor: _SqlExecutor, run_id: str) -> DagDefinition:
        row = executor.execute(
            "select definition_json, dag_digest from orchestration_runs where run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"orchestration run {run_id!r} does not exist")
        payload = json.loads(str(row["definition_json"]))
        if not isinstance(payload, dict):
            raise OrchestrationSchedulerIntegrityError("stored DAG definition is not an object")
        try:
            definition = dag_definition_from_payload(payload)
        except (TypeError, ValueError) as error:
            raise OrchestrationSchedulerIntegrityError(
                "stored DAG definition failed validation"
            ) from error
        if definition.dag_digest != str(row["dag_digest"]):
            raise OrchestrationSchedulerIntegrityError("stored DAG digest does not match content")
        return definition

    def _snapshot(self, executor: _SqlExecutor, run_id: str) -> OrchestrationRunSnapshot:
        run = executor.execute(
            """
            select status, submitted_by, submitted_at, updated_at
            from orchestration_runs where run_id = ?
            """,
            (run_id,),
        ).fetchone()
        if run is None:
            raise LookupError(f"orchestration run {run_id!r} does not exist")
        definition = self._definition(executor, run_id)
        rows = executor.execute(
            "select * from orchestration_nodes where run_id = ? order by node_id",
            (run_id,),
        ).fetchall()
        nodes = tuple(_node_snapshot_from_row(row) for row in rows)
        if tuple(item.node_id for item in nodes) != tuple(
            item.node_id for item in definition.nodes
        ):
            raise OrchestrationSchedulerIntegrityError("stored scheduler nodes differ from the DAG")
        for row, node in zip(rows, definition.nodes, strict=True):
            if str(row["node_digest"]) != node.node_digest:
                raise OrchestrationSchedulerIntegrityError(
                    f"stored scheduler node {node.node_id!r} failed its content identity"
                )
        approval_rows = executor.execute(
            """
            select node_id, role, principal_id, approved, decided_at, decision_digest
            from orchestration_approvals where run_id = ? order by node_id, role
            """,
            (run_id,),
        ).fetchall()
        approvals = tuple(
            _approval_from_row(
                str(row["node_id"]),
                OrchestrationRole(str(row["role"])),
                row,
            )
            for row in approval_rows
        )
        status = OrchestrationRunStatus(str(run["status"]))
        if status is not _derive_run_status(rows):
            raise OrchestrationSchedulerIntegrityError("stored run status differs from node state")
        return OrchestrationRunSnapshot(
            run_id,
            definition,
            status,
            nodes,
            approvals,
            str(run["submitted_by"]),
            datetime.fromisoformat(str(run["submitted_at"])),
            datetime.fromisoformat(str(run["updated_at"])),
        )

    def _node_row(
        self,
        executor: _SqlExecutor,
        run_id: str,
        node_id: str,
    ) -> sqlite3.Row:
        row = executor.execute(
            "select * from orchestration_nodes where run_id = ? and node_id = ?",
            (run_id, node_id),
        ).fetchone()
        if row is None:
            raise LookupError(f"orchestration node {run_id!r}/{node_id!r} does not exist")
        return row

    def _node_snapshot(
        self,
        executor: _SqlExecutor,
        run_id: str,
        node_id: str,
    ) -> OrchestrationNodeSnapshot:
        return _node_snapshot_from_row(self._node_row(executor, run_id, node_id))

    def _validate_active_lease(
        self,
        transaction: SQLiteKernelTransaction,
        lease: OrchestrationNodeLease,
        node: DagNode,
        at: datetime,
    ) -> sqlite3.Row:
        if lease.node.node_digest != node.node_digest:
            raise OrchestrationLeaseConflict("lease node definition is stale")
        state = self._node_row(transaction, lease.run_id, node.node_id)
        if NodeStatus(str(state["status"])) is not NodeStatus.RUNNING:
            raise OrchestrationLeaseConflict("node no longer holds an active lease")
        stored_identity = (
            int(state["attempts"]),
            int(state["fencing_token"]),
            str(state["lease_worker_id"]),
            datetime.fromisoformat(str(state["lease_acquired_at"])),
            datetime.fromisoformat(str(state["lease_expires_at"])),
        )
        supplied_identity = (
            lease.attempt,
            lease.fencing_token,
            lease.worker_id,
            lease.acquired_at,
            lease.expires_at,
        )
        if stored_identity != supplied_identity:
            raise OrchestrationLeaseConflict("lease identity or fencing token is stale")
        if at >= lease.expires_at:
            raise OrchestrationLeaseConflict("lease has expired")
        return state

    def _idempotent_outcome(
        self,
        transaction: SQLiteKernelTransaction,
        lease: OrchestrationNodeLease,
        outcome_digest: str,
        conflict_type: type[OrchestrationResultConflict],
    ) -> bool:
        row = transaction.execute(
            """
            select outcome_digest from orchestration_attempt_outcomes
            where run_id = ? and node_id = ? and fencing_token = ?
            """,
            (lease.run_id, lease.node.node_id, lease.fencing_token),
        ).fetchone()
        if row is None:
            return False
        if str(row["outcome_digest"]) != outcome_digest:
            raise conflict_type("fencing token already owns a different terminal outcome")
        return True

    def _insert_outcome(
        self,
        transaction: SQLiteKernelTransaction,
        lease: OrchestrationNodeLease,
        outcome: str,
        outcome_digest: str,
        usage: NodeUsage,
        at: datetime,
        *,
        result_digest: str | None = None,
        failure_code: str | None = None,
    ) -> None:
        transaction.execute(
            """
            insert into orchestration_attempt_outcomes(
                run_id, node_id, fencing_token, outcome, outcome_digest,
                result_digest, failure_code, input_tokens, output_tokens,
                latency_ms, cost_microusd, recorded_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                lease.run_id,
                lease.node.node_id,
                lease.fencing_token,
                outcome,
                outcome_digest,
                result_digest,
                failure_code,
                *_usage_values(usage),
                at.isoformat(),
            ),
        )

    def _block_descendants(
        self,
        transaction: SQLiteKernelTransaction,
        run_id: str,
        at: datetime,
    ) -> list[_Transition]:
        blocked: list[str] = []
        while True:
            rows = transaction.execute(
                """
                select distinct child.node_id
                from orchestration_nodes child
                join orchestration_dependencies dependency
                  on dependency.run_id = child.run_id
                 and dependency.node_id = child.node_id
                join orchestration_nodes parent
                  on parent.run_id = dependency.run_id
                 and parent.node_id = dependency.dependency_node_id
                where child.run_id = ?
                  and child.status = 'pending'
                  and parent.status in ('failed', 'blocked', 'denied')
                order by child.node_id
                """,
                (run_id,),
            ).fetchall()
            node_ids = tuple(str(row["node_id"]) for row in rows)
            if not node_ids:
                break
            for node_id in node_ids:
                transaction.execute(
                    """
                    update orchestration_nodes
                    set status = 'blocked', failure_code = 'dependency-not-satisfied',
                        updated_at = ?
                    where run_id = ? and node_id = ? and status = 'pending'
                    """,
                    (at.isoformat(), run_id, node_id),
                )
                blocked.append(node_id)
        return [
            _Transition(
                "OrchestrationNodeBlocked",
                f"orchestration:{run_id}:node:{node_id}:blocked",
                {"failure_code": "dependency-not-satisfied", "node_id": node_id},
            )
            for node_id in blocked
        ]

    def _update_run_status(
        self,
        transaction: SQLiteKernelTransaction,
        run_id: str,
        at: datetime,
    ) -> list[_Transition]:
        run = transaction.execute(
            "select status from orchestration_runs where run_id = ?",
            (run_id,),
        ).fetchone()
        if run is None:  # pragma: no cover - foreign-key invariant
            raise OrchestrationSchedulerIntegrityError("scheduler run disappeared")
        rows = transaction.execute(
            "select status, attempts from orchestration_nodes where run_id = ?",
            (run_id,),
        ).fetchall()
        previous = OrchestrationRunStatus(str(run["status"]))
        current = _derive_run_status(rows)
        if current is previous:
            return []
        if previous in {
            OrchestrationRunStatus.SUCCEEDED,
            OrchestrationRunStatus.FAILED,
            OrchestrationRunStatus.DENIED,
        }:
            raise OrchestrationSchedulerIntegrityError("terminal run status cannot change")
        transaction.execute(
            "update orchestration_runs set status = ?, updated_at = ? where run_id = ?",
            (current.value, at.isoformat(), run_id),
        )
        return [
            _Transition(
                "OrchestrationRunStatusChanged",
                f"orchestration:{run_id}:status:{current.value}",
                {"previous_status": previous.value, "status": current.value},
            )
        ]

    def _block_remaining(
        self,
        transaction: SQLiteKernelTransaction,
        run_id: str,
        at: datetime,
    ) -> list[_Transition]:
        rows = transaction.execute(
            """
            select node_id, status, fencing_token
            from orchestration_nodes
            where run_id = ? and status in ('pending', 'running')
            order by node_id
            """,
            (run_id,),
        ).fetchall()
        transitions: list[_Transition] = []
        for row in rows:
            node_id = str(row["node_id"])
            previous_status = str(row["status"])
            fencing_token = int(row["fencing_token"])
            transaction.execute(
                """
                update orchestration_nodes
                set status = 'blocked', failure_code = 'run-terminal',
                    lease_worker_id = null, lease_acquired_at = null,
                    lease_expires_at = null, updated_at = ?
                where run_id = ? and node_id = ? and status in ('pending', 'running')
                """,
                (at.isoformat(), run_id, node_id),
            )
            transitions.append(
                _Transition(
                    "OrchestrationNodeBlocked",
                    f"orchestration:{run_id}:node:{node_id}:blocked",
                    {
                        "failure_code": "run-terminal",
                        "fencing_token": fencing_token,
                        "node_id": node_id,
                        "previous_status": previous_status,
                    },
                )
            )
        return transitions

    def _append_transitions(
        self,
        transaction: SQLiteKernelTransaction,
        run_id: str,
        actor: str,
        at: datetime,
        transitions: Sequence[_Transition],
    ) -> None:
        if not transitions:
            return
        transaction.execute(
            "update orchestration_runs set updated_at = ? where run_id = ?",
            (at.isoformat(), run_id),
        )
        stream_id = f"orchestration-run:{run_id}"
        current = transaction.current_sequence(stream_id)
        row = transaction.execute(
            """
            select event_id from kernel_events
            where stream_id = ? order by stream_sequence desc limit 1
            """,
            (stream_id,),
        ).fetchone()
        causation_id = None if row is None else str(row["event_id"])
        events: list[EventEnvelope] = []
        for offset, transition in enumerate(transitions, start=1):
            event = EventEnvelope.create(
                stream_id=stream_id,
                stream_sequence=current + offset,
                event_type=transition.event_type,
                actor=actor,
                source=_SOURCE,
                payload=transition.payload,
                recorded_at=at,
                effective_at=at,
                correlation_id=run_id,
                causation_id=causation_id,
                idempotency_key=transition.idempotency_key,
            )
            events.append(event)
            causation_id = event.event_id
        transaction.append_events(events, expected_sequences={stream_id: current})


def _execute_schema(connection: sqlite3.Connection, schema: str) -> None:
    pending = ""
    for line in schema.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            connection.execute(pending)
            pending = ""
    if pending.strip():  # pragma: no cover - static schema invariant
        raise OrchestrationSchedulerIntegrityError("orchestration schema is incomplete")


def _definition_node(definition: DagDefinition, node_id: str) -> DagNode:
    try:
        return definition.node(node_id)
    except LookupError as error:
        raise LookupError(
            f"orchestration node {definition.dag_id!r}/{node_id!r} does not exist"
        ) from error


def _node_snapshot_from_row(row: sqlite3.Row) -> OrchestrationNodeSnapshot:
    return OrchestrationNodeSnapshot(
        node_id=str(row["node_id"]),
        status=NodeStatus(str(row["status"])),
        attempts=int(row["attempts"]),
        fencing_token=int(row["fencing_token"]),
        available_at=datetime.fromisoformat(str(row["available_at"])),
        lease_worker_id=(None if row["lease_worker_id"] is None else str(row["lease_worker_id"])),
        lease_acquired_at=(
            None
            if row["lease_acquired_at"] is None
            else datetime.fromisoformat(str(row["lease_acquired_at"]))
        ),
        lease_expires_at=(
            None
            if row["lease_expires_at"] is None
            else datetime.fromisoformat(str(row["lease_expires_at"]))
        ),
        result_digest=None if row["result_digest"] is None else str(row["result_digest"]),
        failure_code=None if row["failure_code"] is None else str(row["failure_code"]),
        usage=_usage_from_row(row),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
    )


def _approval_from_row(
    node_id: str,
    role: OrchestrationRole,
    row: sqlite3.Row,
) -> OrchestrationApproval:
    return OrchestrationApproval(
        node_id,
        role,
        str(row["principal_id"]),
        bool(row["approved"]),
        datetime.fromisoformat(str(row["decided_at"])),
        str(row["decision_digest"]),
    )


def _derive_run_status(rows: Sequence[sqlite3.Row]) -> OrchestrationRunStatus:
    statuses = tuple(NodeStatus(str(row["status"])) for row in rows)
    if not statuses:
        raise OrchestrationSchedulerIntegrityError("orchestration run has no nodes")
    if any(status is NodeStatus.DENIED for status in statuses):
        return OrchestrationRunStatus.DENIED
    if any(status in {NodeStatus.FAILED, NodeStatus.BLOCKED} for status in statuses):
        return OrchestrationRunStatus.FAILED
    if all(status is NodeStatus.SUCCEEDED for status in statuses):
        return OrchestrationRunStatus.SUCCEEDED
    if any(
        status in {NodeStatus.RUNNING, NodeStatus.SUCCEEDED} or int(row["attempts"]) > 0
        for status, row in zip(statuses, rows, strict=True)
    ):
        return OrchestrationRunStatus.RUNNING
    return OrchestrationRunStatus.PENDING


def _usage_from_row(row: sqlite3.Row) -> NodeUsage:
    return NodeUsage(
        int(row["input_tokens"]),
        int(row["output_tokens"]),
        int(row["latency_ms"]),
        int(row["cost_microusd"]),
    )


def _usage_values(usage: NodeUsage) -> tuple[int, int, int, int]:
    return (
        usage.input_tokens,
        usage.output_tokens,
        usage.latency_ms,
        usage.cost_microusd,
    )


def _usage_payload(usage: NodeUsage) -> dict[str, int]:
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "latency_ms": usage.latency_ms,
        "cost_microusd": usage.cost_microusd,
    }


def _lease_identity_payload(lease: OrchestrationNodeLease) -> dict[str, object]:
    return {
        "acquired_at": lease.acquired_at.isoformat(),
        "attempt": lease.attempt,
        "expires_at": lease.expires_at.isoformat(),
        "fencing_token": lease.fencing_token,
        "node_digest": lease.node.node_digest,
        "node_id": lease.node.node_id,
        "run_id": lease.run_id,
        "worker_id": lease.worker_id,
    }


def _required_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must not be empty")
    return value


def _timestamp(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _content_digest(value: str, field_name: str) -> str:
    _required_text(value, field_name)
    prefix = "sha256:"
    encoded = value.removeprefix(prefix)
    if not value.startswith(prefix) or len(encoded) != 64:
        raise ValueError(f"{field_name} must be a sha256 content digest")
    try:
        bytes.fromhex(encoded)
    except ValueError as error:
        raise ValueError(f"{field_name} must be a sha256 content digest") from error
    return value


def _bounded_failure_code(value: str) -> str:
    if not isinstance(value, str) or _FAILURE_CODE.fullmatch(value) is None:
        raise ValueError("failure_code must be bounded content-free lowercase text")
    return value


__all__ = ["SQLiteOrchestrationScheduler"]
