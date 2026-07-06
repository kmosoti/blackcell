import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Annotated, Any, Literal, Never

import httpx
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
from blackcell.config import (
    BlackcellConfig,
    ConfigError,
    ProjectRef,
    RepositoryRef,
    load_config,
    write_config,
)
from blackcell.config.env import read_shell_env
from blackcell.control_plane import (
    ContractError,
    LocalControlPlane,
    plan_contract_schema,
    refresh_github_capabilities,
    write_github_capabilities,
)
from blackcell.control_plane.capabilities import capability_summary
from blackcell.control_plane.codex_cli import AgentWorkflowProjectionResult
from blackcell.control_plane.models import AgentIssueContext, ProjectShape, ValidationResult
from blackcell.control_plane.pr import PullRequestCommand, PullRequestWorkflowResult
from blackcell.control_plane.sync import SyncResult
from blackcell.harness import HarnessPlan, RunTrace, plan_harness, run_harness
from blackcell.models import IssueRef, ProjectItemRef
from blackcell.nesy import ValidationResult as RuleValidationResult
from blackcell.nesy import build_default_rules, validate_ruleset
from blackcell.providers import CreateIssueRequest, ProjectProvider, default_registry
from blackcell.providers.github import GitHubApiError
from blackcell.runtime import DoctorReport, RuntimeAdapter, doctor_report, list_runtime_adapters
from blackcell.vanguard import (
    draft_changespec_from_agent_context,
    plan_qa,
    read_changespec_file,
    render_templates,
    validate_changespec_file,
)
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

app = BlackCellCli(name="blackcell", help="BlackCell project workflow tooling.")
config_app = App(name="config")
provider_app = App(name="providers")
project_app = App(name="project")
issue_app = App(name="issue")
world_app = App(name="world")
nesy_app = App(name="nesy")
harness_app = App(name="harness")
adapters_app = App(name="adapters")
agents_app = App(name="agents")
control_plane_app = App(name="control-plane")
capabilities_app = App(name="capabilities")
pull_request_app = App(name="pr")
agent_workflow_app = App(name="agent-workflow")
vanguard_app = App(name="vanguard")
vanguard_changespec_app = App(name="changespec")
vanguard_qa_app = App(name="qa")
vanguard_templates_app = App(name="templates")

app.command(config_app)
app.command(provider_app)
app.command(project_app)
app.command(issue_app)
app.command(world_app)
app.command(nesy_app)
app.command(harness_app)
app.command(adapters_app)
app.command(agents_app)
app.command(control_plane_app)
control_plane_app.command(capabilities_app)
control_plane_app.command(pull_request_app)
control_plane_app.command(agent_workflow_app)
app.command(vanguard_app)
vanguard_app.command(vanguard_changespec_app)
vanguard_app.command(vanguard_qa_app)
vanguard_app.command(vanguard_templates_app)


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


@app.command(name="init")
def init_config(
    repository: Annotated[
        str | None,
        Parameter(("--repository", "-r"), help="GitHub repository in owner/name form."),
    ] = None,
    project_id: Annotated[
        str | None,
        Parameter("--project-id", help="GitHub Project node ID."),
    ] = None,
    project_title: Annotated[
        str,
        Parameter("--project-title", help="Project display title."),
    ] = "BlackCell",
    project_number: Annotated[
        int | None,
        Parameter("--project-number", help="GitHub Project number."),
    ] = None,
    project_url: Annotated[
        str | None,
        Parameter("--project-url", help="GitHub Project URL."),
    ] = None,
    repository_id: Annotated[
        str | None,
        Parameter("--repository-id", help="GitHub repository node ID."),
    ] = None,
    provider: Annotated[
        str,
        Parameter("--provider", help="Project provider plugin name."),
    ] = "github",
    overwrite: Annotated[
        bool,
        Parameter("--overwrite", help="Replace an existing blackcell.toml."),
    ] = False,
) -> None:
    """Initialize repo-local BlackCell project config."""
    try:
        repository_ref = RepositoryRef.parse(
            repository or _infer_github_repository(),
            node_id=repository_id,
        )
        config = BlackcellConfig(
            provider=provider,
            repository=repository_ref,
            project=ProjectRef(
                id=project_id or _project_id_from_environment(),
                title=project_title,
                number=project_number,
                url=project_url,
            ),
        )
        path = write_config(config, overwrite=overwrite)
    except (ConfigError, ValueError) as error:
        _fail(str(error))

    _output().emit(
        {"path": path, "config": config},
        rich=f"[green]Wrote[/green] {path}",
    )


