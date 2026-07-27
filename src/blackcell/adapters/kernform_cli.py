"""Pinned, bounded client for Kernform's public agent-mode CLI contract."""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol, cast

from blackcell.adapters.bounded_process import (
    BoundedProcessError,
    BoundedProcessFailureCode,
    BoundedProcessResult,
    BoundedProcessRunner,
    BoundedStreamCapture,
)
from blackcell.interfaces.http import (
    StrictStruct,
    WireContractError,
    contract_to_json_builtins,
    convert_contract,
    decode_contract,
)
from blackcell.interfaces.kernform_contracts import (
    KernformWireArtifact,
    KernformWireCheckResult,
    KernformWireCompileResult,
    KernformWireEnvelope,
    KernformWireInitResult,
    KernformWireStatus,
)
from blackcell.kernel._json import json_digest

SUPPORTED_KERNFORM_VERSION = "0.2.0"
KERNFORM_COMMAND_SCHEMA = "kernform.command/v2"
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
_MAX_FILES_CHECKED = 1_000_000
_MAX_OPERATIONS = 1_000_000
_DIAGNOSTIC_ID = re.compile(r"KF-[A-Z]+-[0-9]{3}\Z")
_ARTIFACT_HASH = re.compile(r"[0-9a-f]{64}\Z")
_PROJECT_NAME = re.compile(r"[a-z][a-z0-9-]{0,62}\Z")

KernformStatus = KernformWireStatus
KernformSignature = Literal["sdk", "cli", "api", "interactive-web", "daemon"]
_SIGNATURES = frozenset({"sdk", "cli", "api", "interactive-web", "daemon"})


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
    command: Literal["check", "compile", "init"]
    status: KernformStatus
    exit_code: int
    result: dict[str, object] | None
    diagnostics: tuple[KernformDiagnostic, ...]
    artifacts: tuple[KernformArtifact, ...]
    argv_digest: str
    result_digest: str
    schema_version: Literal["kernform-invocation/v2"] = "kernform-invocation/v2"


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

    def compile(self, form: Path) -> KernformInvocationResult:
        form_path = _existing_form_path(form)
        cwd = form_path.parent
        version = self._probe_version(cwd=cwd)
        argv = (
            self.executable,
            "--agent",
            "--format",
            "json",
            "compile",
            "--form",
            str(form_path),
        )
        return self._invoke(
            argv,
            command="compile",
            project_root=cwd,
            version=version,
            cwd=cwd,
        )

    def init(
        self,
        *,
        name: str,
        destination: Path,
        signatures: Sequence[KernformSignature] = ("sdk",),
        default_signature: KernformSignature | None = None,
        capabilities: Sequence[str] = (),
        no_git: bool = False,
        initial_commit: bool = False,
    ) -> KernformInvocationResult:
        _require_token(name, code=KernformClientFailureCode.INVALID_ARGUMENT)
        if isinstance(signatures, str | bytes | bytearray) or not isinstance(signatures, Sequence):
            raise KernformClientError(KernformClientFailureCode.INVALID_ARGUMENT)
        normalized_signatures = tuple(signatures)
        if (
            not normalized_signatures
            or len(normalized_signatures) != len(set(normalized_signatures))
            or any(signature not in _SIGNATURES for signature in normalized_signatures)
            or (default_signature is not None and default_signature not in normalized_signatures)
        ):
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
        ]
        for signature in normalized_signatures:
            tokens.extend(("--signature", signature))
        if default_signature is not None:
            tokens.extend(("--default-signature", default_signature))
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
        command: Literal["check", "compile", "init"],
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
    command: Literal["check", "compile", "init"],
    project_root: Path,
    artifacts: tuple[KernformArtifact, ...],
) -> dict[str, object] | None:
    if command == "check":
        return _validated_check_result(envelope, artifacts=artifacts)
    if command == "compile":
        return _validated_compile_result(envelope, artifacts=artifacts)
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
    if envelope.status == "failure":
        return _plain_result_object(envelope.result)
    result = _convert_result(envelope.result, KernformWireCheckResult)
    source_shape = (
        result.conformant
        and result.mode == "source-repository"
        and result.catalog_hash is not None
        and _ARTIFACT_HASH.fullmatch(result.catalog_hash) is not None
        and result.files_checked is None
        and result.legacy_schema is None
        and result.migration_required is None
        and not result.mapped_signatures
        and result.managed_state is None
    )
    managed_shape = (
        result.conformant
        and result.mode is None
        and result.catalog_hash is None
        and result.files_checked is not None
        and not isinstance(result.files_checked, bool)
        and 0 <= result.files_checked <= _MAX_FILES_CHECKED
        and result.legacy_schema is None
        and result.migration_required is None
        and not result.mapped_signatures
        and result.managed_state is None
    )
    legacy_shape = (
        result.mode is None
        and result.catalog_hash is None
        and result.legacy_schema == "kernform/v1"
        and result.migration_required is True
        and bool(result.mapped_signatures)
        and len(set(result.mapped_signatures)) == len(result.mapped_signatures)
        and (
            (result.managed_state is False and result.files_checked is None)
            or (
                result.managed_state is None
                and result.files_checked is not None
                and not isinstance(result.files_checked, bool)
                and 0 <= result.files_checked <= _MAX_FILES_CHECKED
            )
        )
    )
    if not (source_shape or managed_shape or legacy_shape):
        raise KernformClientError(KernformClientFailureCode.INVALID_ENVELOPE)
    return _plain_result_object(envelope.result)


