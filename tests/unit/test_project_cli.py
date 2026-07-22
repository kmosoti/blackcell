from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar, Literal

from blackcell.adapters.kernform_cli import (
    KERNFORM_EXECUTABLE_ENV,
    KernformArtifact,
    KernformClientError,
    KernformClientFailureCode,
    KernformDiagnostic,
    KernformInvocationResult,
)
from blackcell.cli.app import app
from tests.cli_runner import CycloptsCliRunner

runner = CycloptsCliRunner()


class FakeKernformClient:
    instances: ClassVar[list[FakeKernformClient]] = []
    result: ClassVar[KernformInvocationResult]
    calls: ClassVar[list[tuple[str, object]]] = []

    def __init__(self, *, executable: str) -> None:
        self.executable = executable
        type(self).instances.append(self)

    def check(self, project_root: Path) -> KernformInvocationResult:
        type(self).calls.append(("check", project_root))
        return type(self).result

    def init(
        self,
        *,
        name: str,
        destination: Path,
        profile: str,
        capabilities: tuple[str, ...],
        no_git: bool,
        initial_commit: bool,
    ) -> KernformInvocationResult:
        type(self).calls.append(
            (
                "init",
                {
                    "name": name,
                    "destination": destination,
                    "profile": profile,
                    "capabilities": capabilities,
                    "no_git": no_git,
                    "initial_commit": initial_commit,
                },
            )
        )
        return type(self).result


def test_project_check_emits_evidence_and_preserves_kernform_failure_exit(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _install_fake(monkeypatch)
    FakeKernformClient.result = _result(
        root=tmp_path,
        command="check",
        status="failure",
        exit_code=2,
        diagnostics=(
            KernformDiagnostic(
                "KF-STATE-001",
                "error",
                "managed state is missing",
                {"path": str(tmp_path / ".kernform/state.json")},
            ),
        ),
    )

    result = runner.invoke(
        app,
        [
            "project",
            "check",
            "--path",
            str(tmp_path),
            "--kernform",
            "/opt/kernform/bin/kernform",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == "kernform-invocation/v1"
    assert payload["kernform_version"] == "0.1.0"
    assert payload["status"] == "failure"
    assert payload["diagnostics"][0]["id"] == "KF-STATE-001"
    assert FakeKernformClient.instances[0].executable == "/opt/kernform/bin/kernform"
    assert FakeKernformClient.calls == [("check", tmp_path)]


def test_project_init_parses_repeated_capabilities_and_environment_executable(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _install_fake(monkeypatch)
    destination = tmp_path / "alpha-tool"
    monkeypatch.setenv(KERNFORM_EXECUTABLE_ENV, "/env/bin/kernform")
    FakeKernformClient.result = _result(
        root=destination,
        command="init",
        artifacts=(KernformArtifact("managed-state", str(destination / "state.json"), None),),
    )

    result = runner.invoke(
        app,
        [
            "project",
            "init",
            "alpha-tool",
            "--destination",
            str(destination),
            "--profile",
            "api",
            "--with",
            "lint",
            "--with",
            "test",
            "--no-git",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["artifacts"][0]["kind"] == "managed-state"
    assert FakeKernformClient.instances[0].executable == "/env/bin/kernform"
    assert FakeKernformClient.calls == [
        (
            "init",
            {
                "name": "alpha-tool",
                "destination": destination,
                "profile": "api",
                "capabilities": ("lint", "test"),
                "no_git": True,
                "initial_commit": False,
            },
        )
    ]


def test_project_boundary_errors_use_typed_exit_class_and_content_free_json(
    monkeypatch,
) -> None:
    class FailingKernformClient:
        def __init__(self, *, executable: str) -> None:
            del executable
            raise KernformClientError(KernformClientFailureCode.SPAWN_FAILED)

    monkeypatch.setattr("blackcell.cli.app.KernformCliClient", FailingKernformClient)

    result = runner.invoke(app, ["project", "check"], catch_exceptions=False)

    assert result.exit_code == 3
    assert result.stdout == ""
    assert json.loads(result.stderr) == {
        "error": {"message": KernformClientFailureCode.SPAWN_FAILED.value}
    }


def test_project_help_exposes_only_the_initial_check_and_init_contract() -> None:
    result = runner.invoke(app, ["project", "--help"], catch_exceptions=False)

    assert result.exit_code == 0
    assert "check" in result.stdout
    assert "init" in result.stdout
    assert "inspect" not in result.stdout
    assert "adopt" not in result.stdout


def _install_fake(monkeypatch) -> None:
    FakeKernformClient.instances = []
    FakeKernformClient.calls = []
    monkeypatch.delenv(KERNFORM_EXECUTABLE_ENV, raising=False)
    monkeypatch.setattr("blackcell.cli.app.KernformCliClient", FakeKernformClient)


def _result(
    *,
    root: Path,
    command: Literal["check", "init"],
    status: Literal["success", "failure", "refused"] = "success",
    exit_code: int = 0,
    diagnostics: tuple[KernformDiagnostic, ...] = (),
    artifacts: tuple[KernformArtifact, ...] = (),
) -> KernformInvocationResult:
    return KernformInvocationResult(
        kernform_version="0.1.0",
        project_root=root,
        command=command,
        status=status,
        exit_code=exit_code,
        result=None,
        diagnostics=diagnostics,
        artifacts=artifacts,
        argv_digest="sha256:" + "a" * 64,
        result_digest="sha256:" + "b" * 64,
    )
