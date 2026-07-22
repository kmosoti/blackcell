"""Pinned, bounded client for Kernform's public agent-mode CLI contract."""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol, cast

import msgspec

from blackcell.adapters.bounded_process import (
    BoundedProcessError,
    BoundedProcessFailureCode,
    BoundedProcessResult,
    BoundedProcessRunner,
    BoundedStreamCapture,
)
from blackcell.interfaces.http import WireContractError, decode_contract
from blackcell.interfaces.kernform_contracts import (
    KernformWireArtifact,
    KernformWireCheckResult,
    KernformWireEnvelope,
    KernformWireInitResult,
    KernformWireStatus,
)
from blackcell.kernel._json import json_digest

SUPPORTED_KERNFORM_VERSION = "0.1.0"
KERNFORM_COMMAND_SCHEMA = "kernform.command/v1"
KERNFORM_EXECUTABLE_ENV = "BLACKCELL_KERNFORM_EXECUTABLE"
DEFAULT_KERNFORM_EXECUTABLE = "kernform"

_DEFAULT_TIMEOUT_SECONDS = 15.0
_MAX_TIMEOUT_SECONDS = 120.0
_MAX_STDOUT_BYTES = 1024 * 1024
_MAX_STDERR_BYTES = 64 * 1024
_MAX_TOKEN_BYTES = 4096
_MAX_CAPABILITIES = 32
_MAX_DIAGNOSTICS = 256
_MAX_ARTIFACTS = 256
_MAX_REQUIREMENTS = 256
_MAX_FILES_CHECKED = 1_000_000
_MAX_OPERATIONS = 1_000_000
_DIAGNOSTIC_ID = re.compile(r"KF-[A-Z]+-[0-9]{3}\Z")
_ARTIFACT_HASH = re.compile(r"[0-9a-f]{64}\Z")
_TEST_REQUIREMENT_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,127}\Z")

KernformStatus = KernformWireStatus
KernformProfile = Literal["library", "cli", "api"]


class KernformClientFailureCode(StrEnum):
    INVALID_EXECUTABLE = "invalid-kernform-executable"
    INVALID_TIMEOUT = "invalid-kernform-timeout"
    INVALID_PROJECT_ROOT = "invalid-kernform-project-root"
    INVALID_ARGUMENT = "invalid-kernform-argument"
    SPAWN_FAILED = "kernform-spawn-failed"
    TIMED_OUT = "kernform-timed-out"
    OUTPUT_TOO_LARGE = "kernform-output-too-large"
    OUTPUT_INCOMPLETE = "kernform-output-incomplete"
    INVALID_ENVELOPE = "invalid-kernform-envelope"
    UNSUPPORTED_VERSION = "unsupported-kernform-version"
    EXIT_MISMATCH = "kernform-exit-mismatch"
    ARTIFACT_OUTSIDE_ROOT = "kernform-artifact-outside-root"


class KernformClientError(RuntimeError):
    """A typed boundary failure that never includes process output or local paths."""

    def __init__(self, code: KernformClientFailureCode) -> None:
        self.code = code
        super().__init__(code.value)

    @property
    def cli_exit_code(self) -> int:
        if self.code in {
            KernformClientFailureCode.INVALID_EXECUTABLE,
            KernformClientFailureCode.INVALID_TIMEOUT,
            KernformClientFailureCode.INVALID_PROJECT_ROOT,
            KernformClientFailureCode.INVALID_ARGUMENT,
        }:
            return 1
        if self.code in {
            KernformClientFailureCode.SPAWN_FAILED,
            KernformClientFailureCode.TIMED_OUT,
        }:
            return 3
        return 4


@dataclass(frozen=True, slots=True)
class KernformDiagnostic:
    id: str
    severity: Literal["info", "warning", "error"]
    message: str
    context: dict[str, object]


@dataclass(frozen=True, slots=True)
class KernformArtifact:
    kind: str
    path: str
    hash: str | None


@dataclass(frozen=True, slots=True)
class KernformInvocationResult:
    kernform_version: str
    project_root: Path
    command: Literal["check", "init"]
    status: KernformStatus
    exit_code: int
    result: dict[str, object] | None
    diagnostics: tuple[KernformDiagnostic, ...]
    artifacts: tuple[KernformArtifact, ...]
    argv_digest: str
    result_digest: str
    schema_version: Literal["kernform-invocation/v1"] = "kernform-invocation/v1"


