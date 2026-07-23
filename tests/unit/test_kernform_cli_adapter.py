from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

from blackcell.adapters.kernform_cli import (
    KERNFORM_COMMAND_SCHEMA,
    SUPPORTED_KERNFORM_VERSION,
    KernformCliClient,
    KernformClientError,
    KernformClientFailureCode,
    KernformProcessResult,
    KernformStreamCapture,
    SubprocessKernformTransport,
)


@dataclass(frozen=True, slots=True)
class RecordedCall:
    argv: tuple[str, ...]
    cwd: Path
    timeout_seconds: float
    stdout_limit_bytes: int
    stderr_limit_bytes: int


class FakeTransport:
    def __init__(self, *results: KernformProcessResult | KernformClientError) -> None:
        self.results = list(results)
        self.calls: list[RecordedCall] = []

    def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        timeout_seconds: float,
        stdout_limit_bytes: int,
        stderr_limit_bytes: int,
    ) -> KernformProcessResult:
        self.calls.append(
            RecordedCall(
                argv,
                cwd,
                timeout_seconds,
                stdout_limit_bytes,
                stderr_limit_bytes,
            )
        )
        result = self.results.pop(0)
        if isinstance(result, KernformClientError):
            raise result
        return result


def test_check_probes_version_invokes_agent_json_and_retains_evidence(tmp_path: Path) -> None:
    check_document = _envelope(
        command="check",
        status="failure",
        exit_code=2,
        result=None,
        diagnostics=[
            {
                "id": "KF-STATE-001",
                "severity": "error",
                "message": "managed state is missing",
                "context": {"path": str(tmp_path / ".kernform/state.json")},
            }
        ],
    )
    transport = FakeTransport(_process(_version_envelope()), _process(check_document, code=2))
    client = KernformCliClient(executable="kernform-recorded", transport=transport)

    result = client.check(tmp_path)

    expected_argv = (
        "kernform-recorded",
        "--agent",
        "--format",
        "json",
        "check",
        str(tmp_path),
    )
    assert [call.argv for call in transport.calls] == [
        ("kernform-recorded", "--agent", "--version"),
        expected_argv,
    ]
    assert all(call.cwd == tmp_path for call in transport.calls)
    assert all(call.timeout_seconds == 15.0 for call in transport.calls)
    assert result.kernform_version == SUPPORTED_KERNFORM_VERSION
    assert result.project_root == tmp_path
    assert result.command == "check"
    assert result.status == "failure"
    assert result.exit_code == 2
    assert result.result is None
    assert result.diagnostics[0].id == "KF-STATE-001"
    assert result.artifacts == ()
    assert result.argv_digest.startswith("sha256:") and len(result.argv_digest) == 71
    assert result.result_digest.startswith("sha256:") and len(result.result_digest) == 71
    assert result.schema_version == "kernform-invocation/v1"
    assert not hasattr(result, "stdout")


def test_init_builds_exact_argv_and_canonicalizes_in_root_artifacts(tmp_path: Path) -> None:
    destination = tmp_path / "new-project"
    state_path = destination / ".kernform/state.json"
    evidence_path = destination / ".kernform/evidence/apply.json"
    init_document = _envelope(
        command="init",
        status="success",
        exit_code=0,
        result=_init_result(destination, operation_count=7),
        artifacts=[
            {"kind": "managed-state", "path": ".kernform/state.json", "hash": None},
            {"kind": "apply-evidence", "path": str(evidence_path), "hash": "a" * 64},
        ],
    )
    transport = FakeTransport(_process(_version_envelope()), _process(init_document))
    client = KernformCliClient(executable="kernform-recorded", transport=transport)

    result = client.init(
        name="alpha-tool",
        destination=destination,
        profile="cli",
        capabilities=("lint", "test"),
        no_git=True,
    )

    assert [call.argv for call in transport.calls] == [
        ("kernform-recorded", "--agent", "--version"),
        (
            "kernform-recorded",
            "--agent",
            "--format",
            "json",
            "init",
            "alpha-tool",
            "--destination",
            str(destination),
            "--profile",
            "cli",
            "--with",
            "lint",
            "--with",
            "test",
            "--no-git",
        ),
    ]
    assert all(call.cwd == tmp_path for call in transport.calls)
    assert tuple(item.path for item in result.artifacts) == (str(state_path), str(evidence_path))
    assert result.artifacts[1].hash == "a" * 64
    assert result.result == {
        "evidence_path": str(evidence_path),
        "operation_count": 7,
        "plan_id": "b" * 64,
        "state_path": str(state_path),
    }


