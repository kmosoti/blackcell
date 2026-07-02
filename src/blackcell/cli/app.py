import os
import re
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Never

import httpx
import typer
from rich.table import Table

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
from blackcell.control_plane.models import AgentIssueContext, ProjectShape, ValidationResult
from blackcell.control_plane.pr import PullRequestCommand, PullRequestWorkflowResult
from blackcell.control_plane.sync import SyncResult
from blackcell.models import IssueRef, ProjectItemRef
from blackcell.providers import CreateIssueRequest, ProjectProvider, default_registry
from blackcell.providers.github import GitHubApiError
from blackcell.vanguard import (
    draft_changespec_from_agent_context,
    plan_qa,
    read_changespec_file,
    render_templates,
    validate_changespec_file,
)

app = typer.Typer(no_args_is_help=True)
config_app = typer.Typer(no_args_is_help=True)
provider_app = typer.Typer(no_args_is_help=True)
project_app = typer.Typer(no_args_is_help=True)
issue_app = typer.Typer(no_args_is_help=True)
control_plane_app = typer.Typer(no_args_is_help=True)
capabilities_app = typer.Typer(no_args_is_help=True)
pull_request_app = typer.Typer(no_args_is_help=True)
vanguard_app = typer.Typer(no_args_is_help=True)
vanguard_changespec_app = typer.Typer(no_args_is_help=True)
vanguard_qa_app = typer.Typer(no_args_is_help=True)
vanguard_templates_app = typer.Typer(no_args_is_help=True)

app.add_typer(config_app, name="config")
app.add_typer(provider_app, name="providers")
app.add_typer(project_app, name="project")
app.add_typer(issue_app, name="issue")
app.add_typer(control_plane_app, name="control-plane")
control_plane_app.add_typer(capabilities_app, name="capabilities")
control_plane_app.add_typer(pull_request_app, name="pr")
app.add_typer(vanguard_app, name="vanguard")
vanguard_app.add_typer(vanguard_changespec_app, name="changespec")
vanguard_app.add_typer(vanguard_qa_app, name="qa")
vanguard_app.add_typer(vanguard_templates_app, name="templates")


@app.callback()
def configure_cli(
    context: typer.Context,
    rich: Annotated[
        bool,
        typer.Option("--rich", help="Render human-oriented Rich output."),
    ] = False,
    jsonl: Annotated[
        bool,
        typer.Option("--jsonl", help="Render newline-delimited JSON records."),
    ] = False,
    output_format: Annotated[
        str | None,
        typer.Option("--format", help="Output format: json, jsonl, or rich."),
    ] = None,
) -> None:
    """BlackCell project workflow tooling."""
    _configure_output(context, rich=rich, jsonl=jsonl, output_format=output_format, force=True)


@control_plane_app.callback()
def configure_control_plane_cli(
    context: typer.Context,
    rich: Annotated[
        bool,
        typer.Option("--rich", help="Render human-oriented Rich output."),
    ] = False,
    jsonl: Annotated[
        bool,
        typer.Option("--jsonl", help="Render newline-delimited JSON records."),
    ] = False,
    output_format: Annotated[
        str | None,
        typer.Option("--format", help="Output format: json, jsonl, or rich."),
    ] = None,
) -> None:
    """Control-plane contract and capability commands."""
    _configure_output(context, rich=rich, jsonl=jsonl, output_format=output_format)


@capabilities_app.callback()
def configure_capabilities_cli(
    context: typer.Context,
    rich: Annotated[
        bool,
        typer.Option("--rich", help="Render human-oriented Rich output."),
    ] = False,
    jsonl: Annotated[
        bool,
        typer.Option("--jsonl", help="Render newline-delimited JSON records."),
    ] = False,
    output_format: Annotated[
        str | None,
        typer.Option("--format", help="Output format: json, jsonl, or rich."),
    ] = None,
) -> None:
    """GitHub GraphQL capability cache commands."""
    _configure_output(context, rich=rich, jsonl=jsonl, output_format=output_format)


@vanguard_app.callback()
def configure_vanguard_cli(
    context: typer.Context,
    rich: Annotated[
        bool,
        typer.Option("--rich", help="Render human-oriented Rich output."),
    ] = False,
    jsonl: Annotated[
        bool,
        typer.Option("--jsonl", help="Render newline-delimited JSON records."),
    ] = False,
    output_format: Annotated[
        str | None,
        typer.Option("--format", help="Output format: json, jsonl, or rich."),
    ] = None,
) -> None:
    """Vanguard spec-first QA workflow commands."""
    _configure_output(context, rich=rich, jsonl=jsonl, output_format=output_format)