KernformStreamCapture = BoundedStreamCapture
KernformProcessResult = BoundedProcessResult


class KernformTransport(Protocol):
    def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        timeout_seconds: float,
        stdout_limit_bytes: int,
        stderr_limit_bytes: int,
    ) -> KernformProcessResult: ...


class SubprocessKernformTransport:
    """Run one direct argv with bounded capture and process-group timeout cleanup."""

    def __init__(self, runner: BoundedProcessRunner | None = None) -> None:
        self._runner = runner or BoundedProcessRunner()

    def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        timeout_seconds: float,
        stdout_limit_bytes: int,
        stderr_limit_bytes: int,
    ) -> KernformProcessResult:
        try:
            return self._runner.run(
                argv,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
                stdout_limit_bytes=stdout_limit_bytes,
                stderr_limit_bytes=stderr_limit_bytes,
            )
        except BoundedProcessError as error:
            mapping = {
                BoundedProcessFailureCode.INVALID_INVOCATION: (
                    KernformClientFailureCode.INVALID_ARGUMENT
                ),
                BoundedProcessFailureCode.SPAWN_FAILED: KernformClientFailureCode.SPAWN_FAILED,
                BoundedProcessFailureCode.TIMED_OUT: KernformClientFailureCode.TIMED_OUT,
                BoundedProcessFailureCode.OUTPUT_TOO_LARGE: (
                    KernformClientFailureCode.OUTPUT_TOO_LARGE
                ),
                BoundedProcessFailureCode.OUTPUT_INCOMPLETE: (
                    KernformClientFailureCode.OUTPUT_INCOMPLETE
                ),
            }
            raise KernformClientError(mapping[error.code]) from error


