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

import pytest

from blackcell.bootstrap.alpha_verify_process import (
    AlphaVerifyWorkerProcess,
    AlphaVerifyWorkerProcessError,
    AlphaVerifyWorkerProcessFailureCode,
)
from blackcell.bootstrap.alpha_verify_runtime import AlphaVerificationReconciliationReport
from blackcell.bootstrap.alpha_verify_worker import (
    AlphaVerificationWorker,
    AlphaVerificationWorkerCycleResult,
)
from blackcell.bootstrap.process import main
from blackcell.bootstrap.worker_process_lock import WorkerProcessRole, worker_process_lock
from blackcell.config import (
    ALPHA_REVIEW_CONFIG_FILE_ENV,
    ALPHA_REVIEW_CONFIG_SCHEMA,
    ALPHA_VERIFY_CONFIG_FILE_ENV,
    ALPHA_VERIFY_CONFIG_SCHEMA,
    API_TOKEN_ENV,
    DATA_DIR_ENV,
    REPOSITORY_ROOT_ENV,
    AlphaIsolationRuntimeConfig,
    AlphaProviderRuntimeConfig,
    AlphaVerifyConfigError,
    AlphaWorkerLoopConfig,
    AlphaWorkerRuntimeConfig,
    ProcessConfigError,
    ProcessConfigFailureCode,
    RuntimeProcessConfig,
    load_alpha_verify_config,
)
from blackcell.config.process import _require_separate_alpha_authority

TOKEN = "Alpha-verify_process-token.0123456789-ABCDEFG"
type VerifyCycleStatus = Literal[
    "idle",
    "verification-completed",
    "verification-error",
    "claim-conflict",
]


class RecordingCoordinator:
    def __init__(self, statuses: Iterable[VerifyCycleStatus]) -> None:
        self.statuses = iter(statuses)
        self.calls = 0

    def run_once(self) -> AlphaVerificationWorkerCycleResult:
        self.calls += 1
        return AlphaVerificationWorkerCycleResult(status=next(self.statuses))


class RecordingScheduler:
    def __init__(self, order: list[str]) -> None:
        self.order = order

    def reconcile(self, *, principal_id: str) -> AlphaVerificationReconciliationReport:
        self.order.append(f"reconcile:{principal_id}")
        return AlphaVerificationReconciliationReport(())


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


