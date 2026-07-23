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
    AlphaCancelRunRequest,
    AlphaIntentRequest,
    AlphaPlanRequest,
    AlphaProjectRequest,
    AlphaRunRequest,
    WireContractError,
    decode_contract,
)
from blackcell.interfaces.tui.controller import (
    AlphaTuiError,
    AlphaTuiFailureCode,
    AlphaTuiProjection,
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

type AlphaTuiRunAction = Literal["status", "replay", "cancel"]
type AlphaTuiWorkflowOperation = Literal["project", "intent", "plan", "run"]
type AlphaTuiInputMode = Literal["workflow-path", "run-id"]
type AlphaTuiWorkflowRequest = (
    AlphaProjectRequest | AlphaIntentRequest | AlphaPlanRequest | AlphaRunRequest
)
type AlphaTuiControllerFactory = Callable[[], AlphaTuiShellController]
type AlphaTuiIdempotencyFactory = Callable[[], str]
type AlphaTuiTerminalFactory = Callable[[], AlphaTuiTerminal]

_WORKFLOW_KEYS: dict[str, AlphaTuiWorkflowOperation] = {
    "1": "project",
    "2": "intent",
    "3": "plan",
    "4": "run",
}


class AlphaTuiShellController(Protocol):
    @property
    def state(self) -> AlphaTuiProjection: ...

    async def connect(self) -> AlphaTuiProjection: ...

    async def register_project(self, request: AlphaProjectRequest) -> AlphaTuiProjection: ...

    async def accept_intent(self, request: AlphaIntentRequest) -> AlphaTuiProjection: ...

    async def accept_plan(self, request: AlphaPlanRequest) -> AlphaTuiProjection: ...

    async def submit_run(self, request: AlphaRunRequest) -> AlphaTuiProjection: ...

    async def inspect_run(self, run_id: str) -> AlphaTuiProjection: ...

    async def replay_run(self, run_id: str) -> AlphaTuiProjection: ...

    async def cancel_run(
        self,
        run_id: str,
        request: AlphaCancelRunRequest,
    ) -> AlphaTuiProjection: ...

    async def refresh_events(self, *, limit: int = 100) -> AlphaTuiProjection: ...


class AlphaTuiKeyEvent(Protocol):
    code: str
    ctrl: bool
    alt: bool
    shift: bool


class AlphaTuiFrame(Protocol):
    area: Rect

    def render_widget(self, widget: object, area: Rect) -> None: ...


class AlphaTuiTerminal(Protocol):
    async def __aenter__(self) -> AlphaTuiTerminal: ...

    async def __aexit__(self, *args: object) -> bool: ...

    def events(
        self,
        fps: float = 30.0,
        *,
        stop_on_quit: bool = False,
    ) -> AsyncIterator[AlphaTuiKeyEvent | None]: ...

    def draw(self, draw_fn: Callable[[AlphaTuiFrame], None]) -> None: ...


@dataclass(frozen=True, slots=True)
class AlphaTuiView:
    connection: str
    message: str
    message_is_error: bool
    workflow_operation: AlphaTuiWorkflowOperation
    workflow_path: str
    run_id: str
    input_mode: AlphaTuiInputMode | None
    connection_busy: bool
    events_busy: bool
    command_busy: bool
    events: str
    workflow: str
    run: str
    quit_requested: bool
    schema_version: Literal["alpha-tui-view/v1"] = "alpha-tui-view/v1"


class AlphaTuiApp:
    """Thin PyRatatui projection over one injected authority-free alpha controller."""

    def __init__(
        self,
        controller_factory: AlphaTuiControllerFactory,
        *,
        event_refresh_seconds: float | None = 1.0,
        frames_per_second: float = 20.0,
        idempotency_factory: AlphaTuiIdempotencyFactory | None = None,
        terminal_factory: AlphaTuiTerminalFactory | None = None,
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
        self._controller: AlphaTuiShellController | None = None
        self._connection_task: asyncio.Task[None] | None = None
        self._events_task: asyncio.Task[None] | None = None
        self._command_task: asyncio.Task[None] | None = None
        self._workflow_operation: AlphaTuiWorkflowOperation = "project"
        self._workflow_path = ""
        self._run_id = ""
        self._input_mode: AlphaTuiInputMode | None = None
        self._message = "alpha-tui-not-started"
        self._message_is_error = False
        self._quit_requested = False
        self._started = False
        self._last_refresh_started = time.monotonic()

    @property
    def view(self) -> AlphaTuiView:
        state = self._state
        endpoint = state.endpoint or "endpoint-unavailable"
        readiness = "ready" if state.ready else "not-ready"
        connection = f"{readiness} · {endpoint} · cursor {state.cursor}"
        return AlphaTuiView(
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
    def _state(self) -> AlphaTuiProjection:
        if self._controller is None:
            return AlphaTuiProjection()
        return self._controller.state

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        try:
            self._controller = self._controller_factory()
        except Exception:
            self._set_message("alpha-tui-controller-unavailable", error=True)
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

    def handle_key(self, event: AlphaTuiKeyEvent) -> bool:
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
            self._set_message(f"alpha-tui-workflow-{self._workflow_operation}-selected")
        elif code == "w" and event.ctrl:
            self.action_submit_workflow()
        elif code == "w":
            self._input_mode = "workflow-path"
            self._set_message("alpha-tui-workflow-path-editing")
        elif code == "i":
            self._input_mode = "run-id"
            self._set_message("alpha-tui-run-id-editing")
        elif code == "s":
            self.action_run("status")
        elif code == "p":
            self.action_run("replay")
        elif code == "x" or (event.ctrl and code == "x"):
            self.action_run("cancel")
        else:
            return False
        return True

    def _handle_input_key(self, event: AlphaTuiKeyEvent) -> bool:
        mode = self._input_mode
        assert mode is not None
        if event.code in {"esc", "escape"}:
            self._input_mode = None
            self._set_message("alpha-tui-input-canceled")
            return True
        if event.code == "enter":
            self._input_mode = None
            if mode == "workflow-path":
                self.action_submit_workflow()
            else:
                self._set_message("alpha-tui-run-id-accepted")
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
                self._set_message("alpha-tui-input-limit", error=True)
                return True
            if mode == "run-id" and event.code not in _RUN_ID_CHARACTERS:
                self._set_message("alpha-tui-invalid-run-id", error=True)
                return True
            self._replace_input(mode, current + event.code)
            return True
        return False

    def _input_value(self, mode: AlphaTuiInputMode) -> str:
        return self._workflow_path if mode == "workflow-path" else self._run_id

    def _replace_input(self, mode: AlphaTuiInputMode, value: str) -> None:
        if mode == "workflow-path":
            self._workflow_path = value
        else:
            self._run_id = value

    def action_connect(self) -> bool:
        if self._controller is None:
            self._set_message("alpha-tui-controller-unavailable", error=True)
            return False
        return self._schedule("_connection_task", self._connect)

    def action_refresh_events(self) -> bool:
        if self._controller is None or not self._controller.state.connected:
            self._set_message("alpha-tui-not-connected", error=True)
            return False
        started = self._schedule("_events_task", self._refresh_events)
        if started:
            self._last_refresh_started = time.monotonic()
        return started

    def action_submit_workflow(self) -> bool:
        if self._controller is None or not self._controller.state.connected:
            self._set_message("alpha-tui-not-connected", error=True)
            return False
        raw_path = self._workflow_path
        self._workflow_path = ""
        path = _valid_workflow_path(raw_path)
        if path is None:
            self._set_message("alpha-tui-invalid-workflow-request", error=True)
            return False
        operation = self._workflow_operation
        return self._schedule(
            "_command_task",
            lambda: self._workflow_command(operation, path),
        )

    def action_run(self, operation: AlphaTuiRunAction) -> bool:
        if self._controller is None or not self._controller.state.connected:
            self._set_message("alpha-tui-not-connected", error=True)
            return False
        run_id = _valid_run_id(self._run_id)
        if run_id is None:
            self._set_message("alpha-tui-invalid-run-id", error=True)
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
            self._set_message("alpha-tui-operation-busy", error=True)
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
            self._set_message("alpha-tui-operation-failed", error=True)

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
        self._set_message("alpha-tui-connecting")
        try:
            await self._controller.connect()
        except Exception as error:
            self._set_message(_failure_code(error), error=True)
        else:
            self._set_message("alpha-tui-connected")

    async def _refresh_events(self) -> None:
        assert self._controller is not None
        try:
            await self._controller.refresh_events(limit=_EVENT_PAGE_LIMIT)
        except Exception as error:
            self._set_message(_failure_code(error), error=True)
        else:
            self._set_message("alpha-tui-events-refreshed")

    async def _workflow_command(
        self,
        operation: AlphaTuiWorkflowOperation,
        path: Path,
    ) -> None:
        assert self._controller is not None
        self._set_message(f"alpha-tui-workflow-{operation}-pending")
        try:
            request = await asyncio.to_thread(_load_workflow_request, operation, path)
            if operation == "project" and isinstance(request, AlphaProjectRequest):
                await self._controller.register_project(request)
            elif operation == "intent" and isinstance(request, AlphaIntentRequest):
                await self._controller.accept_intent(request)
            elif operation == "plan" and isinstance(request, AlphaPlanRequest):
                await self._controller.accept_plan(request)
            elif operation == "run" and isinstance(request, AlphaRunRequest):
                await self._controller.submit_run(request)
            else:  # pragma: no cover - closed loader invariant
                raise AlphaTuiError(AlphaTuiFailureCode.INVALID_WORKFLOW_REQUEST)
        except Exception as error:
            self._set_message(_failure_code(error), error=True)
        else:
            self._set_message(f"alpha-tui-workflow-{operation}-complete")

    async def _run_command(self, operation: AlphaTuiRunAction, run_id: str) -> None:
        assert self._controller is not None
        self._set_message(f"alpha-tui-run-{operation}-pending")
        try:
            if operation == "status":
                await self._controller.inspect_run(run_id)
            elif operation == "replay":
                await self._controller.replay_run(run_id)
            else:
                await self._controller.cancel_run(
                    run_id,
                    AlphaCancelRunRequest(
                        schema_version="alpha-cancel-run-request/v1",
                        idempotency_key=self._idempotency_factory(),
                    ),
                )
        except Exception as error:
            self._set_message(_failure_code(error), error=True)
        else:
            self._set_message(f"alpha-tui-run-{operation}-complete")

    def _set_message(self, value: str, *, error: bool = False) -> None:
        self._message = value
        self._message_is_error = error

    def render(self, frame: AlphaTuiFrame) -> None:
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
            .constraints([Constraint.percentage(54), Constraint.fill(1)])
        )
        events_area, details_area = columns.split(body_area)
        details = (
            Layout()
            .direction(Direction.Vertical)
            .constraints([Constraint.percentage(52), Constraint.fill(1)])
        )
        workflow_area, run_area = details.split(details_area)

        frame.render_widget(
            Paragraph.from_string(f"BlackCell Alpha · {view.connection}")
            .style(Style().fg(Color.cyan()).bold())
            .block(Block().bordered().title(" PyRatatui ")),
            header_area,
        )
        frame.render_widget(
            _panel(view.events, f" Ordered events · {len(self._state.events)} retained "),
            events_area,
        )
        frame.render_widget(
            _panel(_workflow_panel(view), " Project workflow "),
            workflow_area,
        )
        frame.render_widget(_panel(_run_panel(view), " Run inspector "), run_area)
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


def _pyratatui_terminal() -> AlphaTuiTerminal:
    # PyRatatui 0.2.9's published stub models its async-generator ``events``
    # method as a coroutine. Runtime behavior is source-checked and covered by
    # the injected-terminal contract test, so keep the mismatch at this edge.
    return cast("AlphaTuiTerminal", AsyncTerminal())


def _panel(value: str, title: str) -> Paragraph:
    return (
        Paragraph.from_string(value)
        .wrap(True, True)
        .block(Block().bordered().title(title).border_style(Style().fg(Color.dark_gray())))
    )


def _workflow_panel(view: AlphaTuiView) -> str:
    marker = ">" if view.input_mode == "workflow-path" else " "
    path = view.workflow_path or "absolute request JSON path"
    busy = "busy" if view.command_busy else "idle"
    return f"Operation: {view.workflow_operation} · {busy}\n{marker} {path}\n\n{view.workflow}"


def _run_panel(view: AlphaTuiView) -> str:
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
    operation: AlphaTuiWorkflowOperation,
    path: Path,
) -> AlphaTuiWorkflowRequest:
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
            return decode_contract(content, AlphaProjectRequest)
        if operation == "intent":
            return decode_contract(content, AlphaIntentRequest)
        if operation == "plan":
            return decode_contract(content, AlphaPlanRequest)
        if operation == "run":
            return decode_contract(content, AlphaRunRequest)
    except (OSError, ValueError, WireContractError) as error:
        raise AlphaTuiError(AlphaTuiFailureCode.INVALID_WORKFLOW_REQUEST) from error
    raise AlphaTuiError(AlphaTuiFailureCode.INVALID_WORKFLOW_REQUEST)


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
    return "alpha-tui-operation-failed"


def _event_summary(state: AlphaTuiProjection) -> str:
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


def _workflow_summary(state: AlphaTuiProjection) -> str:
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
                "Review findings: unavailable in alpha-replay/v2",
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


def _recovery_state(state: AlphaTuiProjection) -> str:
    assert state.run is not None
    if state.run.status == "reconciliation-required":
        return "reconciliation-required"
    if state.run.retained_worktree:
        return "checkout-retained"
    if state.run.cancellation_requested:
        return "cancellation-requested-no-retained-checkout"
    return "no-retained-checkout"


def _run_summary(state: AlphaTuiProjection) -> str:
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


__all__ = [
    "AlphaTuiApp",
    "AlphaTuiControllerFactory",
    "AlphaTuiFrame",
    "AlphaTuiIdempotencyFactory",
    "AlphaTuiInputMode",
    "AlphaTuiKeyEvent",
    "AlphaTuiRunAction",
    "AlphaTuiShellController",
    "AlphaTuiTerminal",
    "AlphaTuiTerminalFactory",
    "AlphaTuiView",
    "AlphaTuiWorkflowOperation",
]
