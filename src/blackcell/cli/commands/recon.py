"""Cross-system reconnaissance commands."""

from typing import Annotated

import typer

from blackcell.cli.output import OutputFormat, invoke

app = typer.Typer(help="Compare local, Linear, and GitHub state.")

FormatOption = Annotated[
    OutputFormat | None,
    typer.Option("--format", help="Output format. Defaults to text on a TTY and JSON otherwise."),
]


@app.command("status")
def recon_status(
    context: typer.Context,
    plan_id: Annotated[str, typer.Argument(help="Directive ID.")],
    output_format: FormatOption = None,
) -> None:
    """Report cross-system state for a directive."""
    invoke(context, lambda client: client.recon_status(plan_id), output_format)
