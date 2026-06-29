"""Linear schema fixture commands."""

from typing import Annotated

import typer

from blackcell.cli.output import OutputFormat, invoke

app = typer.Typer(help="Inspect pinned Linear schema fixture capabilities.")

FormatOption = Annotated[
    OutputFormat | None,
    typer.Option("--format", help="Output format. Defaults to text on a TTY and JSON otherwise."),
]


@app.command("audit")
def audit_schema(
    context: typer.Context,
    output_format: FormatOption = None,
) -> None:
    """Validate the pinned Linear schema fixture."""
    invoke(context, lambda client: client.schema_audit(), output_format)
