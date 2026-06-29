"""BlackCell's covert-themed command-line surface."""

from importlib.metadata import PackageNotFoundError, version
from typing import Annotated

import typer

from blackcell.cli.commands import (
    anomaly,
    assignment,
    chronicle,
    directive,
    echo,
    operation,
    profile,
    recon,
)
from blackcell.cli.output import OutputFormat, emit, invoke, resolve_format, root_format
from blackcell.contracts.errors import ValidationFailure
from blackcell.contracts.result import ResultEnvelope

app = typer.Typer(
    name="blackcell",
    help="BlackCell deterministic Linear planning materialization and GitHub echo verification.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)

app.add_typer(profile.app, name="profile")
app.add_typer(directive.app, name="directive")
app.add_typer(operation.app, name="operation")
app.add_typer(assignment.app, name="assignment")
app.add_typer(echo.app, name="echo")
app.add_typer(recon.app, name="recon")
app.add_typer(chronicle.app, name="chronicle")
app.add_typer(anomaly.app, name="anomaly")

FormatOption = Annotated[
    OutputFormat | None,
    typer.Option("--format", help="Output format. Defaults to text on a TTY and JSON otherwise."),
]


@app.callback()
def main(
    context: typer.Context,
    output_format: FormatOption = None,
) -> None:
    """Set process-wide CLI options."""
    context.ensure_object(dict)
    context.obj["format"] = output_format


@app.command("pulse")
def pulse(
    context: typer.Context,
    target: Annotated[
        str | None,
        typer.Argument(help="Optional integration: linear, github, or echo."),
    ] = None,
    output_format: FormatOption = None,
) -> None:
    """Check profile and integration readiness."""
    allowed = {None, "linear", "github", "echo"}
    if target not in allowed:
        envelope = ResultEnvelope.from_error(
            ValidationFailure(
                f"Unknown pulse target: {target}.",
                recovery="Use linear, github, or echo.",
            )
        )
        emit(envelope, resolve_format(output_format, root_format(context)))
        return
    invoke(context, lambda client: client.pulse(target), output_format)


@app.command("version")
def show_version(
    context: typer.Context,
    output_format: FormatOption = None,
) -> None:
    """Show the installed BlackCell version."""
    try:
        installed_version = version("blackcell")
    except PackageNotFoundError:
        installed_version = "0.1.0"
    emit(
        ResultEnvelope.ok({"version": installed_version}),
        resolve_format(output_format, root_format(context)),
    )


if __name__ == "__main__":
    app()