@dataclass(frozen=True, slots=True)
class KernformCliClient:
    executable: str = DEFAULT_KERNFORM_EXECUTABLE
    transport: KernformTransport = field(
        default_factory=SubprocessKernformTransport,
        repr=False,
        compare=False,
    )
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS
    stdout_limit_bytes: int = _MAX_STDOUT_BYTES
    stderr_limit_bytes: int = _MAX_STDERR_BYTES

    def __post_init__(self) -> None:
        _require_token(
            self.executable,
            code=KernformClientFailureCode.INVALID_EXECUTABLE,
        )
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, int | float)
            or not math.isfinite(self.timeout_seconds)
            or not 0 < self.timeout_seconds <= _MAX_TIMEOUT_SECONDS
        ):
            raise KernformClientError(KernformClientFailureCode.INVALID_TIMEOUT)
        for limit, maximum in (
            (self.stdout_limit_bytes, _MAX_STDOUT_BYTES),
            (self.stderr_limit_bytes, _MAX_STDERR_BYTES),
        ):
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= maximum:
                raise KernformClientError(KernformClientFailureCode.INVALID_ARGUMENT)

    def check(self, project_root: Path) -> KernformInvocationResult:
        root = _existing_project_root(project_root)
        version = self._probe_version(cwd=root)
        argv = (
            self.executable,
            "--agent",
            "--format",
            "json",
            "check",
            str(root),
        )
        return self._invoke(argv, command="check", project_root=root, version=version, cwd=root)

    def init(
        self,
        *,
        name: str,
        destination: Path,
        profile: KernformProfile = "library",
        capabilities: Sequence[str] = (),
        no_git: bool = False,
        initial_commit: bool = False,
    ) -> KernformInvocationResult:
        _require_token(name, code=KernformClientFailureCode.INVALID_ARGUMENT)
        if not isinstance(profile, str) or profile not in {"library", "cli", "api"}:
            raise KernformClientError(KernformClientFailureCode.INVALID_ARGUMENT)
        if isinstance(capabilities, str | bytes | bytearray) or not isinstance(
            capabilities, Sequence
        ):
            raise KernformClientError(KernformClientFailureCode.INVALID_ARGUMENT)
        if len(capabilities) > _MAX_CAPABILITIES:
            raise KernformClientError(KernformClientFailureCode.INVALID_ARGUMENT)
        normalized_capabilities = tuple(capabilities)
        for capability in normalized_capabilities:
            _require_token(capability, code=KernformClientFailureCode.INVALID_ARGUMENT)
        if not isinstance(no_git, bool) or not isinstance(initial_commit, bool):
            raise KernformClientError(KernformClientFailureCode.INVALID_ARGUMENT)
        if initial_commit and no_git:
            raise KernformClientError(KernformClientFailureCode.INVALID_ARGUMENT)

        root = _initialization_root(destination)
        cwd = root if root.is_dir() else root.parent
        version = self._probe_version(cwd=cwd)
        tokens = [
            self.executable,
            "--agent",
            "--format",
            "json",
            "init",
            name,
            "--destination",
            str(root),
            "--profile",
            profile,
        ]
        for capability in normalized_capabilities:
            tokens.extend(("--with", capability))
        if no_git:
            tokens.append("--no-git")
        if initial_commit:
            tokens.append("--initial-commit")
        return self._invoke(
            tuple(tokens),
            command="init",
            project_root=root,
            version=version,
            cwd=cwd,
        )

    def _probe_version(self, *, cwd: Path) -> str:
        argv = (self.executable, "--agent", "--version")
        envelope = self._run_and_decode(argv, expected_command="version", cwd=cwd)
        if (
            envelope.status != "success"
            or envelope.exit_code != 0
            or envelope.result != SUPPORTED_KERNFORM_VERSION
        ):
            raise KernformClientError(KernformClientFailureCode.UNSUPPORTED_VERSION)
        return SUPPORTED_KERNFORM_VERSION

    def _invoke(
        self,
        argv: tuple[str, ...],
        *,
        command: Literal["check", "init"],
        project_root: Path,
        version: str,
        cwd: Path,
    ) -> KernformInvocationResult:
        envelope = self._run_and_decode(argv, expected_command=command, cwd=cwd)
        artifacts = _confined_artifacts(envelope.artifacts, project_root)
        result = _validated_command_result(
            envelope,
            command=command,
            project_root=project_root,
            artifacts=artifacts,
        )
        return KernformInvocationResult(
            kernform_version=version,
            project_root=project_root,
            command=command,
            status=envelope.status,
            exit_code=envelope.exit_code,
            result=result,
            diagnostics=tuple(
                KernformDiagnostic(
                    id=item.id,
                    severity=item.severity,
                    message=item.message,
                    context=dict(item.context),
                )
                for item in envelope.diagnostics
            ),
            artifacts=artifacts,
            argv_digest=json_digest(list(argv)),
            result_digest=json_digest(_envelope_document(envelope)),
        )

    def _run_and_decode(
        self,
        argv: tuple[str, ...],
        *,
        expected_command: str,
        cwd: Path,
    ) -> KernformWireEnvelope:
        process = self.transport.run(
            argv,
            cwd=cwd,
            timeout_seconds=float(self.timeout_seconds),
            stdout_limit_bytes=self.stdout_limit_bytes,
            stderr_limit_bytes=self.stderr_limit_bytes,
        )
        _validate_capture(process.stdout, self.stdout_limit_bytes)
        _validate_capture(process.stderr, self.stderr_limit_bytes)
        if process.stderr.captured:
            raise KernformClientError(KernformClientFailureCode.INVALID_ENVELOPE)
        try:
            envelope = decode_contract(process.stdout.captured, KernformWireEnvelope)
        except WireContractError as error:
            raise KernformClientError(KernformClientFailureCode.INVALID_ENVELOPE) from error
        _validate_envelope(envelope, expected_command=expected_command)
        if process.return_code != envelope.exit_code:
            raise KernformClientError(KernformClientFailureCode.EXIT_MISMATCH)
        return envelope


def _validate_capture(capture: KernformStreamCapture, limit: int) -> None:
    if not capture.complete:
        raise KernformClientError(KernformClientFailureCode.OUTPUT_INCOMPLETE)
    if cast("int", capture.total_bytes) > limit:
        raise KernformClientError(KernformClientFailureCode.OUTPUT_TOO_LARGE)


