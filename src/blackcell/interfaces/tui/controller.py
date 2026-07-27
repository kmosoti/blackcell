from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from itertools import pairwise
from typing import Literal, Protocol

from blackcell.interfaces.http import (
    MAX_RUN_QUERY_SCAN_EVENTS,
    MAX_RUNTIME_EVENT_PAGE_SIZE,
    CancelRunRequest,
    IntentRequest,
    IntentResponse,
    PlanRequest,
    PlanResponse,
    ProjectRequest,
    ProjectResponse,
    ReplayResponse,
    RunQueryItem,
    RunQueryRequest,
    RunQueryResponse,
    RunRequest,
    RunResponse,
    RuntimeEventPageResponse,
    RuntimeEventResponse,
    plan_topological_order,
)
from blackcell.interfaces.tui.cursor import (
    TuiCursorCheckpoint,
    TuiCursorStore,
    TuiCursorWitness,
    tui_endpoint_id,
)

MAX_TUI_RETAINED_EVENTS = 500


class TuiFailureCode(StrEnum):
    INVALID_CURSOR = "tui-invalid-cursor"
    INVALID_EVENT_LIMIT = "tui-invalid-event-limit"
    INVALID_EVENT_PAGE = "tui-invalid-event-page"
    INVALID_CURSOR_CHECKPOINT = "tui-invalid-cursor-checkpoint"
    INVALID_WORKFLOW_REQUEST = "tui-invalid-workflow-request"
    CURSOR_STORE_NOT_CONNECTED = "tui-cursor-store-not-connected"
    RESPONSE_BINDING_MISMATCH = "tui-response-binding-mismatch"
    INVALID_RUN_QUERY = "tui-invalid-run-query"


class TuiError(RuntimeError):
    """A content-free local projection failure."""

    def __init__(self, code: TuiFailureCode) -> None:
        self.code = code
        super().__init__(code.value)


class ServiceStatus(Protocol):
    @property
    def endpoint(self) -> str: ...

    @property
    def live(self) -> bool: ...

    @property
    def ready(self) -> bool: ...


class TuiClient(Protocol):
    """The complete authority-free client surface consumed by an interactive projection."""

    def status(self) -> ServiceStatus: ...

    def register_project(self, request: ProjectRequest) -> ProjectResponse: ...

    def accept_intent(self, request: IntentRequest) -> IntentResponse: ...

    def accept_plan(self, request: PlanRequest) -> PlanResponse: ...

    def submit_run(self, request: RunRequest) -> RunResponse: ...

    def inspect_run(self, run_id: str) -> RunResponse: ...

    def query_runs(self, request: RunQueryRequest) -> RunQueryResponse: ...

    def cancel_run(
        self,
        run_id: str,
        request: CancelRunRequest,
    ) -> RunResponse: ...

    def replay_run(self, run_id: str) -> ReplayResponse: ...

    def list_events(
        self,
        *,
        after_cursor: int = 0,
        limit: int = 100,
    ) -> RuntimeEventPageResponse: ...


TuiOperation = Literal[
    "connect",
    "project-register",
    "intent-accept",
    "plan-accept",
    "run-submit",
    "run-status",
    "run-cancel",
    "run-replay",
    "events-refresh",
]


@dataclass(frozen=True, slots=True)
class TuiProjection:
    connected: bool = False
    ready: bool = False
    endpoint: str | None = None
    cursor: int = 0
    events: tuple[RuntimeEventResponse, ...] = ()
    runs: tuple[RunQueryItem, ...] = ()
    project: ProjectResponse | None = None
    intent: IntentResponse | None = None
    plan: PlanResponse | None = None
    run: RunResponse | None = None
    replay: ReplayResponse | None = None
    last_operation: TuiOperation | None = None
    revision: int = 0
    schema_version: Literal["tui-projection/v1"] = "tui-projection/v1"


