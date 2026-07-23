from __future__ import annotations

import json
import shutil
import signal
import stat
from collections.abc import Callable, Iterable
from dataclasses import replace
from pathlib import Path
from threading import Event
from types import FrameType
from typing import Any, Literal, cast

from blackcell.bootstrap.alpha_review_process import AlphaReviewWorkerProcess
from blackcell.bootstrap.alpha_review_runtime import AlphaReviewReconciliationReport
from blackcell.bootstrap.alpha_review_worker import (
    AlphaReviewWorker,
    AlphaReviewWorkerCycleResult,
)
from blackcell.bootstrap.process import main
from blackcell.config import (
    ALPHA_REVIEW_CONFIG_FILE_ENV,
    ALPHA_REVIEW_CONFIG_SCHEMA,
    API_TOKEN_ENV,
    DATA_DIR_ENV,
    REPOSITORY_ROOT_ENV,
    RuntimeProcessConfig,
)

TOKEN = "Alpha-review_process-token.0123456789-ABCDEFG"
type ReviewCycleStatus = Literal[
    "idle",
    "review-succeeded",
    "review-failed",
    "claim-conflict",
]


class RecordingCoordinator:
    def __init__(self, statuses: Iterable[ReviewCycleStatus]) -> None:
        self.statuses = iter(statuses)
        self.calls = 0

    def run_once(self) -> AlphaReviewWorkerCycleResult:
        self.calls += 1
        return AlphaReviewWorkerCycleResult(status=next(self.statuses))


class RecordingScheduler:
    def __init__(self, order: list[str]) -> None:
        self.order = order

    def reconcile(self, *, principal_id: str) -> AlphaReviewReconciliationReport:
        self.order.append(f"reconcile:{principal_id}")
        return AlphaReviewReconciliationReport((), ())


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


def test_alpha_review_process_reconciles_and_runs_once_against_shared_storage(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)

    process = AlphaReviewWorkerProcess.from_config(config, environment={})

    assert process.serve(once=True) == 3
    assert config.security.paths.database_path.is_file()
    assert stat.S_IMODE(config.security.paths.database_path.stat().st_mode) == 0o600
    assert config.security.paths.artifact_root.is_dir()
    coordinator = cast("AlphaReviewWorker", process.coordinator)
    assert coordinator.policy.worker_id == "alpha-reviewer.test"
    assert coordinator.policy.lease_seconds == 300
    assert coordinator.policy.budget.max_input_tokens == 64_000
    assert not hasattr(coordinator.execution, "claim_node")
    assert not hasattr(coordinator.execution, "maintain_successful_worktrees")
    assert not hasattr(coordinator.scheduler, "reconcile")
    assert not hasattr(coordinator.artifacts, "get_bytes")


