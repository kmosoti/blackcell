from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from blackcell.evaluation.aggregation import (
    BenchmarkAggregate,
    PairedBootstrapInterval,
    aggregate_scores,
    paired_bootstrap_delta,
)
from blackcell.evaluation.contexts import build_trial_context
from blackcell.evaluation.grading import DeterministicGrader
from blackcell.evaluation.runner import ModelScenarioRunner
from blackcell.evaluation.scenarios import scenario_digest
from blackcell.evaluation.types import (
    BenchmarkScenario,
    ContextCondition,
    ToolStatus,
    Trial,
    TrialScore,
)
from blackcell.features.retrieve_evidence import EvidenceRetriever
from blackcell.kernel._json import json_digest
from blackcell.models import (
    ACTION_PROPOSAL_SCHEMA,
    ActionProposal,
    DecisionModel,
    JsonObject,
    ModelInvocation,
    RecordedModel,
    action_proposal_to_mapping,
)

_CONDITIONS = tuple(ContextCondition)
_COMPARISON_PAIRS = (
    ("raw-to-structured", ContextCondition.RAW_CHRONOLOGICAL, ContextCondition.STRUCTURED),
    ("latest-to-structured", ContextCondition.LATEST_N, ContextCondition.STRUCTURED),
    ("term-to-fts5", ContextCondition.TERM_RETRIEVAL, ContextCondition.FTS5_RETRIEVAL),
)
_COMPARISON_METRICS = (
    ("success", "higher"),
    ("evidence_recall", "higher"),
    ("evidence_precision", "higher"),
    ("context_chars", "lower"),
    ("latency_ms", "lower"),
)


@dataclass(frozen=True, slots=True)
class ComparativeExperimentDesign:
    experiment_id: str
    conditions: tuple[ContextCondition, ...] = _CONDITIONS
    replicates_per_scenario: int = 1
    context_character_budget: int = 12_000
    latest_n: int = 1
    retrieval_result_limit: int = 2
    bootstrap_samples: int = 2_000
    bootstrap_confidence: float = 0.95
    bootstrap_seed: int = 23

    def __post_init__(self) -> None:
        if not self.experiment_id.strip():
            raise ValueError("experiment_id must not be empty")
        if self.conditions != _CONDITIONS:
            raise ValueError("WP23 comparisons require every canonical treatment in order")
        if self.replicates_per_scenario < 1:
            raise ValueError("replicates_per_scenario must be positive")
        if self.context_character_budget < 1:
            raise ValueError("context_character_budget must be positive")
        if self.latest_n < 1 or self.retrieval_result_limit < 1:
            raise ValueError("latest_n and retrieval_result_limit must be positive")
        if self.bootstrap_samples < 1:
            raise ValueError("bootstrap_samples must be positive")
        if not 0 < self.bootstrap_confidence < 1:
            raise ValueError("bootstrap_confidence must be between zero and one")


@dataclass(frozen=True, slots=True)
class ComparativeTrialRecord:
    trial: Trial
    context: JsonObject
    context_digest: str
    proposal: JsonObject
    proposal_digest: str
    invocation: ModelInvocation
    policy_allowed: bool
    policy_violations: tuple[str, ...]
    execution_status: ToolStatus
    goal_satisfied: bool
    score: TrialScore


@dataclass(frozen=True, slots=True)
class AblationComparison:
    comparison: str
    preferred_direction: str
    interval: PairedBootstrapInterval


@dataclass(frozen=True, slots=True)
class ComparativeExperimentReport:
    experiment_id: str
    scenario_digest: str
    scenario_count: int
    model_name: str
    provider: str
    model: str | None
    replayed: bool
    inferential: bool
    action_schema_digest: str
    grader: str
    design: ComparativeExperimentDesign
    trials: tuple[ComparativeTrialRecord, ...]
    aggregates: tuple[BenchmarkAggregate, ...]
    ablations: tuple[AblationComparison, ...]
    limitations: tuple[str, ...]
    schema_version: str = "operator-bench-comparison/v1"
    report_id: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != "operator-bench-comparison/v1":
            raise ValueError("comparative experiment report schema is unsupported")
        if not self.trials or not self.ablations:
            raise ValueError("comparative reports require trials and ablations")
        object.__setattr__(self, "report_id", json_digest(_report_payload(self)))


