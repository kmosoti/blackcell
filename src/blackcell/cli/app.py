from __future__ import annotations

import asyncio
import os
import shutil
import stat
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, Never

from cyclopts import App, Parameter
from cyclopts.exceptions import CycloptsError
from rich.console import Console

from blackcell import __version__
from blackcell.adapters.daemon_systemd import (
    SystemdLifecycleResult,
    SystemdServiceError,
    SystemdServiceFailureCode,
    SystemdUnitStatus,
    SystemdUserServiceManager,
)
from blackcell.adapters.models import tooling_surface_catalog
from blackcell.adapters.runtime_http import (
    DEFAULT_RUNTIME_ENDPOINT,
    RUNTIME_ENDPOINT_ENV,
    RuntimeClientError,
    RuntimeHttpClient,
    RuntimeServiceStatus,
)
from blackcell.adapters.tui_cursor import FileTuiCursorStore
from blackcell.bootstrap.process import main as runtime_process_main
from blackcell.cli.output import OutputRenderer
from blackcell.config import (
    DATA_DIR_ENV,
    SecurityConfigError,
    SecurityConfigFailureCode,
    load_service_token,
)
from blackcell.gateway import ToolingSurfaceCatalog
from blackcell.interfaces.http import (
    CancelRunRequest,
    IntentRequest,
    PlanRequest,
    ProjectRequest,
    RunQueryRequest,
    RunRequest,
    StrictStruct,
    WireContractError,
    decode_contract,
)
from blackcell.interfaces.tui import TuiApp, TuiController, TuiCursorError


class BlackCellCli(App):
    def __call__(
        self,
        tokens: None | str | Iterable[str] = None,
        *,
        console: Console | None = None,
        error_console: Console | None = None,
        print_error: bool | None = None,
        exit_on_error: bool | None = None,
        help_on_error: bool | None = None,
        verbose: bool | None = None,
        end_of_options_delimiter: str | None = None,
        backend: Literal["asyncio", "trio"] | None = None,
        result_action: Any = None,
        error_formatter: Callable[[CycloptsError], Any] | None = None,
    ) -> Any:
        raw_tokens = sys.argv[1:] if tokens is None else tokens
        parsed_tokens, rich, jsonl, output_format = _extract_output_flags(raw_tokens)
        try:
            _configure_output(rich=rich, jsonl=jsonl, output_format=output_format, force=True)
        except ValueError as error:
            OutputRenderer().emit_error(str(error))
            raise SystemExit(2) from error
        if not parsed_tokens:
            parsed_tokens = ["--help"]
        return super().__call__(
            parsed_tokens,
            console=console,
            error_console=error_console,
            print_error=print_error,
            exit_on_error=exit_on_error,
            help_on_error=help_on_error,
            verbose=verbose,
            end_of_options_delimiter=end_of_options_delimiter,
            backend=backend,
            result_action=result_action,
            error_formatter=error_formatter,
        )


@dataclass(frozen=True, slots=True)
class DaemonStatusResult:
    endpoint: str | None
    live: bool
    ready: bool
    runtime_error: str | None
    service: SystemdUnitStatus
    schema_version: Literal["daemon-status/v1"] = "daemon-status/v1"


@dataclass(frozen=True, slots=True)
class DaemonForegroundResult:
    operation: Literal["foreground"] = "foreground"
    outcome: Literal["stopped"] = "stopped"
    schema_version: Literal["daemon-lifecycle/v1"] = "daemon-lifecycle/v1"


_OUTPUT = OutputRenderer()
_MAX_REQUEST_FILE_BYTES = 2 * 1024 * 1024

app = BlackCellCli(
    name="blackcell",
    help="BlackCell project runtime.",
    version=__version__,
)
daemon_app = App(name="daemon")
project_app = App(name="project")
intent_app = App(name="intent")
plan_app = App(name="plan")
run_app = App(name="run")
events_app = App(name="events")
adapters_app = App(name="adapters")

app.command(adapters_app)
app.command(daemon_app)
app.command(project_app)
app.command(intent_app)
app.command(plan_app)
app.command(run_app)
app.command(events_app)


@adapters_app.command(name="inspect")
def adapters_inspect() -> None:
    """Describe supported model CLI boundaries without starting provider processes."""

    _output().emit(tooling_surface_catalog())


@adapters_app.command(name="schema")
def adapters_schema() -> None:
    """Emit the closed JSON Schema for model CLI boundary inspection."""

    _output().emit(ToolingSurfaceCatalog.model_json_schema())