class TuiController:
    """Async projection controller over the synchronous shared daemon client."""

    def __init__(
        self,
        client: TuiClient,
        *,
        initial_cursor: int = 0,
        max_retained_events: int = MAX_TUI_RETAINED_EVENTS,
        cursor_store: TuiCursorStore | None = None,
    ) -> None:
        _validate_cursor(initial_cursor)
        if cursor_store is not None and initial_cursor != 0:
            raise TuiError(TuiFailureCode.INVALID_CURSOR_CHECKPOINT)
        if (
            isinstance(max_retained_events, bool)
            or not isinstance(max_retained_events, int)
            or not 1 <= max_retained_events <= MAX_TUI_RETAINED_EVENTS
        ):
            raise TuiError(TuiFailureCode.INVALID_EVENT_LIMIT)
        self._client = client
        self._state = TuiProjection(cursor=initial_cursor)
        self._max_retained_events = max_retained_events
        self._cursor_store = cursor_store
        self._command_lock = asyncio.Lock()
        self._event_lock = asyncio.Lock()

    @property
    def state(self) -> TuiProjection:
        return self._state

    async def connect(self) -> TuiProjection:
        async with self._command_lock:
            status = await _offload(self._client.status)
            query_request = RunQueryRequest(
                schema_version="run-query-request/v1",
                limit=50,
            )
            query = await _offload(self._client.query_runs, query_request)
            _validate_run_query(query, query_request)
            async with self._event_lock:
                cursor = self._state.cursor
                events = self._state.events
                if self._cursor_store is not None:
                    endpoint_id = tui_endpoint_id(status.endpoint)
                    checkpoint = await _offload(self._cursor_store.load, endpoint_id)
                    events = await self._verify_cursor_checkpoint(checkpoint)
                    cursor = checkpoint.cursor
                self._state = replace(
                    self._state,
                    connected=status.live,
                    ready=status.ready,
                    endpoint=status.endpoint,
                    cursor=cursor,
                    events=events,
                    runs=query.runs,
                    last_operation="connect",
                    revision=self._state.revision + 1,
                )
                return self._state

    async def register_project(self, request: ProjectRequest) -> TuiProjection:
        async with self._command_lock:
            response = await _offload(self._client.register_project, request)
            _require_project_binding(response, request)
            self._state = replace(
                self._state,
                project=response,
                intent=None,
                plan=None,
                run=None,
                replay=None,
                last_operation="project-register",
                revision=self._state.revision + 1,
            )
            return self._state

    async def accept_intent(self, request: IntentRequest) -> TuiProjection:
        async with self._command_lock:
            response = await _offload(self._client.accept_intent, request)
            _require_intent_binding(response, request)
            self._state = replace(
                self._state,
                project=_matching_project(self._state, response.project_id),
                intent=response,
                plan=None,
                run=None,
                replay=None,
                last_operation="intent-accept",
                revision=self._state.revision + 1,
            )
            return self._state

    async def accept_plan(self, request: PlanRequest) -> TuiProjection:
        async with self._command_lock:
            response = await _offload(self._client.accept_plan, request)
            _require_plan_binding(response, request)
            self._state = replace(
                self._state,
                project=_matching_project(self._state, response.project_id),
                intent=_matching_intent(
                    self._state,
                    project_id=response.project_id,
                    intent_id=response.intent_id,
                ),
                plan=response,
                run=None,
                replay=None,
                last_operation="plan-accept",
                revision=self._state.revision + 1,
            )
            return self._state

    async def submit_run(self, request: RunRequest) -> TuiProjection:
        async with self._command_lock:
            response = await _offload(self._client.submit_run, request)
            _require_run_binding(response, request)
            self._state = replace(
                self._state,
                project=_matching_project(self._state, response.project_id),
                intent=_matching_intent(
                    self._state,
                    project_id=response.project_id,
                    intent_id=response.intent_id,
                ),
                plan=_matching_plan(
                    self._state,
                    project_id=response.project_id,
                    intent_id=response.intent_id,
                    plan_id=response.plan_id,
                ),
                run=response,
                replay=None,
                last_operation="run-submit",
                revision=self._state.revision + 1,
            )
            return self._state

    async def inspect_run(self, run_id: str) -> TuiProjection:
        async with self._command_lock:
            response = await _offload(self._client.inspect_run, run_id)
            _require_run_id(response.run_id, run_id)
            self._state = replace(
                self._state,
                project=_matching_project(self._state, response.project_id),
                intent=_matching_intent(
                    self._state,
                    project_id=response.project_id,
                    intent_id=response.intent_id,
                ),
                plan=_matching_plan(
                    self._state,
                    project_id=response.project_id,
                    intent_id=response.intent_id,
                    plan_id=response.plan_id,
                ),
                run=response,
                replay=None,
                last_operation="run-status",
                revision=self._state.revision + 1,
            )
            return self._state

    async def cancel_run(
        self,
        run_id: str,
        request: CancelRunRequest,
    ) -> TuiProjection:
        async with self._command_lock:
            response = await _offload(self._client.cancel_run, run_id, request)
            _require_run_id(response.run_id, run_id)
            self._state = replace(
                self._state,
                project=_matching_project(self._state, response.project_id),
                intent=_matching_intent(
                    self._state,
                    project_id=response.project_id,
                    intent_id=response.intent_id,
                ),
                plan=_matching_plan(
                    self._state,
                    project_id=response.project_id,
                    intent_id=response.intent_id,
                    plan_id=response.plan_id,
                ),
                run=response,
                replay=None,
                last_operation="run-cancel",
                revision=self._state.revision + 1,
            )
            return self._state

    async def replay_run(self, run_id: str) -> TuiProjection:
        async with self._command_lock:
            response = await _offload(self._client.replay_run, run_id)
            _require_replay_binding(response, run_id)
            self._state = replace(
                self._state,
                project=response.project,
                intent=response.intent,
                plan=response.plan,
                run=response.run,
                replay=response,
                last_operation="run-replay",
                revision=self._state.revision + 1,
            )
            return self._state

    async def refresh_events(self, *, limit: int = 100) -> TuiProjection:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_RUNTIME_EVENT_PAGE_SIZE
        ):
            raise TuiError(TuiFailureCode.INVALID_EVENT_LIMIT)
        async with self._event_lock:
            if self._cursor_store is not None and self._state.endpoint is None:
                raise TuiError(TuiFailureCode.CURSOR_STORE_NOT_CONNECTED)
            after_cursor = self._state.cursor
            page = await _offload(
                self._client.list_events,
                after_cursor=after_cursor,
                limit=limit,
            )
            _validate_event_page(page, after_cursor=after_cursor, limit=limit)
            events = (*self._state.events, *page.events)[-self._max_retained_events :]
            if self._cursor_store is not None and page.next_cursor != after_cursor:
                assert self._state.endpoint is not None
                checkpoint = _cursor_checkpoint(
                    endpoint=self._state.endpoint,
                    cursor=page.next_cursor,
                    events=events,
                )
                await _offload(self._cursor_store.save, checkpoint)
            self._state = replace(
                self._state,
                cursor=page.next_cursor,
                events=events,
                last_operation="events-refresh",
                revision=self._state.revision + 1,
            )
            return self._state

    async def _verify_cursor_checkpoint(
        self,
        checkpoint: TuiCursorCheckpoint,
    ) -> tuple[RuntimeEventResponse, ...]:
        if checkpoint.cursor == 0:
            return ()
        position_page = await self._checkpoint_probe(checkpoint.cursor)
        if position_page.scanned_events != 1 or position_page.next_cursor != checkpoint.cursor:
            raise TuiError(TuiFailureCode.INVALID_CURSOR_CHECKPOINT)
        if checkpoint.witness is None:
            if position_page.events:
                raise TuiError(TuiFailureCode.INVALID_CURSOR_CHECKPOINT)
            return ()
        witness_page = (
            position_page
            if checkpoint.witness.cursor == checkpoint.cursor
            else await self._checkpoint_probe(checkpoint.witness.cursor)
        )
        if (
            witness_page.scanned_events != 1
            or witness_page.next_cursor != checkpoint.witness.cursor
            or len(witness_page.events) != 1
        ):
            raise TuiError(TuiFailureCode.INVALID_CURSOR_CHECKPOINT)
        event = witness_page.events[0]
        if (
            event.cursor != checkpoint.witness.cursor
            or event.event_id != checkpoint.witness.event_id
            or event.payload_digest != checkpoint.witness.payload_digest
        ):
            raise TuiError(TuiFailureCode.INVALID_CURSOR_CHECKPOINT)
        return (event,)

    async def _checkpoint_probe(self, cursor: int) -> RuntimeEventPageResponse:
        page = await _offload(
            self._client.list_events,
            after_cursor=cursor - 1,
            limit=1,
        )
        try:
            _validate_event_page(page, after_cursor=cursor - 1, limit=1)
        except TuiError as error:
            raise TuiError(TuiFailureCode.INVALID_CURSOR_CHECKPOINT) from error
        return page


