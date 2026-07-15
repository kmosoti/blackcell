from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import UTC, datetime
from enum import Enum
from importlib import metadata
from pathlib import Path
from typing import Any

from blackcell.kernel._json import bytes_digest, json_digest

_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)s call\s+(.+?)\s*$", re.MULTILINE)
_PASSED = re.compile(r"(\d+) passed")
_SKIPPED = re.compile(r"(\d+) skipped")
_SOURCE_ROOTS = ("src/blackcell",)
_SOURCE_FILES = (
    "tests/integration/test_runtime_api.py",
    "tests/unit/test_runtime_worker.py",
    "tests/unit/test_orchestration_scheduler.py",
    "tests/unit/test_runtime_quotas.py",
    "tests/unit/test_http_api.py",
    "tests/unit/test_runtime_recovery.py",
    "tests/integration/test_runtime_disaster_recovery.py",
    "tests/integration/test_podman_runtime.py",
    "tests/unit/test_runtime_benchmark.py",
    "tests/unit/test_runtime_cli.py",
    "Containerfile",
    "compose.yaml",
    "pyproject.toml",
    "uv.lock",
)
_BASE_ENVIRONMENT_KEYS = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
)


@dataclass(frozen=True, slots=True)
class RuntimeProbeDefinition:
    probe_id: str
    surfaces: tuple[str, ...]
    pytest_targets: tuple[str, ...]
    timeout_seconds: int = 300
    requires_rootless_podman: bool = False

    def __post_init__(self) -> None:
        if not self.probe_id.strip() or not self.surfaces or not self.pytest_targets:
            raise ValueError("runtime probe identity, surfaces, and targets are required")
        if self.timeout_seconds < 1:
            raise ValueError("runtime probe timeout must be positive")

    @property
    def argv(self) -> tuple[str, ...]:
        return (
            "uv",
            "run",
            "pytest",
            "-q",
            "--disable-warnings",
            "--durations=0",
            "--durations-min=0",
            *self.pytest_targets,
        )


@dataclass(frozen=True, slots=True)
class RuntimeBenchmarkDesign:
    experiment_id: str
    include_rootless_podman: bool = False

    def __post_init__(self) -> None:
        if not self.experiment_id.strip():
            raise ValueError("runtime benchmark identity must not be empty")


@dataclass(frozen=True, slots=True)
class RuntimeTestTiming:
    node_id: str
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class RuntimeProbeResult:
    probe_id: str
    surfaces: tuple[str, ...]
    argv: tuple[str, ...]
    environment_overrides: Mapping[str, str]
    status: str
    passed_count: int
    skipped_count: int
    wall_seconds: float
    call_seconds: float
    slowest_call_seconds: float
    test_timings: tuple[RuntimeTestTiming, ...]
    output_digest: str


@dataclass(frozen=True, slots=True)
class RuntimeEnvironmentManifest:
    python: str
    implementation: str
    system: str
    release: str
    machine: str
    cpu_count: int | None
    memory_bytes: int | None
    uv: str
    pytest: str
    podman: str | None
    podman_rootless: bool | None
    base_sha: str
    worktree_dirty_path_count: int
    source_tree_digest: str
    uv_lock_digest: str
    containerfile_digest: str
    compose_digest: str


@dataclass(frozen=True, slots=True)
class RuntimeBenchmarkReport:
    experiment_id: str
    recorded_at: datetime
    design: RuntimeBenchmarkDesign
    environment: RuntimeEnvironmentManifest
    probes: tuple[RuntimeProbeResult, ...]
    required_probe_ids: tuple[str, ...]
    omitted_probe_ids: tuple[str, ...]
    complete: bool
    total_passed: int
    total_skipped: int
    total_wall_seconds: float
    total_call_seconds: float
    limitations: tuple[str, ...]
    schema_version: str = "runtime-benchmark-report/v1"
    report_id: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != "runtime-benchmark-report/v1":
            raise ValueError("runtime benchmark report schema is unsupported")
        if not self.probes:
            raise ValueError("runtime benchmark report requires probe results")
        object.__setattr__(self, "recorded_at", _aware(self.recorded_at))
        object.__setattr__(self, "report_id", json_digest(_report_payload(self)))