def _validated_compile_result(
    envelope: KernformWireEnvelope,
    *,
    artifacts: tuple[KernformArtifact, ...],
) -> dict[str, object] | None:
    if envelope.status != "success" or artifacts or envelope.result is None:
        raise KernformClientError(KernformClientFailureCode.INVALID_ENVELOPE)
    result = _convert_result(envelope.result, KernformWireCompileResult)
    intent = result.intent
    operation_ids: list[str] = []
    if (
        result.generator_version != SUPPORTED_KERNFORM_VERSION
        or not _ARTIFACT_HASH.fullmatch(result.plan_id)
        or not _ARTIFACT_HASH.fullmatch(result.catalog.hash)
        or not _PROJECT_NAME.fullmatch(intent.name)
        or not intent.requested_signatures
        or len(set(intent.requested_signatures)) != len(intent.requested_signatures)
        or not intent.resolved_signatures
        or len(set(intent.resolved_signatures)) != len(intent.resolved_signatures)
        or not set(intent.requested_signatures).issubset(intent.resolved_signatures)
        or (
            intent.default_signature is not None
            and intent.default_signature not in intent.requested_signatures
        )
        or tuple(sorted(set(intent.capabilities))) != intent.capabilities
        or len(result.operations) > _MAX_OPERATIONS
        or len(result.diagnostics) > _MAX_DIAGNOSTICS
    ):
        raise KernformClientError(KernformClientFailureCode.INVALID_ENVELOPE)
    for operation in result.operations:
        operation_id = operation.get("id")
        kind = operation.get("kind")
        if (
            not isinstance(operation_id, str)
            or not operation_id
            or not isinstance(kind, str)
            or not kind
        ):
            raise KernformClientError(KernformClientFailureCode.INVALID_ENVELOPE)
        operation_ids.append(operation_id)
        for key in ("path", "cwd"):
            value = operation.get(key)
            if value is not None and not _safe_relative_path(value):
                raise KernformClientError(KernformClientFailureCode.INVALID_ENVELOPE)
    if len(set(operation_ids)) != len(operation_ids):
        raise KernformClientError(KernformClientFailureCode.INVALID_ENVELOPE)
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
    artifact_paths: dict[str, str] = {}
    for artifact in artifacts:
        if artifact.kind != "managed-state":
            raise KernformClientError(KernformClientFailureCode.INVALID_ENVELOPE)
        if artifact.kind in artifact_paths:
            raise KernformClientError(KernformClientFailureCode.INVALID_ENVELOPE)
        artifact_paths[artifact.kind] = artifact.path
    if artifact_paths != {"managed-state": state_path}:
        raise KernformClientError(KernformClientFailureCode.INVALID_ENVELOPE)
    return {
        "operation_count": result.operation_count,
        "plan_id": result.plan_id,
        "state_path": state_path,
    }


def _convert_result[ResultT: StrictStruct](
    value: object,
    result_type: type[ResultT],
) -> ResultT:
    try:
        return convert_contract(value, result_type)
    except WireContractError as error:
        raise KernformClientError(KernformClientFailureCode.INVALID_ENVELOPE) from error


def _result_document(value: StrictStruct) -> dict[str, object]:
    document = contract_to_json_builtins(value)
    if not isinstance(document, dict) or any(not isinstance(key, str) for key in document):
        raise KernformClientError(KernformClientFailureCode.INVALID_ENVELOPE)
    return cast("dict[str, object]", document)


def _plain_result_object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise KernformClientError(KernformClientFailureCode.INVALID_ENVELOPE)
    return cast("dict[str, object]", value)


def _safe_relative_path(value: object) -> bool:
    if not isinstance(value, str) or not value or "\x00" in value:
        return False
    path = Path(value)
    return not path.is_absolute() and ".." not in path.parts


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


def _existing_form_path(value: Path) -> Path:
    if not isinstance(value, Path):
        raise KernformClientError(KernformClientFailureCode.INVALID_ARGUMENT)
    try:
        form = value.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise KernformClientError(KernformClientFailureCode.INVALID_ARGUMENT) from error
    if not form.is_file():
        raise KernformClientError(KernformClientFailureCode.INVALID_ARGUMENT)
    return form


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
