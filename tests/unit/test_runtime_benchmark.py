from __future__ import annotations

import json
import stat
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest

from blackcell.evaluation import (
    ProbeProcessResult,
    RuntimeBenchmarkDesign,
    RuntimeBenchmarkProbeError,
    RuntimeBenchmarkReportReservation,
    RuntimeBenchmarkRunner,
    RuntimeEnvironmentManifest,
    encode_runtime_benchmark_report,
    write_runtime_benchmark_report,
)

NOW = datetime(2026, 7, 14, 12, tzinfo=UTC)
DIGEST = "sha256:" + "a" * 64


class _TickClock:
    def __init__(self) -> None:
        self._value = 0.0

    def __call__(self) -> float:
        self._value += 1.0
        return self._value


class _SuccessfulExecutor:
    def __init__(self) -> None:
        self.environments: list[dict[str, str]] = []

    def __call__(
        self,
        argv: tuple[str, ...],
        cwd: Path,
        environment: Mapping[str, str],
        timeout_seconds: int,
    ) -> ProbeProcessResult:
        del cwd, timeout_seconds
        captured = dict(environment)
        self.environments.append(captured)
        target = argv[-1]
        output = (
            f".. [100%]\n0.10s call {target}\n0.05s call {target}::second\n"
            "2 passed in 0.20s\nSECRET-MARKER"
        ).encode()
        return ProbeProcessResult(0, output, b"")


def test_runtime_benchmark_is_complete_matched_content_addressed_and_log_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BLACKCELL_API_TOKEN", "must-not-propagate")
    executor = _SuccessfulExecutor()
    runner = RuntimeBenchmarkRunner(
        executor=executor,
        environment_factory=lambda _: _environment(rootless=True),
        monotonic_clock=_TickClock(),
        wall_clock=lambda: NOW,
    )

    first = runner.run(Path.cwd(), RuntimeBenchmarkDesign("wp25-test", True))
    second = RuntimeBenchmarkRunner(
        executor=_SuccessfulExecutor(),
        environment_factory=lambda _: _environment(rootless=True),
        monotonic_clock=_TickClock(),
        wall_clock=lambda: NOW,
    ).run(Path.cwd(), RuntimeBenchmarkDesign("wp25-test", True))

    assert first == second
    assert first.report_id.startswith("sha256:")
    assert first.complete is True
    assert first.omitted_probe_ids == ()
    assert len(first.probes) == 6
    assert first.total_passed == 12
    assert first.total_skipped == 0
    assert first.total_wall_seconds == 6.0
    assert first.total_call_seconds == pytest.approx(0.9)
    assert all(item.status == "pass" for item in first.probes)
    assert all(item.call_seconds == pytest.approx(0.15) for item in first.probes)
    assert all(item.slowest_call_seconds == 0.1 for item in first.probes)
    assert all(item.output_digest.startswith("sha256:") for item in first.probes)
    assert all("BLACKCELL_API_TOKEN" not in item for item in executor.environments)
    rootless = next(item for item in first.probes if item.probe_id == "rootless-container")
    assert rootless.environment_overrides["BLACKCELL_RUN_PODMAN_TESTS"] == "1"
    assert "SECRET-MARKER" not in encode_runtime_benchmark_report(first)


def test_runtime_benchmark_without_rootless_probe_is_explicitly_incomplete() -> None:
    report = RuntimeBenchmarkRunner(
        executor=_SuccessfulExecutor(),
        environment_factory=lambda _: _environment(rootless=None),
        monotonic_clock=_TickClock(),
        wall_clock=lambda: NOW,
    ).run(Path.cwd(), RuntimeBenchmarkDesign("wp25-partial"))

    assert report.complete is False
    assert report.omitted_probe_ids == ("rootless-container",)
    assert len(report.probes) == 5
    assert "rootless-container" in report.required_probe_ids


def test_runtime_benchmark_fails_content_free_and_requires_rootless_host() -> None:
    def fail(
        argv: tuple[str, ...],
        cwd: Path,
        environment: Mapping[str, str],
        timeout_seconds: int,
    ) -> ProbeProcessResult:
        del argv, cwd, environment, timeout_seconds
        return ProbeProcessResult(7, b"secret output", b"more secret output")

    runner = RuntimeBenchmarkRunner(
        executor=fail,
        environment_factory=lambda _: _environment(rootless=True),
        monotonic_clock=_TickClock(),
        wall_clock=lambda: NOW,
    )
    with pytest.raises(RuntimeBenchmarkProbeError) as caught:
        runner.run(Path.cwd(), RuntimeBenchmarkDesign("wp25-fail"))
    assert caught.value.probe_id == "api"
    assert caught.value.returncode == 7
    assert "secret" not in str(caught.value)

    unavailable = RuntimeBenchmarkRunner(
        executor=_SuccessfulExecutor(),
        environment_factory=lambda _: _environment(rootless=False),
    )
    with pytest.raises(RuntimeBenchmarkProbeError) as rootless:
        unavailable.run(Path.cwd(), RuntimeBenchmarkDesign("wp25-rootless", True))
    assert rootless.value.probe_id == "rootless-container"


def test_runtime_benchmark_artifact_is_owner_only_canonical_and_exclusive(
    tmp_path: Path,
) -> None:
    report = _report()
    artifact = tmp_path / "wp25.json"

    write_runtime_benchmark_report(artifact, report)

    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert payload["report_id"] == report.report_id
    assert artifact.read_text(encoding="utf-8") == encode_runtime_benchmark_report(report)
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError, match="already exists"):
        write_runtime_benchmark_report(artifact, report)


def test_uncommitted_runtime_benchmark_reservation_is_removed(tmp_path: Path) -> None:
    artifact = tmp_path / "interrupted.json"

    with (
        pytest.raises(RuntimeError, match="interrupted"),
        RuntimeBenchmarkReportReservation(artifact),
    ):
        raise RuntimeError("interrupted")

    assert not artifact.exists()


def _report():
    return RuntimeBenchmarkRunner(
        executor=_SuccessfulExecutor(),
        environment_factory=lambda _: _environment(rootless=True),
        monotonic_clock=_TickClock(),
        wall_clock=lambda: NOW,
    ).run(Path.cwd(), RuntimeBenchmarkDesign("wp25-artifact", True))


def _environment(*, rootless: bool | None) -> RuntimeEnvironmentManifest:
    return RuntimeEnvironmentManifest(
        python="3.14.6",
        implementation="CPython",
        system="Linux",
        release="test",
        machine="x86_64",
        cpu_count=8,
        memory_bytes=16_000_000_000,
        uv="uv 0.8.0",
        pytest="9.1.1",
        podman="4.9.3" if rootless is not None else None,
        podman_rootless=rootless,
        base_sha="a" * 40,
        worktree_dirty_path_count=1,
        source_tree_digest=DIGEST,
        uv_lock_digest=DIGEST,
        containerfile_digest=DIGEST,
        compose_digest=DIGEST,
    )
