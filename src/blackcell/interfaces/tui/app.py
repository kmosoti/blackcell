from __future__ import annotations

import asyncio
import os
import stat
import time
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, cast
from uuid import uuid4

from pyratatui import (
    AsyncTerminal,
    Block,
    Color,
    Constraint,
    Direction,
    Layout,
    Paragraph,
    Rect,
    Style,
)

from blackcell.interfaces.http import (
    CancelRunRequest,
    IntentRequest,
    PlanRequest,
    ProjectRequest,
    RunBudgetUsageResponse,
    RunQueryItem,
    RunRequest,
    WireContractError,
    decode_contract,
)
from blackcell.interfaces.tui.controller import (
    TuiError,
    TuiFailureCode,
    TuiProjection,
)

_MAX_RUN_ID_CHARS = 120
_MAX_WORKFLOW_PATH_CHARS = 4_096
_MAX_WORKFLOW_REQUEST_BYTES = 1024 * 1024
_MAX_RENDERED_ARTIFACTS = 20
_MAX_RENDERED_FINDINGS = 20
_MAX_RENDERED_TEXT_CHARS = 240
_RUN_ID_CHARACTERS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._")
_EVENT_PAGE_LIMIT = 100
_MIN_REFRESH_SECONDS = 0.25
_MAX_REFRESH_SECONDS = 60.0
_MIN_FRAMES_PER_SECOND = 1.0
_MAX_FRAMES_PER_SECOND = 60.0

type TuiRunAction = Literal["status", "replay", "cancel"]
type TuiWorkflowOperation = Literal["project", "intent", "plan", "run"]
type TuiInputMode = Literal["workflow-path", "run-id"]
type TuiWorkflowRequest = ProjectRequest | IntentRequest | PlanRequest | RunRequest
type TuiControllerFactory = Callable[[], TuiShellController]
type TuiIdempotencyFactory = Callable[[], str]
type TuiTerminalFactory = Callable[[], TuiTerminal]

_WORKFLOW_KEYS: dict[str, TuiWorkflowOperation] = {
    "1": "project",
    "2": "intent",
    "3": "plan",
    "4": "run",
}


class TuiShellController(Protocol):
    @property
    def state(self) -> TuiProjection: ...

    async def connect(self) -> TuiProjection: ...

    async def register_project(self, request: ProjectRequest) -> TuiProjection: ...

    async def accept_intent(self, request: IntentRequest) -> TuiProjection: ...

    async def accept_plan(self, request: PlanRequest) -> TuiProjection: ...

    async def submit_run(self, request: RunRequest) -> TuiProjection: ...

    async def inspect_run(self, run_id: str) -> TuiProjection: ...

    async def replay_run(self, run_id: str) -> TuiProjection: ...

    async def cancel_run(
        self,
        run_id: str,
        request: CancelRunRequest,
    ) -> TuiProjection: ...

    async def refresh_events(self, *, limit: int = 100) -> TuiProjection: ...


class TuiKeyEvent(Protocol):
    code: str
    ctrl: bool
    alt: bool
    shift: bool


class TuiFrame(Protocol):
    area: Rect

    def render_widget(self, widget: object, area: Rect) -> None: ...


class TuiTerminal(Protocol):
    async def __aenter__(self) -> TuiTerminal: ...

    async def __aexit__(self, *args: object) -> bool: ...

    def events(
        self,
        fps: float = 30.0,
        *,
        stop_on_quit: bool = False,
    ) -> AsyncIterator[TuiKeyEvent | None]: ...

    def draw(self, draw_fn: Callable[[TuiFrame], None]) -> None: ...


@dataclass(frozen=True, slots=True)
class TuiView:
    connection: str
    message: str
    message_is_error: bool
    workflow_operation: TuiWorkflowOperation
    workflow_path: str
    run_id: str
    input_mode: TuiInputMode | None
    connection_busy: bool
    events_busy: bool
    command_busy: bool
    events: str
    workflow: str
    run: str
    quit_requested: bool
    schema_version: Literal["tui-view/v1"] = "tui-view/v1"