@config_app.command(name="show")
def show_config() -> None:
    """Show the discovered repo-local config."""
    try:
        config = load_config()
    except (ConfigError, ValueError) as error:
        _fail(str(error))

    _output().emit(config, rich=_config_table(config))


def _config_table(config: BlackcellConfig) -> Table:
    table = Table(title="BlackCell Config")
    table.add_column("Key")
    table.add_column("Value")
    table.add_row("provider", config.provider)
    table.add_row("repository", config.repository.name_with_owner)
    table.add_row("repository_id", config.repository.node_id or "")
    table.add_row("project", config.project.title)
    table.add_row("project_id", config.project.id)
    table.add_row("project_number", str(config.project.number or ""))
    table.add_row("project_url", config.project.url or "")
    return table


@provider_app.command(name="list")
def list_providers() -> None:
    """List available project provider plugins."""
    registry = default_registry()
    providers = [{"name": name} for name in registry.names()]
    _output().emit_collection(
        "providers",
        providers,
        rich=_providers_table(registry.names()),
    )


def _providers_table(names: list[str]) -> Table:
    table = Table(title="Project Providers")
    table.add_column("Name")
    for name in names:
        table.add_row(name)
    return table


@control_plane_app.command(name="validate")
def validate_control_plane() -> None:
    """Validate the repo-authored planning contract."""
    control_plane = LocalControlPlane()
    try:
        result = control_plane.validate_contract()
    except (ContractError, ConfigError, ValueError) as error:
        _fail(str(error))

    _output().emit(result, rich=_validation_table("Control Plane Validation", result))
    if not result.valid:
        raise SystemExit(1)


@control_plane_app.command(name="schema")
def show_control_plane_schema() -> None:
    """Show the planning contract schema."""
    schema = plan_contract_schema()
    _output().emit(schema, rich=_schema_table(schema))


@control_plane_app.command(name="agent-context")
def render_agent_context(
    issue_key: str,
) -> None:
    """Render issue context for an agent worker."""
    control_plane = LocalControlPlane()
    try:
        agent_context = control_plane.render_agent_context(issue_key)
    except (ContractError, ConfigError, ValueError) as error:
        _fail(str(error))

    _output().emit(agent_context, rich=_agent_context_table(agent_context))


@control_plane_app.command(name="shape")
def plan_project_shape() -> None:
    """Render the provider-neutral project shape implied by the contract."""
    control_plane = LocalControlPlane()
    try:
        shape = control_plane.plan_project_shape()
    except (ContractError, ConfigError, ValueError) as error:
        _fail(str(error))

    _output().emit(shape, rich=_project_shape_table(shape))


@control_plane_app.command(name="sync")
def sync_control_plane(
    apply_changes: Annotated[
        bool,
        Parameter("--apply", help="Apply remote GitHub changes. Defaults to dry run."),
    ] = False,
    issue_key: Annotated[
        str | None,
        Parameter("--issue-key", help="Sync one planning contract issue key."),
    ] = None,
    refresh_cache: Annotated[
        bool,
        Parameter("--refresh-cache", help="Ignore cached remote identity and rediscover."),
    ] = False,
) -> None:
    """Create/update GitHub issues from the local planning contract."""
    control_plane = LocalControlPlane()
    try:
        result = control_plane.sync_contract(
            apply_changes=apply_changes,
            issue_key=issue_key,
            refresh_cache=refresh_cache,
        )
    except (ContractError, ConfigError, FileNotFoundError, GitHubApiError, ValueError) as error:
        _fail(str(error))

    _output().emit(result, rich=_sync_table(result))


@agent_workflow_app.command(name="validate")
def validate_agent_workflow_command() -> None:
    """Validate the repo-authored agent workflow and rendered Codex constraints."""
    control_plane = LocalControlPlane()
    try:
        result = control_plane.validate_agent_workflow()
    except (ContractError, ConfigError, ValueError) as error:
        _fail(str(error))

    _output().emit(result, rich=_validation_table("Agent Workflow Validation", result))
    if not result.valid:
        raise SystemExit(1)


@agent_workflow_app.command(name="diff")
def diff_agent_workflow(
    target: Annotated[
        str,
        Parameter("--target", help="Projection target. Supported: codex-cli."),
    ],
) -> None:
    """Compare rendered agent workflow artifacts to the working tree."""
    control_plane = LocalControlPlane()
    try:
        result = control_plane.agent_workflow_diff(target)
    except (ContractError, ConfigError, OSError, ValueError) as error:
        _fail(str(error))

    _output().emit(result, rich=_agent_workflow_projection_table(result))
    if result.conflicts:
        raise SystemExit(1)