@dataclass(frozen=True, slots=True)
class ProbeProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes


type ProbeExecutor = Callable[
    [tuple[str, ...], Path, Mapping[str, str], int],
    ProbeProcessResult,
]
type EnvironmentFactory = Callable[[Path], RuntimeEnvironmentManifest]


class RuntimeBenchmarkProbeError(RuntimeError):
    def __init__(self, probe_id: str, argv: tuple[str, ...], returncode: int) -> None:
        self.probe_id = probe_id
        self.argv = argv
        self.returncode = returncode
        super().__init__(f"runtime benchmark probe failed: {probe_id} (exit {returncode})")


class RuntimeBenchmarkRunner:
    def __init__(
        self,
        *,
        executor: ProbeExecutor | None = None,
        environment_factory: EnvironmentFactory | None = None,
        monotonic_clock: Callable[[], float] = time.perf_counter,
        wall_clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._executor = executor or _execute
        self._environment_factory = environment_factory or runtime_environment_manifest
        self._monotonic_clock = monotonic_clock
        self._wall_clock = wall_clock or (lambda: datetime.now(UTC))

    def run(self, repo_root: Path, design: RuntimeBenchmarkDesign) -> RuntimeBenchmarkReport:
        root = repo_root.resolve()
        environment = self._environment_factory(root)
        if design.include_rootless_podman and environment.podman_rootless is not True:
            raise RuntimeBenchmarkProbeError("rootless-container", ("podman", "info"), 1)
        definitions = runtime_benchmark_probes()
        selected = tuple(
            item
            for item in definitions
            if design.include_rootless_podman or not item.requires_rootless_podman
        )
        omitted = tuple(item.probe_id for item in definitions if item not in selected)
        results = tuple(self._run_probe(root, item) for item in selected)
        required_ids = tuple(item.probe_id for item in definitions)
        passed_ids = {item.probe_id for item in results if item.status == "pass"}
        complete = not omitted and set(required_ids) == passed_ids
        return RuntimeBenchmarkReport(
            experiment_id=design.experiment_id,
            recorded_at=self._wall_clock(),
            design=design,
            environment=environment,
            probes=results,
            required_probe_ids=required_ids,
            omitted_probe_ids=omitted,
            complete=complete,
            total_passed=sum(item.passed_count for item in results),
            total_skipped=sum(item.skipped_count for item in results),
            total_wall_seconds=sum(item.wall_seconds for item in results),
            total_call_seconds=sum(item.call_seconds for item in results),
            limitations=_limitations(),
        )

    def _run_probe(self, repo_root: Path, definition: RuntimeProbeDefinition) -> RuntimeProbeResult:
        overrides = {"PYTHONHASHSEED": "0"}
        if definition.requires_rootless_podman:
            overrides["BLACKCELL_RUN_PODMAN_TESTS"] = "1"
        probe_environment = _probe_environment(overrides)
        started = self._monotonic_clock()
        completed = self._executor(
            definition.argv,
            repo_root,
            probe_environment,
            definition.timeout_seconds,
        )
        wall_seconds = self._monotonic_clock() - started
        if wall_seconds < 0:
            raise ValueError("runtime benchmark clock moved backwards")
        output = completed.stdout + b"\0" + completed.stderr
        decoded = output.decode("utf-8", errors="replace")
        timings = tuple(
            RuntimeTestTiming(node_id, float(duration))
            for duration, node_id in _DURATION.findall(decoded)
        )
        passed = _last_count(_PASSED, decoded)
        skipped = _last_count(_SKIPPED, decoded)
        if completed.returncode != 0:
            raise RuntimeBenchmarkProbeError(
                definition.probe_id,
                definition.argv,
                completed.returncode,
            )
        status = "pass" if passed > 0 else "skipped" if skipped > 0 else "empty"
        if status == "empty":
            raise RuntimeBenchmarkProbeError(definition.probe_id, definition.argv, 5)
        call_seconds = sum(item.duration_seconds for item in timings)
        return RuntimeProbeResult(
            probe_id=definition.probe_id,
            surfaces=definition.surfaces,
            argv=definition.argv,
            environment_overrides=overrides,
            status=status,
            passed_count=passed,
            skipped_count=skipped,
            wall_seconds=wall_seconds,
            call_seconds=call_seconds,
            slowest_call_seconds=max(
                (item.duration_seconds for item in timings),
                default=0.0,
            ),
            test_timings=timings,
            output_digest=bytes_digest(output),
        )


class RuntimeBenchmarkReportReservation:
    """Reserve one owner-only report before any benchmark subprocess starts."""

    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise FileExistsError(f"experiment artifact already exists: {path}") from None
        self._stream = os.fdopen(descriptor, "wb")
        self._committed = False

    def __enter__(self) -> RuntimeBenchmarkReportReservation:
        return self

    def commit(self, report: RuntimeBenchmarkReport) -> None:
        if self._committed or self._stream.closed:
            raise RuntimeError("experiment artifact reservation is already closed")
        self._stream.write(encode_runtime_benchmark_report(report).encode("utf-8"))
        self._stream.flush()
        os.fsync(self._stream.fileno())
        self._stream.close()
        self._committed = True

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        if not self._stream.closed:
            self._stream.close()
        if not self._committed:
            self.path.unlink(missing_ok=True)


def runtime_benchmark_probes() -> tuple[RuntimeProbeDefinition, ...]:
    return (
        RuntimeProbeDefinition(
            "api",
            ("authenticated HTTP", "live-free replay", "storage admission"),
            ("tests/integration/test_runtime_api.py",),
        ),
        RuntimeProbeDefinition(
            "worker",
            ("five-role worker", "restart continuity", "artifact verification"),
            ("tests/unit/test_runtime_worker.py",),
        ),
        RuntimeProbeDefinition(
            "restart-fencing",
            ("scheduler restart", "retry fencing", "expired lease recovery"),
            (
                "tests/unit/test_orchestration_scheduler.py::test_submit_is_content_idempotent_and_reconstructs_after_restart",
                "tests/unit/test_orchestration_scheduler.py::test_retry_backoff_fencing_and_exact_failure_are_enforced",
                "tests/unit/test_orchestration_scheduler.py::test_expired_leases_retry_then_fail_and_block_descendants",
            ),
        ),
        RuntimeProbeDefinition(
            "quota",
            ("request quota", "active storage quota", "artifact quota"),
            (
                "tests/unit/test_runtime_quotas.py",
                "tests/unit/test_http_api.py::test_request_quota_counts_failed_authentication_and_exempts_health",
            ),
        ),
        RuntimeProbeDefinition(
            "recovery",
            ("online backup", "verification", "external restore", "live-free replay"),
            (
                "tests/unit/test_runtime_recovery.py",
                "tests/integration/test_runtime_disaster_recovery.py",
            ),
        ),
        RuntimeProbeDefinition(
            "rootless-container",
            ("rootless Podman", "health", "read-only roots", "restart persistence"),
            ("tests/integration/test_podman_runtime.py",),
            timeout_seconds=900,
            requires_rootless_podman=True,
        ),
    )


def runtime_environment_manifest(repo_root: Path) -> RuntimeEnvironmentManifest:
    root = repo_root.resolve()
    base_sha = _metadata_command(("git", "rev-parse", "HEAD"), root)
    dirty = _metadata_command(
        ("git", "status", "--porcelain=v2", "--untracked-files=all"),
        root,
    )
    uv_version = _metadata_command(("uv", "--version"), root)
    podman_version: str | None = None
    podman_rootless: bool | None = None
    info = _metadata_command(("podman", "info", "--format", "json"), root, required=False)
    if info:
        try:
            payload = json.loads(info)
            podman_version = str(payload["version"]["Version"])
            podman_rootless = bool(payload["host"]["security"]["rootless"])
        except KeyError, TypeError, ValueError:
            podman_version = "unparseable"
            podman_rootless = None
    return RuntimeEnvironmentManifest(
        python=platform.python_version(),
        implementation=platform.python_implementation(),
        system=platform.system(),
        release=platform.release(),
        machine=platform.machine(),
        cpu_count=os.cpu_count(),
        memory_bytes=_memory_bytes(),
        uv=uv_version,
        pytest=_package_version("pytest"),
        podman=podman_version,
        podman_rootless=podman_rootless,
        base_sha=base_sha,
        worktree_dirty_path_count=len(tuple(line for line in dirty.splitlines() if line)),
        source_tree_digest=_source_tree_digest(root),
        uv_lock_digest=_file_digest(root / "uv.lock"),
        containerfile_digest=_file_digest(root / "Containerfile"),
        compose_digest=_file_digest(root / "compose.yaml"),
    )


def encode_runtime_benchmark_report(report: RuntimeBenchmarkReport) -> str:
    return json.dumps(_jsonable(report), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def write_runtime_benchmark_report(path: Path, report: RuntimeBenchmarkReport) -> None:
    with RuntimeBenchmarkReportReservation(path) as reservation:
        reservation.commit(report)


def _execute(
    argv: tuple[str, ...],
    cwd: Path,
    environment: Mapping[str, str],
    timeout_seconds: int,
) -> ProbeProcessResult:
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            env=dict(environment),
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ProbeProcessResult(124, b"", b"")
    return ProbeProcessResult(completed.returncode, completed.stdout, completed.stderr)


def _probe_environment(overrides: Mapping[str, str]) -> dict[str, str]:
    environment = {key: os.environ[key] for key in _BASE_ENVIRONMENT_KEYS if key in os.environ}
    environment.update(overrides)
    return environment


def _metadata_command(
    argv: tuple[str, ...],
    cwd: Path,
    *,
    required: bool = True,
) -> str:
    environment = _probe_environment({})
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        if required:
            raise RuntimeError(f"runtime benchmark metadata command failed: {argv[0]}") from None
        return ""
    if completed.returncode != 0:
        if required:
            raise RuntimeError(f"runtime benchmark metadata command failed: {argv[0]}")
        return ""
    return completed.stdout.strip()


def _source_tree_digest(repo_root: Path) -> str:
    paths: list[Path] = []
    for relative in _SOURCE_ROOTS:
        root = repo_root / relative
        paths.extend(
            item
            for item in root.rglob("*")
            if item.is_file()
            and "__pycache__" not in item.parts
            and item.suffix not in {".pyc", ".pyo"}
        )
    paths.extend(repo_root / relative for relative in _SOURCE_FILES)
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: item.relative_to(repo_root).as_posix()):
        relative = path.relative_to(repo_root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        data = path.read_bytes()
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return f"sha256:{digest.hexdigest()}"


def _file_digest(path: Path) -> str:
    return bytes_digest(path.read_bytes())


def _memory_bytes() -> int | None:
    try:
        return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
    except OSError, ValueError:
        return None


def _package_version(package: str) -> str:
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return "unavailable"


def _last_count(pattern: re.Pattern[str], output: str) -> int:
    matches = pattern.findall(output)
    return int(matches[-1]) if matches else 0


def _limitations() -> tuple[str, ...]:
    return (
        "pytest call durations and subprocess wall time include fixture and harness behavior and "
        "are not service SLOs",
        "the profile is one run on one host without controlled cache state or concurrent load",
        "the benchmark reports reliability acceptance, not sustained throughput or tail latency",
        "the recovery probe establishes integrity and replay, not a production RTO or RPO",
        "the rootless probe builds a local test image and validates one API restart, not "
        "long-running availability",
        "no optimization or runtime default changes are justified by this baseline alone",
    )


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("runtime benchmark timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _report_payload(report: RuntimeBenchmarkReport) -> dict[str, Any]:
    return {
        item.name: _jsonable(getattr(report, item.name))
        for item in fields(report)
        if item.name != "report_id"
    }


def _jsonable(value: object) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _jsonable(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_jsonable(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    return value


__all__ = [
    "ProbeProcessResult",
    "RuntimeBenchmarkDesign",
    "RuntimeBenchmarkProbeError",
    "RuntimeBenchmarkReport",
    "RuntimeBenchmarkReportReservation",
    "RuntimeBenchmarkRunner",
    "RuntimeEnvironmentManifest",
    "RuntimeProbeDefinition",
    "RuntimeProbeResult",
    "RuntimeTestTiming",
    "encode_runtime_benchmark_report",
    "runtime_benchmark_probes",
    "runtime_environment_manifest",
    "write_runtime_benchmark_report",
]