@daemon_app.command(name="status")
def daemon_status(
    endpoint: Annotated[
        str | None,
        Parameter(
            "--endpoint",
            help=(
                f"Runtime base URL; defaults to ${RUNTIME_ENDPOINT_ENV} "
                f"or {DEFAULT_RUNTIME_ENDPOINT}."
            ),
        ),
    ] = None,
) -> None:
    """Report service-manager state and runtime liveness/readiness."""
    try:
        service = SystemdUserServiceManager().status()
    except SystemdServiceError as error:
        _fail(str(error), code=error.cli_exit_code)
    runtime: RuntimeServiceStatus | None = None
    runtime_error: str | None = None
    runtime_client: RuntimeHttpClient | None = None
    try:
        runtime_client = RuntimeHttpClient(endpoint=_daemon_endpoint(endpoint))
        runtime = runtime_client.status()
    except RuntimeClientError as error:
        runtime_error = str(error)
    status = DaemonStatusResult(
        endpoint=(
            runtime.endpoint if runtime is not None else getattr(runtime_client, "endpoint", None)
        ),
        live=runtime.live if runtime is not None else False,
        ready=runtime.ready if runtime is not None else False,
        runtime_error=runtime_error,
        service=service,
    )
    _output().emit(status)
    if not status.ready:
        raise SystemExit(1)


@daemon_app.command(name="foreground")
def daemon_foreground() -> None:
    """Run the API and configured workers in one foreground lifecycle."""
    exit_code = runtime_process_main(("daemon",))
    if exit_code:
        raise SystemExit(exit_code)
    _output().emit(DaemonForegroundResult())


@daemon_app.command(name="install")
def daemon_install(
    environment_file: Annotated[
        Path,
        Parameter(
            "--environment-file",
            help="Existing owner-only systemd EnvironmentFile with runtime configuration.",
        ),
    ],
    runtime_executable: Annotated[
        Path | None,
        Parameter(
            "--runtime-executable",
            help="Absolute blackcell-runtime executable; defaults to PATH resolution.",
        ),
    ] = None,
) -> None:
    """Install and enable the idempotent systemd user service without starting it."""
    try:
        executable = runtime_executable or _default_runtime_executable()
        result = SystemdUserServiceManager().install(
            environment_file=environment_file,
            runtime_executable=executable,
        )
    except SystemdServiceError as error:
        _fail(str(error), code=error.cli_exit_code)
    _output().emit(result)


@daemon_app.command(name="start")
def daemon_start() -> None:
    """Start the installed systemd user service."""
    _emit_daemon_lifecycle("start")


@daemon_app.command(name="stop")
def daemon_stop() -> None:
    """Stop the installed systemd user service."""
    _emit_daemon_lifecycle("stop")


@daemon_app.command(name="restart")
def daemon_restart() -> None:
    """Restart or start the installed systemd user service."""
    _emit_daemon_lifecycle("restart")


@daemon_app.command(name="logs")
def daemon_logs(
    lines: Annotated[
        int,
        Parameter("--lines", help="Most recent journal entries, from 1 through 200."),
    ] = 100,
) -> None:
    """Read a bounded set of typed systemd journal entries."""
    try:
        result = SystemdUserServiceManager().logs(lines=lines)
    except SystemdServiceError as error:
        _fail(str(error), code=error.cli_exit_code)
    _output().emit(result)


@project_app.command(name="register")
def project_register(
    request: Annotated[
        Path,
        Parameter("--request", help="Closed project-request/v1 JSON file."),
    ],
    endpoint: Annotated[
        str | None,
        Parameter("--endpoint", help="Runtime base URL; defaults to configured endpoint."),
    ] = None,
) -> None:
    """Register one project through the runtime client."""
    contract = _load_request(request, ProjectRequest)
    _output().emit(
        _invoke_runtime_http(
            lambda client: client.register_project(contract),
            endpoint=endpoint,
        )
    )


@intent_app.command(name="accept")
def intent_accept(
    request: Annotated[
        Path,
        Parameter("--request", help="Closed intent-request/v1 JSON file."),
    ],
    endpoint: Annotated[
        str | None,
        Parameter("--endpoint", help="Runtime base URL; defaults to configured endpoint."),
    ] = None,
) -> None:
    """Accept one bounded project intent."""
    contract = _load_request(request, IntentRequest)
    _output().emit(
        _invoke_runtime_http(lambda client: client.accept_intent(contract), endpoint=endpoint)
    )


