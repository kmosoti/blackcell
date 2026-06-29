"""Anomaly inspection commands."""

from typing import Annotated

import typer

from blackcell.cli.output import OutputFormat, invoke

app = typer.Typer(help="List and inspect detected anomalies.")

FormatOption = Annotated[
    OutputFormat | None,
    typer.Option("--format", help="Output format. Defaults to text on a TTY and JSON otherwise."),
]


@app.command("list")
def list_anomalies(
    context: typer.Context,
    output_format: FormatOption = None,
) -> None:
    """List recorded anomalies."""
    invoke(context, lambda client: client.anomalies(), output_format)


@app.command("show")
def show_anomaly(
    context: typer.Context,
    anomaly_id: Annotated[int, typer.Argument(help="Chronicle event ID for the anomaly.")],
    output_format: FormatOption = None,
) -> None:
    """Show one recorded anomaly."""
    invoke(context, lambda client: client.anomalies(anomaly_id), output_format)


@app.command("resolve")
def resolve_anomaly(
    context: typer.Context,
    anomaly_id: Annotated[int, typer.Argument(help="Chronicle event ID for the anomaly.")],
    note: Annotated[str, typer.Option("--note", help="Owner review and resolution note.")],
    output_format: FormatOption = None,
) -> None:
    """Append an owner-reviewed anomaly resolution event."""
    invoke(context, lambda client: client.resolve_anomaly(anomaly_id, note), output_format)
