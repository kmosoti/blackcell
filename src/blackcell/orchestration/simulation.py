from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from blackcell.kernel._json import json_digest
from blackcell.orchestration.dag import topological_order
from blackcell.orchestration.models import (
    DagDefinition,
    NodeStatus,
    NodeUsage,
    OrchestrationRole,
    OrchestrationRunStatus,
)


class SimulatedAttemptOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    TRANSIENT_FAILURE = "transient-failure"
    PERMANENT_FAILURE = "permanent-failure"
    WORKER_LOST = "worker-lost"
    STALE_COMPLETION = "stale-completion"
    DUPLICATE_DELIVERY = "duplicate-delivery"


@dataclass(frozen=True, slots=True)
class SimulatedAttempt:
    outcome: SimulatedAttemptOutcome
    usage: NodeUsage = field(default_factory=NodeUsage)
    failure_code: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, SimulatedAttemptOutcome):
            raise TypeError("simulated attempt outcome must be recognized")
        failure = self.outcome in {
            SimulatedAttemptOutcome.TRANSIENT_FAILURE,
            SimulatedAttemptOutcome.PERMANENT_FAILURE,
        }
        if failure != (self.failure_code is not None):
            raise ValueError("simulated failures require exactly one failure code")
        if self.failure_code is not None and (
            not self.failure_code.strip() or len(self.failure_code) > 100
        ):
            raise ValueError("simulated failure code must be bounded non-empty text")


@dataclass(frozen=True, slots=True)
class NodeSimulationPlan:
    node_id: str
    attempts: tuple[SimulatedAttempt, ...]

    def __post_init__(self) -> None:
        if not self.node_id.strip() or not self.attempts:
            raise ValueError("node simulation plans require an id and attempts")


@dataclass(frozen=True, slots=True, order=True)
class SimulatedApproval:
    node_id: str
    role: OrchestrationRole
    principal_id: str
    approved: bool

    def __post_init__(self) -> None:
        if not self.node_id.strip() or not self.principal_id.strip():
            raise ValueError("simulated approval identities must not be empty")


@dataclass(frozen=True, slots=True)
class SimulationScenario:
    plans: tuple[NodeSimulationPlan, ...] = ()
    approvals: tuple[SimulatedApproval, ...] = ()

    def __post_init__(self) -> None:
        plans = tuple(sorted(self.plans, key=lambda item: item.node_id))
        plan_ids = tuple(item.node_id for item in plans)
        if len(plan_ids) != len(set(plan_ids)):
            raise ValueError("simulation node plans must be unique")
        approvals = tuple(sorted(self.approvals))
        approval_keys = tuple((item.node_id, item.role) for item in approvals)
        if len(approval_keys) != len(set(approval_keys)):
            raise ValueError("simulation approvals must be unique by node and role")
        object.__setattr__(self, "plans", plans)
        object.__setattr__(self, "approvals", approvals)


@dataclass(frozen=True, slots=True)
class SimulationAttemptRecord:
    attempt_number: int
    fencing_token: int
    outcome: SimulatedAttemptOutcome
    usage: NodeUsage
    committed: bool
    failure_code: str | None


@dataclass(frozen=True, slots=True)
class SimulationNodeResult:
    node_id: str
    status: NodeStatus
    attempts: tuple[SimulationAttemptRecord, ...]
    usage: NodeUsage
    commit_count: int
    stale_rejections: int
    duplicate_deliveries: int
    terminal_code: str | None = None

    def __post_init__(self) -> None:
        if not self.node_id.strip():
            raise ValueError("simulation node id must not be empty")
        if self.commit_count not in {0, 1}:
            raise ValueError("simulation nodes may commit at most once")
        if self.stale_rejections < 0 or self.duplicate_deliveries < 0:
            raise ValueError("simulation counters must be non-negative")


@dataclass(frozen=True, slots=True)
class SimulationReport:
    dag_digest: str
    run_status: OrchestrationRunStatus
    nodes: tuple[SimulationNodeResult, ...]
    total_usage: NodeUsage
    report_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not self.dag_digest.startswith("sha256:") or not self.nodes:
            raise ValueError("simulation report requires a DAG identity and node results")
        if self.total_usage != sum((item.usage for item in self.nodes), start=NodeUsage()):
            raise ValueError("simulation total usage differs from node evidence")
        object.__setattr__(self, "report_id", json_digest(_report_payload(self)))


class SimulationInvariantError(RuntimeError):
    pass


def simulate_dag(
    definition: DagDefinition,
    scenario: SimulationScenario | None = None,
) -> SimulationReport:
    if scenario is None:
        scenario = SimulationScenario()
    nodes = {item.node_id: item for item in definition.nodes}
    unknown_plans = tuple(item.node_id for item in scenario.plans if item.node_id not in nodes)
    unknown_approvals = tuple(
        item.node_id for item in scenario.approvals if item.node_id not in nodes
    )
    if unknown_plans or unknown_approvals:
        raise ValueError("simulation scenario references nodes outside its DAG")
    plans = {item.node_id: item for item in scenario.plans}
    approvals = {(item.node_id, item.role): item for item in scenario.approvals}
    results: dict[str, SimulationNodeResult] = {}
    for node_id in topological_order(definition):
        node = nodes[node_id]
        dependency_results = tuple(results[item] for item in node.depends_on)
        if any(item.status is not NodeStatus.SUCCEEDED for item in dependency_results):
            results[node_id] = _terminal_without_attempt(
                node_id,
                NodeStatus.BLOCKED,
                "dependency-not-satisfied",
            )
            continue
        approval_result = _approvals(node, approvals)
        if approval_result is not None:
            results[node_id] = approval_result
            continue
        plan = plans.get(
            node_id,
            NodeSimulationPlan(node_id, (SimulatedAttempt(SimulatedAttemptOutcome.SUCCEEDED),)),
        )
        results[node_id] = _simulate_node(node, plan)
    ordered = tuple(results[item] for item in topological_order(definition))
    run_status = _run_status(ordered)
    return SimulationReport(
        definition.dag_digest,
        run_status,
        ordered,
        sum((item.usage for item in ordered), start=NodeUsage()),
    )


