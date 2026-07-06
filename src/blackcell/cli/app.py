import sys
from collections.abc import Callable, Iterable, Sequence
from typing import Annotated, Any, Literal, Never

from cyclopts import App, Parameter
from cyclopts.exceptions import CycloptsError
from rich.console import Console
from rich.table import Table

from blackcell.agents import (
    OPENCODE_TARGET,
    AgentDoctorReport,
    AgentProjectionResult,
    AgentSummary,
    ConfigScope,
    RenderedAgentArtifact,
    check_opencode_agent_pack_drift,
    doctor_opencode_agent_pack,
    install_opencode_agent_pack,
    list_agent_summaries,
    render_opencode_artifacts,
)
from blackcell.cli.output import OutputRenderer
from blackcell.harness import HarnessPlan, RunTrace, plan_harness, run_harness
from blackcell.nesy import ValidationResult as RuleValidationResult
from blackcell.nesy import build_default_rules, validate_ruleset
from blackcell.runtime import DoctorReport, RuntimeAdapter, doctor_report, list_runtime_adapters
from blackcell.world import Fact, WorldSnapshot, observe_repo


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


_OUTPUT = OutputRenderer()

app = BlackCellCli(name="blackcell", help="BlackCell world-model harness tooling.")
world_app = App(name="world")
nesy_app = App(name="nesy")
harness_app = App(name="harness")
adapters_app = App(name="adapters")
agents_app = App(name="agents")

app.command(world_app)
app.command(nesy_app)
app.command(harness_app)
app.command(adapters_app)
app.command(agents_app)


@world_app.command(name="observe")
def world_observe() -> None:
    """Observe the repository and emit a world snapshot."""
    snapshot = observe_repo()
    _output().emit(snapshot, rich=_world_snapshot_table(snapshot))


@world_app.command(name="facts")
def world_facts() -> None:
    """Emit the current typed fact surface."""
    snapshot = observe_repo()
    _output().emit_collection("facts", snapshot.facts, rich=_facts_table(snapshot.facts))


@nesy_app.command(name="validate")
def nesy_validate() -> None:
    """Validate the default NeSy rule scaffold against the repo world model."""
    snapshot = observe_repo()
    result = validate_ruleset(build_default_rules(snapshot))
    _output().emit(result, rich=_rule_validation_table(result))
    if not result.valid:
        raise SystemExit(1)


@harness_app.command(name="plan")
def harness_plan_command() -> None:
    """Plan the first harness loop from the observed repo state."""
    plan = plan_harness(observe_repo())
    _output().emit(plan, rich=_harness_plan_table(plan))


@harness_app.command(name="run")
def harness_run_command(
    runtime: Annotated[
        str,
        Parameter("--runtime", help="Runtime adapter to use."),
    ] = "dry-run",
) -> None:
    """Run the first harness loop through a runtime adapter."""
    plan = plan_harness(observe_repo())
    try:
        trace = run_harness(plan, runtime=runtime)
    except ValueError as error:
        _fail(str(error))
    _output().emit(trace, rich=_run_trace_table(trace))


@adapters_app.command(name="list")
def adapters_list() -> None:
    """List available runtime adapters."""
    adapters = list_runtime_adapters()
    _output().emit_collection("adapters", adapters, rich=_runtime_adapters_table(adapters))


@agents_app.command(name="list")
def agents_list() -> None:
    """List the canonical BlackCell agent pack."""
    agents = list_agent_summaries()
    _output().emit_collection("agents", agents, rich=_agents_table(agents))


@agents_app.command(name="render")
def agents_render(
    target: Annotated[
        str,
        Parameter("--target", help="Agent target. Supported: opencode."),
    ] = OPENCODE_TARGET,
    scope: Annotated[
        str,
        Parameter("--scope", help="Config scope: project or global."),
    ] = ConfigScope.PROJECT.value,
) -> None:
    """Render managed agent artifacts without writing them."""
    try:
        _validate_agent_target(target)
        artifacts = render_opencode_artifacts(scope=scope)
    except ValueError as error:
        _fail(str(error))
    _output().emit_collection("artifacts", artifacts, rich=_agent_artifacts_table(artifacts))


@agents_app.command(name="install")
def agents_install(
    target: Annotated[
        str,
        Parameter("--target", help="Agent target. Supported: opencode."),
    ] = OPENCODE_TARGET,
    scope: Annotated[
        str,
        Parameter("--scope", help="Config scope: project or global."),
    ] = ConfigScope.PROJECT.value,
    apply_changes: Annotated[
        bool,
        Parameter("--apply", help="Write non-conflicting managed artifacts."),
    ] = False,
) -> None:
    """Install managed agent artifacts. Defaults to dry run."""
    try:
        _validate_agent_target(target)
        result = install_opencode_agent_pack(scope=scope, apply_changes=apply_changes)
    except (OSError, ValueError) as error:
        _fail(str(error))
    _output().emit(result, rich=_agent_projection_table(result))
    if result.conflicts:
        raise SystemExit(1)


@agents_app.command(name="check-drift")
def agents_check_drift(
    target: Annotated[
        str,
        Parameter("--target", help="Agent target. Supported: opencode."),
    ] = OPENCODE_TARGET,
    scope: Annotated[
        str,
        Parameter("--scope", help="Config scope: project or global."),
    ] = ConfigScope.PROJECT.value,
) -> None:
    """Fail when managed agent artifacts drift from rendered content."""
    try:
        _validate_agent_target(target)
        result = check_opencode_agent_pack_drift(scope=scope)
    except (OSError, ValueError) as error:
        _fail(str(error))
    _output().emit(result, rich=_agent_projection_table(result))
    if result.drift:
        raise SystemExit(1)


