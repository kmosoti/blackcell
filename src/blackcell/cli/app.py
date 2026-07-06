import sys
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
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
from blackcell.latent import (
    LatentLedgerRecordResult,
    LatentLedgerStats,
    LatentLedgerSummary,
    LatentPredictionError,
    LatentState,
    PredictionSet,
    encode_world_state,
    load_transitions,
    predict_next_states,
    record_simulation,
    simulate_transition,
    summarize_ledger,
    summarize_prediction_stats,
)
from blackcell.ledger import (
    LedgerEvent,
    LedgerRun,
    LedgerSummary,
    init_ledger,
    list_events,
    list_runs,
)
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
latent_app = App(name="latent")
ledger_app = App(name="ledger")
adapters_app = App(name="adapters")
agents_app = App(name="agents")

app.command(world_app)
app.command(nesy_app)
app.command(harness_app)
app.command(latent_app)
app.command(ledger_app)
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
    latent_db: Annotated[
        Path | None,
        Parameter("--latent-db", help="Optional SQLite ledger path for latent recording."),
    ] = None,
    latent: Annotated[
        Literal["off", "summary", "record", "stats"],
        Parameter("--latent", help="Latent mode: off, summary, record, or stats."),
    ] = "summary",
    show_stats: Annotated[
        bool,
        Parameter("--show-stats", help="Include latent ledger stats in dry-run output."),
    ] = False,
) -> None:
    """Run the first harness loop through a runtime adapter."""
    snapshot = observe_repo()
    plan = plan_harness(snapshot)
    latent_mode = _resolve_latent_mode(latent=latent, latent_db=latent_db, show_stats=show_stats)
    try:
        trace = run_harness(
            plan,
            runtime=runtime,
            snapshot=snapshot,
            latent_db=latent_db,
            latent_mode=latent_mode,
        )
    except ValueError as error:
        _fail(str(error))
    _output().emit(trace, rich=_run_trace_table(trace))


@latent_app.command(name="encode")
def latent_encode() -> None:
    """Encode the current world snapshot as a deterministic latent capsule."""
    state = encode_world_state(observe_repo())
    _output().emit(state, rich=_latent_state_table(state))


@latent_app.command(name="predict")
def latent_predict(
    db: Annotated[
        Path | None,
        Parameter("--db", help="Optional SQLite ledger path for transition memory."),
    ] = None,
) -> None:
    """Predict candidate next latent states using non-parametric V0 memory."""
    state = encode_world_state(observe_repo())
    transition_memory = load_transitions(db) if db is not None else ()
    stats = summarize_prediction_stats(db) if db is not None else None
    labels_by_action = (
        {action.action_id: action.confidence_label for action in stats.action_stats}
        if stats is not None
        else None
    )
    predictions = predict_next_states(
        state,
        transition_memory=transition_memory,
        confidence_labels_by_action=labels_by_action,
    )
    _output().emit(predictions, rich=_latent_predictions_table(predictions))


@latent_app.command(name="errors")
def latent_errors() -> None:
    """Simulate prediction/actual comparison and emit latent error evidence."""
    result = simulate_transition(observe_repo())
    _output().emit(result, rich=_latent_error_table(result.error))


@latent_app.command(name="record")
def latent_record(
    db: Annotated[
        Path,
        Parameter("--db", help="SQLite ledger path."),
    ] = Path(".blackcell/latent.sqlite3"),
) -> None:
    """Record a simulated latent transition in the local SQLite ledger."""
    result = record_simulation(simulate_transition(observe_repo()), path=db)
    _output().emit(result, rich=_latent_record_table(result))


@latent_app.command(name="ledger")
def latent_ledger(
    db: Annotated[
        Path,
        Parameter("--db", help="SQLite ledger path."),
    ] = Path(".blackcell/latent.sqlite3"),
) -> None:
    """Summarize the local latent SQLite ledger."""
    summary = summarize_ledger(path=db)
    _output().emit(summary, rich=_latent_ledger_table(summary))


@latent_app.command(name="stats")
def latent_stats(
    db: Annotated[
        Path,
        Parameter("--db", help="SQLite ledger path."),
    ] = Path(".blackcell/latent.sqlite3"),
) -> None:
    """Summarize ledger-backed latent prediction quality by action."""
    stats = summarize_prediction_stats(path=db)
    _output().emit(stats, rich=_latent_stats_table(stats))


@ledger_app.command(name="init")
def ledger_init(
    db: Annotated[
        Path,
        Parameter("--db", help="SQLite ledger path."),
    ] = Path(".blackcell/ledger.sqlite3"),
) -> None:
    """Initialize the local generic run/event ledger."""
    summary = init_ledger(path=db)
    _output().emit(summary, rich=_ledger_summary_table(summary))


@ledger_app.command(name="runs")
def ledger_runs(
    db: Annotated[
        Path,
        Parameter("--db", help="SQLite ledger path."),
    ] = Path(".blackcell/ledger.sqlite3"),
) -> None:
    """List runs from the local generic ledger."""
    runs = list_runs(path=db)
    _output().emit_collection("runs", runs, rich=_ledger_runs_table(runs))


