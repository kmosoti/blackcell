from __future__ import annotations

import heapq

from blackcell.orchestration.models import DagDefinition
from blackcell.orchestration.roles import validate_role_policy


class DagValidationError(ValueError):
    pass


def validate_dag(definition: DagDefinition) -> None:
    nodes = {item.node_id: item for item in definition.nodes}
    for node in definition.nodes:
        validate_role_policy(node)
        missing = tuple(item for item in node.depends_on if item not in nodes)
        if missing:
            raise DagValidationError(f"node {node.node_id!r} has missing dependencies: {missing!r}")
        for binding in node.inputs:
            source = nodes[binding.source_node_id]
            if binding.source_schema != source.output_schema:
                raise DagValidationError(
                    f"node {node.node_id!r} input {binding.input_name!r} schema differs "
                    "from its source output"
                )
    topological_order(definition)


def topological_order(definition: DagDefinition) -> tuple[str, ...]:
    dependents: dict[str, list[str]] = {item.node_id: [] for item in definition.nodes}
    remaining = {item.node_id: len(item.depends_on) for item in definition.nodes}
    for node in definition.nodes:
        for dependency in node.depends_on:
            if dependency in dependents:
                dependents[dependency].append(node.node_id)
    ready = [node_id for node_id, count in remaining.items() if count == 0]
    heapq.heapify(ready)
    ordered: list[str] = []
    while ready:
        node_id = heapq.heappop(ready)
        ordered.append(node_id)
        for dependent in sorted(dependents[node_id]):
            remaining[dependent] -= 1
            if remaining[dependent] == 0:
                heapq.heappush(ready, dependent)
    if len(ordered) != len(definition.nodes):
        cyclic = tuple(sorted(node_id for node_id, count in remaining.items() if count > 0))
        raise DagValidationError(f"DAG contains a dependency cycle: {cyclic!r}")
    return tuple(ordered)


__all__ = ["DagValidationError", "topological_order", "validate_dag"]
