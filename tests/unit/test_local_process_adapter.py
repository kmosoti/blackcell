from __future__ import annotations

import errno
import hashlib
import json
import sys
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from blackcell.adapters.execution.local_process import (
    LOCAL_PROCESS_ADAPTER_ID,
    ArgumentBinding,
    ArgumentKind,
    LocalProcessAdapter,
    LocalProcessAffordance,
    LocalProcessConfigurationError,
    LocalProcessRegistry,
    ProcessCompletion,
    ProcessRun,
    StreamCapture,
)
from blackcell.features.execute_affordance import (
    AffordanceArgument,
    AffordanceArgumentSpec,
    AffordanceDefinition,
    AffordanceInvocation,
    ExecutionResult,
    ExecutionStatus,
    SideEffectClass,
)
from blackcell.kernel import ArtifactStore, JsonScalar
from blackcell.kernel.errors import ArtifactIntegrityError

NOW = datetime(2026, 7, 11, 12, tzinfo=UTC)
EMPTY_DIGEST = f"sha256:{hashlib.sha256(b'').hexdigest()}"


def test_adapter_executes_exact_configuration_and_writes_verified_manifest(
    tmp_path: Path,
) -> None:
    executable = _script(
        tmp_path,
        "echo_argument",
        "import os, sys; os.write(1, sys.argv[1].encode()); os.write(2, b'notice')",
    )
    configuration = _configuration(
        tmp_path,
        executable,
        bindings=(ArgumentBinding("value", ArgumentKind.TEXT, "--value="),),
    )
    artifacts = ArtifactStore(tmp_path / "artifacts")
    adapter = LocalProcessAdapter(LocalProcessRegistry((configuration,)), artifacts)
    invocation = _invocation(AffordanceArgument("value", "hello world"))

    outcome = adapter.execute(invocation, configuration.definition)

    assert outcome.success
    assert outcome.error_code is None
    assert outcome.observed_effects == ()
    raw_manifest = artifacts.get_json(outcome.output_digest)
    assert isinstance(raw_manifest, dict)
    manifest = cast("dict[str, object]", raw_manifest)
    assert set(manifest) == {
        "schema_version",
        "adapter_id",
        "contract_version",
        "configuration_digest",
        "invocation_id",
        "proposal_id",
        "affordance",
        "action_digest",
        "command_digest",
        "started_at",
        "completed_at",
        "completion",
        "return_code",
        "signal_number",
        "spawn_errno",
        "stdout",
        "stderr",
        "observed_effects",
    }
    assert manifest["schema_version"] == "local-process-output/v1"
    assert manifest["adapter_id"] == LOCAL_PROCESS_ADAPTER_ID
    assert manifest["contract_version"] == adapter.contract_version
    assert manifest["configuration_digest"] == configuration.configuration_digest
    assert manifest["action_digest"] == invocation.action_digest
    assert manifest["observed_effects"] == []
    raw_stdout = manifest["stdout"]
    raw_stderr = manifest["stderr"]
    assert isinstance(raw_stdout, dict) and isinstance(raw_stderr, dict)
    stdout = cast("dict[str, object]", raw_stdout)
    stderr = cast("dict[str, object]", raw_stderr)
    assert artifacts.get_bytes(str(stdout["artifact_digest"])) == b"--value=hello world"
    assert artifacts.get_bytes(str(stderr["artifact_digest"])) == b"notice"
    assert stdout["content_digest"] == _digest(b"--value=hello world")
    assert stdout["total_bytes"] == 19
    assert stdout["eof"] is True