async def _offload[**Parameters, ResultT](
    operation: Callable[Parameters, ResultT],
    *args: Parameters.args,
    **kwargs: Parameters.kwargs,
) -> ResultT:
    return await asyncio.to_thread(operation, *args, **kwargs)


def _require_run_id(actual: str, expected: str) -> None:
    if actual != expected:
        raise TuiError(TuiFailureCode.RESPONSE_BINDING_MISMATCH)


def _require_project_binding(
    response: ProjectResponse,
    request: ProjectRequest,
) -> None:
    if (
        response.project_id,
        response.root,
        response.configuration_provider,
        response.configuration_version,
        response.configuration_digest,
    ) != (
        request.project_id,
        request.root,
        request.configuration_provider,
        request.configuration_version,
        request.configuration_digest,
    ):
        raise TuiError(TuiFailureCode.RESPONSE_BINDING_MISMATCH)


def _require_intent_binding(
    response: IntentResponse,
    request: IntentRequest,
) -> None:
    if (
        response.intent_id,
        response.project_id,
        response.objective,
        response.constraints,
        response.assumptions,
        response.unresolved_questions,
    ) != (
        request.intent_id,
        request.project_id,
        request.objective,
        request.constraints,
        request.assumptions,
        request.unresolved_questions,
    ):
        raise TuiError(TuiFailureCode.RESPONSE_BINDING_MISMATCH)


