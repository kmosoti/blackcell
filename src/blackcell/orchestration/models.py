from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from blackcell.gateway import DataClassification, LocalityPolicy, ModelCapability
from blackcell.kernel._json import json_digest

DAG_SCHEMA_VERSION = "orchestration-dag/v1"


class OrchestrationRole(StrEnum):
    PLANNER = "planner"
    EXECUTOR = "executor"
    REVIEWER = "reviewer"
    VERIFIER = "verifier"
    SYNTHESIZER = "synthesizer"


class NodeSideEffect(StrEnum):
    NONE = "none"
    READ_ONLY = "read-only"
    REVERSIBLE = "reversible"
    IRREVERSIBLE = "irreversible"


class NodeStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    DENIED = "denied"


class OrchestrationRunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"


@dataclass(frozen=True, slots=True)
class NodeBudget:
    max_input_tokens: int
    max_output_tokens: int
    max_latency_ms: int
    max_cost_microusd: int

    def __post_init__(self) -> None:
        values = (
            self.max_input_tokens,
            self.max_output_tokens,
            self.max_latency_ms,
            self.max_cost_microusd,
        )
        if any(isinstance(item, bool) or not isinstance(item, int) for item in values):
            raise TypeError("node budget values must be integers")
        if min(values) < 0:
            raise ValueError("node budget values must be non-negative")


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 1
    backoff_seconds: int = 0
    retryable_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int):
            raise TypeError("max_attempts must be an integer")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if isinstance(self.backoff_seconds, bool) or not isinstance(self.backoff_seconds, int):
            raise TypeError("backoff_seconds must be an integer")
        if self.backoff_seconds < 0:
            raise ValueError("backoff_seconds must be non-negative")
        codes = tuple(sorted(set(self.retryable_codes)))
        if any(not item.strip() or len(item) > 100 for item in codes):
            raise ValueError("retryable codes must be bounded non-empty text")
        object.__setattr__(self, "retryable_codes", codes)