@vanguard_changespec_app.callback()
def configure_vanguard_changespec_cli(
    context: typer.Context,
    rich: Annotated[
        bool,
        typer.Option("--rich", help="Render human-oriented Rich output."),
    ] = False,
    jsonl: Annotated[
        bool,
        typer.Option("--jsonl", help="Render newline-delimited JSON records."),
    ] = False,
    output_format: Annotated[
        str | None,
        typer.Option("--format", help="Output format: json, jsonl, or rich."),
    ] = None,
) -> None:
    """Vanguard ChangeSpec commands."""
    _configure_output(context, rich=rich, jsonl=jsonl, output_format=output_format)


@vanguard_qa_app.callback()
def configure_vanguard_qa_cli(
    context: typer.Context,
    rich: Annotated[
        bool,
        typer.Option("--rich", help="Render human-oriented Rich output."),
    ] = False,
    jsonl: Annotated[
        bool,
        typer.Option("--jsonl", help="Render newline-delimited JSON records."),
    ] = False,
    output_format: Annotated[
        str | None,
        typer.Option("--format", help="Output format: json, jsonl, or rich."),
    ] = None,
) -> None:
    """Vanguard QA planning commands."""
    _configure_output(context, rich=rich, jsonl=jsonl, output_format=output_format)


@vanguard_templates_app.callback()
def configure_vanguard_templates_cli(
    context: typer.Context,
    rich: Annotated[
        bool,
        typer.Option("--rich", help="Render human-oriented Rich output."),
    ] = False,
    jsonl: Annotated[
        bool,
        typer.Option("--jsonl", help="Render newline-delimited JSON records."),
    ] = False,
    output_format: Annotated[
        str | None,
        typer.Option("--format", help="Output format: json, jsonl, or rich."),
    ] = None,
) -> None:
    """Vanguard template commands."""
    _configure_output(context, rich=rich, jsonl=jsonl, output_format=output_format)