def _require_plan_binding(response: PlanResponse, request: PlanRequest) -> None:
    if (
        response.plan_id,
        response.project_id,
        response.intent_id,
        response.base_commit,
        response.allowed_effects,
        response.nodes,
        response.topological_order,
    ) != (
        request.plan_id,
        request.project_id,
        request.intent_id,
        request.base_commit,
        request.allowed_effects,
        request.nodes,
        plan_topological_order(request.nodes),
    ):
        raise TuiError(TuiFailureCode.RESPONSE_BINDING_MISMATCH)


def _require_run_binding(response: RunResponse, request: RunRequest) -> None:
    if (
        response.run_id,
        response.project_id,
        response.intent_id,
        response.plan_id,
    ) != (
        request.run_id,
        request.project_id,
        request.intent_id,
        request.plan_id,
    ):
        raise TuiError(TuiFailureCode.RESPONSE_BINDING_MISMATCH)


def _require_replay_binding(response: ReplayResponse, expected_run_id: str) -> None:
    project = response.project
    intent = response.intent
    plan = response.plan
    run = response.run
    if (
        response.run_id != expected_run_id
        or run.run_id != expected_run_id
        or intent.project_id != project.project_id
        or plan.project_id != project.project_id
        or plan.intent_id != intent.intent_id
        or run.project_id != project.project_id
        or run.intent_id != intent.intent_id
        or run.plan_id != plan.plan_id
        or plan.topological_order != plan_topological_order(plan.nodes)
    ):
        raise TuiError(TuiFailureCode.RESPONSE_BINDING_MISMATCH)


def _matching_project(
    state: TuiProjection,
    project_id: str,
) -> ProjectResponse | None:
    return (
        state.project
        if state.project is not None and state.project.project_id == project_id
        else None
    )


def _matching_intent(
    state: TuiProjection,
    *,
    project_id: str,
    intent_id: str,
) -> IntentResponse | None:
    intent = state.intent
    if intent is not None and (intent.project_id, intent.intent_id) == (project_id, intent_id):
        return intent
    return None


def _matching_plan(
    state: TuiProjection,
    *,
    project_id: str,
    intent_id: str,
    plan_id: str,
) -> PlanResponse | None:
    plan = state.plan
    if plan is not None and (plan.project_id, plan.intent_id, plan.plan_id) == (
        project_id,
        intent_id,
        plan_id,
    ):
        return plan
    return None


def _cursor_checkpoint(
    *,
    endpoint: str,
    cursor: int,
    events: tuple[RuntimeEventResponse, ...],
) -> TuiCursorCheckpoint:
    witness = None
    if events:
        event = events[-1]
        witness = TuiCursorWitness(
            cursor=event.cursor,
            event_id=event.event_id,
            payload_digest=event.payload_digest,
        )
    return TuiCursorCheckpoint(
        endpoint_id=tui_endpoint_id(endpoint),
        cursor=cursor,
        witness=witness,
    )