@agent_workflow_app.command(name="install")
def install_agent_workflow(
    target: Annotated[
        str,
        Parameter("--target", help="Projection target. Supported: codex-cli."),
    ],
    apply_changes: Annotated[
        bool,
        Parameter("--apply", help="Write non-conflicting managed artifacts."),
    ] = False,
) -> None:
    """Install rendered agent workflow artifacts. Defaults to dry run."""
    control_plane = LocalControlPlane()
    try:
        result = control_plane.agent_workflow_install(target, apply_changes=apply_changes)
    except (ContractError, ConfigError, OSError, ValueError) as error:
        _fail(str(error))

    _output().emit(result, rich=_agent_workflow_projection_table(result))
    if result.conflicts:
        raise SystemExit(1)


@agent_workflow_app.command(name="check-drift")
def check_agent_workflow_drift(
    target: Annotated[
        str,
        Parameter("--target", help="Projection target. Supported: codex-cli."),
    ],
) -> None:
    """Fail when managed agent workflow artifacts drift from rendered content."""
    control_plane = LocalControlPlane()
    try:
        result = control_plane.agent_workflow_check_drift(target)
    except (ContractError, ConfigError, OSError, ValueError) as error:
        _fail(str(error))

    _output().emit(result, rich=_agent_workflow_projection_table(result))
    if result.drift:
        raise SystemExit(1)


@pull_request_app.command(name="status")
def pull_request_status(
    issue_key: Annotated[
        str,
        Parameter("--issue-key", help="Planning contract issue key."),
    ],
    base_ref_name: Annotated[
        str,
        Parameter("--base", help="Base branch for draft pull request creation."),
    ] = "main",
    run_checks: Annotated[
        bool,
        Parameter("--run-checks", help="Run local required checks while reporting status."),
    ] = False,
) -> None:
    """Report the guarded PR workflow state for an issue."""
    _run_pull_request_workflow(
        issue_key=issue_key,
        command=PullRequestCommand.STATUS,
        apply_changes=False,
        run_checks=run_checks,
        base_ref_name=base_ref_name,
    )


@pull_request_app.command(name="sync")
def pull_request_sync(
    issue_key: Annotated[
        str,
        Parameter("--issue-key", help="Planning contract issue key."),
    ],
    apply_changes: Annotated[
        bool,
        Parameter("--apply", help="Apply remote GitHub PR changes. Defaults to dry run."),
    ] = False,
    base_ref_name: Annotated[
        str,
        Parameter("--base", help="Base branch for draft pull request creation."),
    ] = "main",
) -> None:
    """Create or update the managed draft pull request for an issue."""
    _run_pull_request_workflow(
        issue_key=issue_key,
        command=PullRequestCommand.SYNC,
        apply_changes=apply_changes,
        run_checks=False,
        base_ref_name=base_ref_name,
    )


@pull_request_app.command(name="ready")
def pull_request_ready(
    issue_key: Annotated[
        str,
        Parameter("--issue-key", help="Planning contract issue key."),
    ],
    apply_changes: Annotated[
        bool,
        Parameter("--apply", help="Mark the draft PR ready when all gates pass."),
    ] = False,
    base_ref_name: Annotated[
        str,
        Parameter("--base", help="Base branch for draft pull request creation."),
    ] = "main",
) -> None:
    """Move a managed draft pull request to review when gates pass."""
    _run_pull_request_workflow(
        issue_key=issue_key,
        command=PullRequestCommand.READY,
        apply_changes=apply_changes,
        run_checks=True,
        base_ref_name=base_ref_name,
    )


@capabilities_app.command(name="refresh")
def refresh_capabilities(
    output: Annotated[
        Path | None,
        Parameter("--output", help="Manifest path to write."),
    ] = None,
) -> None:
    """Refresh cached GitHub GraphQL capability metadata."""
    try:
        manifest = refresh_github_capabilities()
        path = write_github_capabilities(manifest, path=output)
    except (httpx.HTTPError, OSError, ValueError) as error:
        _fail(str(error))

    payload = {"path": path, "manifest": capability_summary(manifest)}
    _output().emit(payload, rich=f"[green]Wrote[/green] {path}")