class TuiApp:
    """Thin PyRatatui projection over one injected authority-free execution controller."""

    def __init__(
        self,
        controller_factory: TuiControllerFactory,
        *,
        event_refresh_seconds: float | None = 1.0,
        frames_per_second: float = 20.0,
        idempotency_factory: TuiIdempotencyFactory | None = None,
        terminal_factory: TuiTerminalFactory | None = None,
    ) -> None:
        _validate_interval(
            event_refresh_seconds,
            name="event_refresh_seconds",
            minimum=_MIN_REFRESH_SECONDS,
            maximum=_MAX_REFRESH_SECONDS,
            optional=True,
        )
        _validate_interval(
            frames_per_second,
            name="frames_per_second",
            minimum=_MIN_FRAMES_PER_SECOND,
            maximum=_MAX_FRAMES_PER_SECOND,
            optional=False,
        )
        self._controller_factory = controller_factory
        self._event_refresh_seconds = event_refresh_seconds
        self._frames_per_second = float(frames_per_second)
        self._idempotency_factory = idempotency_factory or _cancel_idempotency_key
        self._terminal_factory = terminal_factory or _pyratatui_terminal
        self._controller: TuiShellController | None = None
        self._connection_task: asyncio.Task[None] | None = None
        self._events_task: asyncio.Task[None] | None = None
        self._command_task: asyncio.Task[None] | None = None
        self._workflow_operation: TuiWorkflowOperation = "project"
        self._workflow_path = ""
        self._run_id = ""
        self._input_mode: TuiInputMode | None = None
        self._message = "tui-not-started"
        self._message_is_error = False
        self._quit_requested = False
        self._started = False
        self._last_refresh_started = time.monotonic()

    @property
    def view(self) -> TuiView:
        state = self._state
        endpoint = state.endpoint or "endpoint-unavailable"
        readiness = "ready" if state.ready else "not-ready"
        connection = f"{readiness} · {endpoint} · cursor {state.cursor}"
        return TuiView(
            connection=connection,
            message=self._message,
            message_is_error=self._message_is_error,
            workflow_operation=self._workflow_operation,
            workflow_path=self._workflow_path,
            run_id=self._run_id,
            input_mode=self._input_mode,
            connection_busy=_task_active(self._connection_task),
            events_busy=_task_active(self._events_task),
            command_busy=_task_active(self._command_task),
            events=_event_summary(state),
            workflow=_workflow_summary(state),
            run=_run_summary(state),
            quit_requested=self._quit_requested,
        )

    @property
    def _state(self) -> TuiProjection:
        if self._controller is None:
            return TuiProjection()
        return self._controller.state

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        try:
            self._controller = self._controller_factory()
        except Exception:
            self._set_message("tui-controller-unavailable", error=True)
            return
        self.action_connect()

    async def run(self) -> None:
        await self.start()
        terminal = self._terminal_factory()
        try:
            async with terminal as active_terminal:
                async for event in active_terminal.events(
                    fps=self._frames_per_second,
                    stop_on_quit=False,
                ):
                    if event is not None:
                        self.handle_key(event)
                    self._start_periodic_refresh()
                    active_terminal.draw(self.render)
                    if self._quit_requested:
                        break
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        tasks = self._tasks
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def wait_idle(self) -> None:
        while tasks := self._tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(0)

    @property
    def _tasks(self) -> tuple[asyncio.Task[None], ...]:
        return tuple(
            task
            for task in (self._connection_task, self._events_task, self._command_task)
            if task is not None and not task.done()
        )

    def handle_key(self, event: TuiKeyEvent) -> bool:
        if self._input_mode is not None:
            return self._handle_input_key(event)
        code = event.code
        if code == "q" or (event.ctrl and code == "c"):
            self._quit_requested = True
        elif code == "c":
            self.action_connect()
        elif code == "r":
            self.action_refresh_events()
        elif code in {"1", "2", "3", "4"}:
            self._workflow_operation = _WORKFLOW_KEYS[code]
            self._set_message(f"tui-workflow-{self._workflow_operation}-selected")
        elif code == "w" and event.ctrl:
            self.action_submit_workflow()
        elif code == "w":
            self._input_mode = "workflow-path"
            self._set_message("tui-workflow-path-editing")
        elif code == "i":
            self._input_mode = "run-id"
            self._set_message("tui-run-id-editing")
        elif code == "s":
            self.action_run("status")
        elif code == "p":
            self.action_run("replay")
        elif code == "x" or (event.ctrl and code == "x"):
            self.action_run("cancel")
        else:
            return False
        return True

    def _handle_input_key(self, event: TuiKeyEvent) -> bool:
        mode = self._input_mode
        assert mode is not None
        if event.code in {"esc", "escape"}:
            self._input_mode = None
            self._set_message("tui-input-canceled")
            return True
        if event.code == "enter":
            self._input_mode = None
            if mode == "workflow-path":
                self.action_submit_workflow()
            else:
                self._set_message("tui-run-id-accepted")
            return True
        if event.ctrl and event.code == "u":
            self._replace_input(mode, "")
            return True
        if event.code == "backspace":
            self._replace_input(mode, self._input_value(mode)[:-1])
            return True
        if len(event.code) == 1 and event.code.isprintable() and not event.ctrl and not event.alt:
            current = self._input_value(mode)
            maximum = _MAX_WORKFLOW_PATH_CHARS if mode == "workflow-path" else _MAX_RUN_ID_CHARS
            if len(current) >= maximum:
                self._set_message("tui-input-limit", error=True)
                return True
            if mode == "run-id" and event.code not in _RUN_ID_CHARACTERS:
                self._set_message("tui-invalid-run-id", error=True)
                return True
            self._replace_input(mode, current + event.code)
            return True
        return False

    def _input_value(self, mode: TuiInputMode) -> str:
        return self._workflow_path if mode == "workflow-path" else self._run_id

    def _replace_input(self, mode: TuiInputMode, value: str) -> None:
        if mode == "workflow-path":
            self._workflow_path = value
        else:
            self._run_id = value

    def action_connect(self) -> bool:
        if self._controller is None:
            self._set_message("tui-controller-unavailable", error=True)
            return False
        return self._schedule("_connection_task", self._connect)

    def action_refresh_events(self) -> bool:
        if self._controller is None or not self._controller.state.connected:
            self._set_message("tui-not-connected", error=True)
            return False
        started = self._schedule("_events_task", self._refresh_events)
        if started:
            self._last_refresh_started = time.monotonic()
        return started

    def action_submit_workflow(self) -> bool:
        if self._controller is None or not self._controller.state.connected:
            self._set_message("tui-not-connected", error=True)
            return False
        raw_path = self._workflow_path
        self._workflow_path = ""
        path = _valid_workflow_path(raw_path)
        if path is None:
            self._set_message("tui-invalid-workflow-request", error=True)
            return False
        operation = self._workflow_operation
        return self._schedule(
            "_command_task",
            lambda: self._workflow_command(operation, path),
        )

    def action_run(self, operation: TuiRunAction) -> bool:
        if self._controller is None or not self._controller.state.connected:
            self._set_message("tui-not-connected", error=True)
            return False
        run_id = _valid_run_id(self._run_id)
        if run_id is None:
            self._set_message("tui-invalid-run-id", error=True)
            return False
        return self._schedule(
            "_command_task",
            lambda: self._run_command(operation, run_id),
        )

    def _schedule(
        self,
        attribute: Literal["_connection_task", "_events_task", "_command_task"],
        operation: Callable[[], Coroutine[object, object, None]],
    ) -> bool:
        current = getattr(self, attribute)
        if _task_active(current):
            self._set_message("tui-operation-busy", error=True)
            return False
        task = asyncio.create_task(operation())
        setattr(self, attribute, task)
        task.add_done_callback(lambda completed: self._finish_task(attribute, completed))
        return True

    def _finish_task(
        self,
        attribute: Literal["_connection_task", "_events_task", "_command_task"],
        task: asyncio.Task[None],
    ) -> None:
        if getattr(self, attribute) is task:
            setattr(self, attribute, None)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            self._set_message("tui-operation-failed", error=True)

    def _start_periodic_refresh(self) -> None:
        interval = self._event_refresh_seconds
        if (
            interval is not None
            and self._controller is not None
            and self._controller.state.connected
            and time.monotonic() - self._last_refresh_started >= interval
        ):
            self.action_refresh_events()

    async def _connect(self) -> None:
        assert self._controller is not None
        self._set_message("tui-connecting")
        try:
            await self._controller.connect()
        except Exception as error:
            self._set_message(_failure_code(error), error=True)
        else:
            self._set_message("tui-connected")

    async def _refresh_events(self) -> None:
        assert self._controller is not None
        try:
            await self._controller.refresh_events(limit=_EVENT_PAGE_LIMIT)
        except Exception as error:
            self._set_message(_failure_code(error), error=True)
        else:
            self._set_message("tui-events-refreshed")

    async def _workflow_command(
        self,
        operation: TuiWorkflowOperation,
        path: Path,
    ) -> None:
        assert self._controller is not None
        self._set_message(f"tui-workflow-{operation}-pending")
        try:
            request = await asyncio.to_thread(_load_workflow_request, operation, path)
            if operation == "project" and isinstance(request, ProjectRequest):
                await self._controller.register_project(request)
            elif operation == "intent" and isinstance(request, IntentRequest):
                await self._controller.accept_intent(request)
            elif operation == "plan" and isinstance(request, PlanRequest):
                await self._controller.accept_plan(request)
            elif operation == "run" and isinstance(request, RunRequest):
                await self._controller.submit_run(request)
            else:  # pragma: no cover - closed loader invariant
                raise TuiError(TuiFailureCode.INVALID_WORKFLOW_REQUEST)
        except Exception as error:
            self._set_message(_failure_code(error), error=True)
        else:
            self._set_message(f"tui-workflow-{operation}-complete")

    async def _run_command(self, operation: TuiRunAction, run_id: str) -> None:
        assert self._controller is not None
        self._set_message(f"tui-run-{operation}-pending")
        try:
            if operation == "status":
                await self._controller.inspect_run(run_id)
            elif operation == "replay":
                await self._controller.replay_run(run_id)
            else:
                await self._controller.cancel_run(
                    run_id,
                    CancelRunRequest(
                        schema_version="execution-cancel-run-request/v1",
                        idempotency_key=self._idempotency_factory(),
                    ),
                )
        except Exception as error:
            self._set_message(_failure_code(error), error=True)
        else:
            self._set_message(f"tui-run-{operation}-complete")

    def _set_message(self, value: str, *, error: bool = False) -> None:
        self._message = value
        self._message_is_error = error

    def render(self, frame: TuiFrame) -> None:
        view = self.view
        outer = (
            Layout()
            .direction(Direction.Vertical)
            .constraints([Constraint.length(3), Constraint.fill(1), Constraint.length(4)])
        )
        header_area, body_area, footer_area = outer.split(frame.area)
        columns = (
            Layout()
            .direction(Direction.Horizontal)
            .constraints([Constraint.percentage(50), Constraint.fill(1)])
        )
        left_area, right_area = columns.split(body_area)
        left = (
            Layout()
            .direction(Direction.Vertical)
            .constraints([Constraint.percentage(34), Constraint.fill(1)])
        )
        runs_area, graph_area = left.split(left_area)
        right = (
            Layout()
            .direction(Direction.Vertical)
            .constraints([Constraint.percentage(50), Constraint.fill(1)])
        )
        attempt_area, verifier_area = right.split(right_area)

        frame.render_widget(
            Paragraph.from_string(f"BlackCell Runtime · {view.connection}")
            .style(Style().fg(Color.cyan()).bold())
            .block(Block().bordered().title(" PyRatatui ")),
            header_area,
        )
        frame.render_widget(
            _panel(_runs_summary(self._state, view.connection), " Runs "),
            runs_area,
        )
        frame.render_widget(
            _panel(_task_graph_summary(self._state), " Task graph "),
            graph_area,
        )
        frame.render_widget(
            _panel(_run_summary(self._state), " Attempt detail "),
            attempt_area,
        )
        frame.render_widget(
            _panel(_verification_summary(self._state), " Verifier / output "),
            verifier_area,
        )
        footer_color = Color.light_red() if view.message_is_error else Color.light_green()
        footer = (
            "1-4 operation · w edit path · Ctrl-W submit · i edit run · "
            "s status · p replay · x cancel · c connect · r refresh · q quit\n"
            f"{view.message}"
        )
        frame.render_widget(
            Paragraph.from_string(footer)
            .style(Style().fg(footer_color))
            .block(Block().bordered().title(" Commands ")),
            footer_area,
        )


