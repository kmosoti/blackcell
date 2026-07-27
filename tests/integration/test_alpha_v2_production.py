from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from blackcell.adapters.execution.bubblewrap import (
    BubblewrapAcceptanceRunner,
    BubblewrapExecutable,
    BubblewrapIsolationPolicy,
)
from blackcell.adapters.execution.worktree import GitWorktreeLifecycle, WorktreeExecutionSpec
from blackcell.adapters.models.alpha_change_provider import (
    AlphaChangeProviderError,
    AlphaChangeProviderFailureCode,
)
from blackcell.adapters.telemetry import AlphaV2TraceObserver
from blackcell.bootstrap.alpha_runtime import AlphaRuntimeApiService
from blackcell.bootstrap.alpha_v2_kernel import (
    AlphaV2ExecutionError,
    AlphaV2ExecutionPolicy,
    ProductionAlphaV2AttemptExecutor,
    ProductionAlphaV2Kernel,
)
from blackcell.gateway import DataClassification, GatewayBudget, LocalityPolicy
from blackcell.interfaces.http import (
    AlphaAcceptanceCheck,
    AlphaIntentRequest,
    AlphaNodeBudget,
    AlphaPlanNode,
    AlphaPlanRequest,
    AlphaProjectRequest,
    AlphaRunRequest,
)
from blackcell.kernel import ArtifactStore, CheckpointStore, EventStore, JsonInput, JsonValue
from blackcell.kernel._json import json_digest, thaw_json
from blackcell.orchestration.alpha_acceptance import (
    AlphaAcceptanceCommand,
    AlphaAcceptanceResult,
)
from blackcell.orchestration.alpha_changes import (
    AlphaChangeProposal,
    AlphaChangeProviderCall,
    AlphaChangeProviderResult,
    AlphaFileChange,
    AlphaTextOperation,
)
from blackcell.orchestration.alpha_v2 import (
    ALPHA_V2_PLAN_DRAFT_SCHEMA,
    AlphaGoalSpec,
    AlphaPlanningRequest,
    AlphaPlanningResult,
    AlphaV2PolicyKernel,
    AlphaVerificationCheck,
    RunLifecycleStatus,
    TaskLifecycleStatus,
    ToolActionRequest,
    compile_alpha_plan,
)
from blackcell.orchestration.alpha_v2_runtime import (
    ALPHA_V2_POLICY_DECIDED,
    ALPHA_V2_TASK_STARTED,
    ALPHA_V2_TASK_VERIFIED,
    AlphaV2Coordinator,
    EventBackedAlphaV2RunJournal,
)
from blackcell.telemetry import TraceRecorder

NOW = datetime(2026, 7, 26, 12, tzinfo=UTC)