def _approvals(node, approvals) -> SimulationNodeResult | None:
    for role in node.required_approvals:
        approval = approvals.get((node.node_id, role))
        if approval is None:
            return _terminal_without_attempt(node.node_id, NodeStatus.BLOCKED, "approval-missing")
        if approval.principal_id == node.principal_id:
            raise SimulationInvariantError("a node principal cannot approve its own work")
        if not approval.approved:
            return _terminal_without_attempt(node.node_id, NodeStatus.DENIED, "approval-denied")
    return None


def _simulate_node(node, plan: NodeSimulationPlan) -> SimulationNodeResult:
    records: list[SimulationAttemptRecord] = []
    usage = NodeUsage()
    stale_rejections = 0
    duplicate_deliveries = 0
    terminal_code: str | None = None
    status = NodeStatus.FAILED
    commit_count = 0
    for attempt_number, planned in enumerate(plan.attempts[: node.retry.max_attempts], start=1):
        usage += planned.usage
        if planned.usage.exceeds(node.budget):
            outcome = SimulatedAttemptOutcome.PERMANENT_FAILURE
            failure_code = "budget-exceeded"
        else:
            outcome = planned.outcome
            failure_code = planned.failure_code
        committed = False
        if outcome is SimulatedAttemptOutcome.SUCCEEDED:
            committed = True
            commit_count = 1
            status = NodeStatus.SUCCEEDED
        elif outcome is SimulatedAttemptOutcome.DUPLICATE_DELIVERY:
            committed = True
            commit_count = 1
            duplicate_deliveries += 1
            status = NodeStatus.SUCCEEDED
        elif outcome is SimulatedAttemptOutcome.STALE_COMPLETION:
            stale_rejections += 1
            terminal_code = "stale-completion"
        elif outcome is SimulatedAttemptOutcome.WORKER_LOST:
            terminal_code = "worker-lost"
        else:
            terminal_code = failure_code
        records.append(
            SimulationAttemptRecord(
                attempt_number,
                attempt_number,
                outcome,
                planned.usage,
                committed,
                failure_code,
            )
        )
        if status is NodeStatus.SUCCEEDED:
            break
        retryable = outcome in {
            SimulatedAttemptOutcome.WORKER_LOST,
            SimulatedAttemptOutcome.STALE_COMPLETION,
        } or (
            outcome is SimulatedAttemptOutcome.TRANSIENT_FAILURE
            and failure_code in node.retry.retryable_codes
        )
        if not retryable:
            break
    if status is not NodeStatus.SUCCEEDED and records:
        terminal_code = terminal_code or "retry-exhausted"
    if not records:
        terminal_code = "simulation-plan-empty"
    return SimulationNodeResult(
        node.node_id,
        status,
        tuple(records),
        usage,
        commit_count,
        stale_rejections,
        duplicate_deliveries,
        terminal_code,
    )


def _terminal_without_attempt(
    node_id: str,
    status: NodeStatus,
    code: str,
) -> SimulationNodeResult:
    return SimulationNodeResult(node_id, status, (), NodeUsage(), 0, 0, 0, code)


def _run_status(nodes: tuple[SimulationNodeResult, ...]) -> OrchestrationRunStatus:
    if all(item.status is NodeStatus.SUCCEEDED for item in nodes):
        return OrchestrationRunStatus.SUCCEEDED
    if any(item.status is NodeStatus.DENIED for item in nodes):
        return OrchestrationRunStatus.DENIED
    return OrchestrationRunStatus.FAILED


def _report_payload(report: SimulationReport) -> dict[str, object]:
    return {
        "dag_digest": report.dag_digest,
        "run_status": report.run_status.value,
        "nodes": [
            {
                "node_id": item.node_id,
                "status": item.status.value,
                "attempts": [
                    {
                        "attempt_number": attempt.attempt_number,
                        "fencing_token": attempt.fencing_token,
                        "outcome": attempt.outcome.value,
                        "usage": _usage_payload(attempt.usage),
                        "committed": attempt.committed,
                        "failure_code": attempt.failure_code,
                    }
                    for attempt in item.attempts
                ],
                "usage": _usage_payload(item.usage),
                "commit_count": item.commit_count,
                "stale_rejections": item.stale_rejections,
                "duplicate_deliveries": item.duplicate_deliveries,
                "terminal_code": item.terminal_code,
            }
            for item in report.nodes
        ],
        "total_usage": _usage_payload(report.total_usage),
    }


def _usage_payload(usage: NodeUsage) -> dict[str, int]:
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "latency_ms": usage.latency_ms,
        "cost_microusd": usage.cost_microusd,
    }


__all__ = [
    "NodeSimulationPlan",
    "SimulatedApproval",
    "SimulatedAttempt",
    "SimulatedAttemptOutcome",
    "SimulationAttemptRecord",
    "SimulationInvariantError",
    "SimulationNodeResult",
    "SimulationReport",
    "SimulationScenario",
    "simulate_dag",
]
