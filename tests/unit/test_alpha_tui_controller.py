from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import msgspec
import pytest

from blackcell.adapters.runtime_http import RuntimeHttpClient
from blackcell.bootstrap.alpha_runtime import AlphaRuntimeApiService
from blackcell.config import SecretValue
from blackcell.interfaces.http import (
    AlphaCancelRunRequest,
    AlphaEventPageResponse,
    AlphaEventResponse,
    AlphaIntentRequest,
    AlphaIntentResponse,
    AlphaPlanRequest,
    AlphaPlanResponse,
    AlphaProjectRequest,
    AlphaProjectResponse,
    AlphaReplayResponse,
    AlphaRunRequest,
    AlphaRunResponse,
)
from blackcell.interfaces.tui import (
    AlphaTuiClient,
    AlphaTuiController,
    AlphaTuiError,
    AlphaTuiFailureCode,
)
from blackcell.kernel import EventStore
from tests.unit.test_alpha_runtime import _intent, _plan, _project, _repository, _run


@dataclass(frozen=True, slots=True)
class _Status:
    endpoint: str = "http://127.0.0.1:8080"
    live: bool = True
    ready: bool = True


class _FakeAlphaTuiClient:
    def __init__(
        self,
        *,
        project: AlphaProjectResponse | None = None,
        intent: AlphaIntentResponse | None = None,
        plan: AlphaPlanResponse | None = None,
        run: AlphaRunResponse | None = None,
        canceled_run: AlphaRunResponse | None = None,
        replay: AlphaReplayResponse | None = None,
        event_pages: list[AlphaEventPageResponse] | None = None,
    ) -> None:
        self.status_response = _Status()
        self.project_response = project
        self.intent_response = intent
        self.plan_response = plan
        self.run_response = run
        self.canceled_run_response = canceled_run
        self.replay_response = replay
        self.event_pages = [] if event_pages is None else event_pages
        self.calls: list[tuple[str, object]] = []
        self.thread_ids: list[int] = []

    def status(self) -> _Status:
        return self._record("status", None, self.status_response)

    def register_alpha_project(self, request: AlphaProjectRequest) -> AlphaProjectResponse:
        assert self.project_response is not None
        return self._record("project", request, self.project_response)

    def accept_alpha_intent(self, request: AlphaIntentRequest) -> AlphaIntentResponse:
        assert self.intent_response is not None
        return self._record("intent", request, self.intent_response)

    def accept_alpha_plan(self, request: AlphaPlanRequest) -> AlphaPlanResponse:
        assert self.plan_response is not None
        return self._record("plan", request, self.plan_response)

    def submit_alpha_run(self, request: AlphaRunRequest) -> AlphaRunResponse:
        assert self.run_response is not None
        return self._record("submit", request, self.run_response)

    def inspect_alpha_run(self, run_id: str) -> AlphaRunResponse:
        assert self.run_response is not None
        return self._record("status-run", run_id, self.run_response)

    def cancel_alpha_run(
        self,
        run_id: str,
        request: AlphaCancelRunRequest,
    ) -> AlphaRunResponse:
        assert self.canceled_run_response is not None
        return self._record("cancel", (run_id, request), self.canceled_run_response)

    def replay_alpha_run(self, run_id: str) -> AlphaReplayResponse:
        assert self.replay_response is not None
        return self._record("replay", run_id, self.replay_response)

    def list_alpha_events(
        self,
        *,
        after_cursor: int = 0,
        limit: int = 100,
    ) -> AlphaEventPageResponse:
        page = self.event_pages.pop(0)
        return self._record("events", (after_cursor, limit), page)

    def _record[ResultT](self, operation: str, request: object, result: ResultT) -> ResultT:
        self.calls.append((operation, request))
        self.thread_ids.append(threading.get_ident())
        return result


