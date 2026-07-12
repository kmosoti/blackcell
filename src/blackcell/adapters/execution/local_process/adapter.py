"""Adapter bridge for trusted commands; no containment or read-only enforcement is implied."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from blackcell.adapters.execution.local_process.configuration import (
    LOCAL_PROCESS_ADAPTER_ID,
    ArgumentBinding,
    ArgumentKind,
    LocalProcessAffordance,
    LocalProcessConfigurationError,
    LocalProcessRegistry,
    canonical_existing_path,
    path_is_protected,
    path_is_within,
    require_supported_platform,
    require_trusted_path_permissions,
)
from blackcell.adapters.execution.local_process.runner import (
    LocalProcessRunner,
    ProcessCompletion,
    ProcessRun,
    StreamCapture,
)
from blackcell.features.execute_affordance import (
    AdapterOutcome,
    AffordanceDefinition,
    AffordanceInvocation,
    ExecutionResult,
    ExecutionStatus,
)
from blackcell.kernel._json import bytes_digest, canonical_json_bytes, json_digest
from blackcell.kernel.errors import ArtifactIntegrityError

_OUTPUT_SCHEMA = "local-process-output/v1"
_SENSITIVE_PATH_COMPONENTS = frozenset(
    {
        ".agents",
        ".aws",
        ".blackcell",
        ".codex",
        ".git",
        ".gnupg",
        ".kube",
        ".netrc",
        ".npmrc",
        ".pypirc",
        ".ssh",
        "authorized_keys",
        "credentials",
        "credentials.json",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
        "kubeconfig",
        "known_hosts",
        "service-account.json",
    }
)
_SENSITIVE_PATH_FRAGMENTS = (
    "access-key",
    "access_key",
    "api-key",
    "api_key",
    "client-secret",
    "credential",
    "password",
    "private-key",
    "secret",
    "token",
)
_SENSITIVE_PATH_PREFIXES = ("id_dsa", "id_ecdsa", "id_ed25519", "id_rsa")
_SENSITIVE_PATH_SUFFIXES = (".key", ".kdbx", ".p12", ".pem", ".pfx")


class ArtifactReferenceLike(Protocol):
    @property
    def digest(self) -> str: ...

    @property
    def size_bytes(self) -> int: ...

    @property
    def media_type(self) -> str: ...

    @property
    def encoding(self) -> str | None: ...


class ArtifactWriter(Protocol):
    def put_bytes(
        self,
        data: bytes,
        *,
        media_type: str = "application/octet-stream",
        encoding: str | None = None,
    ) -> ArtifactReferenceLike: ...

    def stat(self, digest: str) -> ArtifactReferenceLike: ...

    def get_bytes(self, digest: str, *, verify: bool = True) -> bytes: ...


class ProcessRunner(Protocol):
    def run(
        self,
        configuration: LocalProcessAffordance,
        argv: tuple[str, ...],
        environment: Mapping[str, str],
    ) -> ProcessRun: ...


class LocalProcessAdapter:
    """Bridge audited read-only declarations to the trusted-command runner.

    This class does not enforce read-only behavior and is not an isolation boundary.
    """

    def __init__(
        self,
        registry: LocalProcessRegistry,
        artifacts: ArtifactWriter,
        *,
        runner: ProcessRunner | None = None,
    ) -> None:
        require_supported_platform()
        self._registry = registry
        self._artifacts = artifacts
        self._runner = runner or LocalProcessRunner()

    @property
    def adapter_id(self) -> str:
        return LOCAL_PROCESS_ADAPTER_ID

    @property
    def contract_version(self) -> str:
        return self._registry.contract_version

    def execute(
        self,
        invocation: AffordanceInvocation,
        definition: AffordanceDefinition,
    ) -> AdapterOutcome:
        configuration = self._configuration_for(invocation, definition)
        return self._run(invocation, configuration)

    def reconcile(
        self,
        invocation: AffordanceInvocation,
        definition: AffordanceDefinition,
        previous: ExecutionResult | None,
    ) -> AdapterOutcome:
        configuration = self._configuration_for(invocation, definition)
        if previous is not None:
            if previous.status is not ExecutionStatus.UNKNOWN:
                raise LocalProcessConfigurationError("only an unknown execution may be reconciled")
            expected = {
                "invocation_id": invocation.invocation_id,
                "proposal_id": invocation.proposal_id,
                "affordance": invocation.affordance,
                "adapter_id": definition.adapter_id,
                "idempotency_key": invocation.idempotency_key,
                "authorized_action_digest": invocation.action_digest,
            }
            mismatched = tuple(
                name for name, value in expected.items() if getattr(previous, name) != value
            )
            if mismatched:
                raise LocalProcessConfigurationError(
                    f"previous execution differs from reconciliation input: {mismatched}"
                )
        return self._run(invocation, configuration)

    def _configuration_for(
        self,
        invocation: AffordanceInvocation,
        definition: AffordanceDefinition,
    ) -> LocalProcessAffordance:
        if invocation.affordance != definition.name:
            raise LocalProcessConfigurationError(
                "invocation affordance differs from its definition"
            )
        configuration = self._registry.get(invocation.affordance)
        if definition != configuration.definition:
            raise LocalProcessConfigurationError(
                "runtime definition differs from the developer-owned configuration"
            )
        return configuration

    def _run(
        self,
        invocation: AffordanceInvocation,
        configuration: LocalProcessAffordance,
    ) -> AdapterOutcome:
        argv = _build_argv(invocation, configuration)
        environment = {item.name: item.value for item in configuration.environment}
        process_run = self._runner.run(configuration, argv, environment)
        stdout_reference = _put_verified(
            self._artifacts,
            process_run.stdout.captured,
            media_type="application/vnd.blackcell.local-process-stream",
        )
        stderr_reference = _put_verified(
            self._artifacts,
            process_run.stderr.captured,
            media_type="application/vnd.blackcell.local-process-stream",
        )
        manifest = _output_manifest(
            invocation,
            configuration,
            contract_version=self.contract_version,
            argv=argv,
            environment=environment,
            process_run=process_run,
            stdout_digest=stdout_reference.digest,
            stderr_digest=stderr_reference.digest,
        )
        manifest_bytes = canonical_json_bytes(manifest)
        manifest_reference = _put_verified(
            self._artifacts,
            manifest_bytes,
            media_type="application/vnd.blackcell.local-process-output+json",
            encoding="utf-8",
        )
        success, error_code = _terminal_status(process_run)
        return AdapterOutcome(
            success=success,
            output_digest=manifest_reference.digest,
            completed_at=process_run.completed_at,
            observed_effects=(),
            error_code=error_code,
        )


def _build_argv(
    invocation: AffordanceInvocation,
    configuration: LocalProcessAffordance,
) -> tuple[str, ...]:
    arguments = {item.name: item.value for item in invocation.arguments}
    expected = tuple(item.name for item in configuration.bindings)
    if set(arguments) != set(expected):
        unexpected = tuple(sorted(set(arguments) - set(expected)))
        missing = tuple(sorted(set(expected) - set(arguments)))
        raise LocalProcessConfigurationError(
            f"invocation arguments differ from configured bindings; "
            f"unexpected={unexpected}, missing={missing}"
        )
    dynamic = tuple(
        _binding_token(binding, arguments[binding.name], configuration)
        for binding in configuration.bindings
    )
    return (configuration.executable, *configuration.fixed_argv, *dynamic)


def _binding_token(
    binding: ArgumentBinding,
    value: object,
    configuration: LocalProcessAffordance,
) -> str:
    if binding.kind is ArgumentKind.TEXT:
        if not isinstance(value, str):
            raise LocalProcessConfigurationError(f"argument {binding.name!r} must be a string")
        encoded = value
    elif binding.kind is ArgumentKind.INTEGER:
        if isinstance(value, bool) or not isinstance(value, int):
            raise LocalProcessConfigurationError(f"argument {binding.name!r} must be an integer")
        encoded = str(value)
    elif binding.kind is ArgumentKind.BOOLEAN:
        if not isinstance(value, bool):
            raise LocalProcessConfigurationError(f"argument {binding.name!r} must be a boolean")
        encoded = "true" if value else "false"
    elif binding.kind is ArgumentKind.PATH:
        if not isinstance(value, str):
            raise LocalProcessConfigurationError(f"argument {binding.name!r} must be a path string")
        encoded = _confined_path(value, configuration)
    else:  # pragma: no cover - ArgumentBinding validates the enum
        raise LocalProcessConfigurationError("unsupported argument binding kind")
    _validate_dynamic_value(encoded, binding)
    return f"{binding.option_prefix or ''}{encoded}"


def _confined_path(value: str, configuration: LocalProcessAffordance) -> str:
    if not value or "\x00" in value:
        raise LocalProcessConfigurationError("path argument is empty or contains a null byte")
    supplied = Path(value)
    candidate = (
        supplied if supplied.is_absolute() else Path(configuration.working_directory) / supplied
    )
    try:
        canonical = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise LocalProcessConfigurationError(
            "path argument does not resolve canonically"
        ) from error
    if candidate != canonical:
        raise LocalProcessConfigurationError(
            "path argument must be canonical and contain no symlink components"
        )
    if _contains_sensitive_component(canonical):
        raise LocalProcessConfigurationError(
            "proposal-controlled path uses a sensitive or credential-like component"
        )
    canonical_existing_path(
        str(canonical),
        label="path argument",
        kind="file" if canonical.is_file() else "directory",
    )
    require_trusted_path_permissions(canonical, label="proposal-controlled path")
    if path_is_protected(canonical):
        raise LocalProcessConfigurationError("path argument uses a protected path")
    roots = tuple(Path(item) for item in configuration.allowed_path_roots)
    if not any(path_is_within(canonical, root) for root in roots):
        raise LocalProcessConfigurationError("path argument escapes its configured roots")
    return str(canonical)


def _validate_dynamic_value(value: str, binding: ArgumentBinding) -> None:
    if not value:
        raise LocalProcessConfigurationError(
            f"argument {binding.name!r} must not encode to an empty token"
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise LocalProcessConfigurationError(
            f"argument {binding.name!r} contains a control character"
        )
    if len(value.encode("utf-8")) > binding.maximum_bytes:
        raise LocalProcessConfigurationError(
            f"argument {binding.name!r} exceeds its configured byte bound"
        )


def _terminal_status(process_run: ProcessRun) -> tuple[bool, str | None]:
    if (
        process_run.completion is ProcessCompletion.EXITED
        and process_run.return_code == 0
        and process_run.stdout.eof
        and process_run.stderr.eof
    ):
        return True, None
    if process_run.completion is ProcessCompletion.SPAWN_FAILED:
        return False, "process_spawn_failed"
    if process_run.completion is ProcessCompletion.TIMED_OUT:
        return False, "process_timeout"
    if process_run.completion is ProcessCompletion.SIGNALED:
        return False, "process_signaled"
    if process_run.completion is ProcessCompletion.OUTPUT_INCOMPLETE:
        return False, "process_output_incomplete"
    if process_run.completion is ProcessCompletion.LINGERING_PROCESS:
        return False, "process_lingering_descendant"
    return False, "process_exit_nonzero"


def _output_manifest(
    invocation: AffordanceInvocation,
    configuration: LocalProcessAffordance,
    *,
    contract_version: str,
    argv: tuple[str, ...],
    environment: Mapping[str, str],
    process_run: ProcessRun,
    stdout_digest: str,
    stderr_digest: str,
) -> dict[str, object]:
    return {
        "schema_version": _OUTPUT_SCHEMA,
        "adapter_id": LOCAL_PROCESS_ADAPTER_ID,
        "contract_version": contract_version,
        "configuration_digest": configuration.configuration_digest,
        "invocation_id": invocation.invocation_id,
        "proposal_id": invocation.proposal_id,
        "affordance": invocation.affordance,
        "action_digest": invocation.action_digest,
        "command_digest": json_digest(
            {
                "argv": list(argv),
                "environment": [
                    {"name": name, "value": environment[name]} for name in sorted(environment)
                ],
                "working_directory": configuration.working_directory,
            }
        ),
        "started_at": process_run.started_at.isoformat(),
        "completed_at": process_run.completed_at.isoformat(),
        "completion": process_run.completion.value,
        "return_code": process_run.return_code,
        "signal_number": process_run.signal_number,
        "spawn_errno": process_run.spawn_errno,
        "stdout": _stream_manifest(
            process_run.stdout,
            artifact_digest=stdout_digest,
            capture_limit=configuration.stdout_limit_bytes,
        ),
        "stderr": _stream_manifest(
            process_run.stderr,
            artifact_digest=stderr_digest,
            capture_limit=configuration.stderr_limit_bytes,
        ),
        "observed_effects": [],
    }


def _stream_manifest(
    stream: StreamCapture,
    *,
    artifact_digest: str,
    capture_limit: int,
) -> dict[str, object]:
    return {
        "artifact_digest": artifact_digest,
        "captured_bytes": len(stream.captured),
        "capture_limit_bytes": capture_limit,
        "total_bytes": stream.total_bytes,
        "content_digest": stream.content_digest,
        "eof": stream.eof,
        "truncated": stream.truncated,
    }


def _put_verified(
    artifacts: ArtifactWriter,
    data: bytes,
    *,
    media_type: str,
    encoding: str | None = None,
) -> ArtifactReferenceLike:
    reference = artifacts.put_bytes(data, media_type=media_type, encoding=encoding)
    digest = bytes_digest(data)
    _require_exact_artifact_metadata(
        reference,
        digest=digest,
        size_bytes=len(data),
        media_type=media_type,
        encoding=encoding,
        label="artifact writer reference",
    )
    persisted = artifacts.stat(digest)
    _require_exact_artifact_metadata(
        persisted,
        digest=digest,
        size_bytes=len(data),
        media_type=media_type,
        encoding=encoding,
        label="persisted artifact metadata",
    )
    stored = artifacts.get_bytes(digest, verify=True)
    if stored != data:
        raise ArtifactIntegrityError("artifact writer failed verified readback")
    return reference


def _require_exact_artifact_metadata(
    reference: ArtifactReferenceLike,
    *,
    digest: str,
    size_bytes: int,
    media_type: str,
    encoding: str | None,
    label: str,
) -> None:
    try:
        actual = (
            reference.digest,
            reference.size_bytes,
            reference.media_type,
            reference.encoding,
        )
    except (AttributeError, TypeError) as error:
        raise ArtifactIntegrityError(f"{label} is invalid") from error
    expected = (digest, size_bytes, media_type, encoding)
    if actual != expected:
        raise ArtifactIntegrityError(
            f"{label} differs from exact digest, size, media type, or encoding"
        )


def _contains_sensitive_component(path: Path) -> bool:
    for component in path.parts:
        normalized = component.casefold()
        if normalized in _SENSITIVE_PATH_COMPONENTS or normalized.startswith(".env"):
            return True
        if normalized.startswith(_SENSITIVE_PATH_PREFIXES):
            return True
        if normalized.endswith(_SENSITIVE_PATH_SUFFIXES):
            return True
        if any(fragment in normalized for fragment in _SENSITIVE_PATH_FRAGMENTS):
            return True
    return False
