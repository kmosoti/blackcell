from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, replace
from pathlib import Path

import msgspec
import pytest
from pyratatui import Paragraph, Rect

from blackcell.bootstrap.alpha_runtime import AlphaRuntimeApiService
from blackcell.interfaces.http import (
    AlphaCancelRunRequest,
    AlphaEventResponse,
    AlphaIntentRequest,
    AlphaPlanRequest,
    AlphaProjectRequest,
    AlphaReplayFindingResponse,
    AlphaReplayResponse,
    AlphaRunRequest,
    AlphaRunResponse,
    StrictStruct,
    encode_contract,
)
from blackcell.interfaces.tui.app import AlphaTuiApp
from blackcell.interfaces.tui.controller import AlphaTuiProjection
from blackcell.kernel import EventStore
from tests.unit.test_alpha_runtime import _intent, _plan, _project, _repository, _run


@dataclass(frozen=True, slots=True)
class _Key:
    code: str
    ctrl: bool = False
    alt: bool = False
    shift: bool = False


class _Frame:
    def __init__(self, *, width: int = 140, height: int = 44) -> None:
        self.area = Rect(0, 0, width, height)
        self.widgets: list[tuple[object, Rect]] = []

    def render_widget(self, widget: object, area: Rect) -> None:
        self.widgets.append((widget, area))


class _FakeTerminal:
    def __init__(self) -> None:
        self.frame = _Frame()
        self.draw_count = 0
        self.entered = False
        self.exited = False

    async def __aenter__(self) -> _FakeTerminal:
        self.entered = True
        return self

    async def __aexit__(self, *args: object) -> bool:
        del args
        self.exited = True
        return False

    async def events(
        self,
        fps: float = 30.0,
        *,
        stop_on_quit: bool = False,
    ):
        assert fps == 20.0
        assert stop_on_quit is False
        yield None
        await asyncio.sleep(0)
        yield _Key("q")

    def draw(self, draw_fn) -> None:
        self.draw_count += 1
        draw_fn(self.frame)


class _ShellController:
    def __init__(
        self,
        *,
        run: AlphaRunResponse,
        replay: AlphaReplayResponse,
        canceled: AlphaRunResponse,
        events: tuple[AlphaEventResponse, ...],
        refresh_gate: asyncio.Event | None = None,
        refresh_error: Exception | None = None,
    ) -> None:
        self.state = AlphaTuiProjection()
        self._run = run
        self._replay = replay
        self._canceled = canceled
        self._events = events
        self._refresh_gate = refresh_gate
        self._refresh_error = refresh_error
        self.calls: list[str] = []
        self.workflow_requests: list[StrictStruct] = []
        self.cancel_request: AlphaCancelRunRequest | None = None
        self.refresh_started = asyncio.Event()
        self.active_refreshes = 0
        self.max_active_refreshes = 0

    async def connect(self) -> AlphaTuiProjection:
        self.calls.append("connect")
        await asyncio.sleep(0)
        self.state = replace(
            self.state,
            connected=True,
            ready=True,
            endpoint="http://127.0.0.1:8080",
            last_operation="connect",
            revision=self.state.revision + 1,
        )
        return self.state

    async def inspect_run(self, run_id: str) -> AlphaTuiProjection:
        assert run_id == self._run.run_id
        self.calls.append("status")
        await asyncio.sleep(0)
        self.state = replace(
            self.state,
            run=self._run,
            replay=None,
            last_operation="run-status",
            revision=self.state.revision + 1,
        )
        return self.state

    async def register_project(self, request: AlphaProjectRequest) -> AlphaTuiProjection:
        assert request.project_id == self._replay.project.project_id
        self.calls.append("project")
        self.workflow_requests.append(request)
        await asyncio.sleep(0)
        self.state = replace(
            self.state,
            project=self._replay.project,
            intent=None,
            plan=None,
            run=None,
            replay=None,
            last_operation="project-register",
            revision=self.state.revision + 1,
        )
        return self.state

    async def accept_intent(self, request: AlphaIntentRequest) -> AlphaTuiProjection:
        assert request.intent_id == self._replay.intent.intent_id
        self.calls.append("intent")
        self.workflow_requests.append(request)
        await asyncio.sleep(0)
        self.state = replace(
            self.state,
            intent=self._replay.intent,
            plan=None,
            run=None,
            replay=None,
            last_operation="intent-accept",
            revision=self.state.revision + 1,
        )
        return self.state

    async def accept_plan(self, request: AlphaPlanRequest) -> AlphaTuiProjection:
        assert request.plan_id == self._replay.plan.plan_id
        self.calls.append("plan")
        self.workflow_requests.append(request)
        await asyncio.sleep(0)
        self.state = replace(
            self.state,
            plan=self._replay.plan,
            run=None,
            replay=None,
            last_operation="plan-accept",
            revision=self.state.revision + 1,
        )
        return self.state

    async def submit_run(self, request: AlphaRunRequest) -> AlphaTuiProjection:
        assert request.run_id == self._run.run_id
        self.calls.append("submit")
        self.workflow_requests.append(request)
        await asyncio.sleep(0)
        self.state = replace(
            self.state,
            run=self._run,
            replay=None,
            last_operation="run-submit",
            revision=self.state.revision + 1,
        )
        return self.state

    async def replay_run(self, run_id: str) -> AlphaTuiProjection:
        assert run_id == self._run.run_id
        self.calls.append("replay")
        await asyncio.sleep(0)
        self.state = replace(
            self.state,
            project=self._replay.project,
            intent=self._replay.intent,
            plan=self._replay.plan,
            run=self._replay.run,
            replay=self._replay,
            last_operation="run-replay",
            revision=self.state.revision + 1,
        )
        return self.state

    async def cancel_run(
        self,
        run_id: str,
        request: AlphaCancelRunRequest,
    ) -> AlphaTuiProjection:
        assert run_id == self._run.run_id
        self.calls.append("cancel")
        self.cancel_request = request
        await asyncio.sleep(0)
        self.state = replace(
            self.state,
            run=self._canceled,
            replay=None,
            last_operation="run-cancel",
            revision=self.state.revision + 1,
        )
        return self.state

    async def refresh_events(self, *, limit: int = 100) -> AlphaTuiProjection:
        assert limit == 100
        self.calls.append("refresh")
        self.active_refreshes += 1
        self.max_active_refreshes = max(self.max_active_refreshes, self.active_refreshes)
        self.refresh_started.set()
        try:
            if self._refresh_gate is not None:
                await self._refresh_gate.wait()
            if self._refresh_error is not None:
                raise self._refresh_error
            cursor = self._events[-1].cursor if self._events else self.state.cursor
            self.state = replace(
                self.state,
                cursor=cursor,
                events=self._events,
                last_operation="events-refresh",
                revision=self.state.revision + 1,
            )
            return self.state
        finally:
            self.active_refreshes -= 1


