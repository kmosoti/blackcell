from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

import msgspec

from blackcell.adapters.runtime_http import RuntimeClientError, RuntimeClientFailureCode
from blackcell.bootstrap.runtime_service import RuntimeService
from blackcell.cli.app import app
from blackcell.config import API_TOKEN_ENV, API_TOKEN_FILE_ENV, DATA_DIR_ENV, SecretValue
from blackcell.interfaces.http import CancelRunRequest, RunQueryRequest, encode_contract
from blackcell.kernel import EventStore
from tests.cli_runner import CycloptsCliRunner
from tests.unit.test_runtime_service import _intent, _plan, _project, _repository, _run

runner = CycloptsCliRunner()
_TOKEN = "Runtime-cli-token.0123456789-ABCDEFG"


class FakeClient:
    instances: ClassVar[list[FakeClient]] = []
    calls: ClassVar[list[tuple[str, object]]] = []
    responses: ClassVar[dict[str, object]] = {}

    def __init__(self, *, endpoint: str, token: SecretValue) -> None:
        self.endpoint = endpoint
        self.token = token
        type(self).instances.append(self)

    def register_project(self, request: object) -> object:
        return self._call("project", request)

    def accept_intent(self, request: object) -> object:
        return self._call("intent", request)

    def accept_plan(self, request: object) -> object:
        return self._call("plan", request)

    def submit_run(self, request: object) -> object:
        return self._call("submit", request)

    def inspect_run(self, run_id: str) -> object:
        return self._call("status", run_id)

    def query_runs(self, request: object) -> object:
        return self._call("query", request)

    def cancel_run(self, run_id: str, request: object) -> object:
        return self._call("cancel", (run_id, request))

    def replay_run(self, run_id: str) -> object:
        return self._call("replay", run_id)

    def list_events(self, *, after_cursor: int, limit: int) -> object:
        return self._call("events", (after_cursor, limit))

    def _call(self, operation: str, value: object) -> object:
        type(self).calls.append((operation, value))
        return type(self).responses[operation]


