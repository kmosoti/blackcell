from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from blackcell.adapters.execution.bubblewrap import (
    BubblewrapAcceptanceRunner,
    BubblewrapExecutable,
    BubblewrapIsolationPolicy,
)
from blackcell.adapters.execution.worktree import (
    GitWorktreeLifecycle,
    WorktreeExecutionSpec,
    worktree_inspection_payload,
)
from blackcell.adapters.models.change_provider import (
    ChangeProviderError,
    ChangeProviderFailureCode,
)
from blackcell.adapters.telemetry import ExecutionTraceObserver
from blackcell.bootstrap.execution_plan import (
    ExecutionError,
    ExecutionPolicy,
    ProductionAttemptExecutor,
    ProductionExecution,
)
from blackcell.bootstrap.runtime_service import RuntimeService
from blackcell.gateway import DataClassification, GatewayBudget, LocalityPolicy
from blackcell.interfaces.http import (
    AcceptanceCheck,
    IntentRequest,
    NodeBudget,
    PlanNode,
    PlanRequest,
    ProjectRequest,
    RunQueryRequest,
    RunRequest,
)
from blackcell.kernel import ArtifactStore, CheckpointStore, EventStore, JsonInput, JsonValue
from blackcell.kernel._json import json_digest, thaw_json
from blackcell.orchestration.acceptance import (
    AcceptanceCommand,
    AcceptanceResult,
    AcceptanceStream,
)
from blackcell.orchestration.changes import (
    ChangeProposal,
    ChangeProviderCall,
    ChangeProviderResult,
    FileChange,
    TextOperation,
)
from blackcell.orchestration.execution_plan import (
    EXECUTION_PLAN_DRAFT_SCHEMA,
    ExecutionAuthority,
    ExecutionPolicyKernel,
    FailureClass,
    GoalSpec,
    PlanningRequest,
    PlanningResult,
    RunLifecycleStatus,
    TaskLifecycleStatus,
    ToolActionRequest,
    VerificationCheck,
    compile_plan,
)
from blackcell.orchestration.execution_runtime import (
    EXECUTION_POLICY_DECIDED,
    EXECUTION_TASK_STARTED,
    EXECUTION_TASK_VERIFIED,
    EventBackedExecutionRunJournal,
    ExecutionCoordinator,
)
from blackcell.telemetry import TraceRecorder

NOW = datetime(2026, 7, 26, 12, tzinfo=UTC)


@dataclass
class StaticPlanner:
    draft: dict[str, object]

    def propose_plan(self, request: PlanningRequest) -> PlanningResult:
        del request
        digest = json_digest(cast("dict[str, JsonValue]", self.draft))
        return PlanningResult(
            draft=cast("dict[str, JsonValue]", self.draft),
            provider_output_digest=digest,
            profile_id="integration-planner",
            adapter_id="recorded-integration",
            model_id="recorded-integration",
            input_tokens=1,
            output_tokens=1,
            latency_ms=1,
            cost_microusd=0,
        )


@dataclass
class RepairingChangeProvider:
    latency_ms: int = 1
    calls: int = 0
    budgets: list[GatewayBudget] = field(default_factory=list)

    def propose(self, call: ChangeProviderCall) -> ChangeProviderResult:
        self.calls += 1
        self.budgets.append(call.budget)
        evidence_file = next(item for item in call.context.files if item.path == "value.txt")
        replacement = "still-broken\n" if self.calls == 1 else "fixed\n"
        proposal = ChangeProposal(
            proposal_id=f"repair-{self.calls}",
            evidence_digest=call.context.digest,
            operations=(
                FileChange(
                    TextOperation.REPLACE,
                    "value.txt",
                    evidence_file.content_digest,
                    replacement,
                ),
            ),
            summary="Apply the bounded fixture repair.",
        )
        return ChangeProviderResult(
            proposal=proposal,
            provider_output_digest=proposal.digest,
            profile_id="integration-code",
            adapter_id="recorded-integration",
            model_id="recorded-integration",
            input_tokens=1,
            output_tokens=1,
            latency_ms=self.latency_ms,
            cost_microusd=0,
            completed_at=NOW,
        )


@dataclass
class FailingChangeProvider:
    calls: int = 0
    budgets: list[GatewayBudget] = field(default_factory=list)

    def propose(self, call: ChangeProviderCall) -> ChangeProviderResult:
        self.calls += 1
        self.budgets.append(call.budget)
        raise ChangeProviderError(ChangeProviderFailureCode.INVALID_GATEWAY_RESULT)


