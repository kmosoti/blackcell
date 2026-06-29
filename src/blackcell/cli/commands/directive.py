"""Directive lifecycle commands."""

from pathlib import Path
from typing import Annotated

import typer

from blackcell.cli.output import OutputFormat, invoke

app = typer.Typer(help="Validate and materialize planning directives.")

FormatOption = Annotated[
    OutputFormat | None,
    typer.Option("--format", help="Output format. Defaults to text on a TTY and JSON otherwise."),
]


@app.command("validate")
def validate_directive(
    context: typer.Context,
    plan: Annotated[Path, typer.Argument(help="Path to a PlanSpec JSON file.")],
    output_format: FormatOption = None,
) -> None:
    """Validate a directive without remote mutation."""
    invoke(context, lambda client: client.validate_plan(plan), output_format)


@app.command("propose")
def propose_directive(
    context: typer.Context,
    plan: Annotated[Path, typer.Argument(help="Path to a PlanSpec JSON file.")],
    output_format: FormatOption = None,
) -> None:
    """Create or locate a Linear Project proposal."""
    invoke(context, lambda client: client.propose_plan(plan), output_format)


@app.command("show")
def show_directive(
    context: typer.Context,
    plan_id: Annotated[str, typer.Argument(help="Stable directive ID, such as BCP-0001.")],
    output_format: FormatOption = None,
) -> None:
    """Show the remote operation associated with a directive."""
    invoke(context, lambda client: client.get_plan_status(plan_id), output_format)


@app.command("status")
def directive_status(
    context: typer.Context,
    plan_id: Annotated[str, typer.Argument(help="Stable directive ID, such as BCP-0001.")],
    output_format: FormatOption = None,
) -> None:
    """Report directive and operation status."""
    invoke(context, lambda client: client.get_plan_status(plan_id), output_format)


@app.command("materialize")
def materialize_directive(
    context: typer.Context,
    plan_id: Annotated[str, typer.Argument(help="Approved directive ID.")],
    output_format: FormatOption = None,
) -> None:
    """Materialize an approved directive into Linear assignments."""
    invoke(context, lambda client: client.materialize_plan(plan_id), output_format)


@app.command("reconcile")
def reconcile_directive(
    context: typer.Context,
    plan_id: Annotated[str, typer.Argument(help="Directive ID to safely resume.")],
    output_format: FormatOption = None,
) -> None:
    """Resume safe, incomplete directive materialization."""
    invoke(context, lambda client: client.reconcile_plan(plan_id), output_format)
