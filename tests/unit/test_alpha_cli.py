from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

import msgspec

from blackcell.adapters.runtime_http import RuntimeClientError, RuntimeClientFailureCode
from blackcell.bootstrap.alpha_runtime import AlphaRuntimeApiService
from blackcell.cli.app import app
from blackcell.config import API_TOKEN_ENV, API_TOKEN_FILE_ENV, DATA_DIR_ENV, SecretValue
from blackcell.interfaces.http import AlphaCancelRunRequest, encode_contract
from blackcell.kernel import EventStore
from tests.cli_runner import CycloptsCliRunner
from tests.unit.test_alpha_runtime import _intent, _plan, _project, _repository, _run

runner = CycloptsCliRunner()
_TOKEN = "Alpha-cli-token.0123456789-ABCDEFG"


class FakeAlphaClient:
    instances: ClassVar[list[FakeAlphaClient]] = []
    calls: ClassVar[list[tuple[str, object]]] = []
    responses: ClassVar[dict[str, object]] = {}

    def __init__(self, *, endpoint: str, token: SecretValue) -> None:
        self.endpoint = endpoint
        self.token = token
        type(self).instances.append(self)

    def register_alpha_project(self, request: object) -> object:
        return self._call("project", request)

    def accept_alpha_intent(self, request: object) -> object:
        return self._call("intent", request)

    def accept_alpha_plan(self, request: object) -> object:
        return self._call("plan", request)

    def submit_alpha_run(self, request: object) -> object:
        return self._call("submit", request)

    def inspect_alpha_run(self, run_id: str) -> object:
        return self._call("status", run_id)

    def cancel_alpha_run(self, run_id: str, request: object) -> object:
        return self._call("cancel", (run_id, request))

    def replay_alpha_run(self, run_id: str) -> object:
        return self._call("replay", run_id)

    def list_alpha_events(self, *, after_cursor: int, limit: int) -> object:
        return self._call("events", (after_cursor, limit))

    def _call(self, operation: str, value: object) -> object:
        type(self).calls.append((operation, value))
        return type(self).responses[operation]