def _validate_envelope(envelope: KernformWireEnvelope, *, expected_command: str) -> None:
    if envelope.command != expected_command or not 0 <= envelope.exit_code <= 5:
        raise KernformClientError(KernformClientFailureCode.INVALID_ENVELOPE)
    if (envelope.status == "success") != (envelope.exit_code == 0):
        raise KernformClientError(KernformClientFailureCode.INVALID_ENVELOPE)
    if len(envelope.diagnostics) > _MAX_DIAGNOSTICS or len(envelope.artifacts) > _MAX_ARTIFACTS:
        raise KernformClientError(KernformClientFailureCode.INVALID_ENVELOPE)
    for diagnostic in envelope.diagnostics:
        if (
            not _DIAGNOSTIC_ID.fullmatch(diagnostic.id)
            or not diagnostic.message
            or len(diagnostic.message.encode("utf-8")) > _MAX_TOKEN_BYTES
        ):
            raise KernformClientError(KernformClientFailureCode.INVALID_ENVELOPE)
    for artifact in envelope.artifacts:
        if (
            not artifact.kind
            or not artifact.path
            or len(artifact.kind.encode("utf-8")) > _MAX_TOKEN_BYTES
            or len(artifact.path.encode("utf-8")) > _MAX_TOKEN_BYTES
            or (artifact.hash is not None and not _ARTIFACT_HASH.fullmatch(artifact.hash))
        ):
            raise KernformClientError(KernformClientFailureCode.INVALID_ENVELOPE)


def _confined_artifacts(
    artifacts: tuple[KernformWireArtifact, ...],
    project_root: Path,
) -> tuple[KernformArtifact, ...]:
    accepted: list[KernformArtifact] = []
    for artifact in artifacts:
        candidate = Path(artifact.path)
        if not candidate.is_absolute():
            candidate = project_root / candidate
        try:
            canonical = candidate.resolve(strict=False)
        except (OSError, RuntimeError) as error:
            raise KernformClientError(KernformClientFailureCode.ARTIFACT_OUTSIDE_ROOT) from error
        if not canonical.is_relative_to(project_root):
            raise KernformClientError(KernformClientFailureCode.ARTIFACT_OUTSIDE_ROOT)
        accepted.append(KernformArtifact(artifact.kind, str(canonical), artifact.hash))
    return tuple(accepted)


def _validated_command_result(
    envelope: KernformWireEnvelope,
    *,
    command: Literal["check", "init"],
    project_root: Path,
    artifacts: tuple[KernformArtifact, ...],
) -> dict[str, object] | None:
    if command == "check":
        return _validated_check_result(envelope, artifacts=artifacts)
    return _validated_init_result(
        envelope,
        project_root=project_root,
        artifacts=artifacts,
    )


def _validated_check_result(
    envelope: KernformWireEnvelope,
    *,
    artifacts: tuple[KernformArtifact, ...],
) -> dict[str, object] | None:
    if artifacts:
        raise KernformClientError(KernformClientFailureCode.INVALID_ENVELOPE)
    if envelope.result is None:
        if envelope.status == "success":
            raise KernformClientError(KernformClientFailureCode.INVALID_ENVELOPE)
        return None
    if envelope.status == "refused":
        raise KernformClientError(KernformClientFailureCode.INVALID_ENVELOPE)
    result = _convert_result(envelope.result, KernformWireCheckResult)
    if (
        result.conformant != (envelope.status == "success")
        or not _ARTIFACT_HASH.fullmatch(result.catalog_hash)
        or isinstance(result.files_checked, bool)
        or not 0 <= result.files_checked <= _MAX_FILES_CHECKED
    ):
        raise KernformClientError(KernformClientFailureCode.INVALID_ENVELOPE)
    _validate_requirements(result.requirements.conformance, diagnostic_ids=True)
    _validate_requirements(result.requirements.tests, diagnostic_ids=False)
    return _result_document(result)


def _validated_init_result(
    envelope: KernformWireEnvelope,
    *,
    project_root: Path,
    artifacts: tuple[KernformArtifact, ...],
) -> dict[str, object] | None:
    if envelope.status != "success":
        if envelope.result is not None or artifacts:
            raise KernformClientError(KernformClientFailureCode.INVALID_ENVELOPE)
        return None
    if envelope.result is None:
        raise KernformClientError(KernformClientFailureCode.INVALID_ENVELOPE)
    result = _convert_result(envelope.result, KernformWireInitResult)
    if (
        not _ARTIFACT_HASH.fullmatch(result.plan_id)
        or isinstance(result.operation_count, bool)
        or not 1 <= result.operation_count <= _MAX_OPERATIONS
    ):
        raise KernformClientError(KernformClientFailureCode.INVALID_ENVELOPE)
    state_path = _confined_result_path(result.state_path, project_root)
    evidence_path = _confined_result_path(result.evidence_path, project_root)
    artifact_paths: dict[str, str] = {}
    for artifact in artifacts:
        if artifact.kind not in {"managed-state", "apply-evidence"}:
            raise KernformClientError(KernformClientFailureCode.INVALID_ENVELOPE)
        if artifact.kind in artifact_paths:
            raise KernformClientError(KernformClientFailureCode.INVALID_ENVELOPE)
        artifact_paths[artifact.kind] = artifact.path
    if artifact_paths != {
        "managed-state": state_path,
        "apply-evidence": evidence_path,
    }:
        raise KernformClientError(KernformClientFailureCode.INVALID_ENVELOPE)
    return {
        "evidence_path": evidence_path,
        "operation_count": result.operation_count,
        "plan_id": result.plan_id,
        "state_path": state_path,
    }


