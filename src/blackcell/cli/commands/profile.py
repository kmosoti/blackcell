"""Profile inspection commands."""

from typing import Annotated

import typer

from blackcell.cli.output import OutputFormat, invoke
from blackcell.contracts.result import ResultEnvelope
from blackcell.sdk.client import BlackcellClient

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

    def validate(client: BlackcellClient) -> ResultEnvelope:
        config = client.config
        return ResultEnvelope.ok(
            {
                "valid": True,
                "schema_version": config.schema_version,
                "repository": f"{config.repository.owner}/{config.repository.name}",
                "linear_team": {
                    "id": config.linear.team_id,
                    "key": config.linear.team_key,
                    "name": config.linear.team_name,
                },
            }
        )

    invoke(context, validate, output_format)


@app.command("show")
def show_profile(
    context: typer.Context,
    output_format: FormatOption = None,
) -> None:
    """Show the validated non-secret profile."""
    invoke(context, lambda client: client.profile(), output_format)