def test_shell_metacharacters_remain_one_literal_non_shell_token(tmp_path: Path) -> None:
    executable = _script(
        tmp_path,
        "literal_argument",
        "import json, os, sys; os.write(1, json.dumps(sys.argv[1:]).encode())",
    )
    configuration = _configuration(
        tmp_path,
        executable,
        bindings=(ArgumentBinding("value", ArgumentKind.TEXT, "--value="),),
    )
    artifacts = ArtifactStore(tmp_path / "artifacts")
    adapter = LocalProcessAdapter(LocalProcessRegistry((configuration,)), artifacts)
    marker = tmp_path / "must-not-exist"
    value = f"$(touch {marker});`touch {marker}`"

    outcome = adapter.execute(
        _invocation(AffordanceArgument("value", value)),
        configuration.definition,
    )

    raw_manifest = artifacts.get_json(outcome.output_digest)
    assert isinstance(raw_manifest, dict)
    manifest = cast("dict[str, object]", raw_manifest)
    raw_stdout = manifest["stdout"]
    assert isinstance(raw_stdout, dict)
    stdout = cast("dict[str, object]", raw_stdout)
    output = artifacts.get_bytes(str(stdout["artifact_digest"]))
    assert json.loads(output) == [f"--value={value}"]
    assert not marker.exists()


