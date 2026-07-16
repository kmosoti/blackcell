import sys
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Annotated, Any, Literal, Never

from cyclopts import App, Parameter
from cyclopts.exceptions import CycloptsError
from rich.console import Console
from rich.table import Table

from blackcell import __version__
from blackcell.adapters.retrieval import Fts5EvidenceRetriever
from blackcell.bootstrap.repository import (
    compose_repository_runtime,
    default_repository_database_path,
)
from blackcell.cli.output import OutputRenderer
from blackcell.evaluation import (
    BenchmarkAggregate,
    BenchmarkScenario,
    ComparativeExperimentDesign,
    ComparativeExperimentRunner,
    ComparativeReportReservation,
    ContextCondition,
    DeterministicGrader,
    FixtureScenarioRunner,
    PredictionConditionAggregate,
    PredictionExperimentDesign,
    PredictionExperimentRunner,
    PredictionReportReservation,
    RuntimeBenchmarkDesign,
    RuntimeBenchmarkReport,
    RuntimeBenchmarkReportReservation,
    RuntimeBenchmarkRunner,
    Trial,
    aggregate_scores,
    operator_bench_scenarios,
    prediction_bench_scenarios,
    recorded_fixture_model,
    scenario_digest,
)
from blackcell.features.project_operational_state import OperationalBeliefState
from blackcell.features.replay_run import RunReplayReport
from blackcell.features.retrieve_evidence import DeterministicEvidenceRetriever
from blackcell.kernel import EventEnvelope, EventStore, KernelError
from blackcell.models import ActionProposal, CodexExecModel, DecisionModel
from blackcell.operator import (
    DEFAULT_OBJECTIVE,
    CanonicalOperatorRunResult,
    StoredContextFrame,
)


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

app = BlackCellCli(
    name="blackcell",
    help="Blackcell event-sourced control runtime for context-guided agents.",
    version=__version__,
)
operator_app = App(name="operator")
events_app = App(name="events")
bench_app = App(name="bench")

app.command(operator_app)
app.command(events_app)
app.command(bench_app)


@operator_app.command(name="run")
def operator_run(
    repo: Annotated[
        Path,
        Parameter("--repo", help="Repository root to observe and operate on."),
    ] = Path("."),
    db: Annotated[
        Path | None,
        Parameter("--db", help="Kernel database; defaults beneath the repository."),
    ] = None,
    artifacts: Annotated[
        Path | None,
        Parameter("--artifacts", help="Artifact root; defaults beside the kernel database."),
    ] = None,
    model: Annotated[
        Literal["recorded", "codex"],
        Parameter("--model", help="Proposal model boundary."),
    ] = "recorded",
    codex_model: Annotated[
        str | None,
        Parameter("--codex-model", help="Optional model name for the Codex CLI adapter."),
    ] = None,
    objective: Annotated[
        str,
        Parameter("--objective", help="Task objective for ContextFrame projection."),
    ] = DEFAULT_OBJECTIVE,
    approval: Annotated[
        bool,
        Parameter("--approval", help="Record explicit approval for eligible actions."),
    ] = False,
) -> None:
    """Run the complete Repository Operator feedback loop once."""
    resolved_repo = repo.resolve()
    try:
        database = _operator_database(resolved_repo, db)
        operator = compose_repository_runtime(
            resolved_repo,
            database_path=database,
            artifact_root=artifacts,
            model=model,
            codex_model=codex_model,
        ).operator
        result = operator.run(objective=objective, approval_granted=approval)
    except (KernelError, LookupError, OSError, RuntimeError, ValueError) as error:
        _fail(str(error))
    _output().emit(result, rich=_operator_run_table(result))
    if result.status in {"failed", "corrupt"}:
        raise SystemExit(1)


@operator_app.command(name="state")
def operator_state(
    repo: Annotated[
        Path,
        Parameter("--repo", help="Repository root whose state should be projected."),
    ] = Path("."),
    db: Annotated[
        Path | None,
        Parameter("--db", help="Kernel database; defaults beneath the repository."),
    ] = None,
) -> None:
    """Project the current repository state from immutable events."""
    resolved_repo = repo.resolve()
    try:
        database = _operator_database(resolved_repo, db)
        _require_database(database)
        state = compose_repository_runtime(
            resolved_repo,
            database_path=database,
        ).operator.current_state()
    except (KernelError, LookupError, OSError, RuntimeError, ValueError) as error:
        _fail(str(error))
    _output().emit(state, rich=_operator_state_table(state))