def test_client_rejects_open_or_semantically_invalid_command_results(tmp_path: Path) -> None:
    check_result = _check_result()
    accepted = KernformCliClient(
        transport=FakeTransport(
            _process(_version_envelope()),
            _process(_envelope(command="check", result=check_result)),
        )
    ).check(tmp_path)
    assert accepted.result == check_result

    failed_result = _check_result()
    failed_result["conformant"] = False
    failed = KernformCliClient(
        transport=FakeTransport(
            _process(_version_envelope()),
            _process(
                _envelope(
                    command="check",
                    status="failure",
                    exit_code=2,
                    result=failed_result,
                    diagnostics=[
                        {
                            "id": "KF-STATE-001",
                            "severity": "error",
                            "message": "managed state differs",
                            "context": {},
                        }
                    ],
                ),
                code=2,
            ),
        )
    ).check(tmp_path)
    assert failed.status == "failure"
    assert failed.exit_code == 2
    assert failed.result == failed_result

    refused = KernformCliClient(
        transport=FakeTransport(
            _process(_version_envelope()),
            _process(
                _envelope(
                    command="check",
                    status="refused",
                    exit_code=5,
                    diagnostics=[
                        {
                            "id": "KF-BOUNDARY-001",
                            "severity": "error",
                            "message": "project policy refused the check",
                            "context": {},
                        }
                    ],
                ),
                code=5,
            ),
        )
    ).check(tmp_path)
    assert refused.status == "refused"
    assert refused.exit_code == 5
    assert refused.result is None

    invalid_check_results = (
        {**_check_result(), "unexpected": True},
        {**_check_result(), "catalog_hash": "not-a-catalog-hash"},
        {**_check_result(), "conformant": False},
        {
            **_check_result(),
            "requirements": {"conformance": ["KF-ARCH-001"], "tests": ["fast", "fast"]},
        },
    )
    for invalid_result in invalid_check_results:
        transport = FakeTransport(
            _process(_version_envelope()),
            _process(_envelope(command="check", result=invalid_result)),
        )
        with pytest.raises(KernformClientError) as invalid_check:
            KernformCliClient(transport=transport).check(tmp_path)
        assert invalid_check.value.code is KernformClientFailureCode.INVALID_ENVELOPE

    destination = tmp_path / "new-project"
    state_path = destination / ".kernform/state.json"
    evidence_path = destination / ".kernform/evidence/apply.json"
    artifacts: list[dict[str, object]] = [
        {"kind": "managed-state", "path": str(state_path), "hash": None},
        {"kind": "apply-evidence", "path": str(evidence_path), "hash": None},
    ]
    invalid_init_results = (
        {**_init_result(destination), "unexpected": True},
        {**_init_result(destination), "state_path": str(destination / "other-state.json")},
        {**_init_result(destination), "operation_count": 0},
    )
    for invalid_result in invalid_init_results:
        transport = FakeTransport(
            _process(_version_envelope()),
            _process(_envelope(command="init", result=invalid_result, artifacts=artifacts)),
        )
        with pytest.raises(KernformClientError) as invalid_init:
            KernformCliClient(transport=transport).init(
                name="alpha-tool",
                destination=destination,
                no_git=True,
            )
        assert invalid_init.value.code is KernformClientFailureCode.INVALID_ENVELOPE

    escaped_result = {
        **_init_result(destination),
        "evidence_path": str(tmp_path.parent / "escaped-evidence.json"),
    }
    escaped_transport = FakeTransport(
        _process(_version_envelope()),
        _process(_envelope(command="init", result=escaped_result, artifacts=artifacts)),
    )
    with pytest.raises(KernformClientError) as escaped:
        KernformCliClient(transport=escaped_transport).init(
            name="alpha-tool",
            destination=destination,
            no_git=True,
        )
    assert escaped.value.code is KernformClientFailureCode.ARTIFACT_OUTSIDE_ROOT