def _pyratatui_terminal() -> TuiTerminal:
    # PyRatatui 0.2.9's published stub models its async-generator ``events``
    # method as a coroutine. Runtime behavior is source-checked and covered by
    # the injected-terminal contract test, so keep the mismatch at this edge.
    return cast("TuiTerminal", AsyncTerminal())


def _panel(value: str, title: str) -> Paragraph:
    return (
        Paragraph.from_string(value)
        .wrap(True, True)
        .block(Block().bordered().title(title).border_style(Style().fg(Color.dark_gray())))
    )


def _workflow_panel(view: TuiView) -> str:
    marker = ">" if view.input_mode == "workflow-path" else " "
    path = view.workflow_path or "absolute request JSON path"
    busy = "busy" if view.command_busy else "idle"
    return f"Operation: {view.workflow_operation} · {busy}\n{marker} {path}\n\n{view.workflow}"


def _run_panel(view: TuiView) -> str:
    marker = ">" if view.input_mode == "run-id" else " "
    run_id = view.run_id or "run-id"
    return f"{marker} {run_id}\n\n{view.run}"


def _validate_interval(
    value: float | None,
    *,
    name: str,
    minimum: float,
    maximum: float,
    optional: bool,
) -> None:
    if value is None and optional:
        return
    if (
        value is None
        or isinstance(value, bool)
        or not isinstance(value, int | float)
        or not minimum <= value <= maximum
    ):
        raise ValueError(f"{name} must be between {minimum:g} and {maximum:g}")