@operator_app.command(name="context")
def operator_context(
    repo: Annotated[
        Path,
        Parameter("--repo", help="Repository root associated with the run."),
    ] = Path("."),
    db: Annotated[
        Path | None,
        Parameter("--db", help="Kernel database; defaults beneath the repository."),
    ] = None,
    artifacts: Annotated[
        Path | None,
        Parameter("--artifacts", help="Artifact root; defaults beside the kernel database."),
    ] = None,
    run: Annotated[
        str | None,
        Parameter("--run", help="Run ID; defaults to the latest recorded run."),
    ] = None,
) -> None:
    """Inspect the exact ContextFrame artifact used by a run."""
    resolved_repo = repo.resolve()
    try:
        database = _operator_database(resolved_repo, db)
        _require_database(database)
        frame = compose_repository_runtime(
            resolved_repo,
            database_path=database,
            artifact_root=artifacts,
        ).operator.context(run)
    except (KernelError, LookupError, OSError, RuntimeError, ValueError) as error:
        _fail(str(error))
    _output().emit(frame, rich=_operator_context_table(frame))


@operator_app.command(name="replay")
def operator_replay(
    repo: Annotated[
        Path,
        Parameter("--repo", help="Repository root associated with the run."),
    ] = Path("."),
    db: Annotated[
        Path | None,
        Parameter("--db", help="Kernel database; defaults beneath the repository."),
    ] = None,
    artifacts: Annotated[
        Path | None,
        Parameter("--artifacts", help="Artifact root; defaults beside the kernel database."),
    ] = None,
    run: Annotated[
        str | None,
        Parameter("--run", help="Run ID; defaults to the latest recorded run."),
    ] = None,
) -> None:
    """Historically replay a run without model or tool execution."""
    resolved_repo = repo.resolve()
    try:
        database = _operator_database(resolved_repo, db)
        _require_database(database)
        replay = compose_repository_runtime(
            resolved_repo,
            database_path=database,
            artifact_root=artifacts,
        ).operator.replay(run)
    except (KernelError, LookupError, OSError, RuntimeError, ValueError) as error:
        _fail(str(error))
    _output().emit(replay, rich=_operator_replay_table(replay))


@events_app.command(name="list")
def kernel_events_list(
    db: Annotated[
        Path | None,
        Parameter("--db", help="Kernel database; defaults beneath Git metadata."),
    ] = None,
    repo: Annotated[
        Path,
        Parameter("--repo", help="Repository associated with the kernel ledger."),
    ] = Path("."),
    after: Annotated[
        int,
        Parameter("--after", help="Read after this global event position."),
    ] = 0,
    limit: Annotated[
        int,
        Parameter("--limit", help="Maximum number of events to return."),
    ] = 100,
) -> None:
    """List immutable kernel events in global ledger order."""
    try:
        database = _operator_database(repo.resolve(), db)
        _require_database(database)
        events = EventStore(database).read_all(after_position=after, limit=limit)
    except (KernelError, LookupError, OSError, ValueError) as error:
        _fail(str(error))
    _output().emit_collection("events", events, rich=_kernel_events_table(events))


@bench_app.command(name="list")
def bench_list() -> None:
    """List the synthetic OperatorBench scenarios."""
    scenarios = operator_bench_scenarios()
    summaries = tuple(
        {
            "scenario_id": scenario.scenario_id,
            "task_id": scenario.task.task_id,
            "description": scenario.description,
            "tags": scenario.tags,
        }
        for scenario in scenarios
    )
    _output().emit(
        {
            "scenario_digest": scenario_digest(scenarios),
            "scenarios": summaries,
        },
        rich=_bench_scenarios_table(scenarios),
    )


