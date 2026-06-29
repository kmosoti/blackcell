"""Profile inspection commands."""

from typing import Annotated

import typer

from blackcell.cli.output import OutputFormat, invoke

app = typer.Typer(help="Validate and inspect the non-secret BlackCell profile.")

FormatOption = Annotated[
    OutputFormat | None,
    typer.Option("--format", help="Output format. Defaults to text on a TTY and JSON otherwise."),
]


@app.command("validate")
def validate_profile(
    context: typer.Context,
    output_format: FormatOption = None,
) -> None:
    """Validate the discovered BlackCell profile."""
    invoke(context, lambda client: client.validate_profile(), output_format)


@app.command("show")
def show_profile(
    context: typer.Context,
    output_format: FormatOption = None,
) -> None:
    """Show the validated non-secret profile."""
    invoke(context, lambda client: client.profile(), output_format)