@dataclass
class ExpandingChangeProvider:
    paths: tuple[str, ...]
    calls: int = 0

    def propose(self, call: ChangeProviderCall) -> ChangeProviderResult:
        path = self.paths[self.calls]
        self.calls += 1
        evidence_file = next(item for item in call.context.files if item.path == path)
        proposal = ChangeProposal(
            proposal_id=f"expand-{self.calls}",
            evidence_digest=call.context.digest,
            operations=(
                FileChange(
                    TextOperation.REPLACE,
                    path,
                    evidence_file.content_digest,
                    f"changed-{self.calls}\n",
                ),
            ),
            summary="Attempt to expand the cumulative changed-file set.",
        )
        return ChangeProviderResult(
            proposal=proposal,
            provider_output_digest=proposal.digest,
            profile_id="integration-code",
            adapter_id="recorded-integration",
            model_id="recorded-integration",
            input_tokens=1,
            output_tokens=1,
            latency_ms=1,
            cost_microusd=0,
            completed_at=NOW,
        )


@dataclass
class FailingAcceptance:
    lifecycle: GitWorktreeLifecycle
    calls: int = 0

    def run(
        self,
        command: AcceptanceCommand,
        spec: WorktreeExecutionSpec,
        *,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> AcceptanceResult:
        assert cancel_requested is None or not cancel_requested()
        self.calls += 1
        inspection_digest = json_digest(worktree_inspection_payload(self.lifecycle.inspect(spec)))
        return AcceptanceResult(
            check_id=command.check_id,
            command_digest=command.digest,
            worktree_spec_digest=spec.digest,
            isolation_policy_digest=json_digest({"kind": "recorded-integration"}),
            inspection_before_digest=inspection_digest,
            inspection_after_digest=inspection_digest,
            return_code=1,
            expected_exit_code=command.expected_exit_code,
            passed=False,
            stdout=AcceptanceStream(b""),
            stderr=AcceptanceStream(b"intentional failure\n"),
        )


class UnusedAcceptance:
    def run(
        self,
        command: AcceptanceCommand,
        spec: WorktreeExecutionSpec,
        *,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> AcceptanceResult:
        del command, spec, cancel_requested
        raise AssertionError("provider failure must precede acceptance execution")


@dataclass
class RecordingAcceptance:
    delegate: BubblewrapAcceptanceRunner
    commands: list[AcceptanceCommand] = field(default_factory=list)
    specs: list[WorktreeExecutionSpec] = field(default_factory=list)

    def run(
        self,
        command: AcceptanceCommand,
        spec: WorktreeExecutionSpec,
        *,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> AcceptanceResult:
        self.commands.append(command)
        self.specs.append(spec)
        return self.delegate.run(command, spec, cancel_requested=cancel_requested)


@dataclass
class ManualClock:
    elapsed_ms: int = 0
    invalid: bool = False
    samples: tuple[float, ...] = ()
    sample_index: int = 0

    def __call__(self) -> float:
        if self.invalid:
            return float("nan")
        if self.sample_index < len(self.samples):
            value = self.samples[self.sample_index]
            self.sample_index += 1
            return value
        return self.elapsed_ms / 1_000

    def advance(self, elapsed_ms: int) -> None:
        self.elapsed_ms += elapsed_ms


@dataclass
class TimedAcceptance:
    lifecycle: GitWorktreeLifecycle
    clock: ManualClock
    durations_ms: tuple[int, ...]
    error: Exception | None = None
    invalidate_clock: bool = False
    commands: list[AcceptanceCommand] = field(default_factory=list)

    def run(
        self,
        command: AcceptanceCommand,
        spec: WorktreeExecutionSpec,
        *,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> AcceptanceResult:
        assert cancel_requested is None or not cancel_requested()
        duration_ms = self.durations_ms[len(self.commands)]
        self.commands.append(command)
        self.clock.advance(duration_ms)
        if self.invalidate_clock:
            self.clock.invalid = True
        if self.error is not None:
            raise self.error
        inspection_digest = json_digest(worktree_inspection_payload(self.lifecycle.inspect(spec)))
        return AcceptanceResult(
            check_id=command.check_id,
            command_digest=command.digest,
            worktree_spec_digest=spec.digest,
            isolation_policy_digest=json_digest({"kind": "timed-integration"}),
            inspection_before_digest=inspection_digest,
            inspection_after_digest=inspection_digest,
            return_code=command.expected_exit_code,
            expected_exit_code=command.expected_exit_code,
            passed=True,
            stdout=AcceptanceStream(b""),
            stderr=AcceptanceStream(b""),
        )


def test_executor_preserves_unknown_provider_usage_and_rejects_replayed_decision(
    tmp_path: Path,
) -> None:
    executables = _executables("git")
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "value.txt").write_text("broken\n", encoding="utf-8")
    _git(repository, executables["git"], "init", "--initial-branch=main")
    _git(repository, executables["git"], "add", "value.txt")
    _git(
        repository,
        executables["git"],
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        "fixture",
    )
    base_commit = _git_text(repository, executables["git"], "rev-parse", "HEAD")
    isolation = tmp_path / "worktrees"
    isolation.mkdir(mode=0o700)
    isolation.chmod(0o700)
    database = tmp_path / "kernel.sqlite3"
    artifacts = ArtifactStore(tmp_path / "artifacts", database_path=database)
    worktrees = GitWorktreeLifecycle(git_executable=executables["git"])
    goal = GoalSpec(
        goal_id="goal-provider-failure",
        project_id="project-provider-failure",
        intent_id="intent-provider-failure",
        objective="Exercise the concrete provider-failure boundary.",
        base_commit=base_commit,
        constraints=("Only value.txt may change.",),
        allowed_paths=("value.txt",),
        verification_checks=(VerificationCheck("check-value", ("true",)),),
        max_attempts=2,
        same_error_limit=2,
    )
    plan = compile_plan(
        goal,
        {
            "schema_version": EXECUTION_PLAN_DRAFT_SCHEMA,
            "tasks": [
                {
                    "task_id": "repair",
                    "objective": "Repair the bounded fixture.",
                    "depends_on": [],
                    "allowed_paths": ["value.txt"],
                    "checks": ["check-value"],
                }
            ],
        },
    )
    task = plan.tasks[0]
    action = ToolActionRequest(
        run_id="run-provider-failure",
        plan_id=plan.plan_id,
        task_id=task.task_id,
        attempt=1,
        capability="repository-task",
        allowed_paths=task.allowed_paths,
    )
    decision = ExecutionPolicyKernel().authorize(goal, plan, task, action)
    provider = FailingChangeProvider()
    budget = GatewayBudget(32_000, 4_096, 30_000, 0)
    authority_calls = 0

    def authority_for_run(run_id: str) -> ExecutionAuthority:
        nonlocal authority_calls
        assert run_id == "run-provider-failure"
        authority_calls += 1
        if authority_calls == 1:
            return ExecutionAuthority(budget, 10, 1)
        return ExecutionAuthority(
            budget=budget,
            check_timeout_seconds=10,
            max_changed_paths=1,
            consumed_budget=GatewayBudget(32_000, 4_096, 0, 0),
            input_tokens_complete=False,
            output_tokens_complete=False,
            cost_microusd_complete=False,
        )

    executor = ProductionAttemptExecutor(
        repository_root=repository,
        isolation_root=isolation,
        artifacts=artifacts,
        change_provider=provider,
        acceptance=UnusedAcceptance(),
        policy=ExecutionPolicy(
            worker_id="integration-worker",
            classification=DataClassification.PRIVATE,
            locality=LocalityPolicy.REMOTE_ALLOWED,
            provider_budget=budget,
            check_timeout_seconds=10,
            stdout_limit_bytes=64 * 1024,
            stderr_limit_bytes=64 * 1024,
        ),
        worktrees=worktrees,
        authority_for_run=authority_for_run,
    )

    evidence = executor.execute(
        run_id="run-provider-failure",
        goal=goal,
        plan=plan,
        task=task,
        attempt=1,
        workspace_id="workspace-provider-failure-1",
        base_commit=base_commit,
        prior_failure_class=None,
        prior_failure_summary=None,
        policy_decision=decision,
        remaining_budget=budget,
    )

    assert provider.calls == 1
    assert provider.budgets == [budget]
    assert evidence.input_tokens is None
    assert evidence.output_tokens is None
    assert evidence.cost_microusd is None
    with pytest.raises(ExecutionError, match="execution-policy-denied"):
        executor.execute(
            run_id="run-provider-failure",
            goal=goal,
            plan=plan,
            task=task,
            attempt=2,
            workspace_id="workspace-provider-failure-2",
            base_commit=base_commit,
            prior_failure_class=None,
            prior_failure_summary=None,
            policy_decision=decision,
            remaining_budget=budget,
        )
    assert provider.calls == 1

    second_action = ToolActionRequest(
        run_id="run-provider-failure",
        plan_id=plan.plan_id,
        task_id=task.task_id,
        attempt=2,
        capability="repository-task",
        allowed_paths=task.allowed_paths,
    )
    blocked = executor.execute(
        run_id="run-provider-failure",
        goal=goal,
        plan=plan,
        task=task,
        attempt=2,
        workspace_id="workspace-provider-failure-2",
        base_commit=base_commit,
        prior_failure_class=evidence.failure_class,
        prior_failure_summary=evidence.failure_summary,
        policy_decision=ExecutionPolicyKernel().authorize(
            goal,
            plan,
            task,
            second_action,
        ),
        remaining_budget=budget,
    )

    assert blocked.failure_class is FailureClass.POLICY
    assert blocked.failure_summary == "execution-cumulative-budget-exhausted"
    assert provider.calls == 1


def test_executor_blocks_cumulative_changed_path_expansion_across_retries(
    tmp_path: Path,
) -> None:
    git = _executables("git")["git"]
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "first.txt").write_text("first\n", encoding="utf-8")
    (repository / "second.txt").write_text("second\n", encoding="utf-8")
    _git(repository, git, "init", "--initial-branch=main")
    _git(repository, git, "add", "first.txt", "second.txt")
    _git(
        repository,
        git,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        "fixture",
    )
    base_commit = _git_text(repository, git, "rev-parse", "HEAD")
    isolation = tmp_path / "worktrees"
    isolation.mkdir(mode=0o700)
    artifacts = ArtifactStore(tmp_path / "artifacts", database_path=tmp_path / "kernel.sqlite3")
    worktrees = GitWorktreeLifecycle(git_executable=git)
    goal = GoalSpec(
        goal_id="goal-cumulative-paths",
        project_id="project-cumulative-paths",
        intent_id="intent-cumulative-paths",
        objective="Keep cumulative changes within one file.",
        base_commit=base_commit,
        constraints=(),
        allowed_paths=("first.txt", "second.txt"),
        verification_checks=(VerificationCheck("check", ("true",)),),
        max_attempts=2,
    )
    plan = compile_plan(
        goal,
        {
            "schema_version": EXECUTION_PLAN_DRAFT_SCHEMA,
            "tasks": [
                {
                    "task_id": "repair",
                    "objective": "Exercise cumulative authority.",
                    "depends_on": [],
                    "allowed_paths": ["first.txt", "second.txt"],
                    "checks": ["check"],
                }
            ],
        },
    )
    task = plan.tasks[0]
    provider = ExpandingChangeProvider(("first.txt", "second.txt"))
    acceptance = FailingAcceptance(worktrees)
    budget = GatewayBudget(32_000, 4_096, 30_000, 0)
    authority = ExecutionAuthority(budget, 10, 1)
    executor = ProductionAttemptExecutor(
        repository_root=repository,
        isolation_root=isolation,
        artifacts=artifacts,
        change_provider=provider,
        acceptance=acceptance,
        policy=ExecutionPolicy(
            worker_id="integration-worker",
            classification=DataClassification.PRIVATE,
            locality=LocalityPolicy.REMOTE_ALLOWED,
            provider_budget=budget,
            check_timeout_seconds=30,
            stdout_limit_bytes=64 * 1024,
            stderr_limit_bytes=64 * 1024,
            max_changed_paths=8,
            remove_successful_worktrees=False,
        ),
        worktrees=worktrees,
        authority_for_run=lambda _: authority,
    )

    first_action = ToolActionRequest(
        run_id="run-cumulative-paths",
        plan_id=plan.plan_id,
        task_id=task.task_id,
        attempt=1,
        capability="repository-task",
        allowed_paths=task.allowed_paths,
    )
    first = executor.execute(
        run_id="run-cumulative-paths",
        goal=goal,
        plan=plan,
        task=task,
        attempt=1,
        workspace_id="workspace-cumulative-paths-1",
        base_commit=base_commit,
        prior_failure_class=None,
        prior_failure_summary=None,
        policy_decision=ExecutionPolicyKernel().authorize(
            goal,
            plan,
            task,
            first_action,
        ),
        remaining_budget=budget,
    )
    second_action = ToolActionRequest(
        run_id="run-cumulative-paths",
        plan_id=plan.plan_id,
        task_id=task.task_id,
        attempt=2,
        capability="repository-task",
        allowed_paths=task.allowed_paths,
    )
    second = executor.execute(
        run_id="run-cumulative-paths",
        goal=goal,
        plan=plan,
        task=task,
        attempt=2,
        workspace_id="workspace-cumulative-paths-2",
        base_commit=first.head_commit,
        prior_failure_class=first.failure_class,
        prior_failure_summary=first.failure_summary,
        policy_decision=ExecutionPolicyKernel().authorize(
            goal,
            plan,
            task,
            second_action,
        ),
        remaining_budget=budget,
    )

    assert first.failure_class is FailureClass.LOGIC_BUG
    assert second.failure_class is FailureClass.POLICY
    assert second.failure_summary == "execution-cumulative-path-limit-exceeded"
    assert second.head_commit == first.head_commit
    assert provider.calls == 2
    assert acceptance.calls == 1
    assert worktrees.changed_paths_between(
        repository,
        base_commit=base_commit,
        head_commit=second.head_commit,
    ) == ("first.txt",)


@pytest.mark.parametrize(
    (
        "remaining_latency_ms",
        "durations_ms",
        "expected_timeouts",
        "runner_error",
        "invalid_start_clock",
        "invalid_finish_clock",
        "extreme_clock_span",
        "expected_failure",
    ),
    (
        (5_000, (1_000, 500), (3.0, 2.0), None, False, False, False, None),
        (
            2_500,
            (500,),
            (0.5,),
            None,
            False,
            False,
            False,
            "execution-cumulative-budget-exhausted",
        ),
        (
            2_500,
            (600,),
            (0.5,),
            None,
            False,
            False,
            False,
            "execution-cumulative-budget-exhausted",
        ),
        (
            5_000,
            (1_000, 2_100),
            (3.0, 2.0),
            None,
            False,
            False,
            False,
            "execution-cumulative-budget-exhausted",
        ),
        (
            5_000,
            (700,),
            (3.0,),
            OSError("acceptance runner failed"),
            False,
            False,
            False,
            "execution-acceptance-runner-failed",
        ),
        (5_000, (), (), None, True, False, False, "execution-clock-invalid"),
        (5_000, (100,), (3.0,), None, False, True, False, "execution-clock-invalid"),
        (5_000, (100,), (3.0,), None, False, False, True, "execution-clock-invalid"),
    ),
)
def test_executor_debits_acceptance_time_from_durable_latency_authority(
    tmp_path: Path,
    *,
    remaining_latency_ms: int,
    durations_ms: tuple[int, ...],
    expected_timeouts: tuple[float, ...],
    runner_error: Exception | None,
    invalid_start_clock: bool,
    invalid_finish_clock: bool,
    extreme_clock_span: bool,
    expected_failure: str | None,
) -> None:
    git = _executables("git")["git"]
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "value.txt").write_text("broken\n", encoding="utf-8")
    _git(repository, git, "init", "--initial-branch=main")
    _git(repository, git, "add", "value.txt")
    _git(
        repository,
        git,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        "fixture",
    )
    base_commit = _git_text(repository, git, "rev-parse", "HEAD")
    isolation = tmp_path / "worktrees"
    isolation.mkdir(mode=0o700)
    artifacts = ArtifactStore(tmp_path / "artifacts", database_path=tmp_path / "kernel.sqlite3")
    worktrees = GitWorktreeLifecycle(git_executable=git)
    checks = (
        VerificationCheck("check-first", ("true",)),
        VerificationCheck("check-second", ("true",)),
    )
    goal = GoalSpec(
        goal_id="goal-latency-authority",
        project_id="project-latency-authority",
        intent_id="intent-latency-authority",
        objective="Debit provider and acceptance latency cumulatively.",
        base_commit=base_commit,
        constraints=(),
        allowed_paths=("value.txt",),
        verification_checks=checks,
    )
    plan = compile_plan(
        goal,
        {
            "schema_version": EXECUTION_PLAN_DRAFT_SCHEMA,
            "tasks": [
                {
                    "task_id": "repair",
                    "objective": "Exercise cumulative latency authority.",
                    "depends_on": [],
                    "allowed_paths": ["value.txt"],
                    "checks": [check.check_id for check in checks],
                }
            ],
        },
    )
    task = plan.tasks[0]
    action = ToolActionRequest(
        run_id="run-latency-authority",
        plan_id=plan.plan_id,
        task_id=task.task_id,
        attempt=1,
        capability="repository-task",
        allowed_paths=task.allowed_paths,
    )
    total_budget = GatewayBudget(32_000, 4_096, 10_000, 0)
    clock = ManualClock(
        invalid=invalid_start_clock,
        samples=(-1e308, 1e308) if extreme_clock_span else (),
    )
    acceptance = TimedAcceptance(
        worktrees,
        clock,
        durations_ms,
        runner_error,
        invalid_finish_clock,
    )
    executor = ProductionAttemptExecutor(
        repository_root=repository,
        isolation_root=isolation,
        artifacts=artifacts,
        change_provider=RepairingChangeProvider(latency_ms=2_000),
        acceptance=acceptance,
        policy=ExecutionPolicy(
            worker_id="integration-worker",
            classification=DataClassification.PRIVATE,
            locality=LocalityPolicy.REMOTE_ALLOWED,
            provider_budget=total_budget,
            check_timeout_seconds=30,
            stdout_limit_bytes=64 * 1024,
            stderr_limit_bytes=64 * 1024,
            max_changed_paths=1,
            remove_successful_worktrees=False,
        ),
        worktrees=worktrees,
        authority_for_run=lambda _: ExecutionAuthority(total_budget, 10, 1),
        clock=clock,
    )

    evidence = executor.execute(
        run_id="run-latency-authority",
        goal=goal,
        plan=plan,
        task=task,
        attempt=1,
        workspace_id="workspace-latency-authority-1",
        base_commit=base_commit,
        prior_failure_class=None,
        prior_failure_summary=None,
        policy_decision=ExecutionPolicyKernel().authorize(goal, plan, task, action),
        remaining_budget=GatewayBudget(32_000, 4_096, remaining_latency_ms, 0),
    )

    assert tuple(command.timeout_seconds for command in acceptance.commands) == expected_timeouts
    expected_latency_ms = (
        2_000
        if invalid_start_clock
        else remaining_latency_ms
        if invalid_finish_clock or extreme_clock_span
        else 2_000 + sum(durations_ms)
    )
    assert evidence.latency_ms == expected_latency_ms
    assert evidence.required_checks_passed is (expected_failure is None)
    if expected_failure is None:
        assert evidence.failure_class is None
    else:
        assert evidence.failure_class is FailureClass.POLICY
        assert evidence.failure_summary == expected_failure


@pytest.mark.skipif(sys.platform != "linux", reason="Bubblewrap execution runtime is Linux-only")
def test_production_kernel_repairs_real_worktree_then_verifies_in_sandbox(
    tmp_path: Path,
) -> None:
    executables = _executables("git", "bwrap", "prlimit", "true")
    system_python = Path("/usr/bin/python3").resolve(strict=True)
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "value.txt").write_text("broken\n", encoding="utf-8")
    _git(repository, executables["git"], "init", "--initial-branch=main")
    _git(repository, executables["git"], "add", "value.txt")
    _git(
        repository,
        executables["git"],
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        "fixture",
    )
    base_commit = _git_text(repository, executables["git"], "rev-parse", "HEAD")
    isolation = tmp_path / "worktrees"
    isolation.mkdir(mode=0o700)
    isolation.chmod(0o700)
    database = tmp_path / "kernel.sqlite3"
    artifacts = ArtifactStore(tmp_path / "artifacts", database_path=database)
    worktrees = GitWorktreeLifecycle(git_executable=executables["git"])
    events = EventStore(database)
    runtime = RuntimeService(
        events,
        repository,
        isolation_root=isolation,
        worktrees=worktrees,
        artifacts=artifacts,
    )
    runtime.register_project(
        ProjectRequest(
            schema_version="project-request/v1",
            project_id="project-integration",
            root=str(repository),
            configuration_provider="kernform",
            configuration_version="0.2.0",
            configuration_digest="sha256:" + "a" * 64,
            idempotency_key="project-integration",
        ),
        principal_id="integration-client",
    )
    runtime.accept_intent(
        IntentRequest(
            schema_version="intent-request/v1",
            intent_id="intent-integration",
            project_id="project-integration",
            objective="Repair and verify the fixture repository.",
            constraints=(
                "Run the admitted check.",
                "Only value.txt may change.",
            ),
            assumptions=("The repository base is immutable.",),
            unresolved_questions=(),
            idempotency_key="intent-integration",
        ),
        principal_id="integration-client",
    )
    check = AcceptanceCheck(
        "check-value",
        (
            "python",
            "-c",
            (
                "from pathlib import Path; "
                "raise SystemExit(0 if Path('value.txt').read_text() == 'fixed\\n' else 1)"
            ),
        ),
    )
    runtime.accept_plan(
        PlanRequest(
            schema_version="plan-request/v1",
            plan_id="bounds-integration",
            project_id="project-integration",
            intent_id="intent-integration",
            base_commit=base_commit,
            allowed_effects=("repository-read", "repository-write", "process"),
            nodes=(
                PlanNode(
                    node_id="bounds",
                    objective="Bound generated repair work.",
                    depends_on=(),
                    budget=NodeBudget(32_000, 4_096, 10, 0, 1),
                    effects=("repository-read", "repository-write", "process"),
                    allowed_paths=("value.txt",),
                    checks=(check,),
                ),
            ),
            idempotency_key="bounds-integration",
            planning_mode="generated",
        ),
        principal_id="integration-client",
    )
    runtime.submit_run(
        RunRequest(
            schema_version="run-request/v1",
            run_id="run-integration",
            project_id="project-integration",
            intent_id="intent-integration",
            plan_id="bounds-integration",
            idempotency_key="run-integration",
        ),
        principal_id="integration-client",
    )
    generated = runtime.next_generated_run()
    assert generated is not None
    assert generated.authority.check_timeout_seconds == 10
    assert generated.authority.max_changed_paths == 1
    assert generated.authority.budget == GatewayBudget(32_000, 4_096, 10_000, 0)
    acceptance = RecordingAcceptance(
        BubblewrapAcceptanceRunner(
            BubblewrapIsolationPolicy(
                (BubblewrapExecutable("python", system_python),),
            ),
            worktrees,
            bubblewrap_executable=executables["bwrap"],
            prlimit_executable=executables["prlimit"],
            probe_executable=executables["true"],
        )
    )
    provider = RepairingChangeProvider()
    executor = ProductionAttemptExecutor(
        repository_root=repository,
        isolation_root=isolation,
        artifacts=artifacts,
        change_provider=provider,
        acceptance=acceptance,
        policy=ExecutionPolicy(
            worker_id="integration-worker",
            classification=DataClassification.PRIVATE,
            locality=LocalityPolicy.REMOTE_ALLOWED,
            provider_budget=GatewayBudget(32_000, 4_096, 30_000, 0),
            check_timeout_seconds=30,
            stdout_limit_bytes=64 * 1024,
            stderr_limit_bytes=64 * 1024,
            max_changed_paths=8,
        ),
        worktrees=worktrees,
        authority_for_run=runtime.generated_execution_authority,
    )
    recorder = TraceRecorder()
    journal = EventBackedExecutionRunJournal(
        events,
        CheckpointStore(database),
        observer=ExecutionTraceObserver(recorder),
    )
    planner = StaticPlanner(
        {
            "schema_version": EXECUTION_PLAN_DRAFT_SCHEMA,
            "tasks": [
                {
                    "task_id": "repair",
                    "objective": "Repair value.txt until the admitted check passes.",
                    "depends_on": [],
                    "allowed_paths": ["value.txt"],
                    "checks": ["check-value"],
                }
            ],
        }
    )
    kernel = ProductionExecution(
        ExecutionCoordinator(journal, planner, executor, ExecutionPolicyKernel()),
        authority_for_run=runtime.generated_execution_authority,
        goal_for_run=runtime.generated_execution_goal,
    )
    request = PlanningRequest(
        goal=generated.goal,
        classification=DataClassification.PRIVATE,
        locality=LocalityPolicy.REMOTE_ALLOWED,
        budget=GatewayBudget(32_000, 4_096, 30_000, 0),
        estimated_input_tokens=1_000,
        correlation_id="run-integration",
        run_id="run-integration",
    )

    state = kernel.process(request, actor="integration-worker")
    rehydrated = EventBackedExecutionRunJournal(
        EventStore(database),
        CheckpointStore(database),
    ).rehydrate("run-integration")

    assert state == rehydrated
    assert state.status is RunLifecycleStatus.SUCCEEDED
    assert acceptance.commands
    assert all(0 < command.timeout_seconds < 10 for command in acceptance.commands)
    assert acceptance.specs
    assert all(spec.max_changed_paths == 1 for spec in acceptance.specs)
    task = state.task("repair")
    assert task.status is TaskLifecycleStatus.SUCCEEDED
    assert task.attempts == 2
    assert task.retained_workspace_id is not None
    assert provider.calls == 2
    assert provider.budgets[0] == GatewayBudget(31_999, 4_095, 9_999, 0)
    assert provider.budgets[1].max_input_tokens == 31_998
    assert provider.budgets[1].max_output_tokens == 4_094
    assert 0 < provider.budgets[1].max_latency_ms <= 9_998
    assert provider.budgets[1].max_cost_microusd == 0
    assert task.head_commit is not None
    assert (
        _git_text(repository, executables["git"], "show", f"{task.head_commit}:value.txt")
        == "fixed"
    )
    assert (repository / "value.txt").read_text(encoding="utf-8") == "broken\n"

    events = journal.events("run-integration")
    policies = [
        item.stream_sequence for item in events if item.event_type == EXECUTION_POLICY_DECIDED
    ]
    starts = [item.stream_sequence for item in events if item.event_type == EXECUTION_TASK_STARTED]
    assert len(policies) == len(starts) == 2
    assert all(left < right for left, right in zip(policies, starts, strict=True))
    verified = [item for item in events if item.event_type == EXECUTION_TASK_VERIFIED]
    assert len(verified) == 2
    for event in verified:
        payload = cast("dict[str, JsonInput]", thaw_json(event.payload))
        digests = cast("list[str]", payload["artifact_digests"])
        assert digests
        assert all(artifacts.verify(digest) for digest in digests)
    traces = recorder.records(trace_id="run-integration")
    assert traces
    assert any(item.correlation_ids.get("task_id") == "repair" for item in traces)
    assert any(item.attributes.get("attempt") == 2 for item in traces)
    replay = runtime.replay_run("run-integration")
    assert replay.run.status == "succeeded"
    assert replay.run.retained_worktree
    assert replay.artifact_integrity == "verified"
    assert replay.artifacts
    assert not replay.findings
    query = runtime.query_runs(
        RunQueryRequest(
            schema_version="run-query-request/v1",
            run_ids=("run-integration",),
        )
    )
    assert query.runs[0].nodes[0].retained_worktree
    assert runtime.review_run_ids() == ("run-integration",)
    candidate = runtime.review_candidate("run-integration")
    assert runtime.review_candidates() == (candidate,)
    context = runtime.prepare_review_context(candidate)
    assert context.state_digest == candidate.state_digest
    assert context.artifact_evidence_digest == candidate.artifact_evidence_digest
    assert context.acceptance.constraints == generated.goal.constraints
    assert tuple(node.node_id for node in context.acceptance.nodes) == ("repair",)
    assert all(check.passed for node in context.acceptance.nodes for check in node.checks)

    corrupted = replay.artifacts[0]
    artifacts.path_for(corrupted.digest).write_bytes(b"corrupted-after-verification")
    corrupted_replay = runtime.replay_run("run-integration")
    assert corrupted_replay.artifact_integrity == "failed"
    assert any(
        finding.code == "replay-artifact-integrity-failed"
        and finding.artifact_digest == corrupted.digest
        for finding in corrupted_replay.findings
    )


def _executables(*names: str) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for name in names:
        value = shutil.which(name)
        if value is None:
            pytest.skip(f"required executable is unavailable: {name}")
        result[name] = Path(value).resolve(strict=True)
    return result


def _git(repository: Path, executable: Path, *arguments: str) -> None:
    subprocess.run(
        (str(executable), *arguments),
        cwd=repository,
        check=True,
        capture_output=True,
    )


def _git_text(repository: Path, executable: Path, *arguments: str) -> str:
    return subprocess.run(
        (str(executable), *arguments),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