@bench_app.command(name="run")
def bench_run(
    condition: Annotated[
        Literal["raw-chronological", "latest-n", "structured"],
        Parameter("--condition", help="Context construction condition."),
    ] = "structured",
    trials: Annotated[
        int,
        Parameter("--trials", help="Must be 1 for the deterministic fixture-contract pilot."),
    ] = 1,
    latest_n: Annotated[
        int,
        Parameter("--latest-n", help="Observation count for the latest-N condition."),
    ] = 1,
) -> None:
    """Validate deterministic OperatorBench fixture and grading contracts."""
    if trials != 1:
        _fail("--trials must be 1 for the deterministic fixture-contract pilot", code=2)
    if latest_n < 1:
        _fail("--latest-n must be positive", code=2)
    selected_condition = ContextCondition(condition)
    scenarios = operator_bench_scenarios()
    runner = FixtureScenarioRunner()
    grader = DeterministicGrader()
    scores = []
    for scenario in scenarios:
        for replicate in range(trials):
            trial = Trial(
                trial_id=(f"{scenario.scenario_id}:{selected_condition.value}:{replicate}"),
                scenario_id=scenario.scenario_id,
                condition=selected_condition,
                replicate=replicate,
                latest_n=latest_n,
            )
            scores.append(grader.grade(scenario, runner.run(scenario, trial)))
    aggregates = aggregate_scores(scores)
    result = {
        "mode": "fixture-contract-pilot",
        "inferential": False,
        "scenario_digest": scenario_digest(scenarios),
        "condition": selected_condition,
        "replicates_per_scenario": trials,
        "trial_count": len(scores),
        "scores": tuple(scores),
        "aggregates": aggregates,
    }
    _output().emit(result, rich=_bench_results_table(aggregates))


@bench_app.command(name="compare")
def bench_compare(
    model: Annotated[
        Literal["recorded", "codex"],
        Parameter("--model", help="One decision-model boundary shared by every treatment."),
    ] = "recorded",
    codex_model: Annotated[
        str | None,
        Parameter("--codex-model", help="Required model identifier when --model=codex."),
    ] = None,
    replicates: Annotated[
        int,
        Parameter("--replicates", help="Replicates per scenario and treatment."),
    ] = 1,
    context_budget: Annotated[
        int,
        Parameter("--context-budget", help="Shared model-context character ceiling."),
    ] = 12_000,
    latest_n: Annotated[
        int,
        Parameter("--latest-n", help="Observation count for the latest-N treatment."),
    ] = 1,
    retrieval_limit: Annotated[
        int,
        Parameter("--retrieval-limit", help="Shared result cap for term and FTS5 retrieval."),
    ] = 2,
    bootstrap_samples: Annotated[
        int,
        Parameter("--bootstrap-samples", help="Deterministic resamples per paired interval."),
    ] = 2_000,
    artifact: Annotated[
        Path | None,
        Parameter("--artifact", help="Exclusive path for the canonical comparison report."),
    ] = None,
) -> None:
    """Run the matched WP23 context and retrieval comparison."""
    if model == "codex":
        if codex_model is None or not codex_model.strip():
            _fail("--codex-model is required when --model=codex", code=2)
        if replicates < 3:
            _fail("--replicates must be at least 3 for a live Codex comparison", code=2)
        if artifact is None:
            _fail("--artifact is required for a live Codex comparison", code=2)
    elif codex_model is not None:
        _fail("--codex-model is only valid when --model=codex", code=2)
    try:
        design = ComparativeExperimentDesign(
            experiment_id="wp23-operator-bench-context-retrieval",
            replicates_per_scenario=replicates,
            context_character_budget=context_budget,
            latest_n=latest_n,
            retrieval_result_limit=retrieval_limit,
            bootstrap_samples=bootstrap_samples,
        )
        scenarios = operator_bench_scenarios()
        retrievers = {
            ContextCondition.TERM_RETRIEVAL: DeterministicEvidenceRetriever(),
            ContextCondition.FTS5_RETRIEVAL: Fts5EvidenceRetriever(),
        }
        selected_model: DecisionModel[ActionProposal]
        if model == "recorded":
            selected_model = recorded_fixture_model(
                scenarios,
                design,
                retrievers=retrievers,
            )
        else:
            selected_model = CodexExecModel(model=codex_model)
        reservation = ComparativeReportReservation(artifact) if artifact is not None else None
        if reservation is None:
            report = ComparativeExperimentRunner(
                selected_model,
                retrievers=retrievers,
                clock=lambda: 0.0,
            ).run(scenarios, design)
        else:
            with reservation:
                runner = (
                    ComparativeExperimentRunner(selected_model, retrievers=retrievers)
                    if model == "codex"
                    else ComparativeExperimentRunner(
                        selected_model,
                        retrievers=retrievers,
                        clock=lambda: 0.0,
                    )
                )
                report = runner.run(scenarios, design)
                reservation.commit(report)
    except (FileExistsError, OSError, RuntimeError, ValueError) as error:
        _fail(str(error))
    _output().emit(report, rich=_bench_results_table(report.aggregates))