@dataclass(frozen=True, slots=True, order=True)
class NodeInputBinding:
    input_name: str
    source_node_id: str
    source_schema: str

    def __post_init__(self) -> None:
        for name in ("input_name", "source_node_id", "source_schema"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")


@dataclass(frozen=True, slots=True)
class DagNode:
    node_id: str
    role: OrchestrationRole
    principal_id: str
    handler: str
    output_schema: str
    depends_on: tuple[str, ...]
    inputs: tuple[NodeInputBinding, ...]
    retry: RetryPolicy
    timeout_seconds: int
    budget: NodeBudget
    side_effect: NodeSideEffect = NodeSideEffect.NONE
    required_approvals: tuple[OrchestrationRole, ...] = ()
    model_capability: ModelCapability | None = None
    classification: DataClassification = DataClassification.INTERNAL
    locality: LocalityPolicy = LocalityPolicy.LOCAL_ONLY
    deterministic_required: bool = False
    node_digest: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("node_id", "principal_id", "handler", "output_schema"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        if not isinstance(self.role, OrchestrationRole):
            raise TypeError("node role must be recognized")
        if not isinstance(self.side_effect, NodeSideEffect):
            raise TypeError("node side effect must be recognized")
        if self.model_capability is not None and not isinstance(
            self.model_capability, ModelCapability
        ):
            raise TypeError("node model capability must be recognized")
        if not isinstance(self.classification, DataClassification):
            raise TypeError("node data classification must be recognized")
        if not isinstance(self.locality, LocalityPolicy):
            raise TypeError("node locality policy must be recognized")
        if isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, int):
            raise TypeError("node timeout must be an integer")
        if self.timeout_seconds < 1:
            raise ValueError("node timeout must be positive")
        if self.budget.max_latency_ms > self.timeout_seconds * 1_000:
            raise ValueError("node latency budget cannot exceed its timeout")
        dependencies = tuple(sorted(set(self.depends_on)))
        if self.node_id in dependencies or any(not item.strip() for item in dependencies):
            raise ValueError("node dependencies must be non-empty and cannot include itself")
        inputs = tuple(sorted(self.inputs))
        input_names = tuple(item.input_name for item in inputs)
        if len(input_names) != len(set(input_names)):
            raise ValueError("node input names must be unique")
        if any(item.source_node_id not in dependencies for item in inputs):
            raise ValueError("node input sources must also be declared dependencies")
        approvals = tuple(sorted(set(self.required_approvals), key=str))
        object.__setattr__(self, "depends_on", dependencies)
        object.__setattr__(self, "inputs", inputs)
        object.__setattr__(self, "required_approvals", approvals)
        object.__setattr__(self, "node_digest", json_digest(dag_node_payload(self)))


@dataclass(frozen=True, slots=True)
class DagDefinition:
    dag_id: str
    nodes: tuple[DagNode, ...]
    schema_version: str = DAG_SCHEMA_VERSION
    dag_digest: str = field(init=False)

    def __post_init__(self) -> None:
        if not self.dag_id.strip():
            raise ValueError("dag_id must not be empty")
        if self.schema_version != DAG_SCHEMA_VERSION:
            raise ValueError("unsupported DAG schema version")
        nodes = tuple(sorted(self.nodes, key=lambda item: item.node_id))
        if not nodes:
            raise ValueError("a DAG requires at least one node")
        identifiers = tuple(item.node_id for item in nodes)
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("DAG node ids must be unique")
        object.__setattr__(self, "nodes", nodes)
        from blackcell.orchestration.dag import validate_dag

        validate_dag(self)
        object.__setattr__(self, "dag_digest", json_digest(dag_definition_payload(self)))

    def node(self, node_id: str) -> DagNode:
        try:
            return next(item for item in self.nodes if item.node_id == node_id)
        except StopIteration as error:
            raise LookupError(f"DAG node {node_id!r} does not exist") from error


def dag_node_payload(node: DagNode) -> dict[str, object]:
    return {
        "node_id": node.node_id,
        "role": node.role.value,
        "principal_id": node.principal_id,
        "handler": node.handler,
        "output_schema": node.output_schema,
        "depends_on": list(node.depends_on),
        "inputs": [
            {
                "input_name": item.input_name,
                "source_node_id": item.source_node_id,
                "source_schema": item.source_schema,
            }
            for item in node.inputs
        ],
        "retry": {
            "max_attempts": node.retry.max_attempts,
            "backoff_seconds": node.retry.backoff_seconds,
            "retryable_codes": list(node.retry.retryable_codes),
        },
        "timeout_seconds": node.timeout_seconds,
        "budget": {
            "max_input_tokens": node.budget.max_input_tokens,
            "max_output_tokens": node.budget.max_output_tokens,
            "max_latency_ms": node.budget.max_latency_ms,
            "max_cost_microusd": node.budget.max_cost_microusd,
        },
        "side_effect": node.side_effect.value,
        "required_approvals": [item.value for item in node.required_approvals],
        "model_capability": (
            None if node.model_capability is None else node.model_capability.value
        ),
        "classification": node.classification.name.lower(),
        "locality": node.locality.value,
        "deterministic_required": node.deterministic_required,
    }


def dag_definition_payload(definition: DagDefinition) -> dict[str, object]:
    return {
        "schema_version": definition.schema_version,
        "dag_id": definition.dag_id,
        "nodes": [dag_node_payload(item) for item in definition.nodes],
    }


__all__ = [
    "DAG_SCHEMA_VERSION",
    "DagDefinition",
    "DagNode",
    "NodeBudget",
    "NodeInputBinding",
    "NodeSideEffect",
    "NodeStatus",
    "OrchestrationRole",
    "OrchestrationRunStatus",
    "RetryPolicy",
    "dag_definition_payload",
    "dag_node_payload",
]