def _task_active(task: asyncio.Task[None] | None) -> bool:
    return task is not None and not task.done()


def _valid_run_id(value: str) -> str | None:
    normalized = value.strip()
    if not 1 <= len(normalized) <= _MAX_RUN_ID_CHARS or any(
        character not in _RUN_ID_CHARACTERS for character in normalized
    ):
        return None
    return normalized


def _valid_workflow_path(value: str) -> Path | None:
    normalized = value.strip()
    if (
        not 1 <= len(normalized) <= _MAX_WORKFLOW_PATH_CHARS
        or "\x00" in normalized
        or not Path(normalized).is_absolute()
    ):
        return None
    return Path(normalized)


def _load_workflow_request(
    operation: TuiWorkflowOperation,
    path: Path,
) -> TuiWorkflowRequest:
    try:
        canonical = path.resolve(strict=True)
        if canonical != path:
            raise ValueError
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(canonical, flags), "rb") as handle:
            before = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(before.st_mode)
                or not 1 <= before.st_size <= _MAX_WORKFLOW_REQUEST_BYTES
            ):
                raise ValueError
            content = handle.read(_MAX_WORKFLOW_REQUEST_BYTES + 1)
            after = os.fstat(handle.fileno())
        if (
            len(content) != before.st_size
            or len(content) > _MAX_WORKFLOW_REQUEST_BYTES
            or (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
            )
            != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            )
        ):
            raise ValueError
        if operation == "project":
            return decode_contract(content, ProjectRequest)
        if operation == "intent":
            return decode_contract(content, IntentRequest)
        if operation == "plan":
            return decode_contract(content, PlanRequest)
        if operation == "run":
            return decode_contract(content, RunRequest)
    except (OSError, ValueError, WireContractError) as error:
        raise TuiError(TuiFailureCode.INVALID_WORKFLOW_REQUEST) from error
    raise TuiError(TuiFailureCode.INVALID_WORKFLOW_REQUEST)