def test_alpha_review_process_validates_provider_environment_before_storage(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    assert config.alpha_review_worker is not None
    configured = replace(
        config,
        alpha_review_worker=replace(
            config.alpha_review_worker,
            provider=replace(
                config.alpha_review_worker.provider,
                environment_variables=("OPENAI_API_KEY",),
            ),
        ),
    )

    try:
        AlphaReviewWorkerProcess.from_config(configured, environment={})
    except ValueError as error:
        assert str(error) == "alpha review provider environment is incomplete"
    else:
        raise AssertionError("missing provider environment was accepted")

    assert not config.security.paths.database_path.exists()


def test_alpha_review_process_rejects_invalid_deadline_relationship_before_storage(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    assert config.alpha_review_worker is not None
    invalid = replace(
        config,
        alpha_review_worker=replace(
            config.alpha_review_worker,
            worker=replace(config.alpha_review_worker.worker, lease_seconds=180),
        ),
    )

    try:
        AlphaReviewWorkerProcess.from_config(invalid, environment={})
    except ValueError as error:
        assert str(error) == "invalid alpha review worker policy"
    else:
        raise AssertionError("review lease accepted the complete provider deadline")

    assert not config.security.paths.database_path.exists()


def test_alpha_review_process_loop_uses_review_polling_policy(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert config.alpha_review_worker is not None
    order: list[str] = []
    coordinator = RecordingCoordinator(("review-succeeded", "idle"))
    stop_event = StopAfterWait(order)
    process = AlphaReviewWorkerProcess(
        coordinator,
        RecordingScheduler(order),
        config,
        stop_event,
        FixedQuota(True),
    )

    assert process.serve() == 0
    assert coordinator.calls == 2
    assert order == [
        f"reconcile:{config.alpha_review_worker.worker.supervisor_id}",
        "wait",
    ]
    assert stop_event.timeouts == [config.alpha_review_worker.worker.poll_milliseconds / 1_000]

    blocked = RecordingCoordinator(("review-succeeded",))
    blocked_process = AlphaReviewWorkerProcess(
        blocked,
        RecordingScheduler([]),
        config,
        Event(),
        FixedQuota(False),
    )
    assert blocked_process.serve(once=True) == 3
    assert blocked.calls == 0


def test_alpha_review_process_entrypoint_restores_signal_handlers_and_fails_closed(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    installed: list[tuple[signal.Signals, object]] = []
    previous = {signal.SIGINT: object(), signal.SIGTERM: object()}

    def fake_getsignal(kind: signal.Signals) -> object:
        return previous[kind]

    def fake_signal(kind: signal.Signals, handler: object) -> None:
        installed.append((kind, handler))

    monkeypatch.setattr("blackcell.bootstrap.process.signal.getsignal", fake_getsignal)
    monkeypatch.setattr("blackcell.bootstrap.process.signal.signal", fake_signal)

    unconfigured = replace(_config(tmp_path), alpha_review_worker=None)
    monkeypatch.setattr(
        "blackcell.bootstrap.process.RuntimeProcessConfig.from_environment",
        lambda: unconfigured,
    )
    assert main(("alpha-review-worker", "--once")) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == '{"error": {"code": "alpha-review-worker-not-configured"}}\n'
    assert installed[-2:] == [
        (signal.SIGINT, previous[signal.SIGINT]),
        (signal.SIGTERM, previous[signal.SIGTERM]),
    ]

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

    installed.clear()
    monkeypatch.setattr(
        "blackcell.bootstrap.process.RuntimeProcessConfig.from_environment",
        lambda: object(),
    )
    monkeypatch.setattr(
        "blackcell.bootstrap.process.AlphaReviewWorkerProcess.from_config",
        fake_from_config,
    )
    assert main(("alpha-review-worker", "--once")) == 3
    assert installed[-2:] == [
        (signal.SIGINT, previous[signal.SIGINT]),
        (signal.SIGTERM, previous[signal.SIGTERM]),
    ]


def _config(tmp_path: Path) -> RuntimeProcessConfig:
    repository = tmp_path / "repository"
    repository.mkdir()
    source = tmp_path / "alpha-review.json"
    source.write_text(
        json.dumps(
            {
                "schema_version": ALPHA_REVIEW_CONFIG_SCHEMA,
                "provider": {
                    "profile_id": "alpha-review",
                    "model_id": "gpt-review",
                    "codex_executable": str(_executable("true")),
                    "git_executable": str(_executable("git")),
                    "classification": "private",
                    "locality": "remote-allowed",
                    "max_input_tokens": 64_000,
                    "max_output_tokens": 8_192,
                    "max_cost_microusd": 0,
                    "timeout_ceiling_seconds": 180,
                    "environment_variables": [],
                },
                "worker": {
                    "worker_id": "alpha-reviewer.test",
                    "supervisor_id": "alpha-review-supervisor.test",
                    "lease_seconds": 300,
                    "poll_milliseconds": 125,
                },
            }
        ),
        encoding="utf-8",
    )
    source.chmod(0o600)
    return RuntimeProcessConfig.from_environment(
        {
            DATA_DIR_ENV: str(tmp_path / "data"),
            API_TOKEN_ENV: TOKEN,
            REPOSITORY_ROOT_ENV: str(repository),
            ALPHA_REVIEW_CONFIG_FILE_ENV: str(source),
        }
    )


def _executable(name: str) -> Path:
    value = shutil.which(name)
    assert value is not None
    return Path(value).resolve(strict=True)