def test_path_bindings_are_canonical_confined_and_symlink_free(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    target = root / "input.txt"
    target.write_text("evidence", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    linked = root / "linked.txt"
    linked.symlink_to(target)
    executable = _script(
        root,
        "path_argument",
        "import os, sys; os.write(1, sys.argv[1].encode())",
    )
    configuration = _configuration(
        root,
        executable,
        bindings=(ArgumentBinding("value", ArgumentKind.PATH, "--path="),),
    )
    adapter = LocalProcessAdapter(
        LocalProcessRegistry((configuration,)), ArtifactStore(tmp_path / "artifacts")
    )

    accepted = adapter.execute(
        _invocation(AffordanceArgument("value", "input.txt")),
        configuration.definition,
    )
    assert accepted.success
    with pytest.raises(LocalProcessConfigurationError, match="symlink"):
        adapter.execute(
            _invocation(AffordanceArgument("value", "linked.txt")),
            configuration.definition,
        )
    with pytest.raises(LocalProcessConfigurationError, match="roots"):
        adapter.execute(
            _invocation(AffordanceArgument("value", str(outside))),
            configuration.definition,
        )
    with pytest.raises(LocalProcessConfigurationError, match="protected"):
        adapter.execute(
            _invocation(AffordanceArgument("value", "/etc/passwd")),
            configuration.definition,
        )


@pytest.mark.parametrize(
    "relative_path",
    (
        ".git/config",
        ".blackcell/state.db",
        ".codex/config.json",
        ".agents/identity",
        ".env",
        ".env.local",
        "credentials.json",
        "id_rsa",
        "id_rsa.pub",
        ".ssh/config",
        "access-token.txt",
        "service-private-key.pem",
    ),
)
def test_path_bindings_reject_sensitive_and_credential_like_components(
    tmp_path: Path,
    relative_path: str,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    target = root / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("sensitive", encoding="utf-8")
    executable = _script(root, "sensitive_path", "pass")
    configuration = _configuration(
        root,
        executable,
        bindings=(ArgumentBinding("value", ArgumentKind.PATH, "--path="),),
    )
    adapter = LocalProcessAdapter(
        LocalProcessRegistry((configuration,)), ArtifactStore(tmp_path / "artifacts")
    )

    with pytest.raises(LocalProcessConfigurationError, match="sensitive or credential"):
        adapter.execute(
            _invocation(AffordanceArgument("value", relative_path)),
            configuration.definition,
        )


def test_path_binding_rejects_group_or_world_writable_input(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    target = root / "input.txt"
    target.write_text("mutable", encoding="utf-8")
    target.chmod(0o666)
    executable = _script(root, "mutable_path", "pass")
    configuration = _configuration(
        root,
        executable,
        bindings=(ArgumentBinding("value", ArgumentKind.PATH, "--path="),),
    )
    adapter = LocalProcessAdapter(
        LocalProcessRegistry((configuration,)), ArtifactStore(tmp_path / "artifacts")
    )

    with pytest.raises(LocalProcessConfigurationError, match="group- or world-writable"):
        adapter.execute(
            _invocation(AffordanceArgument("value", "input.txt")),
            configuration.definition,
        )


@pytest.mark.parametrize(
    ("body", "error_code"),
    (
        ("raise SystemExit(9)", "process_exit_nonzero"),
        (
            "import os, signal; os.kill(os.getpid(), signal.SIGTERM)",
            "process_signaled",
        ),
        ("import time; time.sleep(60)", "process_timeout"),
    ),
)
def test_adapter_maps_known_terminal_failures_to_explicit_codes(
    tmp_path: Path,
    body: str,
    error_code: str,
) -> None:
    executable = _script(tmp_path, "failure", body)
    configuration = _configuration(
        tmp_path,
        executable,
        bindings=(),
        timeout=0.05 if error_code == "process_timeout" else 1.0,
    )
    adapter = LocalProcessAdapter(
        LocalProcessRegistry((configuration,)), ArtifactStore(tmp_path / "artifacts")
    )

    outcome = adapter.execute(_invocation(), configuration.definition)

    assert not outcome.success
    assert outcome.error_code == error_code
    assert outcome.output_digest.startswith("sha256:")
    assert outcome.observed_effects == ()


def test_adapter_requires_exact_developer_owned_definition_and_argument_types(
    tmp_path: Path,
) -> None:
    executable = _script(tmp_path, "strict", "pass")
    configuration = _configuration(
        tmp_path,
        executable,
        bindings=(ArgumentBinding("value", ArgumentKind.INTEGER, "--count="),),
    )
    adapter = LocalProcessAdapter(
        LocalProcessRegistry((configuration,)), ArtifactStore(tmp_path / "artifacts")
    )

    with pytest.raises(LocalProcessConfigurationError, match="developer-owned"):
        adapter.execute(
            _invocation(AffordanceArgument("value", 3)),
            replace(configuration.definition, timeout_seconds=2),
        )
    with pytest.raises(LocalProcessConfigurationError, match="integer"):
        adapter.execute(
            _invocation(AffordanceArgument("value", "3")),
            configuration.definition,
        )
    with pytest.raises(LocalProcessConfigurationError, match="differ"):
        adapter.execute(_invocation(), configuration.definition)


@pytest.mark.parametrize(
    ("kind", "value", "message"),
    (
        (ArgumentKind.TEXT, 1, "string"),
        (ArgumentKind.BOOLEAN, "true", "boolean"),
        (ArgumentKind.PATH, 1, "path string"),
        (ArgumentKind.TEXT, "", "empty token"),
        (ArgumentKind.TEXT, "line\nbreak", "control character"),
        (ArgumentKind.TEXT, "12345", "byte bound"),
    ),
)
def test_dynamic_binding_values_fail_closed_before_spawn(
    tmp_path: Path,
    kind: ArgumentKind,
    value: object,
    message: str,
) -> None:
    executable = _script(tmp_path, "binding", "raise SystemExit(99)")
    binding = ArgumentBinding("value", kind, "--value=", maximum_bytes=4)
    configuration = _configuration(tmp_path, executable, bindings=(binding,))
    adapter = LocalProcessAdapter(
        LocalProcessRegistry((configuration,)), ArtifactStore(tmp_path / "artifacts")
    )

    with pytest.raises(LocalProcessConfigurationError, match=message):
        adapter.execute(
            _invocation(AffordanceArgument("value", cast("JsonScalar", value))),
            configuration.definition,
        )


@pytest.mark.parametrize(
    ("run", "error_code"),
    (
        (
            ProcessRun(
                ProcessCompletion.SPAWN_FAILED,
                NOW,
                NOW,
                None,
                None,
                StreamCapture(b"", 0, EMPTY_DIGEST, True, False),
                StreamCapture(b"", 0, EMPTY_DIGEST, True, False),
                errno.EAGAIN,
            ),
            "process_spawn_failed",
        ),
        (
            ProcessRun(
                ProcessCompletion.OUTPUT_INCOMPLETE,
                NOW,
                NOW,
                0,
                None,
                StreamCapture(b"partial", None, None, False, True),
                StreamCapture(b"", 0, EMPTY_DIGEST, True, False),
            ),
            "process_output_incomplete",
        ),
    ),
)
def test_adapter_records_spawn_and_incomplete_output_manifests(
    tmp_path: Path,
    run: ProcessRun,
    error_code: str,
) -> None:
    executable = _script(tmp_path, "static", "raise SystemExit(99)")
    configuration = _configuration(tmp_path, executable, bindings=())
    adapter = LocalProcessAdapter(
        LocalProcessRegistry((configuration,)),
        ArtifactStore(tmp_path / "artifacts"),
        runner=_StaticRunner(run),
    )

    outcome = adapter.execute(_invocation(), configuration.definition)

    assert not outcome.success
    assert outcome.error_code == error_code


def test_reconcile_reexecutes_only_an_exact_unknown_read_only_execution(
    tmp_path: Path,
) -> None:
    executable = _script(tmp_path, "reconcile", "pass")
    configuration = _configuration(tmp_path, executable, bindings=())
    adapter = LocalProcessAdapter(
        LocalProcessRegistry((configuration,)), ArtifactStore(tmp_path / "artifacts")
    )
    invocation = _invocation()
    previous = _unknown_result(invocation)

    outcome = adapter.reconcile(invocation, configuration.definition, previous)

    assert outcome.success
    with pytest.raises(LocalProcessConfigurationError, match="differs"):
        adapter.reconcile(
            replace(invocation, invocation_id="invocation:other"),
            configuration.definition,
            previous,
        )
    with pytest.raises(LocalProcessConfigurationError, match="only an unknown"):
        adapter.reconcile(
            invocation,
            configuration.definition,
            replace(
                previous,
                status=ExecutionStatus.FAILED,
                output_digest=f"sha256:{'1' * 64}",
            ),
        )


def test_adapter_rejects_artifact_writer_digest_forgery(tmp_path: Path) -> None:
    executable = _script(tmp_path, "artifact", "pass")
    configuration = _configuration(tmp_path, executable, bindings=())
    adapter = LocalProcessAdapter(LocalProcessRegistry((configuration,)), _ForgedArtifacts())

    with pytest.raises(ArtifactIntegrityError, match="exact digest"):
        adapter.execute(_invocation(), configuration.definition)


@pytest.mark.parametrize(
    ("media_type", "encoding"),
    (
        ("text/plain", None),
        ("application/vnd.blackcell.local-process-stream", "utf-8"),
    ),
)
def test_adapter_rejects_preseeded_artifact_metadata_collision(
    tmp_path: Path,
    media_type: str,
    encoding: str | None,
) -> None:
    executable = _script(tmp_path, "artifact_collision", "pass")
    configuration = _configuration(tmp_path, executable, bindings=())
    artifacts = ArtifactStore(tmp_path / "artifacts")
    artifacts.put_bytes(b"", media_type=media_type, encoding=encoding)
    adapter = LocalProcessAdapter(LocalProcessRegistry((configuration,)), artifacts)

    with pytest.raises(ArtifactIntegrityError, match="media type, or encoding"):
        adapter.execute(_invocation(), configuration.definition)


def test_adapter_rejects_persisted_metadata_that_differs_from_returned_reference(
    tmp_path: Path,
) -> None:
    executable = _script(tmp_path, "artifact_stat", "pass")
    configuration = _configuration(tmp_path, executable, bindings=())
    artifacts = _DivergentMetadataArtifacts()
    adapter = LocalProcessAdapter(LocalProcessRegistry((configuration,)), artifacts)

    with pytest.raises(ArtifactIntegrityError, match="persisted artifact metadata"):
        adapter.execute(_invocation(), configuration.definition)


def test_adapter_rejects_artifact_store_readback_mismatch(tmp_path: Path) -> None:
    executable = _script(tmp_path, "artifact_readback", "pass")
    configuration = _configuration(tmp_path, executable, bindings=())
    adapter = LocalProcessAdapter(
        LocalProcessRegistry((configuration,)),
        _WrongReadbackArtifacts(),
    )

    with pytest.raises(ArtifactIntegrityError, match="verified readback"):
        adapter.execute(_invocation(), configuration.definition)


def _configuration(
    root: Path,
    executable: _TrustedProgram,
    *,
    bindings: tuple[ArgumentBinding, ...],
    timeout: float = 1.0,
) -> LocalProcessAffordance:
    arguments = tuple(AffordanceArgumentSpec(item.name) for item in bindings)
    return LocalProcessAffordance(
        definition=AffordanceDefinition(
            "probe",
            LOCAL_PROCESS_ADAPTER_ID,
            SideEffectClass.READ_ONLY,
            timeout,
            arguments,
        ),
        executable=str(executable.binary.resolve()),
        fixed_argv=("-I", "-S", "-c", f"exec({executable.body!r})"),
        bindings=bindings,
        working_directory=str(root.resolve()),
        allowed_path_roots=(str(root.resolve()),),
        stdout_limit_bytes=1024,
        stderr_limit_bytes=1024,
        termination_grace_seconds=0.1,
        drain_grace_seconds=0.1,
    )


def _invocation(*arguments: AffordanceArgument) -> AffordanceInvocation:
    return AffordanceInvocation(
        "invocation:1",
        "proposal:1",
        "probe",
        arguments,
        "idempotency:1",
        NOW,
    )


def _unknown_result(invocation: AffordanceInvocation) -> ExecutionResult:
    return ExecutionResult(
        invocation_id=invocation.invocation_id,
        proposal_id=invocation.proposal_id,
        authorization_decision_id="authorization:1",
        affordance=invocation.affordance,
        adapter_id=LOCAL_PROCESS_ADAPTER_ID,
        idempotency_key=invocation.idempotency_key,
        authorized_action_digest=invocation.action_digest,
        execution_identity_digest=f"sha256:{'2' * 64}",
        status=ExecutionStatus.UNKNOWN,
        started_at=NOW,
        completed_at=NOW,
        output_digest=None,
        observed_effects=(),
        error_code="outcome_unknown",
        reconciled=False,
    )


@dataclass(frozen=True, slots=True)
class _TrustedProgram:
    binary: Path
    body: str


def _script(root: Path, name: str, body: str) -> _TrustedProgram:
    del root, name
    return _TrustedProgram(Path(sys.executable).resolve(), body)


def _digest(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


@dataclass(frozen=True)
class _ForgedReference:
    digest: str = f"sha256:{'0' * 64}"
    size_bytes: int = 0
    media_type: str = "application/octet-stream"
    encoding: str | None = None


class _ForgedArtifacts:
    def put_bytes(
        self,
        data: bytes,
        *,
        media_type: str = "application/octet-stream",
        encoding: str | None = None,
    ) -> _ForgedReference:
        del data, media_type, encoding
        return _ForgedReference()

    def stat(self, digest: str) -> _ForgedReference:
        del digest
        return _ForgedReference()

    def get_bytes(self, digest: str, *, verify: bool = True) -> bytes:
        del digest, verify
        return b""


class _DivergentMetadataArtifacts:
    def __init__(self) -> None:
        self._data = b""
        self._reference = _ForgedReference()

    def put_bytes(
        self,
        data: bytes,
        *,
        media_type: str = "application/octet-stream",
        encoding: str | None = None,
    ) -> _ForgedReference:
        self._data = data
        self._reference = _ForgedReference(
            digest=_digest(data),
            size_bytes=len(data),
            media_type=media_type,
            encoding=encoding,
        )
        return self._reference

    def stat(self, digest: str) -> _ForgedReference:
        del digest
        return replace(self._reference, media_type="application/x-forged")

    def get_bytes(self, digest: str, *, verify: bool = True) -> bytes:
        del digest, verify
        return self._data


class _WrongReadbackArtifacts(_DivergentMetadataArtifacts):
    def stat(self, digest: str) -> _ForgedReference:
        del digest
        return self._reference

    def get_bytes(self, digest: str, *, verify: bool = True) -> bytes:
        del digest, verify
        return b"forged"


@dataclass(frozen=True, slots=True)
class _StaticRunner:
    result: ProcessRun

    def run(self, *_args: object, **_kwargs: object) -> ProcessRun:
        return self.result