@app.command("init")
def init_config(
    context: typer.Context,
    repository: Annotated[
        str | None,
        typer.Option("--repository", "-r", help="GitHub repository in owner/name form."),
    ] = None,
    project_id: Annotated[
        str | None,
        typer.Option("--project-id", help="GitHub Project node ID."),
    ] = None,
    project_title: Annotated[
        str,
        typer.Option("--project-title", help="Project display title."),
    ] = "BlackCell",
    project_number: Annotated[
        int | None,
        typer.Option("--project-number", help="GitHub Project number."),
    ] = None,
    project_url: Annotated[
        str | None,
        typer.Option("--project-url", help="GitHub Project URL."),
    ] = None,
    repository_id: Annotated[
        str | None,
        typer.Option("--repository-id", help="GitHub repository node ID."),
    ] = None,
    provider: Annotated[
        str,
        typer.Option("--provider", help="Project provider plugin name."),
    ] = "github",
    overwrite: Annotated[
        bool,
        typer.Option("--overwrite", help="Replace an existing blackcell.toml."),
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
        _fail(context, str(error))

    _output(context).emit(
        {"path": path, "config": config},
        rich=f"[green]Wrote[/green] {path}",
    )


@config_app.command("show")
def show_config(context: typer.Context) -> None:
    """Show the discovered repo-local config."""
    try:
        config = load_config()
    except (ConfigError, ValueError) as error:
        _fail(context, str(error))

    _output(context).emit(config, rich=_config_table(config))


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


@provider_app.command("list")
def list_providers(context: typer.Context) -> None:
    """List available project provider plugins."""
    registry = default_registry()
    providers = [{"name": name} for name in registry.names()]
    _output(context).emit_collection(
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


@control_plane_app.command("validate")
def validate_control_plane(context: typer.Context) -> None:
    """Validate the repo-authored planning contract."""
    control_plane = LocalControlPlane()
    try:
        result = control_plane.validate_contract()
    except (ContractError, ConfigError, ValueError) as error:
        _fail(context, str(error))

    _output(context).emit(result, rich=_validation_table("Control Plane Validation", result))
    if not result.valid:
        raise typer.Exit(1)


@control_plane_app.command("schema")
def show_control_plane_schema(context: typer.Context) -> None:
    """Show the planning contract schema."""
    schema = plan_contract_schema()
    _output(context).emit(schema, rich=_schema_table(schema))


@control_plane_app.command("agent-context")
def render_agent_context(
    context: typer.Context,
    issue_key: Annotated[str, typer.Argument(help="Planning contract issue key.")],
) -> None:
    """Render issue context for an agent worker."""
    control_plane = LocalControlPlane()
    try:
        agent_context = control_plane.render_agent_context(issue_key)
    except (ContractError, ConfigError, ValueError) as error:
        _fail(context, str(error))

    _output(context).emit(agent_context, rich=_agent_context_table(agent_context))


@control_plane_app.command("shape")
def plan_project_shape(context: typer.Context) -> None:
    """Render the provider-neutral project shape implied by the contract."""
    control_plane = LocalControlPlane()
    try:
        shape = control_plane.plan_project_shape()
    except (ContractError, ConfigError, ValueError) as error:
        _fail(context, str(error))

    _output(context).emit(shape, rich=_project_shape_table(shape))


@control_plane_app.command("sync")
def sync_control_plane(
    context: typer.Context,
    apply_changes: Annotated[
        bool,
        typer.Option("--apply", help="Apply remote GitHub changes. Defaults to dry run."),
    ] = False,
    issue_key: Annotated[
        str | None,
        typer.Option("--issue-key", help="Sync one planning contract issue key."),
    ] = None,
    refresh_cache: Annotated[
        bool,
        typer.Option("--refresh-cache", help="Ignore cached remote identity and rediscover."),
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
        _fail(context, str(error))

    _output(context).emit(result, rich=_sync_table(result))


@pull_request_app.command("status")
def pull_request_status(
    context: typer.Context,
    issue_key: Annotated[
        str,
        typer.Option("--issue-key", help="Planning contract issue key."),
    ],
    base_ref_name: Annotated[
        str,
        typer.Option("--base", help="Base branch for draft pull request creation."),
    ] = "main",
    run_checks: Annotated[
        bool,
        typer.Option("--run-checks", help="Run local required checks while reporting status."),
    ] = False,
) -> None:
    """Report the guarded PR workflow state for an issue."""
    _run_pull_request_workflow(
        context,
        issue_key=issue_key,
        command=PullRequestCommand.STATUS,
        apply_changes=False,
        run_checks=run_checks,
        base_ref_name=base_ref_name,
    )


@pull_request_app.command("sync")
def pull_request_sync(
    context: typer.Context,
    issue_key: Annotated[
        str,
        typer.Option("--issue-key", help="Planning contract issue key."),
    ],
    apply_changes: Annotated[
        bool,
        typer.Option("--apply", help="Apply remote GitHub PR changes. Defaults to dry run."),
    ] = False,
    base_ref_name: Annotated[
        str,
        typer.Option("--base", help="Base branch for draft pull request creation."),
    ] = "main",
) -> None:
    """Create or update the managed draft pull request for an issue."""
    _run_pull_request_workflow(
        context,
        issue_key=issue_key,
        command=PullRequestCommand.SYNC,
        apply_changes=apply_changes,
        run_checks=False,
        base_ref_name=base_ref_name,
    )


@pull_request_app.command("ready")
def pull_request_ready(
    context: typer.Context,
    issue_key: Annotated[
        str,
        typer.Option("--issue-key", help="Planning contract issue key."),
    ],
    apply_changes: Annotated[
        bool,
        typer.Option("--apply", help="Mark the draft PR ready when all gates pass."),
    ] = False,
    base_ref_name: Annotated[
        str,
        typer.Option("--base", help="Base branch for draft pull request creation."),
    ] = "main",
) -> None:
    """Move a managed draft pull request to review when gates pass."""
    _run_pull_request_workflow(
        context,
        issue_key=issue_key,
        command=PullRequestCommand.READY,
        apply_changes=apply_changes,
        run_checks=True,
        base_ref_name=base_ref_name,
    )


@capabilities_app.command("refresh")
def refresh_capabilities(
    context: typer.Context,
    output: Annotated[
        Path | None,
        typer.Option("--output", help="Manifest path to write."),
    ] = None,
) -> None:
    """Refresh cached GitHub GraphQL capability metadata."""
    try:
        manifest = refresh_github_capabilities()
        path = write_github_capabilities(manifest, path=output)
    except (httpx.HTTPError, OSError, ValueError) as error:
        _fail(context, str(error))

    payload = {"path": path, "manifest": capability_summary(manifest)}
    _output(context).emit(payload, rich=f"[green]Wrote[/green] {path}")


@capabilities_app.command("check")
def check_capabilities(
    context: typer.Context,
    manifest: Annotated[
        Path | None,
        typer.Option("--manifest", help="Capability manifest path to check."),
    ] = None,
) -> None:
    """Check referenced GraphQL capabilities against the cached manifest."""
    control_plane = LocalControlPlane(capability_manifest_path=manifest)
    try:
        result = control_plane.validate_github_capabilities()
    except (ConfigError, FileNotFoundError, ValueError) as error:
        _fail(context, str(error))

    _output(context).emit(result, rich=_validation_table("GitHub GraphQL Capabilities", result))
    if not result.valid:
        raise typer.Exit(1)


@vanguard_changespec_app.command("init")
def init_vanguard_changespec(
    context: typer.Context,
    issue_key: Annotated[
        str,
        typer.Option("--issue-key", help="Planning contract issue key."),
    ],
) -> None:
    """Render a draft Vanguard ChangeSpec for a control-plane issue."""
    control_plane = LocalControlPlane()
    try:
        agent_context = control_plane.render_agent_context(issue_key)
    except (ContractError, ConfigError, ValueError) as error:
        _fail(context, str(error))

    _output(context).emit(draft_changespec_from_agent_context(agent_context))


@vanguard_changespec_app.command("validate")
def validate_vanguard_changespec(
    context: typer.Context,
    path: Annotated[Path, typer.Argument(help="ChangeSpec JSON path.")],
) -> None:
    """Validate a Vanguard ChangeSpec JSON file."""
    try:
        result = validate_changespec_file(path)
    except OSError as error:
        _fail(context, str(error))

    _output(context).emit(result, rich=_validation_table("Vanguard ChangeSpec", result))
    if not result.valid:
        raise typer.Exit(1)


@vanguard_qa_app.command("plan")
def plan_vanguard_qa(
    context: typer.Context,
    path: Annotated[Path, typer.Argument(help="ChangeSpec JSON path.")],
) -> None:
    """Render deterministic, non-mutating QA command records."""
    try:
        spec, validation = read_changespec_file(path)
    except OSError as error:
        _fail(context, str(error))
    if not validation.valid or spec is None:
        _output(context).emit(validation, rich=_validation_table("Vanguard ChangeSpec", validation))
        raise typer.Exit(1)

    try:
        qa_plan = plan_qa(spec)
    except ValueError as error:
        _fail(context, str(error))

    _output(context).emit(qa_plan)


@vanguard_templates_app.command("render")
def render_vanguard_templates(context: typer.Context) -> None:
    """Render deterministic Vanguard workflow templates."""
    _output(context).emit_collection("templates", render_templates())


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
    context: typer.Context,
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
        _fail(context, str(error))

    _output(context).emit(result, rich=_pull_request_table(result))


@project_app.command("items")
def list_project_items(
    context: typer.Context,
    first: Annotated[
        int,
        typer.Option("--first", min=1, max=100, help="Number of project items to read."),
    ] = 20,
) -> None:
    """List project items through the active project provider."""
    provider = _provider(context)
    try:
        items = provider.list_project_items(first=first)
    except GitHubApiError as error:
        _fail(context, str(error))

    _output(context).emit_collection("items", items, rich=_project_items_table(items))


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


@issue_app.command("read")
def read_issue(
    context: typer.Context,
    number: Annotated[int, typer.Argument(help="Issue number to read.")],
) -> None:
    """Read a configured repo issue through the active project provider."""
    provider = _provider(context)
    try:
        issue = provider.read_issue(number)
    except GitHubApiError as error:
        _fail(context, str(error))

    _output(context).emit(issue, rich=_issue_table(issue))


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


@issue_app.command("create")
def create_issue(
    context: typer.Context,
    title: Annotated[str, typer.Argument(help="Issue title.")],
    body: Annotated[
        str,
        typer.Option("--body", "-b", help="Issue body."),
    ] = "",
) -> None:
    """Create a repo issue and attach it to the configured project."""
    provider = _provider(context)
    try:
        issue = provider.create_issue(CreateIssueRequest(title=title, body=body))
    except GitHubApiError as error:
        _fail(context, str(error))

    _output(context).emit(issue, rich=f"[green]Created[/green] #{issue.number}: {issue.url}")


def _provider(context: typer.Context) -> ProjectProvider:
    try:
        config = load_config()
        return default_registry().create(config.provider, config)
    except (ConfigError, ValueError) as error:
        _fail(context, str(error))


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


def _output(context: typer.Context) -> OutputRenderer:
    context.ensure_object(dict)
    output = context.obj.get("output")
    if isinstance(output, OutputRenderer):
        return output

    output = OutputRenderer()
    context.obj["output"] = output
    return output


def _configure_output(
    context: typer.Context,
    *,
    rich: bool,
    jsonl: bool,
    output_format: str | None,
    force: bool = False,
) -> None:
    context.ensure_object(dict)
    if (
        not force
        and not rich
        and not jsonl
        and output_format is None
        and isinstance(context.obj.get("output"), OutputRenderer)
    ):
        return

    try:
        output = OutputRenderer.from_flags(
            rich=rich,
            jsonl=jsonl,
            output_format=output_format,
        )
    except ValueError as error:
        OutputRenderer().emit_error(str(error))
        raise typer.Exit(2) from error

    context.obj["output"] = output


def _fail(context: typer.Context, message: str, *, code: int = 1) -> Never:
    _output(context).emit_error(message)
    raise typer.Exit(code)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
