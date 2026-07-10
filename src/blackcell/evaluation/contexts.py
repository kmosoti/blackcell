from __future__ import annotations

import json

from blackcell.evaluation.types import BenchmarkScenario, ContextCondition, EvidenceFixture
from blackcell.models import JsonObject


def build_context(
    scenario: BenchmarkScenario,
    condition: ContextCondition,
    *,
    latest_n: int = 3,
) -> JsonObject:
    """Render one of OperatorBench's controlled context interventions."""

    if latest_n <= 0:
        raise ValueError("latest_n must be positive")
    ordered = sorted(scenario.observations, key=lambda item: (item.sequence, item.evidence_id))
    task = {
        "task_id": scenario.task.task_id,
        "instruction": scenario.task.instruction,
        "available_actions": list(scenario.task.safe_actions + scenario.task.forbidden_actions),
    }

    if condition is ContextCondition.RAW_CHRONOLOGICAL:
        return {
            "condition": condition.value,
            "task": task,
            "observations": [_render_observation(item) for item in ordered],
        }
    if condition is ContextCondition.LATEST_N:
        selected = ordered[-latest_n:]
        return {
            "condition": condition.value,
            "task": task,
            "latest_n": latest_n,
            "observations": [_render_observation(item) for item in selected],
        }
    if condition is ContextCondition.STRUCTURED:
        return {
            "condition": condition.value,
            "task": task,
            "state": dict(scenario.structured_context),
        }
    raise ValueError(f"unknown context condition: {condition!r}")


def serialized_chars(context: JsonObject) -> int:
    return len(json.dumps(context, sort_keys=True, separators=(",", ":"), ensure_ascii=False))


def _render_observation(item: EvidenceFixture) -> JsonObject:
    return {
        "evidence_id": item.evidence_id,
        "sequence": item.sequence,
        "kind": item.kind,
        "content": item.content,
        "observed_at": item.observed_at,
        "stale": item.stale,
        "supersedes": list(item.supersedes),
        "attributes": dict(item.attributes),
    }
