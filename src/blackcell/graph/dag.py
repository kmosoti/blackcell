"""Deterministic DAG operations."""

from collections import defaultdict
from collections.abc import Iterable, Mapping

from blackcell.contracts.errors import ValidationFailure


def topological_order(nodes: Iterable[str], dependencies: Mapping[str, Iterable[str]]) -> list[str]:
    """Return a stable topological order or raise on a dependency cycle."""
    ordered_nodes = list(nodes)
    position = {node: index for index, node in enumerate(ordered_nodes)}
    indegree = dict.fromkeys(ordered_nodes, 0)
    dependents: dict[str, list[str]] = defaultdict(list)
    for node in ordered_nodes:
        for dependency in dependencies.get(node, ()):
            indegree[node] += 1
            dependents[dependency].append(node)

    ready = [node for node in ordered_nodes if indegree[node] == 0]
    result: list[str] = []
    while ready:
        ready.sort(key=position.__getitem__)
        node = ready.pop(0)
        result.append(node)
        for dependent in dependents[node]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)

    if len(result) != len(ordered_nodes):
        cyclic = [node for node, degree in indegree.items() if degree > 0]
        raise ValidationFailure(
            "Work item dependencies contain a cycle.",
            details={"cyclic_keys": cyclic},
        )
    return result