@capabilities_app.command(name="check")
def check_capabilities(
    manifest: Annotated[
        Path | None,
        Parameter("--manifest", help="Capability manifest path to check."),
    ] = None,
) -> None:
    """Check referenced GraphQL capabilities against the cached manifest."""
    control_plane = LocalControlPlane(capability_manifest_path=manifest)
    try:
        result = control_plane.validate_github_capabilities()
    except (ConfigError, FileNotFoundError, ValueError) as error:
        _fail(str(error))

    _output().emit(result, rich=_validation_table("GitHub GraphQL Capabilities", result))
    if not result.valid:
        raise SystemExit(1)


@vanguard_changespec_app.command(name="init")
def init_vanguard_changespec(
    issue_key: Annotated[
        str,
        Parameter("--issue-key", help="Planning contract issue key."),
    ],
) -> None:
    """Render a draft Vanguard ChangeSpec for a control-plane issue."""
    control_plane = LocalControlPlane()
    try:
        agent_context = control_plane.render_agent_context(issue_key)
    except (ContractError, ConfigError, ValueError) as error:
        _fail(str(error))

    _output().emit(draft_changespec_from_agent_context(agent_context))


@vanguard_changespec_app.command(name="validate")
def validate_vanguard_changespec(
    path: Path,
) -> None:
    """Validate a Vanguard ChangeSpec JSON file."""
    try:
        result = validate_changespec_file(path)
    except OSError as error:
        _fail(str(error))

    _output().emit(result, rich=_validation_table("Vanguard ChangeSpec", result))
    if not result.valid:
        raise SystemExit(1)


@vanguard_qa_app.command(name="plan")
def plan_vanguard_qa(
    path: Path,
) -> None:
    """Render deterministic, non-mutating QA command records."""
    try:
        spec, validation = read_changespec_file(path)
    except OSError as error:
        _fail(str(error))
    if not validation.valid or spec is None:
        _output().emit(validation, rich=_validation_table("Vanguard ChangeSpec", validation))
        raise SystemExit(1)

    try:
        qa_plan = plan_qa(spec)
    except ValueError as error:
        _fail(str(error))

    _output().emit(qa_plan)


@vanguard_templates_app.command(name="render")
def render_vanguard_templates() -> None:
    """Render deterministic Vanguard workflow templates."""
    _output().emit_collection("templates", render_templates())


def _validation_table(title: str, result: ValidationResult) -> Table:
    table = Table(title=title)
    table.add_column("Level")
    table.add_column("Code")
    table.add_column("Path")
    table.add_column("Message")
    messages = (*result.errors, *result.warnings)
    if not messages:
        table.add_row("ok", "", "", "valid")
        return table
    for message in messages:
        table.add_row(message.level, message.code, message.path, message.message)
    return table


def _schema_table(schema: dict[str, object]) -> Table:
    properties = schema.get("properties")
    required = schema.get("required")
    table = Table(title="Control Plane Schema")
    table.add_column("Key")
    table.add_column("Value")
    table.add_row("title", str(schema.get("title", "")))
    if isinstance(required, Sequence) and not isinstance(required, str | bytes | bytearray):
        table.add_row("required", ", ".join(str(item) for item in required))
    if isinstance(properties, dict):
        table.add_row("properties", ", ".join(sorted(str(key) for key in properties)))
    return table


def _agent_context_table(agent_context: AgentIssueContext) -> Table:
    table = Table(title=f"Agent Context {agent_context.key}")
    table.add_column("Key")
    table.add_column("Value")
    table.add_row("title", agent_context.title)
    table.add_row("status", agent_context.status)
    table.add_row("priority", agent_context.priority)
    table.add_row("complexity", str(agent_context.complexity.value))
    table.add_row("blocked_by", ", ".join(item.key for item in agent_context.blocked_by))
    table.add_row("scope", "\n".join(agent_context.scope))
    table.add_row("change_spec", "\n".join(agent_context.change_spec))
    return table


def _project_shape_table(shape: ProjectShape) -> Table:
    table = Table(title="Project Shape")
    table.add_column("Field")
    table.add_column("Type")
    table.add_column("Options")
    for field in shape.fields:
        table.add_row(field.name, field.type, ", ".join(str(option) for option in field.options))
    return table


def _sync_table(result: SyncResult) -> Table:
    title = "Control Plane Sync"
    if result.dry_run:
        title += " (dry run)"
    table = Table(title=title)
    table.add_column("Action")
    table.add_column("Issue")
    table.add_column("Applied")
    table.add_column("Message")
    table.add_column("URL")
    for action in result.actions:
        table.add_row(
            action.type.value,
            action.issue_key,
            str(action.applied),
            action.message,
            action.issue_url or "",
        )
    return table