def _convert_result[ResultT: msgspec.Struct](value: object, result_type: type[ResultT]) -> ResultT:
    try:
        return msgspec.convert(value, type=result_type, strict=True)
    except (msgspec.ValidationError, TypeError, ValueError) as error:
        raise KernformClientError(KernformClientFailureCode.INVALID_ENVELOPE) from error


def _result_document(value: msgspec.Struct) -> dict[str, object]:
    document = msgspec.json.decode(msgspec.json.encode(value))
    if not isinstance(document, dict) or any(not isinstance(key, str) for key in document):
        raise KernformClientError(KernformClientFailureCode.INVALID_ENVELOPE)
    return cast("dict[str, object]", document)


def _validate_requirements(values: tuple[str, ...], *, diagnostic_ids: bool) -> None:
    if len(values) > _MAX_REQUIREMENTS or tuple(sorted(set(values))) != values:
        raise KernformClientError(KernformClientFailureCode.INVALID_ENVELOPE)
    pattern = _DIAGNOSTIC_ID if diagnostic_ids else _TEST_REQUIREMENT_ID
    if any(pattern.fullmatch(value) is None for value in values):
        raise KernformClientError(KernformClientFailureCode.INVALID_ENVELOPE)


def _confined_result_path(value: str, project_root: Path) -> str:
    _require_token(value, code=KernformClientFailureCode.INVALID_ENVELOPE)
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = project_root / candidate
    try:
        canonical = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise KernformClientError(KernformClientFailureCode.ARTIFACT_OUTSIDE_ROOT) from error
    if not canonical.is_relative_to(project_root):
        raise KernformClientError(KernformClientFailureCode.ARTIFACT_OUTSIDE_ROOT)
    return str(canonical)


def _envelope_document(envelope: KernformWireEnvelope) -> dict[str, object]:
    return {
        "schema": envelope.schema,
        "command": envelope.command,
        "status": envelope.status,
        "exit_code": envelope.exit_code,
        "result": envelope.result,
        "diagnostics": [
            {
                "id": item.id,
                "severity": item.severity,
                "message": item.message,
                "context": item.context,
            }
            for item in envelope.diagnostics
        ],
        "artifacts": [
            {"kind": item.kind, "path": item.path, "hash": item.hash} for item in envelope.artifacts
        ],
    }


def _existing_project_root(value: Path) -> Path:
    if not isinstance(value, Path):
        raise KernformClientError(KernformClientFailureCode.INVALID_PROJECT_ROOT)
    try:
        root = value.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise KernformClientError(KernformClientFailureCode.INVALID_PROJECT_ROOT) from error
    if not root.is_dir():
        raise KernformClientError(KernformClientFailureCode.INVALID_PROJECT_ROOT)
    return root


def _initialization_root(value: Path) -> Path:
    if not isinstance(value, Path):
        raise KernformClientError(KernformClientFailureCode.INVALID_PROJECT_ROOT)
    try:
        root = value.resolve(strict=False)
        parent = root.parent.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise KernformClientError(KernformClientFailureCode.INVALID_PROJECT_ROOT) from error
    if root.parent != parent or root == root.parent or (root.exists() and not root.is_dir()):
        raise KernformClientError(KernformClientFailureCode.INVALID_PROJECT_ROOT)
    return root


def _require_token(value: object, *, code: KernformClientFailureCode) -> None:
    if not isinstance(value, str) or not value.strip():
        raise KernformClientError(code)
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise KernformClientError(code) from error
    if len(encoded) > _MAX_TOKEN_BYTES or any(
        ord(character) < 0x20 or ord(character) == 0x7F for character in value
    ):
        raise KernformClientError(code)
