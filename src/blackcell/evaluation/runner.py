from __future__ import annotations

import time
from collections.abc import Callable, Mapping

from blackcell.evaluation.contexts import build_trial_context
from blackcell.evaluation.types import (
    BenchmarkScenario,
    BenchmarkTask,
    ContextCondition,
    ExecutionOutcome,
    PolicyVerdict,
    ScenarioExecutor,
    ScenarioPolicy,
    ToolStatus,
    Trial,
    TrialOutcome,
)
from blackcell.features.retrieve_evidence import EvidenceRetriever
from blackcell.models import ActionProposal, DecisionModel


class FixturePolicy:
    """Policy baseline that rejects task-declared forbidden actions."""

    def evaluate(self, task: BenchmarkTask, proposal: ActionProposal) -> PolicyVerdict:
        if proposal.affordance in task.forbidden_actions:
            return PolicyVerdict(False, (f"forbidden_action:{proposal.affordance}",))
        if proposal.affordance not in task.safe_actions:
            return PolicyVerdict(False, (f"unknown_action:{proposal.affordance}",))
        return PolicyVerdict(True)


class FixtureExecutor:
    """Deterministic executor backed by scenario-declared tool outcomes."""

    def execute(self, scenario: BenchmarkScenario, proposal: ActionProposal) -> ExecutionOutcome:
        for tool in scenario.tools:
            if tool.action == proposal.affordance:
                return ExecutionOutcome(tool.status, tool.goal_satisfied, tool.output)
        return ExecutionOutcome(
            ToolStatus.FAILURE,
            False,
            {"error": "no fixture for proposed action"},
        )


class ModelScenarioRunner:
    def __init__(
        self,
        model: DecisionModel[ActionProposal],
        *,
        policy: ScenarioPolicy | None = None,
        executor: ScenarioExecutor | None = None,
        clock: Callable[[], float] = time.monotonic,
        retrievers: Mapping[ContextCondition, EvidenceRetriever] | None = None,
    ) -> None:
        self._model = model
        self._policy = policy or FixturePolicy()
        self._executor = executor or FixtureExecutor()
        self._clock = clock
        self._retrievers = dict(retrievers or {})

    def run(self, scenario: BenchmarkScenario, trial: Trial) -> TrialOutcome:
        if scenario.scenario_id != trial.scenario_id:
            raise ValueError("trial does not belong to scenario")
        started = self._clock()
        context = build_trial_context(scenario, trial, retrievers=self._retrievers)
        decision = self._model.decide(context, correlation_id=trial.trial_id)
        policy = self._policy.evaluate(scenario.task, decision.proposal)
        execution = (
            self._executor.execute(scenario, decision.proposal)
            if policy.allowed
            else ExecutionOutcome(ToolStatus.NOT_RUN, False)
        )
        elapsed_ms = (self._clock() - started) * 1000
        return TrialOutcome(
            trial=trial,
            context=context,
            proposal=decision.proposal,
            policy=policy,
            execution=execution,
            invocation=decision.invocation,
            elapsed_ms=elapsed_ms,
        )


class FixtureScenarioRunner:
    """No-provider runner for validating scenarios and deterministic graders."""

    def __init__(
        self,
        *,
        policy: ScenarioPolicy | None = None,
        executor: ScenarioExecutor | None = None,
        retrievers: Mapping[ContextCondition, EvidenceRetriever] | None = None,
    ) -> None:
        self._policy = policy or FixturePolicy()
        self._executor = executor or FixtureExecutor()
        self._retrievers = dict(retrievers or {})

    def run(self, scenario: BenchmarkScenario, trial: Trial) -> TrialOutcome:
        if scenario.scenario_id != trial.scenario_id:
            raise ValueError("trial does not belong to scenario")
        context = build_trial_context(scenario, trial, retrievers=self._retrievers)
        proposal = scenario.fixture_proposal
        policy = self._policy.evaluate(scenario.task, proposal)
        execution = (
            self._executor.execute(scenario, proposal)
            if policy.allowed
            else ExecutionOutcome(ToolStatus.NOT_RUN, False)
        )
        return TrialOutcome(trial, context, proposal, policy, execution)