@dataclass
class StaticPlanner:
    draft: dict[str, object]

    def propose_plan(self, request: AlphaPlanningRequest) -> AlphaPlanningResult:
        del request
        digest = json_digest(cast("dict[str, JsonValue]", self.draft))
        return AlphaPlanningResult(
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
    calls: int = 0

    def propose(self, call: AlphaChangeProviderCall) -> AlphaChangeProviderResult:
        self.calls += 1
        evidence_file = next(item for item in call.context.files if item.path == "value.txt")
        replacement = "still-broken\n" if self.calls == 1 else "fixed\n"
        proposal = AlphaChangeProposal(
            proposal_id=f"repair-{self.calls}",
            evidence_digest=call.context.digest,
            operations=(
                AlphaFileChange(
                    AlphaTextOperation.REPLACE,
                    "value.txt",
                    evidence_file.content_digest,
                    replacement,
                ),
            ),
            summary="Apply the bounded fixture repair.",
        )
        return AlphaChangeProviderResult(
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
class FailingChangeProvider:
    calls: int = 0

    def propose(self, call: AlphaChangeProviderCall) -> AlphaChangeProviderResult:
        del call
        self.calls += 1
        raise AlphaChangeProviderError(AlphaChangeProviderFailureCode.INVALID_GATEWAY_RESULT)


class UnusedAcceptance:
    def run(
        self,
        command: AlphaAcceptanceCommand,
        spec: WorktreeExecutionSpec,
        *,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> AlphaAcceptanceResult:
        del command, spec, cancel_requested
        raise AssertionError("provider failure must precede acceptance execution")


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
    goal = AlphaGoalSpec(
        goal_id="goal-provider-failure",
        project_id="project-provider-failure",
        intent_id="intent-provider-failure",
        objective="Exercise the concrete provider-failure boundary.",
        base_commit=base_commit,
        constraints=("Only value.txt may change.",),
        allowed_paths=("value.txt",),
        verification_checks=(AlphaVerificationCheck("check-value", ("true",)),),
        max_attempts=2,
        same_error_limit=2,
    )
    plan = compile_alpha_plan(
        goal,
        {
            "schema_version": ALPHA_V2_PLAN_DRAFT_SCHEMA,
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
    decision = AlphaV2PolicyKernel().authorize(goal, plan, task, action)
    provider = FailingChangeProvider()
    budget = GatewayBudget(32_000, 4_096, 30_000, 0)
    executor = ProductionAlphaV2AttemptExecutor(
        repository_root=repository,
        isolation_root=isolation,
        artifacts=artifacts,
        change_provider=provider,
        acceptance=UnusedAcceptance(),
        policy=AlphaV2ExecutionPolicy(
            worker_id="integration-worker",
            classification=DataClassification.PRIVATE,
            locality=LocalityPolicy.REMOTE_ALLOWED,
            provider_budget=budget,
            check_timeout_seconds=10,
            stdout_limit_bytes=64 * 1024,
            stderr_limit_bytes=64 * 1024,
        ),
        worktrees=worktrees,
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
    assert evidence.input_tokens is None
    assert evidence.output_tokens is None
    assert evidence.cost_microusd is None
    with pytest.raises(AlphaV2ExecutionError, match="alpha-v2-policy-denied"):
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


@pytest.mark.skipif(sys.platform != "linux", reason="Bubblewrap alpha runtime is Linux-only")
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
    runtime = AlphaRuntimeApiService(
        events,
        repository,
        isolation_root=isolation,
        worktrees=worktrees,
        artifacts=artifacts,
    )
    runtime.register_project(
        AlphaProjectRequest(
            schema_version="alpha-project-request/v1",
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
        AlphaIntentRequest(
            schema_version="alpha-intent-request/v1",
            intent_id="intent-integration",
            project_id="project-integration",
            objective="Repair and verify the fixture repository.",
            constraints=("Only value.txt may change.",),
            assumptions=(),
            unresolved_questions=(),
            idempotency_key="intent-integration",
        ),
        principal_id="integration-client",
    )
    check = AlphaAcceptanceCheck(
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
        AlphaPlanRequest(
            schema_version="alpha-plan-request/v1",
            plan_id="bounds-integration",
            project_id="project-integration",
            intent_id="intent-integration",
            base_commit=base_commit,
            allowed_effects=("repository-read", "repository-write", "process"),
            nodes=(
                AlphaPlanNode(
                    node_id="bounds",
                    objective="Bound generated repair work.",
                    depends_on=(),
                    budget=AlphaNodeBudget(32_000, 4_096, 10, 0, 1),
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
        AlphaRunRequest(
            schema_version="alpha-run-request/v1",
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
    acceptance = BubblewrapAcceptanceRunner(
        BubblewrapIsolationPolicy(
            (BubblewrapExecutable("python", system_python),),
        ),
        worktrees,
        bubblewrap_executable=executables["bwrap"],
        prlimit_executable=executables["prlimit"],
        probe_executable=executables["true"],
    )
    provider = RepairingChangeProvider()
    executor = ProductionAlphaV2AttemptExecutor(
        repository_root=repository,
        isolation_root=isolation,
        artifacts=artifacts,
        change_provider=provider,
        acceptance=acceptance,
        policy=AlphaV2ExecutionPolicy(
            worker_id="integration-worker",
            classification=DataClassification.PRIVATE,
            locality=LocalityPolicy.REMOTE_ALLOWED,
            provider_budget=GatewayBudget(32_000, 4_096, 30_000, 0),
            check_timeout_seconds=10,
            stdout_limit_bytes=64 * 1024,
            stderr_limit_bytes=64 * 1024,
        ),
        worktrees=worktrees,
    )
    recorder = TraceRecorder()
    journal = EventBackedAlphaV2RunJournal(
        events,
        CheckpointStore(database),
        observer=AlphaV2TraceObserver(recorder),
    )
    planner = StaticPlanner(
        {
            "schema_version": ALPHA_V2_PLAN_DRAFT_SCHEMA,
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
    kernel = ProductionAlphaV2Kernel(
        AlphaV2Coordinator(journal, planner, executor, AlphaV2PolicyKernel())
    )
    request = AlphaPlanningRequest(
        goal=generated.goal,
        classification=DataClassification.PRIVATE,
        locality=LocalityPolicy.REMOTE_ALLOWED,
        budget=GatewayBudget(32_000, 4_096, 30_000, 0),
        estimated_input_tokens=1_000,
        correlation_id="run-integration",
        run_id="run-integration",
    )

    state = kernel.process(request, actor="integration-worker")
    rehydrated = EventBackedAlphaV2RunJournal(
        EventStore(database),
        CheckpointStore(database),
    ).rehydrate("run-integration")

    assert state == rehydrated
    assert state.status is RunLifecycleStatus.SUCCEEDED
    task = state.task("repair")
    assert task.status is TaskLifecycleStatus.SUCCEEDED
    assert task.attempts == 2
    assert provider.calls == 2
    assert task.head_commit is not None
    assert (
        _git_text(repository, executables["git"], "show", f"{task.head_commit}:value.txt")
        == "fixed"
    )
    assert (repository / "value.txt").read_text(encoding="utf-8") == "broken\n"

    events = journal.events("run-integration")
    policies = [
        item.stream_sequence for item in events if item.event_type == ALPHA_V2_POLICY_DECIDED
    ]
    starts = [item.stream_sequence for item in events if item.event_type == ALPHA_V2_TASK_STARTED]
    assert len(policies) == len(starts) == 2
    assert all(left < right for left, right in zip(policies, starts, strict=True))
    verified = [item for item in events if item.event_type == ALPHA_V2_TASK_VERIFIED]
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
    assert replay.artifact_integrity == "verified"
    assert replay.artifacts
    assert not replay.findings

    corrupted = replay.artifacts[0]
    artifacts.path_for(corrupted.digest).write_bytes(b"corrupted-after-verification")
    corrupted_replay = runtime.replay_run("run-integration")
    assert corrupted_replay.artifact_integrity == "failed"
    assert any(
        finding.code == "alpha-replay-artifact-integrity-failed"
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