def _agent_workflow_projection_table(result: AgentWorkflowProjectionResult) -> Table:
    title = f"Agent Workflow {result.operation}"
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


def _pull_request_table(result: PullRequestWorkflowResult) -> Table:
    title = "Control Plane PR"
    if result.dry_run:
        title += " (dry run)"
    table = Table(title=title)
    table.add_column("Key")
    table.add_column("Value")
    table.add_row("issue", result.issue_key)
    table.add_row("state", result.state.value)
    table.add_row("branch", result.git.branch or "")
    table.add_row("pull_request", result.pull_request.url if result.pull_request else "")
    table.add_row("blockers", "\n".join(result.blockers))
    table.add_row("next", "\n".join(result.next_commands))
    return table


def _run_pull_request_workflow(
    *,
    issue_key: str,
    command: PullRequestCommand,
    apply_changes: bool,
    run_checks: bool,
    base_ref_name: str,
) -> None:
    control_plane = LocalControlPlane()
    try:
        result = control_plane.pull_request_workflow(
            issue_key=issue_key,
            command=command,
            apply_changes=apply_changes,
            run_checks=run_checks,
            base_ref_name=base_ref_name,
        )
    except (ContractError, ConfigError, FileNotFoundError, GitHubApiError, ValueError) as error:
        _fail(str(error))

    _output().emit(result, rich=_pull_request_table(result))


@project_app.command(name="items")
def list_project_items(
    first: Annotated[
        int,
        Parameter("--first", help="Number of project items to read."),
    ] = 20,
) -> None:
    """List project items through the active project provider."""
    provider = _provider()
    try:
        items = provider.list_project_items(first=first)
    except GitHubApiError as error:
        _fail(str(error))

    _output().emit_collection("items", items, rich=_project_items_table(items))


def _project_items_table(items: Sequence[ProjectItemRef]) -> Table:
    table = Table(title="Project Items")
    table.add_column("Type")
    table.add_column("Title")
    table.add_column("URL")
    table.add_column("Item ID")
    for item in items:
        table.add_row(
            item.content_type or item.type,
            item.content_title or "",
            item.content_url or "",
            item.id,
        )
    return table


@issue_app.command(name="read")
def read_issue(
    number: int,
) -> None:
    """Read a configured repo issue through the active project provider."""
    provider = _provider()
    try:
        issue = provider.read_issue(number)
    except GitHubApiError as error:
        _fail(str(error))

    _output().emit(issue, rich=_issue_table(issue))


def _issue_table(issue: IssueRef) -> Table:
    table = Table(title=f"Issue #{issue.number}")
    table.add_column("Key")
    table.add_column("Value")
    table.add_row("title", issue.title)
    table.add_row("state", issue.state)
    table.add_row("repository", issue.repository.name_with_owner)
    table.add_row("url", issue.url)
    table.add_row("id", issue.id)
    return table


@issue_app.command(name="create")
def create_issue(
    title: str,
    body: Annotated[
        str,
        Parameter(("--body", "-b"), help="Issue body."),
    ] = "",
) -> None:
    """Create a repo issue and attach it to the configured project."""
    provider = _provider()
    try:
        issue = provider.create_issue(CreateIssueRequest(title=title, body=body))
    except GitHubApiError as error:
        _fail(str(error))

    _output().emit(issue, rich=f"[green]Created[/green] #{issue.number}: {issue.url}")


def _provider() -> ProjectProvider:
    try:
        config = load_config()
        return default_registry().create(config.provider, config)
    except (ConfigError, ValueError) as error:
        _fail(str(error))


def _project_id_from_environment() -> str:
    value = os.getenv("BLACKCELL_PROJECT_ID")
    if value:
        return value

    repo_env = Path.cwd() / ".github" / "blackcell.env"
    values = read_shell_env(repo_env)
    if project_id := values.get("BLACKCELL_PROJECT_ID"):
        return project_id

    raise ValueError("missing --project-id or BLACKCELL_PROJECT_ID")


def _infer_github_repository() -> str:
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ValueError("missing --repository and could not read git remote origin")

    remote = result.stdout.strip()
    patterns = [
        r"^https://github\.com/(?P<repo>[^/]+/[^/.]+)(?:\.git)?$",
        r"^git@github\.com:(?P<repo>[^/]+/[^/.]+)(?:\.git)?$",
    ]
    for pattern in patterns:
        if match := re.match(pattern, remote):
            return match.group("repo")

    raise ValueError(f"could not infer GitHub repository from origin remote: {remote}")


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
