from __future__ import annotations

import signal
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Event
from types import FrameType
from typing import cast

import pytest

from blackcell.bootstrap.daemon import RuntimeDaemon
from blackcell.bootstrap.process import main
from blackcell.config import (
    API_TOKEN_ENV,
    DATA_DIR_ENV,
    REPOSITORY_ROOT_ENV,
    AlphaReviewWorkerRuntimeConfig,
    AlphaVerifyWorkerRuntimeConfig,
    AlphaWorkerRuntimeConfig,
    RuntimeProcessConfig,
)

TOKEN = "Alpha-daemon_test-token.0123456789-ABCDEFG"


class FakeProcess:
    def __init__(self, returncode: int | None = None, *, stale: bool = False) -> None:
        self.returncode = returncode
        self.stale = stale
        self.terminated = False
        self.killed = False
        self.wait_timeouts: list[float | None] = []

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        self.wait_timeouts.append(timeout)
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fake-runtime", timeout or 0)
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        if not self.stale:
            self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


@dataclass(frozen=True, slots=True)
class SpawnRecord:
    argv: tuple[str, ...]
    options: dict[str, object]


class FakeFactory:
    def __init__(self, *processes: FakeProcess | OSError) -> None:
        self.processes = list(processes)
        self.records: list[SpawnRecord] = []

    def __call__(self, argv: list[str], **options: object) -> FakeProcess:
        self.records.append(SpawnRecord(tuple(argv), dict(options)))
        process = self.processes.pop(0)
        if isinstance(process, OSError):
            raise process
        return process


def test_daemon_starts_api_only_without_alpha_configuration(
    tmp_path: Path,
) -> None:
    stop_event = Event()
    stop_event.set()
    api = FakeProcess()
    factory = FakeFactory(api)
    environment = {
        "BLACKCELL_DATA_DIR": str(tmp_path / "data"),
        "BLACKCELL_REPOSITORY_ROOT": str(tmp_path),
    }
    daemon = RuntimeDaemon(
        tmp_path,
        graceful_timeout_seconds=1,
        stop_event=stop_event,
        process_factory=factory,
        command_prefix=("python", "-m", "blackcell.bootstrap.process"),
        environment=environment,
    )

    assert daemon.serve() == 0
    assert [record.argv for record in factory.records] == [
        ("python", "-m", "blackcell.bootstrap.process", "api"),
    ]
    assert all(record.options["cwd"] == tmp_path for record in factory.records)
    assert all(record.options["env"] == environment for record in factory.records)
    assert all(record.options["shell"] is False for record in factory.records)
    assert api.terminated and not api.killed


def test_daemon_adds_alpha_worker_but_never_legacy_worker_when_configured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    configured = replace(
        config,
        alpha_worker=cast("AlphaWorkerRuntimeConfig", object()),
    )
    validated: list[RuntimeProcessConfig] = []

    def validate(
        value: RuntimeProcessConfig,
        *,
        environment: dict[str, str],
    ) -> None:
        assert environment == {}
        validated.append(value)

    monkeypatch.setattr(
        "blackcell.bootstrap.daemon.validate_alpha_worker_runtime_config",
        validate,
    )
    stop_event = Event()
    stop_event.set()
    api = FakeProcess()
    alpha_worker = FakeProcess()
    factory = FakeFactory(api, alpha_worker)

    daemon = RuntimeDaemon.from_config(
        configured,
        stop_event=stop_event,
        environment={},
        process_factory=factory,
        command_prefix=("runtime",),
    )

    assert daemon.serve() == 0
    assert validated == [configured]
    assert [record.argv for record in factory.records] == [
        ("runtime", "api"),
        ("runtime", "alpha-worker"),
    ]
    assert all("worker" not in record.argv for record in factory.records)
    assert api.terminated and alpha_worker.terminated