def test_alpha_tui_app_projects_events_and_run_operations_without_blocking(
    tmp_path: Path,
) -> None:
    run, replay, canceled, events = _runtime_records(tmp_path)
    controller = _ShellController(run=run, replay=replay, canceled=canceled, events=events)
    app = AlphaTuiApp(
        lambda: controller,
        event_refresh_seconds=None,
        idempotency_factory=lambda: "tui-cancel-test-1",
    )

    async def exercise() -> None:
        await app.start()
        await app.wait_idle()
        assert controller.calls == ["connect"]
        assert app.view.connection == "ready · http://127.0.0.1:8080 · cursor 0"

        assert app.handle_key(_Key("r"))
        await app.wait_idle()
        assert f"cursor {events[-1].cursor}" in app.view.connection
        assert events[-1].event_type in app.view.events

        _edit(app, "i", run.run_id)
        assert app.handle_key(_Key("enter"))
        assert app.handle_key(_Key("s"))
        await app.wait_idle()
        assert "Status: queued" in app.view.run

        assert app.handle_key(_Key("p"))
        await app.wait_idle()
        assert "Artifact integrity: not-applicable" in app.view.run
        assert "Verification: not-started" in app.view.run

        assert app.handle_key(_Key("x"))
        await app.wait_idle()
        assert "Status: canceled" in app.view.run

    asyncio.run(exercise())

    frame = _Frame()
    app.render(frame)
    assert len(frame.widgets) == 5
    assert all(isinstance(widget, Paragraph) for widget, _area in frame.widgets)
    assert controller.calls == ["connect", "refresh", "status", "replay", "cancel"]
    assert controller.cancel_request is not None
    assert controller.cancel_request.idempotency_key == "tui-cancel-test-1"
    assert controller.cancel_request.schema_version == "alpha-cancel-run-request/v1"