class ComparativeExperimentRunner:
    def __init__(
        self,
        model: DecisionModel[ActionProposal],
        *,
        retrievers: Mapping[ContextCondition, EvidenceRetriever],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._model = model
        self._retrievers = dict(retrievers)
        self._clock = clock

    def run(
        self,
        scenarios: Sequence[BenchmarkScenario],
        design: ComparativeExperimentDesign,
    ) -> ComparativeExperimentReport:
        fixtures = tuple(scenarios)
        if not fixtures:
            raise ValueError("comparative experiments require at least one scenario")
        required_retrievers = {
            ContextCondition.TERM_RETRIEVAL,
            ContextCondition.FTS5_RETRIEVAL,
        }
        if not required_retrievers <= self._retrievers.keys():
            raise ValueError("comparative experiments require term and FTS5 retrievers")
        runner = ModelScenarioRunner(
            self._model,
            retrievers=self._retrievers,
            clock=self._clock,
        )
        grader = DeterministicGrader()
        records: list[ComparativeTrialRecord] = []
        for scenario in fixtures:
            for condition in design.conditions:
                for replicate in range(design.replicates_per_scenario):
                    trial = _trial(design, scenario, condition, replicate)
                    outcome = runner.run(scenario, trial)
                    score = grader.grade(scenario, outcome)
                    proposal = action_proposal_to_mapping(outcome.proposal)
                    if outcome.invocation is None:
                        raise ValueError("comparative model trials require invocation evidence")
                    records.append(
                        ComparativeTrialRecord(
                            trial=trial,
                            context=outcome.context,
                            context_digest=json_digest(outcome.context),
                            proposal=proposal,
                            proposal_digest=json_digest(proposal),
                            invocation=outcome.invocation,
                            policy_allowed=outcome.policy.allowed,
                            policy_violations=outcome.policy.violations,
                            execution_status=outcome.execution.status,
                            goal_satisfied=outcome.execution.goal_satisfied,
                            score=score,
                        )
                    )
        invocations = tuple(record.invocation for record in records)
        providers = {item.provider for item in invocations}
        models = {item.model for item in invocations}
        replayed = {item.replayed for item in invocations}
        if len(providers) != 1 or len(models) != 1 or len(replayed) != 1:
            raise ValueError("comparative trials must use one provider, model, and replay mode")
        scores = tuple(record.score for record in records)
        ablations = _ablations(scores, design)
        is_replayed = replayed.pop()
        inferential = (
            not is_replayed and len(fixtures) >= 20 and design.replicates_per_scenario >= 3
        )
        return ComparativeExperimentReport(
            experiment_id=design.experiment_id,
            scenario_digest=scenario_digest(fixtures),
            scenario_count=len(fixtures),
            model_name=self._model.name,
            provider=providers.pop(),
            model=models.pop(),
            replayed=is_replayed,
            inferential=inferential,
            action_schema_digest=json_digest(ACTION_PROPOSAL_SCHEMA),
            grader="DeterministicGrader/v1",
            design=design,
            trials=tuple(records),
            aggregates=aggregate_scores(scores),
            ablations=ablations,
            limitations=_limitations(is_replayed, len(fixtures)),
        )


class ComparativeReportReservation:
    """Reserve one owner-only artifact path before a live experiment starts."""

    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise FileExistsError(f"experiment artifact already exists: {path}") from None
        self._stream = os.fdopen(descriptor, "wb")
        self._committed = False

    def __enter__(self) -> ComparativeReportReservation:
        return self

    def commit(self, report: ComparativeExperimentReport) -> None:
        if self._committed or self._stream.closed:
            raise RuntimeError("experiment artifact reservation is already closed")
        self._stream.write(encode_comparative_report(report).encode("utf-8"))
        self._stream.flush()
        os.fsync(self._stream.fileno())
        self._stream.close()
        self._committed = True

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        if not self._stream.closed:
            self._stream.close()
        if not self._committed:
            self.path.unlink(missing_ok=True)


def recorded_fixture_model(
    scenarios: Sequence[BenchmarkScenario],
    design: ComparativeExperimentDesign,
    *,
    retrievers: Mapping[ContextCondition, EvidenceRetriever],
) -> RecordedModel[ActionProposal]:
    frames: dict[str, tuple[JsonObject, ActionProposal]] = {}
    for scenario in scenarios:
        for condition in design.conditions:
            trial = _trial(design, scenario, condition, 0)
            frames[f"{scenario.scenario_id}:{condition.value}"] = (
                build_trial_context(scenario, trial, retrievers=retrievers),
                scenario.fixture_proposal,
            )
    return RecordedModel.for_frames(frames, name="operator-bench-recorded")


def encode_comparative_report(report: ComparativeExperimentReport) -> str:
    return json.dumps(_jsonable(report), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def write_comparative_report(path: Path, report: ComparativeExperimentReport) -> None:
    with ComparativeReportReservation(path) as reservation:
        reservation.commit(report)


def _trial(
    design: ComparativeExperimentDesign,
    scenario: BenchmarkScenario,
    condition: ContextCondition,
    replicate: int,
) -> Trial:
    return Trial(
        trial_id=(f"{design.experiment_id}:{scenario.scenario_id}:{condition.value}:r{replicate}"),
        scenario_id=scenario.scenario_id,
        condition=condition,
        replicate=replicate,
        latest_n=design.latest_n,
        context_character_budget=design.context_character_budget,
        retrieval_result_limit=design.retrieval_result_limit,
    )


def _ablations(
    scores: tuple[TrialScore, ...],
    design: ComparativeExperimentDesign,
) -> tuple[AblationComparison, ...]:
    comparisons: list[AblationComparison] = []
    seed = design.bootstrap_seed
    for comparison, left, right in _COMPARISON_PAIRS:
        for metric, preferred_direction in _COMPARISON_METRICS:
            comparisons.append(
                AblationComparison(
                    comparison=comparison,
                    preferred_direction=preferred_direction,
                    interval=paired_bootstrap_delta(
                        scores,
                        left=left,
                        right=right,
                        metric=metric,
                        samples=design.bootstrap_samples,
                        confidence=design.bootstrap_confidence,
                        seed=seed,
                    ),
                )
            )
            seed += 1
    return tuple(comparisons)


def _limitations(replayed: bool, scenario_count: int) -> tuple[str, ...]:
    limitations = [
        f"the public dataset contains {scenario_count} synthetic scenarios, "
        "below the 20-scenario inferential gate",
        "paired bootstrap intervals describe this fixed scenario set and do not establish "
        "population effects",
        "retrieval treatments use lexical evidence and do not evaluate graph, vector, "
        "embedding, or learned retrieval",
        "policy and environment grading are exact fixtures rather than blinded human "
        "utility judgments",
        "recorded reports use a deterministic zero clock; retrieval and end-to-end latency "
        "must be measured by an explicitly retained live trial",
    ]
    if replayed:
        limitations.insert(
            0,
            "recorded proposals validate the matched experiment contract but do not estimate "
            "a live model context effect",
        )
    return tuple(limitations)


def _report_payload(report: ComparativeExperimentReport) -> dict[str, Any]:
    return {
        item.name: _jsonable(getattr(report, item.name))
        for item in fields(report)
        if item.name != "report_id"
    }


def _jsonable(value: object) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _jsonable(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_jsonable(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    return value


__all__ = [
    "AblationComparison",
    "ComparativeExperimentDesign",
    "ComparativeExperimentReport",
    "ComparativeExperimentRunner",
    "ComparativeReportReservation",
    "ComparativeTrialRecord",
    "encode_comparative_report",
    "recorded_fixture_model",
    "write_comparative_report",
]