def test_client_rejects_unsupported_version_closed_envelope_and_exit_mismatch(
    tmp_path: Path,
) -> None:
    unsupported = FakeTransport(
        _process(_envelope(command="version", result="0.2.0")),
    )
    with pytest.raises(KernformClientError) as wrong_version:
        KernformCliClient(transport=unsupported).check(tmp_path)
    assert wrong_version.value.code is KernformClientFailureCode.UNSUPPORTED_VERSION

    unknown_field = _envelope(command="check")
    unknown_field["unexpected"] = True
    malformed = FakeTransport(_process(_version_envelope()), _process(unknown_field))
    with pytest.raises(KernformClientError) as invalid:
        KernformCliClient(transport=malformed).check(tmp_path)
    assert invalid.value.code is KernformClientFailureCode.INVALID_ENVELOPE

    mismatched = FakeTransport(
        _process(_version_envelope()),
        _process(_envelope(command="check"), code=2),
    )
    with pytest.raises(KernformClientError) as wrong_exit:
        KernformCliClient(transport=mismatched).check(tmp_path)
    assert wrong_exit.value.code is KernformClientFailureCode.EXIT_MISMATCH


def test_client_rejects_incomplete_oversized_or_stderr_output(tmp_path: Path) -> None:
    cases = (
        (
            KernformProcessResult(
                0,
                KernformStreamCapture(b"{}", None, False),
                _capture(b""),
            ),
            KernformClientFailureCode.OUTPUT_INCOMPLETE,
        ),
        (
            KernformProcessResult(
                0,
                KernformStreamCapture(b"{}", 1024 * 1024 + 1, True),
                _capture(b""),
            ),
            KernformClientFailureCode.OUTPUT_TOO_LARGE,
        ),
        (
            KernformProcessResult(
                0,
                _capture(_json_bytes(_version_envelope())),
                _capture(b"ambient warning"),
            ),
            KernformClientFailureCode.INVALID_ENVELOPE,
        ),
    )
    for response, expected_code in cases:
        with pytest.raises(KernformClientError) as caught:
            KernformCliClient(transport=FakeTransport(response)).check(tmp_path)
        assert caught.value.code is expected_code
        assert "ambient warning" not in str(caught.value)


def test_client_rejects_artifact_escape_and_invalid_init_inputs(tmp_path: Path) -> None:
    escaped = FakeTransport(
        _process(_version_envelope()),
        _process(
            _envelope(
                command="init",
                artifacts=[{"kind": "evidence", "path": "../escaped.json", "hash": None}],
            )
        ),
    )
    with pytest.raises(KernformClientError) as outside:
        KernformCliClient(transport=escaped).init(
            name="alpha-tool",
            destination=tmp_path / "project",
            no_git=True,
        )
    assert outside.value.code is KernformClientFailureCode.ARTIFACT_OUTSIDE_ROOT

    with pytest.raises(KernformClientError) as conflicting_git:
        KernformCliClient(transport=FakeTransport()).init(
            name="alpha-tool",
            destination=tmp_path / "project",
            no_git=True,
            initial_commit=True,
        )
    assert conflicting_git.value.code is KernformClientFailureCode.INVALID_ARGUMENT

    with pytest.raises(KernformClientError) as missing_parent:
        KernformCliClient(transport=FakeTransport()).init(
            name="alpha-tool",
            destination=tmp_path / "missing" / "project",
        )
    assert missing_parent.value.code is KernformClientFailureCode.INVALID_PROJECT_ROOT

    with pytest.raises(KernformClientError) as string_capabilities:
        KernformCliClient(transport=FakeTransport()).init(
            name="alpha-tool",
            destination=tmp_path / "project",
            capabilities="lint",
        )
    assert string_capabilities.value.code is KernformClientFailureCode.INVALID_ARGUMENT

    with pytest.raises(KernformClientError) as non_path:
        KernformCliClient(transport=FakeTransport()).check(cast(Path, "not-a-path"))
    assert non_path.value.code is KernformClientFailureCode.INVALID_PROJECT_ROOT

    with pytest.raises(KernformClientError) as unbounded_capture:
        KernformCliClient(stdout_limit_bytes=1024 * 1024 + 1)
    assert unbounded_capture.value.code is KernformClientFailureCode.INVALID_ARGUMENT