def _cancel_idempotency_key() -> str:
    return f"tui-cancel-{uuid4().hex}"


def _failure_code(error: Exception) -> str:
    code = getattr(error, "code", None)
    value = getattr(code, "value", None)
    if (
        isinstance(value, str)
        and 1 <= len(value) <= 100
        and all(character in _RUN_ID_CHARACTERS for character in value)
    ):
        return value
    return "tui-operation-failed"


def _event_summary(state: TuiProjection) -> str:
    if not state.events:
        return "No events retained."
    lines = ["CURSOR  EVENT  STREAM  RECORDED"]
    for item in reversed(state.events):
        lines.append(
            f"{item.cursor:>6}  {_bounded_display(item.event_type, limit=34)}  "
            f"{_bounded_display(item.stream_id, limit=30)}  "
            f"{_bounded_display(item.recorded_at, limit=30)}"
        )
    return "\n".join(lines)


def _workflow_summary(state: TuiProjection) -> str:
    lines: list[str] = []
    if state.project is not None:
        lines.extend(
            (
                f"Project: {state.project.project_id}",
                f"Root: {_bounded_display(state.project.root)}",
                "Configuration: "
                f"{state.project.configuration_provider} "
                f"{state.project.configuration_version}",
            )
        )
    if state.intent is not None:
        lines.extend(
            (
                f"Intent: {state.intent.intent_id}",
                f"Objective: {_bounded_display(state.intent.objective)}",
                f"Constraints: {len(state.intent.constraints)}",
                f"Unresolved questions: {len(state.intent.unresolved_questions)}",
            )
        )
    if state.plan is not None:
        nodes = {node.node_id: node for node in state.plan.nodes}
        lines.extend((f"Plan: {state.plan.plan_id}", "DAG:"))
        for node_id in state.plan.topological_order:
            node = nodes[node_id]
            dependencies = ",".join(node.depends_on) or "root"
            effects = ",".join(node.effects)
            lines.append(f"  {node_id} <- {dependencies} [{effects}]")
    replay = state.replay
    if replay is not None:
        lines.append(f"Evidence: {replay.artifact_integrity} · {len(replay.artifacts)} artifacts")
        for artifact in replay.artifacts[:_MAX_RENDERED_ARTIFACTS]:
            check = f"/{artifact.check_id}" if artifact.check_id is not None else ""
            lines.append(
                f"  {artifact.node_id}:{artifact.role}{check} "
                f"verified={str(artifact.verified).lower()} {artifact.digest}"
            )
        if len(replay.artifacts) > _MAX_RENDERED_ARTIFACTS:
            lines.append(
                f"  ... {len(replay.artifacts) - _MAX_RENDERED_ARTIFACTS} artifacts omitted"
            )
        lines.append(f"Integrity findings: {len(replay.findings)}")
        for finding in replay.findings[:_MAX_RENDERED_FINDINGS]:
            lines.append(
                f"  {finding.code} node={finding.node_id or '-'} "
                f"role={finding.role or '-'} check={finding.check_id or '-'}"
            )
        if len(replay.findings) > _MAX_RENDERED_FINDINGS:
            lines.append(f"  ... {len(replay.findings) - _MAX_RENDERED_FINDINGS} findings omitted")
        verification = replay.verification
        lines.extend(
            (
                "Review findings: unavailable in replay/v2",
                "Verification: "
                f"{verification.lifecycle_status} · verdict={verification.verdict or '-'} · "
                f"evidence={verification.artifact_integrity}",
                f"Verification finding: {verification.finding_code or '-'}",
            )
        )
    if state.run is not None:
        lines.append(f"Recovery state: {_recovery_state(state)}")
    return "\n".join(lines) if lines else "No project workflow selected."


