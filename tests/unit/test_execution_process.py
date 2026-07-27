from __future__ import annotations

import json
import shutil
import signal
import stat
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Event
from types import FrameType
from typing import Any, Literal, cast

import pytest

import blackcell.bootstrap.execution_process as execution_process_module
from blackcell.adapters.models import AgyCliModelAdapter, CodexCliModelAdapter
from blackcell.bootstrap.execution_process import (
    CHANGE_AGY_MAX_INPUT_BYTES,
    CHANGE_AGY_MAX_STDOUT_BYTES,
    ExecutionWorkerProcess,
    ExecutionWorkerProcessError,
    ExecutionWorkerProcessFailureCode,
)
from blackcell.bootstrap.execution_worker import ExecutionWorker, ExecutionWorkerCycleResult
from blackcell.bootstrap.process import main
from blackcell.bootstrap.runtime_service import (
    GeneratedRun,
    RuntimeService,
    WorktreeMaintenanceReport,
)
from blackcell.bootstrap.worker_process_lock import WorkerProcessRole, worker_process_lock
from blackcell.config import (
    API_TOKEN_ENV,
    DATA_DIR_ENV,
    EXECUTION_CONFIG_FILE_ENV,
    EXECUTION_CONFIG_SCHEMA,
    REPOSITORY_ROOT_ENV,
    ExecutionProviderAdapter,
    RuntimeProcessConfig,
)
from blackcell.gateway import GatewayBudget
from blackcell.interfaces.http import (
    AcceptanceCheck,
    IntentRequest,
    NodeBudget,
    PlanNode,
    PlanRequest,
    ProjectRequest,
    RunRequest,
)
from blackcell.kernel import ArtifactStore, EventStore
from blackcell.orchestration.changes import (
    MAX_CHANGE_CONTEXT_BYTES,
    MAX_CHANGE_PROPOSAL_BYTES,
)
from blackcell.orchestration.execution_plan import (
    ExecutionAuthority,
    GoalSpec,
    PlanningRequest,
    RunLifecycleStatus,
    VerificationCheck,
)

TOKEN = "Runtime-worker_process-token.0123456789-ABCDEFG"
CONFIGURATION_DIGEST = "sha256:" + "a" * 64
type CycleStatus = Literal[
    "idle",
    "node-succeeded",
    "node-failed",
    "node-canceled",
    "claim-conflict",
]


class RecordingCoordinator:
    def __init__(self, statuses: Iterable[CycleStatus]) -> None:
        self.statuses = iter(statuses)
        self.calls = 0

    def run_once(self) -> ExecutionWorkerCycleResult:
        self.calls += 1
        return ExecutionWorkerCycleResult(status=next(self.statuses))


class RecordingRuntime:
    def __init__(self, order: list[str]) -> None:
        self.order = order

    def next_generated_run(self) -> GeneratedRun | None:
        return None

    def generated_execution_authority(self, run_id: str) -> ExecutionAuthority:
        del run_id
        raise AssertionError("no generated execution authority is available")

    def generated_execution_goal(self, run_id: str) -> GoalSpec:
        del run_id
        raise AssertionError("no generated execution goal is available")

    def should_cancel_generated_run(self, run_id: str) -> bool:
        return False

    def reconcile_startup(self, *, principal_id: str) -> tuple[object, ...]:
        self.order.append(f"reconcile:{principal_id}")
        return ()

    def maintain_successful_worktrees(
        self,
        *,
        max_retained: int,
        principal_id: str,
    ) -> WorktreeMaintenanceReport:
        self.order.append(f"maintain:{principal_id}:{max_retained}")
        return WorktreeMaintenanceReport(0, 0, 0, 0, 0, True)


class StopAfterWait(Event):
    def __init__(self, order: list[str]) -> None:
        super().__init__()
        self.order = order
        self.timeouts: list[float | None] = []

    def wait(self, timeout: float | None = None) -> bool:
        self.order.append("wait")
        self.timeouts.append(timeout)
        self.set()
        return True


class FixedQuota:
    def __init__(self, available: bool) -> None:
        self.available = available

    def has_mutation_capacity(self) -> bool:
        return self.available