def test_alpha_cli_executes_complete_json_first_client_surface(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    service = AlphaRuntimeApiService(EventStore(tmp_path / "state.sqlite3"), repository)
    project_request = _project(repository)
    intent_request = _intent()
    plan_request = _plan()
    run_request = _run()
    cancel_request = AlphaCancelRunRequest(
        schema_version="alpha-cancel-run-request/v1",
        idempotency_key="cancel-run-1",
    )
    project = service.register_project(project_request, principal_id="client:test")
    intent = service.accept_intent(intent_request, principal_id="client:test")
    plan = service.accept_plan(plan_request, principal_id="client:test")
    run = service.submit_run(run_request, principal_id="client:test")
    events = service.list_events(after_cursor=0, limit=20)
    replay = service.replay_run("run-1")
    canceled = service.cancel_run("run-1", cancel_request, principal_id="client:test")
    FakeAlphaClient.instances = []
    FakeAlphaClient.calls = []
    FakeAlphaClient.responses = {
        "project": project,
        "intent": intent,
        "plan": plan,
        "submit": run,
        "status": run,
        "events": events,
        "replay": replay,
        "cancel": canceled,
    }
    monkeypatch.setattr("blackcell.cli.app.RuntimeHttpClient", FakeAlphaClient)
    monkeypatch.setenv(API_TOKEN_ENV, _TOKEN)
    monkeypatch.delenv(API_TOKEN_FILE_ENV, raising=False)
    request_files = {
        "project": _request_file(tmp_path, "project.json", project_request),
        "intent": _request_file(tmp_path, "intent.json", intent_request),
        "plan": _request_file(tmp_path, "plan.json", plan_request),
        "run": _request_file(tmp_path, "run.json", run_request),
        "cancel": _request_file(tmp_path, "cancel.json", cancel_request),
    }
    commands = (
        ("project", ["alpha", "project", "register", "--request", request_files["project"]]),
        ("intent", ["alpha", "intent", "accept", "--request", request_files["intent"]]),
        ("plan", ["alpha", "plan", "accept", "--request", request_files["plan"]]),
        ("submit", ["alpha", "run", "submit", "--request", request_files["run"]]),
        ("status", ["alpha", "run", "status", "run-1"]),
        ("events", ["alpha", "events", "list", "--after", "0", "--limit", "20"]),
        ("replay", ["alpha", "run", "replay", "run-1"]),
        (
            "cancel",
            ["alpha", "run", "cancel", "run-1", "--request", request_files["cancel"]],
        ),
    )

    outputs: dict[str, Any] = {}
    for operation, command in commands:
        result = runner.invoke(
            app,
            [*command, "--endpoint", "https://runtime.example"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        outputs[operation] = json.loads(result.stdout)

    assert outputs["project"]["schema_version"] == "alpha-project/v1"
    assert outputs["submit"]["status"] == "queued"
    assert outputs["events"]["events"][0]["event_type"] == "alpha.project.registered"
    assert outputs["replay"]["verification"]["lifecycle_status"] == "not-started"
    assert outputs["cancel"]["status"] == "canceled"
    assert [operation for operation, _ in FakeAlphaClient.calls] == [
        "project",
        "intent",
        "plan",
        "submit",
        "status",
        "events",
        "replay",
        "cancel",
    ]
    assert all(
        instance.endpoint == "https://runtime.example" for instance in FakeAlphaClient.instances
    )
    assert all(_TOKEN not in repr(instance.token) for instance in FakeAlphaClient.instances)


def test_alpha_cli_rejects_invalid_request_files_and_redacts_client_failures(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(API_TOKEN_ENV, _TOKEN)
    monkeypatch.delenv(API_TOKEN_FILE_ENV, raising=False)
    malformed = tmp_path / "malformed.json"
    malformed.write_text('{"schema_version":"alpha-project-request/v1","secret":"leak"}')

    invalid = runner.invoke(
        app,
        ["alpha", "project", "register", "--request", str(malformed)],
        catch_exceptions=False,
    )
    assert invalid.exit_code == 2
    assert invalid.stdout == ""
    assert json.loads(invalid.stderr) == {"error": {"message": "invalid-alpha-request-file"}}
    assert "leak" not in invalid.stderr

    class DeniedClient(FakeAlphaClient):
        def inspect_alpha_run(self, run_id: str) -> object:
            del run_id
            raise RuntimeClientError(
                RuntimeClientFailureCode.REQUEST_REJECTED,
                status_code=403,
                service_error="authorization-denied",
            )

    monkeypatch.setattr("blackcell.cli.app.RuntimeHttpClient", DeniedClient)
    denied = runner.invoke(
        app,
        ["alpha", "run", "status", "run-1"],
        catch_exceptions=False,
    )
    assert denied.exit_code == 4
    assert denied.stdout == ""
    assert json.loads(denied.stderr)["error"]["message"] == (
        "runtime-request-rejected: status=403 error=authorization-denied"
    )
    assert _TOKEN not in denied.stderr


def test_alpha_help_exposes_client_surface_without_legacy_submission() -> None:
    alpha = runner.invoke(app, ["alpha", "--help"], catch_exceptions=False)
    run = runner.invoke(app, ["alpha", "run", "--help"], catch_exceptions=False)

    assert alpha.exit_code == run.exit_code == 0
    for command in ("project", "intent", "plan", "run", "events", "tui"):
        assert command in alpha.stdout
    for command in ("submit", "status", "cancel", "replay"):
        assert command in run.stdout
    assert "--token" not in alpha.stdout
    assert "/api/v1/runs" not in alpha.stdout
    assert "operator" not in alpha.stdout


def test_alpha_tui_command_composes_shared_client_cursor_and_controller(
    monkeypatch,
    tmp_path: Path,
) -> None:
    calls: dict[str, object] = {}

    class FakeCursorStore:
        @classmethod
        def prepare(cls, path: Path) -> FakeCursorStore:
            calls["cursor_dir"] = path
            return cls()

    class FakeController:
        def __init__(self, client: object, *, cursor_store: object) -> None:
            calls["client"] = client
            calls["cursor_store"] = cursor_store

    class FakeTuiApp:
        def __init__(
            self,
            controller_factory,
            *,
            event_refresh_seconds: float | None,
            frames_per_second: float,
        ) -> None:
            calls["controller"] = controller_factory()
            calls["refresh_seconds"] = event_refresh_seconds
            calls["frames_per_second"] = frames_per_second

        async def run(self) -> None:
            calls["ran"] = True

    FakeAlphaClient.instances = []
    monkeypatch.setattr("blackcell.cli.app.FileAlphaTuiCursorStore", FakeCursorStore)
    monkeypatch.setattr("blackcell.cli.app.AlphaTuiController", FakeController)
    monkeypatch.setattr("blackcell.cli.app.AlphaTuiApp", FakeTuiApp)
    monkeypatch.setattr("blackcell.cli.app.RuntimeHttpClient", FakeAlphaClient)
    monkeypatch.setenv(API_TOKEN_ENV, _TOKEN)
    monkeypatch.delenv(API_TOKEN_FILE_ENV, raising=False)
    data_root = tmp_path / "runtime-data"
    monkeypatch.setenv(DATA_DIR_ENV, str(data_root))

    result = runner.invoke(
        app,
        [
            "alpha",
            "tui",
            "--endpoint",
            "https://runtime.example",
            "--refresh-seconds",
            "2.5",
            "--frames-per-second",
            "30",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert result.stdout == result.stderr == ""
    assert calls["cursor_dir"] == data_root / "alpha-tui-cursors"
    assert calls["refresh_seconds"] == 2.5
    assert calls["frames_per_second"] == 30.0
    assert calls["ran"] is True
    assert len(FakeAlphaClient.instances) == 1
    assert FakeAlphaClient.instances[0].endpoint == "https://runtime.example"
    assert _TOKEN not in repr(FakeAlphaClient.instances[0].token)


def _request_file(tmp_path: Path, name: str, contract: msgspec.Struct) -> str:
    path = tmp_path / name
    path.write_bytes(encode_contract(contract))
    return str(path)
