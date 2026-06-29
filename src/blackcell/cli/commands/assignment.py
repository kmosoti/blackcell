"""Linear assignment commands."""

from typing import Annotated

import typer

from blackcell.cli.output import OutputFormat, invoke

app = typer.Typer(help="List and verify Linear assignments.")

FormatOption = Annotated[
    OutputFormat | None,
    typer.Option("--format", help="Output format. Defaults to text on a TTY and JSON otherwise."),
]


@app.command("list")
def list_assignments(
    context: typer.Context,
    plan_id: Annotated[str, typer.Argument(help="Directive ID.")],
    output_format: FormatOption = None,
) -> None:
    """List assignments associated with a directive."""
    invoke(context, lambda client: client.assignments(plan_id), output_format)


@app.command("verify")
def verify_assignments(
    context: typer.Context,
    plan_id: Annotated[str, typer.Argument(help="Directive ID.")],
    output_format: FormatOption = None,
) -> None:
    """Verify assignments associated with a directive."""
    invoke(context, lambda client: client.verify_assignments(plan_id), output_format)