def test_alpha_tui_app_submits_bounded_workflow_and_projects_replay_evidence(
    tmp_path: Path,
) -> None:
    run, replay, canceled, events = _runtime_records(tmp_path)
    retained_run = msgspec.structs.replace(replay.run, status="failed", retained_worktree=True)
    finding = AlphaReplayFindingResponse(
        code="alpha-replay-artifact-missing",
        node_id=replay.plan.nodes[0].node_id,
        role="outcome",
        check_id=None,
        artifact_digest="sha256:" + ("f" * 64),
    )
    evidence_replay = msgspec.structs.replace(
        replay,
        run=retained_run,
        artifact_integrity="failed",
        findings=(finding,),
    )
    controller = _ShellController(
        run=run,
        replay=evidence_replay,
        canceled=canceled,
        events=events,
    )
    requests = _workflow_requests(evidence_replay)
    request_paths = {
        operation: _request_file(tmp_path / f"{operation}.json", request)
        for operation, request in requests.items()
    }
    app = AlphaTuiApp(lambda: controller, event_refresh_seconds=None)

    async def exercise() -> None:
        await app.start()
        await app.wait_idle()
        for key, operation in zip("1234", ("project", "intent", "plan", "run"), strict=True):
            assert app.handle_key(_Key(key))
            assert app.view.workflow_operation == operation
            _edit(app, "w", request_paths[operation])
            assert app.handle_key(_Key("enter"))
            assert app.view.workflow_path == ""
            await app.wait_idle()
            assert app.view.message == f"alpha-tui-workflow-{operation}-complete"

        _edit(app, "i", run.run_id)
        app.handle_key(_Key("enter"))
        assert app.handle_key(_Key("p"))
        await app.wait_idle()
        output = app.view.workflow
        assert f"Project: {evidence_replay.project.project_id}" in output
        assert f"Intent: {evidence_replay.intent.intent_id}" in output
        assert f"Plan: {evidence_replay.plan.plan_id}" in output
        assert "DAG:" in output
        assert "Evidence: failed · 0 artifacts" in output
        assert "Integrity findings: 1" in output
        assert "alpha-replay-artifact-missing" in output
        assert "Review findings: unavailable in alpha-replay/v2" in output
        assert "Verification: not-started" in output
        assert "Recovery state: checkout-retained" in output

    asyncio.run(exercise())

    assert controller.calls == ["connect", "project", "intent", "plan", "submit", "replay"]
    assert controller.workflow_requests == list(requests.values())


def test_alpha_tui_app_rejects_unsafe_workflow_files_content_free(tmp_path: Path) -> None:
    run, replay, canceled, events = _runtime_records(tmp_path)
    controller = _ShellController(run=run, replay=replay, canceled=canceled, events=events)
    valid_project = _request_file(tmp_path / "project.json", _workflow_requests(replay)["project"])
    missing = tmp_path / "missing.json"
    linked = tmp_path / "linked.json"
    linked.symlink_to(valid_project)
    empty = tmp_path / "empty.json"
    empty.write_bytes(b"")
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"x" * 1_048_577)
    malformed = tmp_path / "malformed.json"
    malformed.write_text(
        json.dumps(
            {
                "schema_version": "alpha-project-request/v1",
                "secret": "secret-workflow-payload",
            }
        ),
        encoding="utf-8",
    )
    app = AlphaTuiApp(lambda: controller, event_refresh_seconds=None)

    async def exercise() -> None:
        await app.start()
        await app.wait_idle()
        cases = (
            ("1", missing),
            ("1", linked),
            ("1", empty),
            ("1", oversized),
            ("1", malformed),
            ("2", Path(valid_project)),
        )
        for operation_key, path in cases:
            app.handle_key(_Key(operation_key))
            _edit(app, "w", str(path))
            app.handle_key(_Key("enter"))
            await app.wait_idle()
            assert app.view.message == "alpha-tui-invalid-workflow-request"
            assert "secret-workflow-payload" not in app.view.message
            assert app.view.workflow_path == ""

    asyncio.run(exercise())

    assert controller.calls == ["connect"]
    assert controller.workflow_requests == []