@plan_app.command(name="accept")
def plan_accept(
    request: Annotated[
        Path,
        Parameter("--request", help="Closed plan-request/v1 JSON file."),
    ],
    endpoint: Annotated[
        str | None,
        Parameter("--endpoint", help="Runtime base URL; defaults to configured endpoint."),
    ] = None,
) -> None:
    """Accept one dependency-safe execution plan."""
    contract = _load_request(request, PlanRequest)
    _output().emit(
        _invoke_runtime_http(lambda client: client.accept_plan(contract), endpoint=endpoint)
    )


@run_app.command(name="submit")
def run_submit(
    request: Annotated[
        Path,
        Parameter("--request", help="Closed run-request/v1 JSON file."),
    ],
    endpoint: Annotated[
        str | None,
        Parameter("--endpoint", help="Runtime base URL; defaults to configured endpoint."),
    ] = None,
) -> None:
    """Submit one asynchronous run."""
    contract = _load_request(request, RunRequest)
    _output().emit(
        _invoke_runtime_http(lambda client: client.submit_run(contract), endpoint=endpoint)
    )


@run_app.command(name="status")
def run_status(
    run_id: str,
    endpoint: Annotated[
        str | None,
        Parameter("--endpoint", help="Runtime base URL; defaults to configured endpoint."),
    ] = None,
) -> None:
    """Read authoritative run status."""
    _output().emit(
        _invoke_runtime_http(lambda client: client.inspect_run(run_id), endpoint=endpoint)
    )


@run_app.command(name="query")
def run_query(
    request: Annotated[
        Path,
        Parameter("--request", help="Closed run-query-request/v1 JSON file."),
    ],
    endpoint: Annotated[
        str | None,
        Parameter("--endpoint", help="Runtime base URL; defaults to configured endpoint."),
    ] = None,
) -> None:
    """Search bounded run projections through RFC 10008 QUERY."""
    contract = _load_request(request, RunQueryRequest)
    _output().emit(
        _invoke_runtime_http(lambda client: client.query_runs(contract), endpoint=endpoint)
    )


@run_app.command(name="cancel")
def run_cancel(
    run_id: str,
    request: Annotated[
        Path,
        Parameter("--request", help="Closed cancel-run-request/v1 JSON file."),
    ],
    endpoint: Annotated[
        str | None,
        Parameter("--endpoint", help="Runtime base URL; defaults to configured endpoint."),
    ] = None,
) -> None:
    """Request cooperative cancellation."""
    contract = _load_request(request, CancelRunRequest)
    _output().emit(
        _invoke_runtime_http(
            lambda client: client.cancel_run(run_id, contract),
            endpoint=endpoint,
        )
    )


@run_app.command(name="replay")
def run_replay(
    run_id: str,
    endpoint: Annotated[
        str | None,
        Parameter("--endpoint", help="Runtime base URL; defaults to configured endpoint."),
    ] = None,
) -> None:
    """Replay execution and verification evidence without live effects."""
    _output().emit(
        _invoke_runtime_http(lambda client: client.replay_run(run_id), endpoint=endpoint)
    )


@events_app.command(name="list")
def events_list(
    after: Annotated[
        int,
        Parameter("--after", help="Resume after this global event cursor."),
    ] = 0,
    limit: Annotated[
        int,
        Parameter("--limit", help="Maximum runtime events to return, from 1 through 200."),
    ] = 100,
    endpoint: Annotated[
        str | None,
        Parameter("--endpoint", help="Runtime base URL; defaults to configured endpoint."),
    ] = None,
) -> None:
    """Read runtime events in durable global order."""
    _output().emit(
        _invoke_runtime_http(
            lambda client: client.list_events(after_cursor=after, limit=limit),
            endpoint=endpoint,
        )
    )


@app.command(name="tui")
def tui(
    endpoint: Annotated[
        str | None,
        Parameter("--endpoint", help="Runtime base URL; defaults to configured endpoint."),
    ] = None,
    cursor_dir: Annotated[
        Path | None,
        Parameter(
            "--cursor-dir",
            help=f"Owner-only cursor directory; defaults to ${DATA_DIR_ENV}/tui-cursors.",
        ),
    ] = None,
    refresh_seconds: Annotated[
        float | None,
        Parameter(
            "--refresh-seconds",
            help="Ordered-event refresh interval from 0.25 through 60; use none to disable.",
        ),
    ] = 1.0,
    frames_per_second: Annotated[
        float,
        Parameter("--frames-per-second", help="Terminal render rate from 1 through 60."),
    ] = 20.0,
) -> None:
    """Run the terminal projection over the authenticated runtime client."""
    try:
        _launch_tui(
            endpoint=endpoint,
            cursor_dir=cursor_dir,
            refresh_seconds=refresh_seconds,
            frames_per_second=frames_per_second,
        )
    except SecurityConfigError as error:
        _fail(str(error), code=2)
    except RuntimeClientError as error:
        _fail(str(error), code=error.cli_exit_code)
    except TuiCursorError as error:
        _fail(str(error), code=2)
    except ValueError:
        _fail("invalid-tui-configuration", code=2)