def test_daemon_composes_explicit_alpha_execution_and_review_children(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    configured = replace(
        config,
        alpha_worker=cast("AlphaWorkerRuntimeConfig", object()),
        alpha_review_worker=cast("AlphaReviewWorkerRuntimeConfig", object()),
    )
    execution_validated: list[RuntimeProcessConfig] = []
    review_validated: list[RuntimeProcessConfig] = []

    def validate_execution(
        value: RuntimeProcessConfig,
        *,
        environment: dict[str, str],
    ) -> None:
        assert environment == {}
        execution_validated.append(value)

    def validate_review(
        value: RuntimeProcessConfig,
        *,
        environment: dict[str, str],
    ) -> None:
        assert environment == {}
        review_validated.append(value)

    monkeypatch.setattr(
        "blackcell.bootstrap.daemon.validate_alpha_worker_runtime_config",
        validate_execution,
    )
    monkeypatch.setattr(
        "blackcell.bootstrap.daemon.validate_alpha_review_worker_runtime_config",
        validate_review,
    )
    stop_event = Event()
    stop_event.set()
    api = FakeProcess()
    execution = FakeProcess()
    review = FakeProcess()
    factory = FakeFactory(api, execution, review)

    daemon = RuntimeDaemon.from_config(
        configured,
        stop_event=stop_event,
        environment={},
        process_factory=factory,
        command_prefix=("runtime",),
    )

    assert daemon.serve() == 0
    assert execution_validated == [configured]
    assert review_validated == [configured]
    assert [record.argv for record in factory.records] == [
        ("runtime", "api"),
        ("runtime", "alpha-worker"),
        ("runtime", "alpha-review-worker"),
    ]
    assert all(record.argv[-1] != "worker" for record in factory.records)
    assert api.terminated and execution.terminated and review.terminated

    review_only = replace(config, alpha_review_worker=configured.alpha_review_worker)
    review_validated.clear()
    stop_event = Event()
    stop_event.set()
    api = FakeProcess()
    review = FakeProcess()
    factory = FakeFactory(api, review)
    daemon = RuntimeDaemon.from_config(
        review_only,
        stop_event=stop_event,
        environment={},
        process_factory=factory,
        command_prefix=("runtime",),
    )

    assert daemon.serve() == 0
    assert review_validated == [review_only]
    assert [record.argv for record in factory.records] == [
        ("runtime", "api"),
        ("runtime", "alpha-review-worker"),
    ]


def test_daemon_composes_explicit_alpha_verification_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    configured = replace(
        config,
        alpha_verify_worker=cast("AlphaVerifyWorkerRuntimeConfig", object()),
    )
    validated: list[RuntimeProcessConfig] = []

    def validate(value: RuntimeProcessConfig) -> None:
        validated.append(value)

    monkeypatch.setattr(
        "blackcell.bootstrap.daemon.validate_alpha_verify_worker_runtime_config",
        validate,
    )
    stop_event = Event()
    stop_event.set()
    api = FakeProcess()
    verifier = FakeProcess()
    factory = FakeFactory(api, verifier)

    daemon = RuntimeDaemon.from_config(
        configured,
        stop_event=stop_event,
        environment={},
        process_factory=factory,
        command_prefix=("runtime",),
    )

    assert daemon.serve() == 0
    assert validated == [configured]
    assert [record.argv for record in factory.records] == [
        ("runtime", "api"),
        ("runtime", "alpha-verify-worker"),
    ]
    assert all(record.argv[-1] != "worker" for record in factory.records)
    assert api.terminated and verifier.terminated


def test_daemon_stops_sibling_when_a_component_exits(tmp_path: Path) -> None:
    api = FakeProcess(7)
    worker = FakeProcess()
    daemon = RuntimeDaemon(
        tmp_path,
        graceful_timeout_seconds=1,
        process_factory=FakeFactory(api, worker),
        command_prefix=("runtime",),
        components=("api", "alpha-worker"),
        environment={},
    )

    assert daemon.serve() == 7
    assert not api.terminated
    assert worker.terminated

    surviving_api = FakeProcess()
    failing_factory = FakeFactory(surviving_api, OSError("sensitive spawn detail"))
    with pytest.raises(RuntimeError, match="daemon-component-startup-failed") as startup:
        RuntimeDaemon(
            tmp_path,
            graceful_timeout_seconds=1,
            process_factory=failing_factory,
            command_prefix=("runtime",),
            components=("api", "alpha-worker"),
            environment={},
        ).serve()
    assert "sensitive" not in str(startup.value)
    assert surviving_api.terminated


def test_daemon_forces_a_stale_child_after_the_grace_period(tmp_path: Path) -> None:
    stop_event = Event()
    stop_event.set()
    stale = FakeProcess(stale=True)
    worker = FakeProcess()
    daemon = RuntimeDaemon(
        tmp_path,
        graceful_timeout_seconds=0.01,
        stop_event=stop_event,
        process_factory=FakeFactory(stale, worker),
        command_prefix=("runtime",),
        components=("api", "alpha-worker"),
        environment={},
    )

    assert daemon.serve() == 0
    assert stale.terminated and stale.killed
    assert stale.returncode == -9
    assert worker.terminated and not worker.killed


def test_runtime_entrypoint_accepts_daemon_and_restores_signal_handlers(monkeypatch) -> None:
    installed: list[tuple[signal.Signals, object]] = []
    previous = {
        signal.SIGINT: object(),
        signal.SIGTERM: object(),
    }

    def fake_getsignal(kind: signal.Signals) -> object:
        return previous[kind]

    def fake_signal(kind: signal.Signals, handler: object) -> None:
        installed.append((kind, handler))

    class FakeDaemon:
        def serve(self) -> int:
            return 0

    def fake_from_config(
        config: object,
        *,
        stop_event: Event,
        environment: dict[str, str],
    ) -> FakeDaemon:
        del config, environment
        handler = cast(
            "Callable[[int, FrameType | None], object]",
            next(handler for kind, handler in installed if kind is signal.SIGTERM),
        )
        handler(signal.SIGTERM, None)
        assert stop_event.is_set()
        return FakeDaemon()

    monkeypatch.setattr("blackcell.bootstrap.process.signal.getsignal", fake_getsignal)
    monkeypatch.setattr("blackcell.bootstrap.process.signal.signal", fake_signal)
    monkeypatch.setattr(
        "blackcell.bootstrap.process.RuntimeProcessConfig.from_environment",
        lambda: object(),
    )
    monkeypatch.setattr("blackcell.bootstrap.process.RuntimeDaemon.from_config", fake_from_config)

    assert main(("daemon",)) == 0
    assert installed[-2:] == [
        (signal.SIGINT, previous[signal.SIGINT]),
        (signal.SIGTERM, previous[signal.SIGTERM]),
    ]


def _config(tmp_path: Path) -> RuntimeProcessConfig:
    repository = tmp_path / "repository"
    repository.mkdir()
    return RuntimeProcessConfig.from_environment(
        {
            DATA_DIR_ENV: str(tmp_path / "data"),
            API_TOKEN_ENV: TOKEN,
            REPOSITORY_ROOT_ENV: str(repository),
        }
    )
