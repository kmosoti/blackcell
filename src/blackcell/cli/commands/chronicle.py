"""Append-only chronicle commands."""

from typing import Annotated

import typer

from blackcell.cli.output import OutputFormat, invoke

app = typer.Typer(help="Inspect the append-only local chronicle.")

FormatOption = Annotated[
    OutputFormat | None,
    typer.Option("--format", help="Output format. Defaults to text on a TTY and JSON otherwise."),
]


@app.command("show")
def show_chronicle(
    context: typer.Context,
    plan_id: Annotated[str, typer.Argument(help="Directive ID.")],
    output_format: FormatOption = None,
) -> None:
    """Show chronicle events for a directive."""
    invoke(context, lambda client: client.chronicle_events(plan_id), output_format)