def test_alpha_verify_config_is_owner_only_closed_and_authority_separated(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path)
    config = RuntimeProcessConfig.from_environment(environment)
    assert config.alpha_verify_worker is not None
    assert config.alpha_verify_worker.schema_version == ALPHA_VERIFY_CONFIG_SCHEMA
    assert config.alpha_verify_worker.worker.worker_id == "alpha-verifier.test"
    assert config.alpha_verify_worker.worker.supervisor_id == "alpha-verify-supervisor.test"

    source = Path(environment[ALPHA_VERIFY_CONFIG_FILE_ENV])
    source.chmod(0o644)
    with pytest.raises(ProcessConfigError) as permissions:
        RuntimeProcessConfig.from_environment(environment)
    assert permissions.value.code is ProcessConfigFailureCode.INVALID_ALPHA_VERIFY_CONFIG
    assert str(source) not in str(permissions.value)

    source.chmod(0o600)
    with pytest.raises(AlphaVerifyConfigError):
        load_alpha_verify_config(
            environment,
            repository_root=config.repository_root,
            expected_uid=source.stat().st_uid + 1,
        )

    payload = json.loads(source.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    payload["ambient"] = "authority"
    source.write_text(json.dumps(payload), encoding="utf-8")
    source.chmod(0o600)
    with pytest.raises(ProcessConfigError) as closed:
        RuntimeProcessConfig.from_environment(environment)
    assert closed.value.code is ProcessConfigFailureCode.INVALID_ALPHA_VERIFY_CONFIG
    assert "authority" not in str(closed.value)

    collisions = (
        ("alpha-verifier.test", "alpha-review-supervisor.test"),
        ("alpha-verify-supervisor.test", "alpha-review-supervisor.test"),
        ("alpha-reviewer.test", "alpha-verifier.test"),
        ("alpha-reviewer.test", "alpha-verify-supervisor.test"),
    )
    for index, (reviewer, supervisor) in enumerate(collisions):
        collision_root = tmp_path / f"collision-{index}"
        collision_root.mkdir()
        collision_environment = _environment(
            collision_root,
            review_worker_id=reviewer,
            review_supervisor_id=supervisor,
        )
        with pytest.raises(ProcessConfigError) as collision:
            RuntimeProcessConfig.from_environment(collision_environment)
        assert collision.value.code is ProcessConfigFailureCode.INVALID_ALPHA_VERIFY_CONFIG
        assert reviewer not in str(collision.value)
        assert supervisor not in str(collision.value)

    assert config.alpha_verify_worker is not None
    execution = AlphaWorkerRuntimeConfig(
        source_path=tmp_path / "alpha-worker.json",
        provider=cast("AlphaProviderRuntimeConfig", object()),
        isolation=cast("AlphaIsolationRuntimeConfig", object()),
        worker=AlphaWorkerLoopConfig("alpha-executor.test", 1, 1, 1, 0),
    )
    for verification in (
        replace(
            config.alpha_verify_worker,
            worker=replace(
                config.alpha_verify_worker.worker,
                worker_id="alpha-executor.test",
            ),
        ),
        replace(
            config.alpha_verify_worker,
            worker=replace(
                config.alpha_verify_worker.worker,
                supervisor_id="alpha-executor.test",
            ),
        ),
    ):
        with pytest.raises(ProcessConfigError) as execution_collision:
            _require_separate_alpha_authority(execution, None, verification)
        assert (
            execution_collision.value.code is ProcessConfigFailureCode.INVALID_ALPHA_VERIFY_CONFIG
        )


def test_alpha_verify_config_rejects_malformed_identity_and_file_boundaries(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    valid: dict[str, object] = {
        "schema_version": ALPHA_VERIFY_CONFIG_SCHEMA,
        "worker": {
            "worker_id": "alpha-verifier.test",
            "supervisor_id": "alpha-verify-supervisor.test",
            "lease_seconds": 300,
            "poll_milliseconds": 125,
        },
    }
    assert load_alpha_verify_config({}, repository_root=repository) is None

    variants: list[tuple[str, dict[str, object]]] = []

    def add(
        name: str,
        *,
        root: tuple[str, object] | None = None,
        worker: tuple[str, object] | None = None,
    ) -> None:
        payload = json.loads(json.dumps(valid))
        assert isinstance(payload, dict)
        if root is not None:
            payload[root[0]] = root[1]
        if worker is not None:
            raw_worker = payload["worker"]
            assert isinstance(raw_worker, dict)
            raw_worker[worker[0]] = worker[1]
        variants.append((name, cast("dict[str, object]", payload)))

    add("schema", root=("schema_version", "blackcell.alpha-verify-config/v2"))
    add("worker-shape", root=("worker", None))
    add("worker-empty", worker=("worker_id", ""))
    add("worker-invalid", worker=("worker_id", "bad:worker"))
    add("lease-type", worker=("lease_seconds", True))
    add("poll-range", worker=("poll_milliseconds", 0))
    same_identity = json.loads(json.dumps(valid))
    assert isinstance(same_identity, dict)
    same_worker = same_identity["worker"]
    assert isinstance(same_worker, dict)
    same_worker["supervisor_id"] = same_worker["worker_id"]
    variants.append(("same-identity", cast("dict[str, object]", same_identity)))

    for index, (name, payload) in enumerate(variants):
        source = tmp_path / f"invalid-{index}-{name}.json"
        source.write_text(json.dumps(payload), encoding="utf-8")
        source.chmod(0o600)
        with pytest.raises(AlphaVerifyConfigError):
            load_alpha_verify_config(
                {ALPHA_VERIFY_CONFIG_FILE_ENV: str(source)},
                repository_root=repository,
            )

    malformed = (
        (b"[]", "array"),
        (b"\xff", "utf8"),
        (b"x" * (64 * 1024 + 1), "oversized"),
        (
            b'{"schema_version":"blackcell.alpha-verify-config/v1",'
            b'"schema_version":"blackcell.alpha-verify-config/v1","worker":{}}',
            "duplicate",
        ),
    )
    for content, name in malformed:
        source = tmp_path / f"malformed-{name}.json"
        source.write_bytes(content)
        source.chmod(0o600)
        with pytest.raises(AlphaVerifyConfigError):
            load_alpha_verify_config(
                {ALPHA_VERIFY_CONFIG_FILE_ENV: str(source)},
                repository_root=repository,
            )

    for source_value in ("", "relative.json", str((tmp_path / "missing.json").resolve())):
        with pytest.raises(AlphaVerifyConfigError):
            load_alpha_verify_config(
                {ALPHA_VERIFY_CONFIG_FILE_ENV: source_value},
                repository_root=repository,
            )

    repository_source = repository / "verify.json"
    repository_source.write_text(json.dumps(valid), encoding="utf-8")
    repository_source.chmod(0o600)
    with pytest.raises(AlphaVerifyConfigError):
        load_alpha_verify_config(
            {ALPHA_VERIFY_CONFIG_FILE_ENV: str(repository_source)},
            repository_root=repository,
        )

    valid_source = tmp_path / "valid-root-type.json"
    valid_source.write_text(json.dumps(valid), encoding="utf-8")
    valid_source.chmod(0o600)
    with pytest.raises(AlphaVerifyConfigError):
        load_alpha_verify_config(
            {ALPHA_VERIFY_CONFIG_FILE_ENV: str(valid_source)},
            repository_root=cast("Path", object()),
        )


def test_alpha_verify_process_reconciles_and_runs_once_against_shared_storage(
    tmp_path: Path,
) -> None:
    config = RuntimeProcessConfig.from_environment(_environment(tmp_path))

    process = AlphaVerifyWorkerProcess.from_config(config)

    assert process.serve(once=True) == 3
    assert config.security.paths.database_path.is_file()
    assert stat.S_IMODE(config.security.paths.database_path.stat().st_mode) == 0o600
    assert config.security.paths.artifact_root.is_dir()
    coordinator = cast("AlphaVerificationWorker", process.coordinator)
    assert coordinator.policy.worker_id == "alpha-verifier.test"
    assert coordinator.policy.lease_seconds == 300


def test_alpha_verify_process_loop_uses_verification_polling_and_storage_policy(
    tmp_path: Path,
) -> None:
    config = RuntimeProcessConfig.from_environment(_environment(tmp_path))
    assert config.alpha_verify_worker is not None
    order: list[str] = []
    coordinator = RecordingCoordinator(("verification-completed", "idle"))
    stop_event = StopAfterWait(order)
    process = AlphaVerifyWorkerProcess(
        coordinator,
        RecordingScheduler(order),
        config,
        stop_event,
        FixedQuota(True),
    )

    assert process.serve() == 0
    assert coordinator.calls == 2
    assert order == [
        f"reconcile:{config.alpha_verify_worker.worker.supervisor_id}",
        "wait",
    ]
    assert stop_event.timeouts == [config.alpha_verify_worker.worker.poll_milliseconds / 1_000]

    blocked = RecordingCoordinator(("verification-completed",))
    blocked_process = AlphaVerifyWorkerProcess(
        blocked,
        RecordingScheduler([]),
        config,
        Event(),
        FixedQuota(False),
    )
    assert blocked_process.serve(once=True) == 3
    assert blocked.calls == 0


def test_alpha_verify_process_rejects_duplicate_role_before_reconciliation(
    tmp_path: Path,
) -> None:
    config = RuntimeProcessConfig.from_environment(_environment(tmp_path))
    order: list[str] = []
    coordinator = RecordingCoordinator(("idle",))
    process = AlphaVerifyWorkerProcess(coordinator, RecordingScheduler(order), config)

    with (
        worker_process_lock(config.security.paths, WorkerProcessRole.ALPHA_VERIFICATION),
        pytest.raises(AlphaVerifyWorkerProcessError) as duplicate,
    ):
        process.serve(once=True)

    assert duplicate.value.code is AlphaVerifyWorkerProcessFailureCode.ALREADY_RUNNING
    assert order == []
    assert coordinator.calls == 0


def test_alpha_verify_process_entrypoint_restores_signal_handlers_and_fails_closed(
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

    unconfigured = RuntimeProcessConfig.from_environment(_environment(tmp_path, verify=False))
    monkeypatch.setattr(
        "blackcell.bootstrap.process.RuntimeProcessConfig.from_environment",
        lambda: unconfigured,
    )
    assert main(("alpha-verify-worker", "--once")) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == '{"error": {"code": "alpha-verify-worker-not-configured"}}\n'
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
        "blackcell.bootstrap.process.AlphaVerifyWorkerProcess.from_config",
        fake_from_config,
    )
    assert main(("alpha-verify-worker", "--once")) == 3
    assert installed[-2:] == [
        (signal.SIGINT, previous[signal.SIGINT]),
        (signal.SIGTERM, previous[signal.SIGTERM]),
    ]


def test_alpha_verify_process_facades_exclude_source_reads_and_supervisor_authority(
    tmp_path: Path,
) -> None:
    config = RuntimeProcessConfig.from_environment(_environment(tmp_path))
    process = AlphaVerifyWorkerProcess.from_config(config)
    coordinator = cast("AlphaVerificationWorker", process.coordinator)

    assert not hasattr(coordinator.source, "events")
    assert not hasattr(coordinator.source, "execution")
    assert not hasattr(coordinator.source, "artifacts")
    assert not hasattr(coordinator.scheduler, "reconcile")
    assert not hasattr(coordinator.artifacts, "get_bytes")
    assert not hasattr(coordinator.verifier, "review")


def _environment(
    tmp_path: Path,
    *,
    verify: bool = True,
    review_worker_id: str | None = None,
    review_supervisor_id: str = "alpha-review-supervisor.test",
) -> dict[str, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    environment = {
        DATA_DIR_ENV: str(tmp_path / "data"),
        API_TOKEN_ENV: TOKEN,
        REPOSITORY_ROOT_ENV: str(repository),
    }
    if verify:
        source = tmp_path / "alpha-verify.json"
        source.write_text(
            json.dumps(
                {
                    "schema_version": ALPHA_VERIFY_CONFIG_SCHEMA,
                    "worker": {
                        "worker_id": "alpha-verifier.test",
                        "supervisor_id": "alpha-verify-supervisor.test",
                        "lease_seconds": 300,
                        "poll_milliseconds": 125,
                    },
                }
            ),
            encoding="utf-8",
        )
        source.chmod(0o600)
        environment[ALPHA_VERIFY_CONFIG_FILE_ENV] = str(source)
    if review_worker_id is not None:
        review = tmp_path / "alpha-review.json"
        review.write_text(
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
                        "worker_id": review_worker_id,
                        "supervisor_id": review_supervisor_id,
                        "lease_seconds": 300,
                        "poll_milliseconds": 125,
                    },
                }
            ),
            encoding="utf-8",
        )
        review.chmod(0o600)
        environment[ALPHA_REVIEW_CONFIG_FILE_ENV] = str(review)
    return environment


def _executable(name: str) -> Path:
    value = shutil.which(name)
    assert value is not None
    return Path(value).resolve(strict=True)