@dataclass
class CapturingGeneratedExecution:
    request: PlanningRequest | None = None

    def process(self, request: PlanningRequest, *, actor: str) -> object:
        assert actor == "execution-worker.test"
        self.request = request
        return type("GeneratedState", (), {"status": RunLifecycleStatus.SUCCEEDED})()


class OneGeneratedRuntime(RecordingRuntime):
    def __init__(self, generated: GeneratedRun) -> None:
        super().__init__([])
        self.generated = generated

    def next_generated_run(self) -> GeneratedRun | None:
        generated, self.generated = self.generated, None
        return generated


def test_execution_process_reconciles_then_runs_once_against_shared_storage(tmp_path: Path) -> None:
    config = _config(tmp_path)

    process = ExecutionWorkerProcess.from_config(config)

    assert process.serve(once=True) == 3
    assert config.security.paths.database_path.is_file()
    assert stat.S_IMODE(config.security.paths.database_path.stat().st_mode) == 0o600
    assert config.security.paths.artifact_root.is_dir()


def test_generated_execution_intersects_admitted_and_worker_budgets(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert config.execution_worker is not None
    goal = GoalSpec(
        goal_id="goal-bounded",
        project_id="project-bounded",
        intent_id="intent-bounded",
        objective="Exercise admitted generated-run bounds.",
        base_commit="a" * 40,
        constraints=(),
        allowed_paths=("value.txt",),
        verification_checks=(VerificationCheck("check", ("true",)),),
    )
    generated = GeneratedRun(
        "run-bounded",
        goal,
        ExecutionAuthority(
            budget=GatewayBudget(1_000, 500, 3_000, 0),
            check_timeout_seconds=3,
            max_changed_paths=1,
        ),
    )
    execution = CapturingGeneratedExecution()
    process = ExecutionWorkerProcess(
        RecordingCoordinator(("idle",)),
        OneGeneratedRuntime(generated),
        config,
        execution=cast("Any", execution),
    )

    result = process._run_generated_once(
        config.execution_worker,
        actor="execution-worker.test",
    )

    assert result is not None
    assert result.status == "node-succeeded"
    assert execution.request is not None
    assert execution.request.goal == goal
    assert execution.request.budget == generated.authority.budget


def test_execution_process_composes_codex_caps_from_change_wire_contracts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def recording_adapter(**kwargs: Any) -> CodexCliModelAdapter:
        captured.update(kwargs)
        return CodexCliModelAdapter(**kwargs)

    monkeypatch.setattr(execution_process_module, "CodexCliModelAdapter", recording_adapter)

    process = ExecutionWorkerProcess.from_config(_config(tmp_path), environment={})

    assert captured["max_input_bytes"] == MAX_CHANGE_CONTEXT_BYTES + 1024 * 1024
    assert captured["max_response_bytes"] == MAX_CHANGE_PROPOSAL_BYTES + 1024 * 1024
    assert (
        captured["max_stdout_bytes"] == 2 * (MAX_CHANGE_PROPOSAL_BYTES + 1024 * 1024) + 1024 * 1024
    )
    coordinator = cast("ExecutionWorker", process.coordinator)
    assert coordinator.evidence.lifecycle is coordinator.worktrees


def test_execution_process_composes_agy_as_active_change_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def recording_adapter(**kwargs: Any) -> AgyCliModelAdapter:
        captured.update(kwargs)
        return AgyCliModelAdapter(**kwargs)

    monkeypatch.setattr(execution_process_module, "AgyCliModelAdapter", recording_adapter)
    config = _config(tmp_path)
    assert config.execution_worker is not None
    provider = replace(
        config.execution_worker.provider,
        adapter=ExecutionProviderAdapter.AGY_CLI,
        model_id="gemini-execution",
        effort="high",
    )
    execution = replace(config.execution_worker, provider=provider)

    process = ExecutionWorkerProcess.from_config(
        replace(config, execution_worker=execution),
        environment={},
    )

    assert "auth_token_path" not in captured
    assert captured["effort"] == "high"
    assert captured["max_input_bytes"] == CHANGE_AGY_MAX_INPUT_BYTES
    assert captured["max_stdout_bytes"] == CHANGE_AGY_MAX_STDOUT_BYTES
    coordinator = cast("ExecutionWorker", process.coordinator)
    assert coordinator.evidence.lifecycle is coordinator.worktrees


def test_execution_process_dispatches_check_only_run_through_real_composition(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    assert config.execution_worker is not None
    database = config.security.paths.ensure_database_file()
    events = EventStore(database)
    runtime = RuntimeService(
        events,
        config.repository_root,
        isolation_root=config.execution_worker.isolation.root,
    )
    base_commit = _git_text(config.repository_root, "rev-parse", "HEAD")
    _submit_check_only(runtime, config.repository_root, base_commit)
    process = ExecutionWorkerProcess.from_config(config, environment={})

    assert process.serve(once=True) == 0
    assert runtime.inspect_run("run-1").status == "succeeded"
    succeeded = next(
        event for event in events.read_stream("run:run-1") if event.event_type == "node.succeeded"
    )
    outcome_digest = cast("str", succeeded.payload["result_digest"])
    artifacts = ArtifactStore(
        config.security.paths.artifact_root,
        database_path=database,
        max_total_bytes=config.quota.artifact_max_total_bytes,
    )
    outcome = cast("dict[str, object]", artifacts.get_json(outcome_digest))
    assert outcome["status"] == "succeeded"
    assert outcome["context_artifact"] is None
    assert outcome["proposal_artifact"] is None
    assert outcome["effect_artifact"] is None
    assert len(cast("list[object]", outcome["checks"])) == 1
    replay = cast("RuntimeService", process.runtime).replay_run("run-1")
    assert replay.artifact_integrity == "verified"
    assert replay.findings == ()
    api = RuntimeService.from_config(
        config.security,
        repository_root=config.repository_root,
        artifact_max_total_bytes=config.quota.artifact_max_total_bytes,
        isolation_root=config.execution_worker.isolation.root,
    )
    assert api.replay_run("run-1") == replay


def test_execution_process_enforces_successful_worktree_retention(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert config.execution_worker is not None
    execution = replace(
        config.execution_worker,
        worker=replace(
            config.execution_worker.worker,
            max_retained_successful_worktrees=0,
        ),
    )
    configured = replace(config, execution_worker=execution)
    database = configured.security.paths.ensure_database_file()
    events = EventStore(database)
    runtime = RuntimeService(
        events,
        configured.repository_root,
        isolation_root=execution.isolation.root,
    )
    base_commit = _git_text(configured.repository_root, "rev-parse", "HEAD")
    _submit_check_only(runtime, configured.repository_root, base_commit)

    process = ExecutionWorkerProcess.from_config(configured, environment={})
    assert process.serve(once=True) == 0

    run = runtime.inspect_run("run-1")
    assert run.status == "succeeded"
    assert not run.retained_worktree
    event_types = tuple(event.event_type for event in events.read_stream("run:run-1"))
    assert event_types[-2:] == (
        "node.worktree-cleanup-requested",
        "node.worktree-cleaned",
    )


def test_execution_process_rejects_missing_provider_environment_before_storage(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    assert config.execution_worker is not None
    provider = replace(
        config.execution_worker.provider,
        environment_variables=("OPENAI_API_KEY",),
    )
    configured = replace(
        config,
        execution_worker=replace(config.execution_worker, provider=provider),
    )

    with pytest.raises(ValueError, match="provider environment is incomplete"):
        ExecutionWorkerProcess.from_config(configured, environment={})

    assert not config.security.paths.database_path.exists()


def test_execution_process_loop_polls_only_when_idle_or_conflicted(tmp_path: Path) -> None:
    config = _config(tmp_path)
    for blocking_status in ("idle", "claim-conflict"):
        order: list[str] = []
        runtime = RecordingRuntime(order)
        stop_event = StopAfterWait(order)
        coordinator = RecordingCoordinator(("node-succeeded", blocking_status))
        process = ExecutionWorkerProcess(
            coordinator,
            runtime,
            config,
            stop_event,
            FixedQuota(True),
        )

        assert process.serve() == 0
        assert coordinator.calls == 2
        assert config.execution_worker is not None
        worker = config.execution_worker.worker
        assert order == [
            f"reconcile:{worker.worker_id}",
            f"maintain:{worker.worker_id}:{worker.max_retained_successful_worktrees}",
            f"maintain:{worker.worker_id}:{worker.max_retained_successful_worktrees}",
            "wait",
        ]
        assert stop_event.timeouts == [0.25]

    blocked = RecordingCoordinator(("node-succeeded",))
    blocked_process = ExecutionWorkerProcess(
        blocked,
        RecordingRuntime([]),
        config,
        Event(),
        FixedQuota(False),
    )
    assert blocked_process.serve(once=True) == 3
    assert blocked.calls == 0


def test_execution_process_requires_exclusive_role_ownership_before_reconciliation(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    order: list[str] = []
    coordinator = RecordingCoordinator(("idle",))
    process = ExecutionWorkerProcess(coordinator, RecordingRuntime(order), config)

    with (
        worker_process_lock(config.security.paths, WorkerProcessRole.EXECUTION),
        worker_process_lock(config.security.paths, WorkerProcessRole.REVIEW),
        worker_process_lock(config.security.paths, WorkerProcessRole.VERIFICATION),
        pytest.raises(ExecutionWorkerProcessError) as duplicate,
    ):
        process.serve(once=True)

    assert duplicate.value.code is ExecutionWorkerProcessFailureCode.ALREADY_RUNNING
    assert order == []
    assert coordinator.calls == 0
    for role in WorkerProcessRole:
        lock_path = config.security.paths.data_root / f".{role.value}.lock"
        assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600


def test_execution_process_entrypoint_restores_signal_handlers(monkeypatch: Any) -> None:
    installed: list[tuple[signal.Signals, object]] = []
    previous = {signal.SIGINT: object(), signal.SIGTERM: object()}

    def fake_getsignal(kind: signal.Signals) -> object:
        return previous[kind]

    def fake_signal(kind: signal.Signals, handler: object) -> None:
        installed.append((kind, handler))

    class FakeWorker:
        def serve(self, *, once: bool = False) -> int:
            assert once
            return 3

    def fake_from_config(
        config: object,
        *,
        stop_event: Event,
    ) -> FakeWorker:
        del config
        handler = cast(
            "Callable[[int, FrameType | None], object]",
            next(handler for kind, handler in installed if kind is signal.SIGTERM),
        )
        handler(signal.SIGTERM, None)
        assert stop_event.is_set()
        return FakeWorker()

    monkeypatch.setattr("blackcell.bootstrap.process.signal.getsignal", fake_getsignal)
    monkeypatch.setattr("blackcell.bootstrap.process.signal.signal", fake_signal)
    monkeypatch.setattr(
        "blackcell.bootstrap.process.RuntimeProcessConfig.from_environment",
        lambda: object(),
    )
    monkeypatch.setattr(
        "blackcell.bootstrap.process.ExecutionWorkerProcess.from_config",
        fake_from_config,
    )

    assert main(("execution-worker", "--once")) == 3
    assert installed[-2:] == [
        (signal.SIGINT, previous[signal.SIGINT]),
        (signal.SIGTERM, previous[signal.SIGTERM]),
    ]


def test_execution_process_entrypoint_fails_closed_when_unconfigured(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    config = replace(_config(tmp_path), execution_worker=None)
    monkeypatch.setattr(
        "blackcell.bootstrap.process.RuntimeProcessConfig.from_environment",
        lambda: config,
    )

    assert main(("execution-worker", "--once")) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == '{"error": {"code": "execution-worker-not-configured"}}\n'


def _config(tmp_path: Path) -> RuntimeProcessConfig:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(
        (_executable("git"), "init", "--quiet"),
        cwd=repository,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        (_executable("git"), "config", "user.name", "BlackCell Test"),
        cwd=repository,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        (_executable("git"), "config", "user.email", "blackcell@example.invalid"),
        cwd=repository,
        check=True,
        capture_output=True,
    )
    (repository / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(
        (_executable("git"), "add", "README.md"),
        cwd=repository,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        (_executable("git"), "commit", "-m", "initial"),
        cwd=repository,
        check=True,
        capture_output=True,
    )
    data_root = tmp_path / "data"
    data_root.mkdir(mode=0o700)
    data_root.chmod(0o700)
    isolation_root = data_root / "execution-worktrees"
    isolation_root.mkdir(mode=0o700)
    isolation_root.chmod(0o700)
    true = _executable("true")
    source = tmp_path / "execution-worker.json"
    source.write_text(
        json.dumps(
            {
                "schema_version": EXECUTION_CONFIG_SCHEMA,
                "provider": {
                    "adapter": "codex-cli",
                    "profile_id": "execution",
                    "model_id": "gpt-execution",
                    "executable": str(true),
                    "git_executable": str(_executable("git")),
                    "classification": "private",
                    "locality": "remote-allowed",
                    "max_input_tokens": 32_000,
                    "max_output_tokens": 4_096,
                    "max_cost_microusd": 0,
                    "timeout_ceiling_seconds": 120,
                    "environment_variables": [],
                },
                "isolation": {
                    "root": str(isolation_root),
                    "executables": {"true": str(true)},
                    "runtime_roots": [],
                    "bubblewrap_executable": str(_executable("bwrap")),
                    "prlimit_executable": str(_executable("prlimit")),
                    "probe_executable": str(true),
                    "limits": {
                        "address_space_bytes": 1_073_741_824,
                        "cpu_seconds": 60,
                        "processes": 128,
                        "open_files": 128,
                        "file_size_bytes": 16_777_216,
                        "tmpfs_bytes": 67_108_864,
                    },
                },
                "worker": {
                    "worker_id": "execution-worker.test",
                    "stdout_limit_bytes": 65_536,
                    "stderr_limit_bytes": 32_768,
                    "lease_grace_seconds": 15,
                    "max_retained_successful_worktrees": 2,
                },
            }
        ),
        encoding="utf-8",
    )
    source.chmod(0o600)
    return RuntimeProcessConfig.from_environment(
        {
            DATA_DIR_ENV: str(data_root),
            API_TOKEN_ENV: TOKEN,
            REPOSITORY_ROOT_ENV: str(repository),
            EXECUTION_CONFIG_FILE_ENV: str(source),
        }
    )


def _executable(name: str) -> Path:
    value = shutil.which(name)
    assert value is not None
    return Path(value).resolve(strict=True)


def _submit_check_only(
    runtime: RuntimeService,
    repository: Path,
    base_commit: str,
) -> None:
    runtime.register_project(
        ProjectRequest(
            schema_version="project-request/v1",
            project_id="project-1",
            root=str(repository),
            configuration_provider="kernform",
            configuration_version="0.2.0",
            configuration_digest=CONFIGURATION_DIGEST,
            idempotency_key="project-1",
        ),
        principal_id="operator",
    )
    runtime.accept_intent(
        IntentRequest(
            schema_version="intent-request/v1",
            intent_id="intent-1",
            project_id="project-1",
            objective="Verify the admitted repository without mutation.",
            constraints=(),
            assumptions=(),
            unresolved_questions=(),
            idempotency_key="intent-1",
        ),
        principal_id="operator",
    )
    node = PlanNode(
        node_id="verify",
        objective="Run the configured no-op acceptance command.",
        depends_on=(),
        budget=NodeBudget(0, 0, 10, 0, 0),
        effects=("repository-read", "process"),
        allowed_paths=(),
        checks=(AcceptanceCheck("true-check", ("true",)),),
    )
    runtime.accept_plan(
        PlanRequest(
            schema_version="plan-request/v1",
            plan_id="plan-1",
            project_id="project-1",
            intent_id="intent-1",
            base_commit=base_commit,
            allowed_effects=("repository-read", "process"),
            nodes=(node,),
            idempotency_key="plan-1",
        ),
        principal_id="operator",
    )
    runtime.submit_run(
        RunRequest(
            schema_version="run-request/v1",
            run_id="run-1",
            project_id="project-1",
            intent_id="intent-1",
            plan_id="plan-1",
            idempotency_key="run-1",
        ),
        principal_id="operator",
    )


def _git_text(cwd: Path, *arguments: str) -> str:
    completed = subprocess.run(
        (_executable("git"), *arguments),
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()
