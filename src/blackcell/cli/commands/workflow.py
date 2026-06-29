"""Orchestrated workflow commands."""

from typing import Annotated

import typer

from blackcell.cli.output import OutputFormat, invoke

app = typer.Typer(help="Run and resume BlackCell orchestrated workflows.")

FormatOption = Annotated[
    OutputFormat | None,
    typer.Option("--format", help="Output format. Defaults to text on a TTY and JSON otherwise."),
]


@app.command("run")
def run_workflow(
    context: typer.Context,
    plan_id: Annotated[str, typer.Argument(help="Directive ID.")],
    output_format: FormatOption = None,
) -> None:
    """Run a workflow until the next gate or completion."""
    invoke(context, lambda client: client.workflow_run(plan_id), output_format)


@app.command("status")
def workflow_status(
    context: typer.Context,
    plan_id: Annotated[str, typer.Argument(help="Directive ID.")],
    output_format: FormatOption = None,
) -> None:
    """Show recorded workflow step evidence."""
    invoke(context, lambda client: client.workflow_status(plan_id), output_format)


@app.command("resume")
def resume_workflow(
    context: typer.Context,
    plan_id: Annotated[str, typer.Argument(help="Directive ID.")],
    output_format: FormatOption = None,
) -> None:
    """Resume a workflow from the current provider state."""
    invoke(context, lambda client: client.workflow_resume(plan_id), output_format)