def _bounded_display(value: str, *, limit: int = _MAX_RENDERED_TEXT_CHARS) -> str:
    normalized = " ".join(value.splitlines())
    normalized = "".join(character if character.isprintable() else "�" for character in normalized)
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: limit - 1]}…"


def _recovery_state(state: TuiProjection) -> str:
    assert state.run is not None
    if state.run.status == "reconciliation-required":
        return "reconciliation-required"
    if state.run.retained_worktree:
        return "checkout-retained"
    if state.run.cancellation_requested:
        return "cancellation-requested-no-retained-checkout"
    return "no-retained-checkout"


def _runs_summary(state: TuiProjection, connection: str) -> str:
    lines = [f"Service: {connection}"]
    selected_run_id = None if state.run is None else state.run.run_id
    if not state.runs:
        lines.append("No runs discovered.")
    for item in state.runs[:10]:
        marker = ">" if item.run.run_id == selected_run_id else " "
        active = item.run.active_node_id or "-"
        lines.append(
            f"{marker} {item.run.run_id} · {item.run.status} · "
            f"node={active} · attempt={item.run.attempt}"
        )
        if item.usage is not None:
            lines.append(f"    budget {_usage_summary(item.usage)}")
    if len(state.runs) > 10:
        lines.append(f"  ... {len(state.runs) - 10} runs omitted")
    if selected_run_id is None:
        lines.append("No run selected.")
    lines.append(f"Projection cursor: {state.cursor}")
    lines.append(f"Retained events: {len(state.events)}")
    return "\n".join(lines)