def _output() -> OutputRenderer:
    return _OUTPUT


def _configure_output(
    *,
    rich: bool,
    jsonl: bool,
    output_format: str | None,
    force: bool = False,
) -> None:
    global _OUTPUT
    if not force and not rich and not jsonl and output_format is None:
        return
    _OUTPUT = OutputRenderer.from_flags(
        rich=rich,
        jsonl=jsonl,
        output_format=output_format,
    )


def _extract_output_flags(tokens: str | Iterable[str]) -> tuple[list[str], bool, bool, str | None]:
    token_list = tokens.split() if isinstance(tokens, str) else list(tokens)
    parsed: list[str] = []
    rich = False
    jsonl = False
    output_format: str | None = None
    index = 0
    while index < len(token_list):
        token = token_list[index]
        if token == "--rich":
            rich = True
        elif token == "--jsonl":
            jsonl = True
        elif token == "--format":
            index += 1
            if index >= len(token_list):
                raise SystemExit(2)
            output_format = token_list[index]
        elif token.startswith("--format="):
            output_format = token.removeprefix("--format=")
        else:
            parsed.append(token)
        index += 1
    return parsed, rich, jsonl, output_format


def _daemon_endpoint(value: str | None) -> str:
    if value is not None:
        return value
    return os.environ.get(RUNTIME_ENDPOINT_ENV, DEFAULT_RUNTIME_ENDPOINT)


def _default_runtime_executable() -> Path:
    executable = shutil.which("blackcell-runtime")
    if executable is None:
        raise SystemdServiceError(SystemdServiceFailureCode.INVALID_EXECUTABLE)
    return Path(executable)


def _emit_daemon_lifecycle(operation: Literal["start", "stop", "restart"]) -> None:
    manager = SystemdUserServiceManager()
    try:
        result: SystemdLifecycleResult
        if operation == "start":
            result = manager.start()
        elif operation == "stop":
            result = manager.stop()
        else:
            result = manager.restart()
    except SystemdServiceError as error:
        _fail(str(error), code=error.cli_exit_code)
    _output().emit(result)


def _invoke_runtime_http[ResultT](
    operation: Callable[[RuntimeHttpClient], ResultT],
    *,
    endpoint: str | None,
) -> ResultT:
    try:
        token = load_service_token(os.environ)
        client = RuntimeHttpClient(endpoint=_daemon_endpoint(endpoint), token=token)
        return operation(client)
    except SecurityConfigError as error:
        _fail(str(error), code=2)
    except RuntimeClientError as error:
        _fail(str(error), code=error.cli_exit_code)


def _launch_tui(
    *,
    endpoint: str | None,
    cursor_dir: Path | None,
    refresh_seconds: float | None,
    frames_per_second: float,
) -> None:
    token = load_service_token(os.environ)
    selected_endpoint = _daemon_endpoint(endpoint)
    selected_cursor_dir = cursor_dir
    if selected_cursor_dir is None:
        data_root = os.environ.get(DATA_DIR_ENV)
        if data_root is None:
            raise SecurityConfigError(SecurityConfigFailureCode.INVALID_DATA_DIRECTORY)
        selected_cursor_dir = Path(data_root) / "tui-cursors"
    cursor_store = FileTuiCursorStore.prepare(selected_cursor_dir)
    client = RuntimeHttpClient(endpoint=selected_endpoint, token=token)
    controller = TuiController(client, cursor_store=cursor_store)
    shell = TuiApp(
        lambda: controller,
        event_refresh_seconds=refresh_seconds,
        frames_per_second=frames_per_second,
    )
    asyncio.run(shell.run())


def _load_request[ContractT: StrictStruct](
    path: Path,
    contract_type: type[ContractT],
) -> ContractT:
    try:
        metadata = path.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or not 1 <= metadata.st_size <= _MAX_REQUEST_FILE_BYTES
        ):
            raise ValueError
        with path.open("rb") as handle:
            content = handle.read(_MAX_REQUEST_FILE_BYTES + 1)
        if len(content) != metadata.st_size or len(content) > _MAX_REQUEST_FILE_BYTES:
            raise ValueError
        return decode_contract(content, contract_type)
    except OSError, ValueError, WireContractError:
        _fail("invalid-runtime-request-file", code=2)


def _fail(message: str, *, code: int = 1) -> Never:
    _output().emit_error(message)
    raise SystemExit(code)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