def test_alpha_tui_app_serializes_refresh_and_keeps_input_responsive(tmp_path: Path) -> None:
    run, replay, canceled, events = _runtime_records(tmp_path)
    gate = asyncio.Event()
    controller = _ShellController(
        run=run,
        replay=replay,
        canceled=canceled,
        events=events,
        refresh_gate=gate,
        refresh_error=RuntimeError("secret-service-path"),
    )
    app = AlphaTuiApp(lambda: controller, event_refresh_seconds=None)

    async def exercise() -> None:
        await app.start()
        await app.wait_idle()
        assert app.action_refresh_events()
        await controller.refresh_started.wait()
        assert app.action_refresh_events() is False

        _edit(app, "i", run.run_id)
        assert app.view.run_id == run.run_id
        assert controller.calls.count("refresh") == 1
        assert controller.max_active_refreshes == 1

        gate.set()
        await app.wait_idle()
        assert app.view.message == "alpha-tui-operation-failed"
        assert "secret-service-path" not in app.view.message
        assert app.view.events_busy is False

    asyncio.run(exercise())

    with pytest.raises(ValueError, match="event_refresh_seconds"):
        AlphaTuiApp(lambda: controller, event_refresh_seconds=0.1)
    with pytest.raises(ValueError, match="frames_per_second"):
        AlphaTuiApp(lambda: controller, frames_per_second=0)


def test_alpha_tui_app_runs_and_restores_injected_async_terminal(tmp_path: Path) -> None:
    run, replay, canceled, events = _runtime_records(tmp_path)
    controller = _ShellController(run=run, replay=replay, canceled=canceled, events=events)
    terminal = _FakeTerminal()
    app = AlphaTuiApp(
        lambda: controller,
        event_refresh_seconds=None,
        terminal_factory=lambda: terminal,
    )

    asyncio.run(app.run())

    assert terminal.entered is True
    assert terminal.exited is True
    assert terminal.draw_count == 2
    assert app.view.quit_requested is True
    assert all(isinstance(widget, Paragraph) for widget, _area in terminal.frame.widgets)


def _edit(app: AlphaTuiApp, key: str, value: str) -> None:
    assert app.handle_key(_Key(key))
    for character in value:
        assert app.handle_key(_Key(character))


def _workflow_requests(replay: AlphaReplayResponse) -> dict[str, StrictStruct]:
    project = AlphaProjectRequest(
        schema_version="alpha-project-request/v1",
        project_id=replay.project.project_id,
        root=replay.project.root,
        configuration_provider=replay.project.configuration_provider,
        configuration_version=replay.project.configuration_version,
        configuration_digest=replay.project.configuration_digest,
        idempotency_key="tui-project-request",
    )
    intent = AlphaIntentRequest(
        schema_version="alpha-intent-request/v1",
        intent_id=replay.intent.intent_id,
        project_id=replay.intent.project_id,
        objective=replay.intent.objective,
        constraints=replay.intent.constraints,
        assumptions=replay.intent.assumptions,
        unresolved_questions=replay.intent.unresolved_questions,
        idempotency_key="tui-intent-request",
    )
    plan = AlphaPlanRequest(
        schema_version="alpha-plan-request/v1",
        plan_id=replay.plan.plan_id,
        project_id=replay.plan.project_id,
        intent_id=replay.plan.intent_id,
        base_commit=replay.plan.base_commit,
        allowed_effects=replay.plan.allowed_effects,
        nodes=replay.plan.nodes,
        idempotency_key="tui-plan-request",
    )
    run = AlphaRunRequest(
        schema_version="alpha-run-request/v1",
        run_id=replay.run.run_id,
        project_id=replay.run.project_id,
        intent_id=replay.run.intent_id,
        plan_id=replay.run.plan_id,
        idempotency_key="tui-run-request",
    )
    return {"project": project, "intent": intent, "plan": plan, "run": run}


def _request_file(path: Path, request: StrictStruct) -> str:
    path.write_bytes(encode_contract(request))
    return str(path)


def _runtime_records(
    tmp_path: Path,
) -> tuple[
    AlphaRunResponse,
    AlphaReplayResponse,
    AlphaRunResponse,
    tuple[AlphaEventResponse, ...],
]:
    repository = _repository(tmp_path)
    runtime = AlphaRuntimeApiService(EventStore(tmp_path / "state.sqlite3"), repository)
    runtime.register_project(_project(repository), principal_id="tui:test")
    runtime.accept_intent(_intent(), principal_id="tui:test")
    runtime.accept_plan(_plan(repository), principal_id="tui:test")
    run = runtime.submit_run(_run(), principal_id="tui:test")
    replay = runtime.replay_run(run.run_id)
    events = runtime.list_events(after_cursor=0, limit=100).events
    canceled = runtime.cancel_run(
        run.run_id,
        AlphaCancelRunRequest(
            schema_version="alpha-cancel-run-request/v1",
            idempotency_key="fixture-cancel-1",
        ),
        principal_id="tui:test",
    )
    return run, replay, canceled, events