def test_subprocess_transport_bounds_capture_timeout_and_spawn_errors(tmp_path: Path) -> None:
    transport = SubprocessKernformTransport()
    completed = transport.run(
        (sys.executable, "-c", "import sys;sys.stdout.buffer.write(b'ok')"),
        cwd=tmp_path,
        timeout_seconds=1.0,
        stdout_limit_bytes=2,
        stderr_limit_bytes=2,
    )
    assert completed.return_code == 0
    assert completed.stdout == KernformStreamCapture(b"ok", 2, True)

    with pytest.raises(KernformClientError) as oversized:
        transport.run(
            (sys.executable, "-c", "import sys;sys.stdout.buffer.write(b'123456789')"),
            cwd=tmp_path,
            timeout_seconds=1.0,
            stdout_limit_bytes=8,
            stderr_limit_bytes=8,
        )
    assert oversized.value.code is KernformClientFailureCode.OUTPUT_TOO_LARGE

    with pytest.raises(KernformClientError) as timed_out:
        transport.run(
            (sys.executable, "-c", "import time;time.sleep(10)"),
            cwd=tmp_path,
            timeout_seconds=0.05,
            stdout_limit_bytes=8,
            stderr_limit_bytes=8,
        )
    assert timed_out.value.code is KernformClientFailureCode.TIMED_OUT

    with pytest.raises(KernformClientError) as spawn_failed:
        transport.run(
            (str(tmp_path / "missing-sensitive-executable"),),
            cwd=tmp_path,
            timeout_seconds=1.0,
            stdout_limit_bytes=8,
            stderr_limit_bytes=8,
        )
    assert spawn_failed.value.code is KernformClientFailureCode.SPAWN_FAILED
    assert "sensitive" not in str(spawn_failed.value)


def _version_envelope() -> dict[str, object]:
    return _envelope(command="version", result=SUPPORTED_KERNFORM_VERSION)


def _check_result() -> dict[str, object]:
    return {
        "catalog_hash": "a" * 64,
        "checks": {
            "architecture": True,
            "boundary": True,
            "environment": True,
            "git": True,
            "state": True,
            "testing": True,
            "versions": True,
        },
        "conformant": True,
        "files_checked": 39,
        "mode": "managed-project",
        "requirements": {
            "conformance": ["KF-ARCH-001", "KF-BOUNDARY-001"],
            "tests": ["fast", "python-unit"],
        },
    }


def _init_result(destination: Path, *, operation_count: int = 64) -> dict[str, object]:
    return {
        "evidence_path": str(destination / ".kernform/evidence/apply.json"),
        "operation_count": operation_count,
        "plan_id": "b" * 64,
        "state_path": str(destination / ".kernform/state.json"),
    }


def _envelope(
    *,
    command: str,
    status: str = "success",
    exit_code: int = 0,
    result: object = None,
    diagnostics: list[dict[str, object]] | None = None,
    artifacts: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "schema": KERNFORM_COMMAND_SCHEMA,
        "command": command,
        "status": status,
        "exit_code": exit_code,
        "result": result,
        "diagnostics": diagnostics or [],
        "artifacts": artifacts or [],
    }


def _process(
    document: dict[str, object],
    *,
    code: int = 0,
) -> KernformProcessResult:
    return KernformProcessResult(code, _capture(_json_bytes(document)), _capture(b""))


def _capture(value: bytes) -> KernformStreamCapture:
    return KernformStreamCapture(value, len(value), True)


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