@bench_app.command(name="predict")
def bench_predict(
    repetitions: Annotated[
        int,
        Parameter("--repetitions", help="Latency repetitions per scenario and condition."),
    ] = 50,
    artifact: Annotated[
        Path | None,
        Parameter("--artifact", help="Exclusive path for the canonical prediction report."),
    ] = None,
) -> None:
    """Run the matched credential-free WP24 prediction benchmark."""
    try:
        design = PredictionExperimentDesign(
            experiment_id="wp24-prediction-bench",
            latency_repetitions=repetitions,
        )
        scenarios = prediction_bench_scenarios()
        reservation = PredictionReportReservation(artifact) if artifact is not None else None
        if reservation is None:
            report = PredictionExperimentRunner().run(scenarios, design)
        else:
            with reservation:
                report = PredictionExperimentRunner().run(scenarios, design)
                reservation.commit(report)
    except (FileExistsError, OSError, RuntimeError, ValueError) as error:
        _fail(str(error))
    _output().emit(report, rich=_prediction_results_table(report.aggregates))


@bench_app.command(name="runtime")
def bench_runtime(
    repo_root: Annotated[
        Path,
        Parameter("--repo-root", help="Repository root containing the acceptance surfaces."),
    ] = Path("."),
    include_podman: Annotated[
        bool,
        Parameter("--include-podman", help="Run the live rootless Podman acceptance probe."),
    ] = False,
    artifact: Annotated[
        Path | None,
        Parameter("--artifact", help="Exclusive path for the canonical runtime report."),
    ] = None,
) -> None:
    """Profile the existing WP25 runtime reliability acceptance surfaces."""
    if include_podman and artifact is None:
        _fail("--artifact is required with --include-podman", code=2)
    try:
        design = RuntimeBenchmarkDesign(
            experiment_id="wp25-runtime-performance-reliability",
            include_rootless_podman=include_podman,
        )
        reservation = RuntimeBenchmarkReportReservation(artifact) if artifact is not None else None
        if reservation is None:
            report = RuntimeBenchmarkRunner().run(repo_root, design)
        else:
            with reservation:
                report = RuntimeBenchmarkRunner().run(repo_root, design)
                reservation.commit(report)
    except (FileExistsError, OSError, RuntimeError, ValueError) as error:
        _fail(str(error))
    _output().emit(report, rich=_runtime_benchmark_table(report))


def _kernel_events_table(events: Sequence[EventEnvelope]) -> Table:
    table = Table(title="Kernel Events")
    table.add_column("Position")
    table.add_column("Stream")
    table.add_column("Sequence")
    table.add_column("Type")
    table.add_column("Recorded")
    for event in events:
        table.add_row(
            str(event.global_position or ""),
            event.stream_id,
            str(event.stream_sequence),
            event.event_type,
            event.recorded_at.isoformat(),
        )
    return table


def _operator_run_table(result: CanonicalOperatorRunResult) -> Table:
    table = Table(title="Repository Operator Run")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("Run", result.run_id)
    table.add_row("Status", result.status)
    table.add_row("Outcome", result.outcome or "not recorded")
    table.add_row("Workflow", result.workflow_version or "unknown")
    table.add_row("ContextFrame", result.context_frame_id or "not recorded")
    table.add_row("Authorization", result.authorization_outcome or "not recorded")
    table.add_row("Execution", result.execution_status or "not attempted")
    table.add_row("Evaluation", result.evaluation_verdict or "not evaluated")
    table.add_row("State transition", "recorded" if result.transition_recorded else "none")
    table.add_row("Run events", str(result.run_event_count))
    return table


