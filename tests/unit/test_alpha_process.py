from __future__ import annotations

import json
import shutil
import signal
import stat
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import replace
from pathlib import Path
from threading import Event
from types import FrameType
from typing import Any, Literal, cast

import pytest

import blackcell.bootstrap.alpha_process as alpha_process_module
from blackcell.adapters.models import CodexCliModelAdapter
from blackcell.bootstrap.alpha_process import (
    AlphaWorkerProcess,
    AlphaWorkerProcessError,
    AlphaWorkerProcessFailureCode,
)
from blackcell.bootstrap.alpha_runtime import (
    AlphaRuntimeApiService,
    AlphaWorktreeMaintenanceReport,
)
from blackcell.bootstrap.alpha_worker import AlphaRuntimeWorker, AlphaWorkerCycleResult
from blackcell.bootstrap.process import main
from blackcell.bootstrap.runtime_api import RuntimeApiService
from blackcell.bootstrap.worker_process_lock import WorkerProcessRole, worker_process_lock
from blackcell.config import (
    ALPHA_WORKER_CONFIG_FILE_ENV,
    ALPHA_WORKER_CONFIG_SCHEMA,
    API_TOKEN_ENV,
    DATA_DIR_ENV,
    REPOSITORY_ROOT_ENV,
    WORKER_ID_ENV,
    RuntimeProcessConfig,
)
from blackcell.interfaces.http import (
    AlphaAcceptanceCheck,
    AlphaIntentRequest,
    AlphaNodeBudget,
    AlphaPlanNode,
    AlphaPlanRequest,
    AlphaProjectRequest,
    AlphaRunRequest,
)
from blackcell.kernel import ArtifactStore, EventStore
from blackcell.orchestration.alpha_changes import (
    MAX_ALPHA_CHANGE_CONTEXT_BYTES,
    MAX_ALPHA_CHANGE_PROPOSAL_BYTES,
)

TOKEN = "Alpha-worker_process-token.0123456789-ABCDEFG"
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

    def run_once(self) -> AlphaWorkerCycleResult:
        self.calls += 1
        return AlphaWorkerCycleResult(status=next(self.statuses))


class RecordingRuntime:
    def __init__(self, order: list[str]) -> None:
        self.order = order

    def reconcile_startup(self, *, principal_id: str) -> tuple[object, ...]:
        self.order.append(f"reconcile:{principal_id}")
        return ()

    def maintain_successful_worktrees(
        self,
        *,
        max_retained: int,
        principal_id: str,
    ) -> AlphaWorktreeMaintenanceReport:
        self.order.append(f"maintain:{principal_id}:{max_retained}")
        return AlphaWorktreeMaintenanceReport(0, 0, 0, 0, 0, True)


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


def test_alpha_process_reconciles_then_runs_once_against_shared_storage(tmp_path: Path) -> None:
    config = _config(tmp_path)

    process = AlphaWorkerProcess.from_config(config)

    assert process.serve(once=True) == 3
    assert config.security.paths.database_path.is_file()
    assert stat.S_IMODE(config.security.paths.database_path.stat().st_mode) == 0o600
    assert config.security.paths.artifact_root.is_dir()


def test_alpha_process_composes_codex_caps_from_change_wire_contracts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def recording_adapter(**kwargs: Any) -> CodexCliModelAdapter:
        captured.update(kwargs)
        return CodexCliModelAdapter(**kwargs)

    monkeypatch.setattr(alpha_process_module, "CodexCliModelAdapter", recording_adapter)

    process = AlphaWorkerProcess.from_config(_config(tmp_path), environment={})

    assert captured["max_input_bytes"] == MAX_ALPHA_CHANGE_CONTEXT_BYTES + 1024 * 1024
    assert captured["max_response_bytes"] == MAX_ALPHA_CHANGE_PROPOSAL_BYTES + 1024 * 1024
    assert (
        captured["max_stdout_bytes"]
        == 2 * (MAX_ALPHA_CHANGE_PROPOSAL_BYTES + 1024 * 1024) + 1024 * 1024
    )
    coordinator = cast("AlphaRuntimeWorker", process.coordinator)
    assert coordinator.evidence.lifecycle is coordinator.worktrees


