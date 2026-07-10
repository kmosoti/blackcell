from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from blackcell.models import ActionProposal, JsonObject, JsonValue, ModelInvocation


class ContextCondition(StrEnum):
    RAW_CHRONOLOGICAL = "raw-chronological"
    LATEST_N = "latest-n"
    STRUCTURED = "structured"


class ToolStatus(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILURE = "failure"
    NOT_RUN = "not-run"


@dataclass(frozen=True, slots=True)
class EvidenceFixture:
    evidence_id: str
    sequence: int
    kind: str
    content: str
    observed_at: str
    stale: bool = False
    supersedes: tuple[str, ...] = ()
    attributes: Mapping[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SupportedClaim:
    claim: str
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ToolFixture:
    action: str
    status: ToolStatus
    goal_satisfied: bool
    output: JsonObject = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BenchmarkTask:
    task_id: str
    instruction: str
    expected_action: str
    safe_actions: tuple[str, ...]
    forbidden_actions: tuple[str, ...]
    required_evidence_refs: tuple[str, ...]
    relevant_evidence_refs: tuple[str, ...]
    supported_claims: tuple[SupportedClaim, ...] = ()


@dataclass(frozen=True, slots=True)
class BenchmarkScenario:
    scenario_id: str
    description: str
    tags: tuple[str, ...]
    task: BenchmarkTask
    observations: tuple[EvidenceFixture, ...]
    structured_context: JsonObject
    fixture_proposal: ActionProposal
    tools: tuple[ToolFixture, ...]


@dataclass(frozen=True, slots=True)
class Trial:
    trial_id: str
    scenario_id: str
    condition: ContextCondition
    replicate: int = 0
    latest_n: int = 1


@dataclass(frozen=True, slots=True)
class PolicyVerdict:
    allowed: bool
    violations: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    status: ToolStatus
    goal_satisfied: bool
    output: JsonObject = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TrialOutcome:
    trial: Trial
    context: JsonObject
    proposal: ActionProposal
    policy: PolicyVerdict
    execution: ExecutionOutcome
    invocation: ModelInvocation | None = None
    elapsed_ms: float = 0.0


@dataclass(frozen=True, slots=True)
class TrialScore:
    trial_id: str
    scenario_id: str
    condition: ContextCondition
    replicate: int
    success: bool
    evidence_recall: float
    evidence_precision: float
    invisible_citations: int
    unsupported_claims: int
    violations: int
    false_rejection: bool
    context_chars: int
    response_chars: int
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: float


@runtime_checkable
class ScenarioPolicy(Protocol):
    def evaluate(self, task: BenchmarkTask, proposal: ActionProposal) -> PolicyVerdict: ...


@runtime_checkable
class ScenarioExecutor(Protocol):
    def execute(
        self, scenario: BenchmarkScenario, proposal: ActionProposal
    ) -> ExecutionOutcome: ...


@runtime_checkable
class ScenarioRunner(Protocol):
    def run(self, scenario: BenchmarkScenario, trial: Trial) -> TrialOutcome: ...


@runtime_checkable
class Grader(Protocol):
    def grade(self, scenario: BenchmarkScenario, outcome: TrialOutcome) -> TrialScore: ...