@ledger_app.command(name="events")
def ledger_events(
    db: Annotated[
        Path,
        Parameter("--db", help="SQLite ledger path."),
    ] = Path(".blackcell/ledger.sqlite3"),
    run: Annotated[
        str | None,
        Parameter("--run", help="Optional run ID filter."),
    ] = None,
) -> None:
    """List events from the local generic ledger."""
    events = list_events(path=db, run_id=run)
    _output().emit_collection("events", events, rich=_ledger_events_table(events))


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


def _latent_state_table(state: LatentState) -> Table:
    table = Table(title="Latent State")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("State", state.state_id)
    table.add_row("Source", state.source)
    table.add_row("Encoder", state.encoder_version)
    table.add_row("Facts", str(state.structural.get("fact_count", "unknown")))
    table.add_row("Dirty", str(state.structural.get("workspace_dirty", "unknown")))
    return table


def _latent_predictions_table(prediction_set: PredictionSet) -> Table:
    table = Table(title="Latent Predictions")
    table.add_column("Prediction")
    table.add_column("Action")
    table.add_column("Confidence")
    table.add_column("Label")
    table.add_column("Samples")
    table.add_column("Checks")
    for prediction in prediction_set.predictions:
        table.add_row(
            prediction.prediction_id,
            prediction.action.kind,
            str(prediction.confidence),
            prediction.confidence_label,
            str(prediction.sample_count),
            ", ".join(prediction.required_checks),
        )
    return table


def _latent_error_table(error: object) -> Table:
    typed_error = error if isinstance(error, LatentPredictionError) else None
    table = Table(title="Latent Prediction Error")
    table.add_column("Field")
    table.add_column("Value")
    if typed_error is None:
        return table
    table.add_row("Error", typed_error.error_id)
    table.add_row("Surprise", typed_error.surprise)
    table.add_row("Semantic distance", str(typed_error.semantic_distance))
    table.add_row("Structural deltas", str(len(typed_error.structural_delta)))
    table.add_row("Symbolic deltas", str(len(typed_error.symbolic_delta)))
    return table


def _latent_record_table(result: LatentLedgerRecordResult) -> Table:
    table = Table(title="Latent Ledger Record")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("Path", str(result.path))
    table.add_row("State", result.state_id)
    table.add_row("Prediction", result.prediction_id)
    table.add_row("Actual", result.actual_state_id)
    table.add_row("Error", result.error_id)
    table.add_row("Transition", result.transition_id)
    table.add_row("Sample", result.sample_id)
    return table


def _latent_ledger_table(summary: LatentLedgerSummary) -> Table:
    table = Table(title="Latent Ledger")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("Path", str(summary.path))
    table.add_row("Schema", str(summary.schema_version))
    table.add_row("States", str(summary.state_count))
    table.add_row("Predictions", str(summary.prediction_count))
    table.add_row("Errors", str(summary.error_count))
    table.add_row("Transitions", str(summary.transition_count))
    table.add_row("Samples", str(summary.sample_count))
    return table


def _latent_stats_table(stats: LatentLedgerStats) -> Table:
    table = Table(title="Latent Prediction Stats")
    table.add_column("Action")
    table.add_column("Samples")
    table.add_column("Mean Semantic Distance")
    table.add_column("Surprises")
    table.add_column("Confidence")
    for action in stats.action_stats:
        table.add_row(
            action.action_id,
            str(action.sample_count),
            str(action.mean_semantic_distance),
            str(action.surprise_count),
            action.confidence_label,
        )
    return table


def _ledger_summary_table(summary: LedgerSummary) -> Table:
    table = Table(title="Ledger")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("Path", str(summary.path))
    table.add_row("Schema", str(summary.schema_version))
    table.add_row("Runs", str(summary.run_count))
    table.add_row("Events", str(summary.event_count))
    return table


def _ledger_runs_table(runs: Sequence[LedgerRun]) -> Table:
    table = Table(title="Ledger Runs")
    table.add_column("Run")
    table.add_column("Kind")
    table.add_column("Status")
    table.add_column("Created")
    for run in runs:
        table.add_row(run.run_id, run.kind, run.status, run.created_at)
    return table


def _ledger_events_table(events: Sequence[LedgerEvent]) -> Table:
    table = Table(title="Ledger Events")
    table.add_column("Event")
    table.add_column("Run")
    table.add_column("Seq")
    table.add_column("Kind")
    table.add_column("Source")
    table.add_column("Message")
    for event in events:
        table.add_row(
            event.event_id,
            event.run_id,
            str(event.sequence),
            event.kind,
            event.source,
            event.message,
        )
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


def _resolve_latent_mode(
    *,
    latent: Literal["off", "summary", "record", "stats"],
    latent_db: Path | None,
    show_stats: bool,
) -> Literal["off", "summary", "record", "stats"]:
    if show_stats:
        return "stats"
    if latent == "summary" and latent_db is not None:
        return "record"
    return latent


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