def test_alpha_process_dispatches_check_only_run_through_real_composition(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    assert config.alpha_worker is not None
    database = config.security.paths.ensure_database_file()
    events = EventStore(database)
    runtime = AlphaRuntimeApiService(
        events,
        config.repository_root,
        isolation_root=config.alpha_worker.isolation.root,
    )
    base_commit = _git_text(config.repository_root, "rev-parse", "HEAD")
    _submit_check_only(runtime, config.repository_root, base_commit)
    process = AlphaWorkerProcess.from_config(config, environment={})

    assert process.serve(once=True) == 0
    assert runtime.inspect_run("run-1").status == "succeeded"
    succeeded = next(
        event
        for event in events.read_stream("alpha:run:run-1")
        if event.event_type == "alpha.node.succeeded"
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
    replay = cast("AlphaRuntimeApiService", process.runtime).replay_run("run-1")
    assert replay.artifact_integrity == "verified"
    assert replay.findings == ()
    api = RuntimeApiService.from_config(
        config.security,
        repository_root=config.repository_root,
        artifact_max_total_bytes=config.quota.artifact_max_total_bytes,
        alpha_isolation_root=config.alpha_worker.isolation.root,
    )
    assert api.replay_alpha_run("run-1") == replay


def test_alpha_process_enforces_successful_worktree_retention(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert config.alpha_worker is not None
    alpha = replace(
        config.alpha_worker,
        worker=replace(
            config.alpha_worker.worker,
            max_retained_successful_worktrees=0,
        ),
    )
    configured = replace(config, alpha_worker=alpha)
    database = configured.security.paths.ensure_database_file()
    events = EventStore(database)
    runtime = AlphaRuntimeApiService(
        events,
        configured.repository_root,
        isolation_root=alpha.isolation.root,
    )
    base_commit = _git_text(configured.repository_root, "rev-parse", "HEAD")
    _submit_check_only(runtime, configured.repository_root, base_commit)

    process = AlphaWorkerProcess.from_config(configured, environment={})
    assert process.serve(once=True) == 0

    run = runtime.inspect_run("run-1")
    assert run.status == "succeeded"
    assert not run.retained_worktree
    event_types = tuple(event.event_type for event in events.read_stream("alpha:run:run-1"))
    assert event_types[-2:] == (
        "alpha.node.worktree-cleanup-requested",
        "alpha.node.worktree-cleaned",
    )


def test_alpha_process_rejects_missing_provider_environment_before_storage(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    assert config.alpha_worker is not None
    provider = replace(
        config.alpha_worker.provider,
        environment_variables=("OPENAI_API_KEY",),
    )
    configured = replace(
        config,
        alpha_worker=replace(config.alpha_worker, provider=provider),
    )

    with pytest.raises(ValueError, match="provider environment is incomplete"):
        AlphaWorkerProcess.from_config(configured, environment={})

    assert not config.security.paths.database_path.exists()


def test_alpha_process_loop_polls_only_when_idle_or_conflicted(tmp_path: Path) -> None:
    config = _config(tmp_path)
    for blocking_status in ("idle", "claim-conflict"):
        order: list[str] = []
        runtime = RecordingRuntime(order)
        stop_event = StopAfterWait(order)
        coordinator = RecordingCoordinator(("node-succeeded", blocking_status))
        process = AlphaWorkerProcess(
            coordinator,
            runtime,
            config,
            stop_event,
            FixedQuota(True),
        )

        assert process.serve() == 0
        assert coordinator.calls == 2
        assert config.alpha_worker is not None
        worker = config.alpha_worker.worker
        assert order == [
            f"reconcile:{worker.worker_id}",
            f"maintain:{worker.worker_id}:{worker.max_retained_successful_worktrees}",
            f"maintain:{worker.worker_id}:{worker.max_retained_successful_worktrees}",
            "wait",
        ]
        assert stop_event.timeouts == [config.worker_poll_milliseconds / 1_000]

    blocked = RecordingCoordinator(("node-succeeded",))
    blocked_process = AlphaWorkerProcess(
        blocked,
        RecordingRuntime([]),
        config,
        Event(),
        FixedQuota(False),
    )
    assert blocked_process.serve(once=True) == 3
    assert blocked.calls == 0


def test_alpha_process_requires_exclusive_role_ownership_before_reconciliation(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    order: list[str] = []
    coordinator = RecordingCoordinator(("idle",))
    process = AlphaWorkerProcess(coordinator, RecordingRuntime(order), config)

    with (
        worker_process_lock(config.security.paths, WorkerProcessRole.ALPHA_EXECUTION),
        worker_process_lock(config.security.paths, WorkerProcessRole.ALPHA_REVIEW),
        worker_process_lock(config.security.paths, WorkerProcessRole.ALPHA_VERIFICATION),
        pytest.raises(AlphaWorkerProcessError) as duplicate,
    ):
        process.serve(once=True)

    assert duplicate.value.code is AlphaWorkerProcessFailureCode.ALREADY_RUNNING
    assert order == []
    assert coordinator.calls == 0
    for role in WorkerProcessRole:
        lock_path = config.security.paths.data_root / f".{role.value}.lock"
        assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600


def test_alpha_process_entrypoint_restores_signal_handlers(monkeypatch: Any) -> None:
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
        "blackcell.bootstrap.process.AlphaWorkerProcess.from_config",
        fake_from_config,
    )

    assert main(("alpha-worker", "--once")) == 3
    assert installed[-2:] == [
        (signal.SIGINT, previous[signal.SIGINT]),
        (signal.SIGTERM, previous[signal.SIGTERM]),
    ]


def test_alpha_process_entrypoint_fails_closed_when_unconfigured(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    config = replace(_config(tmp_path), alpha_worker=None)
    monkeypatch.setattr(
        "blackcell.bootstrap.process.RuntimeProcessConfig.from_environment",
        lambda: config,
    )

    assert main(("alpha-worker", "--once")) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == '{"error": {"code": "alpha-worker-not-configured"}}\n'


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
    isolation_root = data_root / "alpha-worktrees"
    isolation_root.mkdir(mode=0o700)
    isolation_root.chmod(0o700)
    true = _executable("true")
    source = tmp_path / "alpha-worker.json"
    source.write_text(
        json.dumps(
            {
                "schema_version": ALPHA_WORKER_CONFIG_SCHEMA,
                "provider": {
                    "profile_id": "alpha-code",
                    "model_id": "gpt-alpha",
                    "codex_executable": str(true),
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
                    "worker_id": "alpha-worker.test",
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
            WORKER_ID_ENV: "alpha-worker:test",
            ALPHA_WORKER_CONFIG_FILE_ENV: str(source),
        }
    )


def _executable(name: str) -> Path:
    value = shutil.which(name)
    assert value is not None
    return Path(value).resolve(strict=True)


def _submit_check_only(
    runtime: AlphaRuntimeApiService,
    repository: Path,
    base_commit: str,
) -> None:
    runtime.register_project(
        AlphaProjectRequest(
            schema_version="alpha-project-request/v1",
            project_id="project-1",
            root=str(repository),
            configuration_provider="kernform",
            configuration_version="0.1.0",
            configuration_digest=CONFIGURATION_DIGEST,
            idempotency_key="project-1",
        ),
        principal_id="operator",
    )
    runtime.accept_intent(
        AlphaIntentRequest(
            schema_version="alpha-intent-request/v1",
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
    node = AlphaPlanNode(
        node_id="verify",
        objective="Run the configured no-op acceptance command.",
        depends_on=(),
        budget=AlphaNodeBudget(0, 0, 10, 0, 0),
        effects=("repository-read", "process"),
        allowed_paths=(),
        checks=(AlphaAcceptanceCheck("true-check", ("true",)),),
    )
    runtime.accept_plan(
        AlphaPlanRequest(
            schema_version="alpha-plan-request/v1",
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
        AlphaRunRequest(
            schema_version="alpha-run-request/v1",
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