def test_runtime_cli_executes_complete_json_first_client_surface(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    service = RuntimeService(EventStore(tmp_path / "state.sqlite3"), repository)
    project_request = _project(repository)
    intent_request = _intent()
    plan_request = _plan(repository)
    run_request = _run()
    cancel_request = CancelRunRequest(
        schema_version="execution-cancel-run-request/v1",
        idempotency_key="cancel-run-1",
    )
    query_request = RunQueryRequest(
        schema_version="run-query-request/v1",
        statuses=("queued",),
        limit=10,
    )
    project = service.register_project(project_request, principal_id="client:test")
    intent = service.accept_intent(intent_request, principal_id="client:test")
    plan = service.accept_plan(plan_request, principal_id="client:test")
    run = service.submit_run(run_request, principal_id="client:test")
    events = service.list_events(after_cursor=0, limit=20)
    replay = service.replay_run("run-1")
    query = service.query_runs(query_request)
    canceled = service.cancel_run("run-1", cancel_request, principal_id="client:test")
    FakeClient.instances = []
    FakeClient.calls = []
    FakeClient.responses = {
        "project": project,
        "intent": intent,
        "plan": plan,
        "submit": run,
        "status": run,
        "query": query,
        "events": events,
        "replay": replay,
        "cancel": canceled,
    }
    monkeypatch.setattr("blackcell.cli.app.RuntimeHttpClient", FakeClient)
    monkeypatch.setenv(API_TOKEN_ENV, _TOKEN)
    monkeypatch.delenv(API_TOKEN_FILE_ENV, raising=False)
    request_files = {
        "project": _request_file(tmp_path, "project.json", project_request),
        "intent": _request_file(tmp_path, "intent.json", intent_request),
        "plan": _request_file(tmp_path, "plan.json", plan_request),
        "run": _request_file(tmp_path, "run.json", run_request),
        "cancel": _request_file(tmp_path, "cancel.json", cancel_request),
        "query": _request_file(tmp_path, "query.json", query_request),
    }
    commands = (
        ("project", ["project", "register", "--request", request_files["project"]]),
        ("intent", ["intent", "accept", "--request", request_files["intent"]]),
        ("plan", ["plan", "accept", "--request", request_files["plan"]]),
        ("submit", ["run", "submit", "--request", request_files["run"]]),
        ("status", ["run", "status", "run-1"]),
        ("query", ["run", "query", "--request", request_files["query"]]),
        ("events", ["events", "list", "--after", "0", "--limit", "20"]),
        ("replay", ["run", "replay", "run-1"]),
        (
            "cancel",
            ["run", "cancel", "run-1", "--request", request_files["cancel"]],
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

    assert outputs["project"]["schema_version"] == "project/v1"
    assert outputs["submit"]["status"] == "queued"
    assert outputs["query"]["runs"][0]["run"]["run_id"] == "run-1"
    assert outputs["events"]["events"][0]["event_type"] == "project.registered"
    assert outputs["replay"]["verification"]["lifecycle_status"] == "not-started"
    assert outputs["cancel"]["status"] == "canceled"
    assert [operation for operation, _ in FakeClient.calls] == [
        "project",
        "intent",
        "plan",
        "submit",
        "status",
        "query",
        "events",
        "replay",
        "cancel",
    ]
    assert all(instance.endpoint == "https://runtime.example" for instance in FakeClient.instances)
    assert all(_TOKEN not in repr(instance.token) for instance in FakeClient.instances)


def test_runtime_cli_rejects_invalid_request_files_and_redacts_client_failures(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(API_TOKEN_ENV, _TOKEN)
    monkeypatch.delenv(API_TOKEN_FILE_ENV, raising=False)
    malformed = tmp_path / "malformed.json"
    malformed.write_text('{"schema_version":"project-request/v1","secret":"leak"}')

    invalid = runner.invoke(
        app,
        ["project", "register", "--request", str(malformed)],
        catch_exceptions=False,
    )
    assert invalid.exit_code == 2
    assert invalid.stdout == ""
    assert json.loads(invalid.stderr) == {"error": {"message": "invalid-runtime-request-file"}}
    assert "leak" not in invalid.stderr

    class DeniedClient(FakeClient):
        def inspect_run(self, run_id: str) -> object:
            del run_id
            raise RuntimeClientError(
                RuntimeClientFailureCode.REQUEST_REJECTED,
                status_code=403,
                service_error="authorization-denied",
            )

    monkeypatch.setattr("blackcell.cli.app.RuntimeHttpClient", DeniedClient)
    denied = runner.invoke(
        app,
        ["run", "status", "run-1"],
        catch_exceptions=False,
    )
    assert denied.exit_code == 4
    assert denied.stdout == ""
    assert json.loads(denied.stderr)["error"]["message"] == (
        "runtime-request-rejected: status=403 error=authorization-denied"
    )
    assert _TOKEN not in denied.stderr


def test_runtime_help_exposes_only_the_project_runtime_client_surface() -> None:
    root = runner.invoke(app, ["--help"], catch_exceptions=False)
    run = runner.invoke(app, ["run", "--help"], catch_exceptions=False)

    assert root.exit_code == run.exit_code == 0
    for command in ("project", "intent", "plan", "run", "events", "tui"):
        assert command in root.stdout
    for command in ("submit", "status", "query", "cancel", "replay"):
        assert command in run.stdout
    assert "--token" not in root.stdout
    assert "/api/v1/runs" not in root.stdout
    assert "operator" not in root.stdout


def test_tui_command_composes_shared_client_cursor_and_controller(
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

    FakeClient.instances = []
    monkeypatch.setattr("blackcell.cli.app.FileTuiCursorStore", FakeCursorStore)
    monkeypatch.setattr("blackcell.cli.app.TuiController", FakeController)
    monkeypatch.setattr("blackcell.cli.app.TuiApp", FakeTuiApp)
    monkeypatch.setattr("blackcell.cli.app.RuntimeHttpClient", FakeClient)
    monkeypatch.setenv(API_TOKEN_ENV, _TOKEN)
    monkeypatch.delenv(API_TOKEN_FILE_ENV, raising=False)
    data_root = tmp_path / "runtime-data"
    monkeypatch.setenv(DATA_DIR_ENV, str(data_root))

    result = runner.invoke(
        app,
        [
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
    assert calls["cursor_dir"] == data_root / "tui-cursors"
    assert calls["refresh_seconds"] == 2.5
    assert calls["frames_per_second"] == 30.0
    assert calls["ran"] is True
    assert len(FakeClient.instances) == 1
    assert FakeClient.instances[0].endpoint == "https://runtime.example"
    assert _TOKEN not in repr(FakeClient.instances[0].token)


def _request_file(tmp_path: Path, name: str, contract: msgspec.Struct) -> str:
    path = tmp_path / name
    path.write_bytes(encode_contract(contract))
    return str(path)
