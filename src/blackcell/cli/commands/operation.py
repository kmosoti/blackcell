"""Linear operation inspection commands."""

from typing import Annotated

import typer

from blackcell.cli.output import OutputFormat, invoke

app = typer.Typer(help="Inspect and verify Linear operations.")

FormatOption = Annotated[
    OutputFormat | None,
    typer.Option("--format", help="Output format. Defaults to text on a TTY and JSON otherwise."),
]


@app.command("inspect")
def inspect_operation(
    context: typer.Context,
    plan_id: Annotated[str, typer.Argument(help="Directive ID.")],
    output_format: FormatOption = None,
) -> None:
    """Inspect the Linear Project for a directive."""
    invoke(context, lambda client: client.inspect_operation(plan_id), output_format)


@app.command("verify")
def verify_operation(
    context: typer.Context,
    plan_id: Annotated[str, typer.Argument(help="Directive ID.")],
    output_format: FormatOption = None,
) -> None:
    """Verify the Linear Project for a directive."""
    invoke(context, lambda client: client.operation(plan_id), output_format)


@app.command("reconcile")
def reconcile_operation(
    context: typer.Context,
    plan_id: Annotated[str, typer.Argument(help="Directive ID.")],
    output_format: FormatOption = None,
) -> None:
    """Reconcile mutable presentation fields while the Project is a Proposal."""
    invoke(context, lambda client: client.reconcile_operation(plan_id), output_format)
