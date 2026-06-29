"""Read-only commit, push, and pull request publication guards."""

from typing import Annotated

import typer

from blackcell.cli.output import OutputFormat, invoke
from blackcell.contracts.publication import PublicationStage

app = typer.Typer(help="Verify executor identity before repository publication.")

FormatOption = Annotated[
    OutputFormat | None,
    typer.Option("--format", help="Output format. Defaults to text on a TTY and JSON otherwise."),
]


@app.command("preflight")
def publication_preflight(
    context: typer.Context,
    stage: Annotated[
        PublicationStage,
        typer.Option("--stage", help="Publication boundary to verify."),
    ] = PublicationStage.PULL_REQUEST,
    output_format: FormatOption = None,
) -> None:
    """Inspect publication identity and target state without mutation."""
    invoke(context, lambda client: client.publication_preflight(stage), output_format)