def _validate_cursor(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2**63 - 1:
        raise TuiError(TuiFailureCode.INVALID_CURSOR)


def _validate_event_page(
    page: RuntimeEventPageResponse,
    *,
    after_cursor: int,
    limit: int,
) -> None:
    cursors = tuple(event.cursor for event in page.events)
    if (
        page.after_cursor != after_cursor
        or page.limit != limit
        or isinstance(page.scanned_events, bool)
        or not isinstance(page.scanned_events, int)
        or not 0 <= page.scanned_events <= limit
        or len(page.events) > page.scanned_events
        or isinstance(page.next_cursor, bool)
        or not isinstance(page.next_cursor, int)
        or not after_cursor <= page.next_cursor <= 2**63 - 1
        or not isinstance(page.has_more, bool)
        or (page.scanned_events == 0 and page.next_cursor != after_cursor)
        or (page.scanned_events > 0 and page.next_cursor <= after_cursor)
        or (page.scanned_events < limit and page.has_more)
        or (page.has_more and page.next_cursor == after_cursor)
        or any(
            isinstance(cursor, bool)
            or not isinstance(cursor, int)
            or not after_cursor < cursor <= page.next_cursor
            for cursor in cursors
        )
        or any(previous >= current for previous, current in pairwise(cursors))
        or len({event.event_id for event in page.events}) != len(page.events)
    ):
        raise TuiError(TuiFailureCode.INVALID_EVENT_PAGE)


def _validate_run_query(
    response: RunQueryResponse,
    request: RunQueryRequest,
) -> None:
    queued_cursors = tuple(item.queued_cursor for item in response.runs)
    run_ids = tuple(item.run.run_id for item in response.runs)
    if (
        not isinstance(response, RunQueryResponse)
        or response.query != request
        or isinstance(response.scanned_events, bool)
        or not isinstance(response.scanned_events, int)
        or not 0 <= response.scanned_events <= MAX_RUN_QUERY_SCAN_EVENTS
        or len(response.runs) > request.limit
        or len(response.runs) > response.scanned_events
        or isinstance(response.next_cursor, bool)
        or not isinstance(response.next_cursor, int)
        or not request.after_cursor <= response.next_cursor <= 2**63 - 1
        or not isinstance(response.has_more, bool)
        or (response.scanned_events == 0 and response.next_cursor != request.after_cursor)
        or (response.scanned_events > 0 and response.next_cursor <= request.after_cursor)
        or (response.has_more and response.next_cursor == request.after_cursor)
        or (
            response.has_more
            and response.scanned_events < MAX_RUN_QUERY_SCAN_EVENTS
            and len(response.runs) < request.limit
        )
        or any(
            isinstance(cursor, bool)
            or not isinstance(cursor, int)
            or not request.after_cursor < cursor <= response.next_cursor
            for cursor in queued_cursors
        )
        or any(previous >= current for previous, current in pairwise(queued_cursors))
        or len(run_ids) != len(set(run_ids))
        or any(not _run_query_item_matches(item, request) for item in response.runs)
    ):
        raise TuiError(TuiFailureCode.INVALID_RUN_QUERY)


def _run_query_item_matches(
    item: RunQueryItem,
    request: RunQueryRequest,
) -> bool:
    run = item.run
    node_ids = tuple(node.node_id for node in item.nodes)
    return not (
        item.queued_cursor > run.cursor
        or (request.statuses and run.status not in request.statuses)
        or (request.project_ids and run.project_id not in request.project_ids)
        or (request.intent_ids and run.intent_id not in request.intent_ids)
        or (request.plan_ids and run.plan_id not in request.plan_ids)
        or (request.run_ids and run.run_id not in request.run_ids)
        or not item.nodes
        or len(node_ids) != len(set(node_ids))
        or any(
            isinstance(node.attempts, bool)
            or not isinstance(node.attempts, int)
            or node.attempts < 0
            or isinstance(node.fencing_token, bool)
            or not isinstance(node.fencing_token, int)
            or node.fencing_token < 0
            for node in item.nodes
        )
    )


__all__ = [
    "MAX_TUI_RETAINED_EVENTS",
    "ServiceStatus",
    "TuiClient",
    "TuiController",
    "TuiError",
    "TuiFailureCode",
    "TuiOperation",
    "TuiProjection",
]