def _task_graph_summary(state: TuiProjection) -> str:
    discovered = _selected_query_run(state)
    if discovered is not None:
        lines = [f"Run plan: {discovered.run.plan_id}"]
        for node in discovered.nodes:
            dependencies = ",".join(node.depends_on) or "root"
            attempts = (
                str(node.attempts)
                if node.max_attempts is None
                else f"{node.attempts}/{node.max_attempts}"
            )
            lines.append(f"{node.node_id} <- {dependencies} [{node.status}; attempts={attempts}]")
        return "\n".join(lines)
    plan = state.plan
    if plan is None:
        return "No plan selected."
    by_id = {item.node_id: item for item in plan.nodes}
    active = None if state.run is None else state.run.active_node_id
    lines = [f"Plan: {plan.plan_id}"]
    for node_id in plan.topological_order:
        node = by_id[node_id]
        dependencies = ",".join(node.depends_on) or "root"
        marker = "running" if node_id == active else "planned"
        lines.append(f"{node_id} <- {dependencies} [{marker}]")
    return "\n".join(lines)


def _verification_summary(state: TuiProjection) -> str:
    replay = state.replay
    if replay is None:
        return "No verifier evidence loaded. Use replay to inspect durable output."
    verification = replay.verification
    lines = [
        f"Lifecycle: {verification.lifecycle_status}",
        f"Verdict: {verification.verdict or '-'}",
        f"Evidence: {verification.artifact_integrity}",
        f"Finding: {verification.finding_code or '-'}",
        f"Artifacts: {len(replay.artifacts)}",
        f"Replay findings: {len(replay.findings)}",
    ]
    for finding in replay.findings[:_MAX_RENDERED_FINDINGS]:
        lines.append(
            f"{finding.code} node={finding.node_id or '-'} check={finding.check_id or '-'}"
        )
    return "\n".join(lines)


def _run_summary(state: TuiProjection) -> str:
    run = state.run
    if run is None:
        return "No run selected."
    lines = [
        f"Run: {run.run_id}",
        f"Status: {run.status}",
        f"Attempt: {run.attempt}",
        f"Active node: {run.active_node_id or '-'}",
        f"Cancellation requested: {str(run.cancellation_requested).lower()}",
        f"Retained worktree: {str(run.retained_worktree).lower()}",
    ]
    discovered = _selected_query_run(state)
    if discovered is not None and discovered.usage is not None:
        lines.append(f"Model budget: {_usage_summary(discovered.usage)}")
    if state.replay is not None:
        lines.extend(
            (
                f"Replay events: {state.replay.processed_events}",
                f"Artifact integrity: {state.replay.artifact_integrity}",
                f"Replay findings: {len(state.replay.findings)}",
                f"Verification: {state.replay.verification.lifecycle_status}",
                f"Verification verdict: {state.replay.verification.verdict or '-'}",
                f"Verification evidence: {state.replay.verification.artifact_integrity}",
            )
        )
    lines.append(f"Recovery state: {_recovery_state(state)}")
    return "\n".join(lines)


def _selected_query_run(state: TuiProjection) -> RunQueryItem | None:
    if state.run is not None:
        selected = next(
            (item for item in state.runs if item.run.run_id == state.run.run_id),
            None,
        )
        if selected is not None:
            return selected
    return None if not state.runs else state.runs[0]


def _usage_summary(usage: RunBudgetUsageResponse) -> str:
    def measured(value: int, complete: bool, maximum: int) -> str:
        prefix = str(value) if complete else f"unknown(known>={value})"
        return f"{prefix}/{maximum}"

    input_value = measured(
        usage.input_tokens,
        usage.input_tokens_complete,
        usage.max_input_tokens,
    )
    output_value = measured(
        usage.output_tokens,
        usage.output_tokens_complete,
        usage.max_output_tokens,
    )
    cost_value = measured(
        usage.cost_microusd,
        usage.cost_microusd_complete,
        usage.max_cost_microusd,
    )
    return (
        f"in={input_value} out={output_value} "
        f"latency={usage.latency_ms}/{usage.max_latency_ms}ms cost={cost_value}"
    )


__all__ = [
    "TuiApp",
    "TuiControllerFactory",
    "TuiFrame",
    "TuiIdempotencyFactory",
    "TuiInputMode",
    "TuiKeyEvent",
    "TuiRunAction",
    "TuiShellController",
    "TuiTerminal",
    "TuiTerminalFactory",
    "TuiView",
    "TuiWorkflowOperation",
]
