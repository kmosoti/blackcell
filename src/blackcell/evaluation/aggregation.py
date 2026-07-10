from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

from blackcell.evaluation.types import ContextCondition, TrialScore


@dataclass(frozen=True, slots=True)
class MetricSummary:
    metric: str
    count: int
    mean: float
    lower: float | None = None
    upper: float | None = None


@dataclass(frozen=True, slots=True)
class BenchmarkAggregate:
    condition: ContextCondition
    trial_count: int
    metrics: tuple[MetricSummary, ...]

    def metric(self, name: str) -> MetricSummary:
        for metric in self.metrics:
            if metric.metric == name:
                return metric
        raise KeyError(name)


@dataclass(frozen=True, slots=True)
class PairedDelta:
    metric: str
    left: ContextCondition
    right: ContextCondition
    pair_count: int
    mean_delta: float
    wins: int
    ties: int
    losses: int


def wilson_interval(
    successes: int, total: int, *, z: float = 1.959963984540054
) -> tuple[float, float]:
    if total <= 0:
        raise ValueError("total must be positive")
    if successes < 0 or successes > total:
        raise ValueError("successes must be between zero and total")
    proportion = successes / total
    denominator = 1 + z**2 / total
    centre = (proportion + z**2 / (2 * total)) / denominator
    margin = (
        z * math.sqrt(proportion * (1 - proportion) / total + z**2 / (4 * total**2)) / denominator
    )
    return max(0.0, centre - margin), min(1.0, centre + margin)


def aggregate_scores(scores: Iterable[TrialScore]) -> tuple[BenchmarkAggregate, ...]:
    grouped: dict[ContextCondition, list[TrialScore]] = {}
    for score in scores:
        grouped.setdefault(score.condition, []).append(score)

    aggregates = []
    for condition in sorted(grouped, key=str):
        rows = grouped[condition]
        proportions = {
            "success": [float(row.success) for row in rows],
            "false_rejection": [float(row.false_rejection) for row in rows],
            "violation_free": [float(row.violations == 0) for row in rows],
        }
        continuous = {
            "evidence_recall": [row.evidence_recall for row in rows],
            "evidence_precision": [row.evidence_precision for row in rows],
            "unsupported_claims": [float(row.unsupported_claims) for row in rows],
            "violations": [float(row.violations) for row in rows],
            "context_chars": [float(row.context_chars) for row in rows],
            "response_chars": [float(row.response_chars) for row in rows],
            "latency_ms": [row.latency_ms for row in rows],
        }
        token_values = {
            "input_tokens": [
                float(row.input_tokens) for row in rows if row.input_tokens is not None
            ],
            "output_tokens": [
                float(row.output_tokens) for row in rows if row.output_tokens is not None
            ],
        }
        metrics: list[MetricSummary] = []
        for name, values in proportions.items():
            successes = int(sum(values))
            lower, upper = wilson_interval(successes, len(values))
            metrics.append(
                MetricSummary(name, len(values), sum(values) / len(values), lower, upper)
            )
        for name, values in continuous.items():
            metrics.append(MetricSummary(name, len(values), sum(values) / len(values)))
        for name, values in token_values.items():
            if values:
                metrics.append(MetricSummary(name, len(values), sum(values) / len(values)))
        aggregates.append(BenchmarkAggregate(condition, len(rows), tuple(metrics)))
    return tuple(aggregates)


def paired_delta(
    scores: Iterable[TrialScore],
    *,
    left: ContextCondition,
    right: ContextCondition,
    metric: str,
) -> PairedDelta:
    rows = list(scores)
    left_rows = {(row.scenario_id, row.replicate): row for row in rows if row.condition is left}
    right_rows = {(row.scenario_id, row.replicate): row for row in rows if row.condition is right}
    common = sorted(left_rows.keys() & right_rows.keys())
    if not common:
        raise ValueError("no paired trials for the requested conditions")
    deltas = [
        _numeric_metric(right_rows[key], metric) - _numeric_metric(left_rows[key], metric)
        for key in common
    ]
    epsilon = 1e-12
    return PairedDelta(
        metric=metric,
        left=left,
        right=right,
        pair_count=len(deltas),
        mean_delta=sum(deltas) / len(deltas),
        wins=sum(delta > epsilon for delta in deltas),
        ties=sum(abs(delta) <= epsilon for delta in deltas),
        losses=sum(delta < -epsilon for delta in deltas),
    )


def _numeric_metric(score: TrialScore, metric: str) -> float:
    if metric not in TrialScore.__dataclass_fields__:
        raise KeyError(metric)
    value = getattr(score, metric)
    if value is None or not isinstance(value, (int, float, bool)):
        raise ValueError(f"metric {metric!r} is not numeric for all pairs")
    return float(value)