def _operator_state_table(state: OperationalBeliefState) -> Table:
    table = Table(title="Operational Belief State")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("Domain", state.scope.domain)
    table.add_row("Stream", state.scope.stream_id or "unbound")
    table.add_row("Ledger position", str(state.cutoff_global_position))
    table.add_row("Stream sequence", str(state.last_source_stream_sequence))
    table.add_row("Claims", str(len(state.claims)))
    table.add_row("Conflicts", str(len(state.conflicts)))
    table.add_row("Unknowns", str(len(state.unknowns)))
    table.add_row("Corrections", str(len(state.applied_corrections)))
    return table


def _operator_context_table(frame: StoredContextFrame) -> Table:
    table = Table(title="Recorded ContextFrame")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("Run", frame.run_id)
    table.add_row("Frame", frame.frame_id)
    table.add_row("Artifact", frame.artifact_digest)
    table.add_row("State position", str(frame.payload.get("state_global_position", "unknown")))
    table.add_row(
        "Model characters",
        str(frame.payload.get("model_payload_characters", "unknown")),
    )
    return table


def _operator_replay_table(replay: RunReplayReport) -> Table:
    table = Table(title="Historical Operator Replay")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("Run", replay.run_id)
    table.add_row("Status", replay.classification.value)
    table.add_row("Outcome", replay.outcome or "not recorded")
    table.add_row("Workflow", replay.protocol_version or "unknown")
    table.add_row("Events", str(replay.event_count))
    table.add_row("Artifacts", str(len(replay.artifacts)))
    table.add_row(
        "Projections",
        ", ".join(item.status.value for item in replay.projections) or "untrusted",
    )
    table.add_row(
        "Integrity",
        (
            "verified"
            if replay.finding is None and all(item.verified for item in replay.artifacts)
            else "failed"
        ),
    )
    return table


def _bench_scenarios_table(scenarios: Sequence[BenchmarkScenario]) -> Table:
    table = Table(title="OperatorBench Scenarios")
    table.add_column("Scenario")
    table.add_column("Task")
    table.add_column("Expected action")
    table.add_column("Tags")
    for scenario in scenarios:
        table.add_row(
            scenario.scenario_id,
            scenario.task.task_id,
            scenario.task.expected_action,
            ", ".join(scenario.tags),
        )
    return table


def _bench_results_table(results: Sequence[BenchmarkAggregate]) -> Table:
    table = Table(title="OperatorBench Results")
    table.add_column("Condition")
    table.add_column("Trials")
    table.add_column("Success")
    table.add_column("Evidence recall")
    table.add_column("Violations")
    for result in results:
        table.add_row(
            result.condition.value,
            str(result.trial_count),
            f"{result.metric('success').mean:.3f}",
            f"{result.metric('evidence_recall').mean:.3f}",
            f"{result.metric('violations').mean:.3f}",
        )
    return table


def _prediction_results_table(results: Sequence[PredictionConditionAggregate]) -> Table:
    table = Table(title="PredictionBench Results")
    table.add_column("Condition")
    table.add_column("Scored")
    table.add_column("Exact match")
    table.add_column("Brier")
    table.add_column("Mean latency ms")
    for result in results:
        table.add_row(
            result.condition.value,
            f"{result.scored_count}/{result.target_count}",
            "—" if result.exact_match_rate is None else f"{result.exact_match_rate:.3f}",
            "—" if result.brier_score is None else f"{result.brier_score:.3f}",
            f"{result.mean_latency_ms:.3f}",
        )
    return table


def _runtime_benchmark_table(report: RuntimeBenchmarkReport) -> Table:
    table = Table(title="Runtime Performance and Reliability")
    table.add_column("Probe")
    table.add_column("Status")
    table.add_column("Passed")
    table.add_column("Call seconds")
    table.add_column("Wall seconds")
    for result in report.probes:
        table.add_row(
            result.probe_id,
            result.status,
            str(result.passed_count),
            f"{result.call_seconds:.3f}",
            f"{result.wall_seconds:.3f}",
        )
    table.caption = "complete" if report.complete else "incomplete: rootless probe omitted"
    return table


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


def _operator_database(repo: Path, database: Path | None) -> Path:
    return database if database is not None else default_repository_database_path(repo)


def _require_database(database: Path) -> None:
    if not database.is_file():
        raise LookupError(f"kernel database does not exist: {database}")


def _fail(message: str, *, code: int = 1) -> Never:
    _output().emit_error(message)
    raise SystemExit(code)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
