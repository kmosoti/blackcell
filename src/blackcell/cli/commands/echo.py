"""GitHub Issue echo verification commands."""

from typing import Annotated

import typer

from blackcell.cli.output import OutputFormat, invoke

app = typer.Typer(help="Inspect read-only GitHub Issue echoes.")

FormatOption = Annotated[
    OutputFormat | None,
    typer.Option("--format", help="Output format. Defaults to text on a TTY and JSON otherwise."),
]


@app.command("status")
def echo_status(
    context: typer.Context,
    plan_id: Annotated[str, typer.Argument(help="Directive ID.")],
    output_format: FormatOption = None,
) -> None:
    """Report GitHub Issue echo status."""
    invoke(context, lambda client: client.echoes(plan_id), output_format)


@app.command("verify")
def verify_echoes(
    context: typer.Context,
    plan_id: Annotated[str, typer.Argument(help="Directive ID.")],
    output_format: FormatOption = None,
) -> None:
    """Verify one GitHub Issue echo per Linear assignment."""
    invoke(context, lambda client: client.echoes(plan_id), output_format)