@agents_app.command(name="doctor")
def agents_doctor(
    target: Annotated[
        str,
        Parameter("--target", help="Agent target. Supported: opencode."),
    ] = OPENCODE_TARGET,
    scope: Annotated[
        str,
        Parameter("--scope", help="Config scope: project or global."),
    ] = ConfigScope.PROJECT.value,
) -> None:
    """Report local target health for managed BlackCell agents."""
    try:
        _validate_agent_target(target)
        report = doctor_opencode_agent_pack(scope=scope)
    except (OSError, ValueError) as error:
        _fail(str(error))
    _output().emit(report, rich=_agent_doctor_table(report))


@app.command(name="doctor")
def runtime_doctor() -> None:
    """Report local runtime adapter and repo observation health."""
    report = doctor_report(observe_repo())
    _output().emit(report, rich=_doctor_table(report))


def _world_snapshot_table(snapshot: WorldSnapshot) -> Table:
    table = Table(title="World Snapshot")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("Repo root", str(snapshot.repo_root))
    table.add_row("Branch", snapshot.branch or "unknown")
    table.add_row("Observations", str(len(snapshot.observations)))
    table.add_row("Facts", str(len(snapshot.facts)))
    table.add_row("Beliefs", str(len(snapshot.beliefs)))
    table.add_row("Expectations", str(len(snapshot.expectations)))
    table.add_row("Surprises", str(len(snapshot.surprises)))
    return table


def _facts_table(facts: Sequence[Fact]) -> Table:
    table = Table(title="Facts")
    table.add_column("Subject")
    table.add_column("Predicate")
    table.add_column("Object")
    table.add_column("Source")
    for fact in facts:
        table.add_row(fact.subject, fact.predicate, fact.object, fact.source)
    return table


def _rule_validation_table(result: RuleValidationResult) -> Table:
    table = Table(title="NeSy Validation")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("Valid", "yes" if result.valid else "no")
    table.add_row("Errors", str(len(result.errors)))
    table.add_row("Warnings", str(len(result.warnings)))
    return table


def _harness_plan_table(plan: HarnessPlan) -> Table:
    table = Table(title="Harness Plan")
    table.add_column("Step")
    table.add_column("Summary")
    table.add_column("Uses")
    for step in plan.steps:
        table.add_row(step.key, step.summary, ", ".join(step.uses))
    return table


def _run_trace_table(trace: RunTrace) -> Table:
    table = Table(title="Run Trace")
    table.add_column("#")
    table.add_column("Kind")
    table.add_column("Message")
    for event in trace.events:
        table.add_row(str(event.index), event.kind, event.message)
    return table


def _runtime_adapters_table(adapters: Sequence[RuntimeAdapter]) -> Table:
    table = Table(title="Runtime Adapters")
    table.add_column("Name")
    table.add_column("Available")
    table.add_column("Kind")
    table.add_column("Write")
    for adapter in adapters:
        table.add_row(
            adapter.name,
            "yes" if adapter.available else "no",
            adapter.kind,
            "yes" if adapter.supports_write else "no",
        )
    return table


def _doctor_table(report: DoctorReport) -> Table:
    table = Table(title="Doctor")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("Repo root", report.repo_root)
    table.add_row("Branch", report.branch or "unknown")
    table.add_row("Adapters", str(report.adapter_count))
    return table


def _agents_table(agents: Sequence[AgentSummary]) -> Table:
    table = Table(title="BlackCell Agents")
    table.add_column("Key")
    table.add_column("Mode")
    table.add_column("Writes")
    table.add_column("Description")
    for agent in agents:
        table.add_row(agent.key, agent.mode, agent.writes, agent.description)
    return table


def _agent_artifacts_table(artifacts: Sequence[RenderedAgentArtifact]) -> Table:
    table = Table(title="Rendered Agent Artifacts")
    table.add_column("Kind")
    table.add_column("Path")
    table.add_column("Digest")
    for artifact in artifacts:
        table.add_row(artifact.kind, artifact.path, artifact.digest)
    return table


def _agent_projection_table(result: AgentProjectionResult) -> Table:
    title = f"Agent Pack {result.operation} ({result.scope})"
    if result.dry_run:
        title += " (dry run)"
    table = Table(title=title)
    table.add_column("Action")
    table.add_column("Path")
    table.add_column("Applied")
    table.add_column("Digest")
    table.add_column("Message")
    for action in result.actions:
        table.add_row(
            action.action,
            action.path,
            str(action.applied),
            action.digest,
            action.message,
        )
    return table


def _agent_doctor_table(report: AgentDoctorReport) -> Table:
    table = Table(title=f"Agent Pack Doctor ({report.scope})")
    table.add_column("Check")
    table.add_column("OK")
    table.add_column("Message")
    for check in report.checks:
        table.add_row(check.key, str(check.ok), check.message)
    return table


def _validate_agent_target(target: str) -> None:
    if target != OPENCODE_TARGET:
        raise ValueError("agent target must be opencode")


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


def _fail(message: str, *, code: int = 1) -> Never:
    _output().emit_error(message)
    raise SystemExit(code)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