def test_tui_controller_offloads_complete_client_surface_and_updates_projection(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    service = AlphaRuntimeApiService(EventStore(tmp_path / "state.sqlite3"), repository)
    project_request = _project(repository)
    intent_request = _intent()
    plan_request = _plan(repository)
    run_request = _run()
    cancel_request = AlphaCancelRunRequest(
        schema_version="alpha-cancel-run-request/v1",
        idempotency_key="cancel-run-1",
    )
    project = service.register_project(project_request, principal_id="tui:test")
    intent = service.accept_intent(intent_request, principal_id="tui:test")
    plan = service.accept_plan(plan_request, principal_id="tui:test")
    run = service.submit_run(run_request, principal_id="tui:test")
    events = service.list_events(after_cursor=0, limit=20)
    replay = service.replay_run(run.run_id)
    canceled = service.cancel_run(run.run_id, cancel_request, principal_id="tui:test")
    client = _FakeAlphaTuiClient(
        project=project,
        intent=intent,
        plan=plan,
        run=run,
        canceled_run=canceled,
        replay=replay,
        event_pages=[events],
    )
    controller = AlphaTuiController(client)
    event_loop_thread = threading.get_ident()

    async def exercise() -> None:
        await controller.connect()
        await controller.register_project(project_request)
        await controller.accept_intent(intent_request)
        await controller.accept_plan(plan_request)
        await controller.submit_run(run_request)
        await controller.refresh_events(limit=20)
        await controller.inspect_run(run.run_id)
        await controller.replay_run(run.run_id)
        await controller.cancel_run(run.run_id, cancel_request)

    asyncio.run(exercise())

    state = controller.state
    assert state.connected is state.ready is True
    assert state.endpoint == "http://127.0.0.1:8080"
    assert state.project == project
    assert state.intent == intent
    assert state.plan == plan
    assert state.run == canceled
    assert state.replay is None
    assert state.cursor == events.next_cursor
    assert state.events == events.events
    assert state.last_operation == "run-cancel"
    assert state.revision == 9
    assert [operation for operation, _ in client.calls] == [
        "status",
        "project",
        "intent",
        "plan",
        "submit",
        "events",
        "status-run",
        "replay",
        "cancel",
    ]
    assert client.thread_ids
    assert all(thread_id != event_loop_thread for thread_id in client.thread_ids)


def test_tui_controller_resumes_and_bounds_ordered_events() -> None:
    first = AlphaEventPageResponse(
        after_cursor=5,
        limit=3,
        scanned_events=2,
        events=(_event(6), _event(7)),
        next_cursor=7,
        has_more=False,
    )
    second = AlphaEventPageResponse(
        after_cursor=7,
        limit=3,
        scanned_events=3,
        events=(_event(8), _event(9), _event(10)),
        next_cursor=10,
        has_more=True,
    )
    client = _FakeAlphaTuiClient(event_pages=[first, second])
    controller = AlphaTuiController(client, initial_cursor=5, max_retained_events=3)

    async def exercise() -> None:
        await controller.refresh_events(limit=3)
        await controller.refresh_events(limit=3)

    asyncio.run(exercise())

    assert [request for operation, request in client.calls if operation == "events"] == [
        (5, 3),
        (7, 3),
    ]
    assert controller.state.cursor == 10
    assert tuple(event.cursor for event in controller.state.events) == (8, 9, 10)
    assert len({event.cursor for event in controller.state.events}) == 3


def test_tui_controller_rejects_invalid_initial_state_and_event_pages() -> None:
    client = _FakeAlphaTuiClient()
    for invalid_cursor in (-1, True, 2**63):
        with pytest.raises(AlphaTuiError) as captured:
            AlphaTuiController(client, initial_cursor=invalid_cursor)
        assert captured.value.code is AlphaTuiFailureCode.INVALID_CURSOR
    for invalid_limit in (0, True, 501):
        with pytest.raises(AlphaTuiError) as captured:
            AlphaTuiController(client, max_retained_events=invalid_limit)
        assert captured.value.code is AlphaTuiFailureCode.INVALID_EVENT_LIMIT

    invalid_pages = (
        AlphaEventPageResponse(
            after_cursor=4,
            limit=2,
            scanned_events=1,
            events=(_event(6),),
            next_cursor=6,
            has_more=False,
        ),
        AlphaEventPageResponse(
            after_cursor=5,
            limit=2,
            scanned_events=1,
            events=(_event(6),),
            next_cursor=5,
            has_more=False,
        ),
        AlphaEventPageResponse(
            after_cursor=5,
            limit=2,
            scanned_events=2,
            events=(_event(6), msgspec.structs.replace(_event(7), cursor=6)),
            next_cursor=7,
            has_more=False,
        ),
    )
    for page in invalid_pages:
        invalid_client = _FakeAlphaTuiClient(event_pages=[page])
        invalid_controller = AlphaTuiController(invalid_client, initial_cursor=5)
        with pytest.raises(AlphaTuiError) as captured:
            asyncio.run(invalid_controller.refresh_events(limit=2))
        assert captured.value.code is AlphaTuiFailureCode.INVALID_EVENT_PAGE
        assert invalid_controller.state.cursor == 5
        assert invalid_controller.state.revision == 0

    run = _run_response("different-run")
    mismatch_client = _FakeAlphaTuiClient(run=run)
    mismatch_controller = AlphaTuiController(mismatch_client)
    with pytest.raises(AlphaTuiError) as captured:
        asyncio.run(mismatch_controller.inspect_run("expected-run"))
    assert captured.value.code is AlphaTuiFailureCode.RESPONSE_BINDING_MISMATCH
    assert mismatch_controller.state.run is None


def test_tui_controller_rejects_cross_record_response_mismatches(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    service = AlphaRuntimeApiService(EventStore(tmp_path / "state.sqlite3"), repository)
    project_request = _project(repository)
    intent_request = _intent()
    plan_request = _plan(repository)
    run_request = _run()
    project = service.register_project(project_request, principal_id="tui:test")
    intent = service.accept_intent(intent_request, principal_id="tui:test")
    plan = service.accept_plan(plan_request, principal_id="tui:test")
    run = service.submit_run(run_request, principal_id="tui:test")
    replay = service.replay_run(run.run_id)

    async def reject(
        controller: AlphaTuiController,
        operation: Callable[[], Awaitable[object]],
    ) -> None:
        with pytest.raises(AlphaTuiError) as captured:
            await operation()
        assert captured.value.code is AlphaTuiFailureCode.RESPONSE_BINDING_MISMATCH
        assert controller.state.revision == 0

    async def exercise() -> None:
        project_controller = AlphaTuiController(
            _FakeAlphaTuiClient(project=msgspec.structs.replace(project, root="/mismatched-root"))
        )
        await reject(
            project_controller,
            lambda: project_controller.register_project(project_request),
        )

        intent_controller = AlphaTuiController(
            _FakeAlphaTuiClient(
                intent=msgspec.structs.replace(intent, objective="mismatched objective")
            )
        )
        await reject(
            intent_controller,
            lambda: intent_controller.accept_intent(intent_request),
        )

        plan_controller = AlphaTuiController(
            _FakeAlphaTuiClient(
                plan=msgspec.structs.replace(
                    plan,
                    topological_order=tuple(reversed(plan.topological_order)),
                )
            )
        )
        await reject(
            plan_controller,
            lambda: plan_controller.accept_plan(plan_request),
        )

        run_controller = AlphaTuiController(
            _FakeAlphaTuiClient(run=msgspec.structs.replace(run, plan_id="mismatched-plan"))
        )
        await reject(
            run_controller,
            lambda: run_controller.submit_run(run_request),
        )

        replay_controller = AlphaTuiController(
            _FakeAlphaTuiClient(
                replay=msgspec.structs.replace(
                    replay,
                    run=msgspec.structs.replace(replay.run, project_id="mismatched-project"),
                )
            )
        )
        await reject(
            replay_controller,
            lambda: replay_controller.replay_run(run.run_id),
        )

    asyncio.run(exercise())


def test_runtime_http_client_satisfies_the_tui_protocol() -> None:
    shared_client: AlphaTuiClient = RuntimeHttpClient(
        token=SecretValue("Alpha-tui-token.0123456789-ABCDEFG"),
    )

    controller = AlphaTuiController(shared_client, initial_cursor=17)

    assert controller.state.cursor == 17
    assert controller.state.endpoint is None


def _event(cursor: int) -> AlphaEventResponse:
    return AlphaEventResponse(
        event_id=f"event-{cursor}",
        cursor=cursor,
        stream_id="run:run-1",
        stream_sequence=cursor,
        event_type="alpha.run.queued",
        event_schema_version=1,
        recorded_at="2026-07-22T00:00:00Z",
        correlation_id="run-1",
        causation_id=None,
        actor="runtime",
        payload_digest="a" * 64,
        payload={},
    )


def _run_response(run_id: str) -> AlphaRunResponse:
    return AlphaRunResponse(
        run_id=run_id,
        project_id="project-1",
        intent_id="intent-1",
        plan_id="plan-1",
        status="queued",
        cancellation_requested=False,
        active_node_id=None,
        attempt=0,
        fencing_token=0,
        retained_worktree=False,
        principal_id="tui:test",
        event_id="event-run",
        cursor=1,
        event_digest="b" * 64,
    )
