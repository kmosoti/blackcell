"""Stable CLI output, redaction, and exit-code handling."""

import json
import sys
from collections.abc import Callable, Mapping
from enum import StrEnum
from typing import Any

import typer
from rich.console import Console
from rich.text import Text
from rich.tree import Tree

from blackcell.contracts.errors import BlackcellError, ExitClass
from blackcell.contracts.result import ResultEnvelope
from blackcell.runtime.observability import redact
from blackcell.sdk.client import BlackcellClient


class OutputFormat(StrEnum):
    TEXT = "text"
    JSON = "json"
    JSONL = "jsonl"


def resolve_format(
    explicit: OutputFormat | None,
    inherited: OutputFormat | None = None,
) -> OutputFormat:
    if explicit is not None:
        return explicit
    if inherited is not None:
        return inherited
    return OutputFormat.TEXT if sys.stdout.isatty() else OutputFormat.JSON


def root_format(context: typer.Context) -> OutputFormat | None:
    root = context.find_root()
    if isinstance(root.obj, dict):
        value = root.obj.get("format")
        if isinstance(value, OutputFormat):
            return value
    return None


def invoke(
    context: typer.Context,
    operation: Callable[[BlackcellClient], ResultEnvelope],
    output_format: OutputFormat | None = None,
) -> None:
    """Invoke one SDK operation, render its envelope, and exit predictably."""
    try:
        envelope = operation(BlackcellClient.from_environment())
    except BlackcellError as error:
        envelope = ResultEnvelope.from_error(error)
    except Exception:
        envelope = ResultEnvelope.from_error(
            BlackcellError(
                "Unexpected BlackCell failure.",
                recovery="Re-run with a valid profile and inspect local diagnostics.",
            )
        )
    emit(envelope, resolve_format(output_format, root_format(context)))


def emit(envelope: ResultEnvelope, output_format: OutputFormat) -> None:
    payload = redact(envelope.model_dump(mode="json", exclude_none=True))
    if output_format in {OutputFormat.JSON, OutputFormat.JSONL}:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    else:
        _render_text(payload)

    exit_code = _exit_code(envelope.exit_class)
    if exit_code:
        raise typer.Exit(exit_code)


def _exit_code(exit_class: str) -> int:
    try:
        return int(ExitClass[exit_class.upper()])
    except KeyError:
        return int(ExitClass.ERROR)


def _render_text(payload: Mapping[str, Any]) -> None:
    console = Console(file=sys.stdout)
    status = str(payload.get("status", "error")).upper()
    color = {"OK": "green", "PENDING": "yellow"}.get(status, "red")
    console.print(
        Text.assemble(
            (status, f"bold {color}"),
            ("  "),
            (str(payload["exit_class"]), "dim"),
        )
    )

    error = payload.get("error")
    if isinstance(error, Mapping):
        console.print(Text(str(error.get("message", "Unknown error")), style="bold"))
        if code := error.get("code"):
            console.print(Text.assemble(("Code: ", "dim"), (str(code), "cyan")))
        if recovery := error.get("recovery"):
            console.print(Text.assemble(("Recovery: ", "dim"), (str(recovery), "yellow")))
        details = error.get("details")
        if details:
            console.print(_tree("Details", details))

    data = payload.get("data")
    if data:
        console.print(_tree("Data", data))


def _tree(label: str, value: Any) -> Tree:
    tree = Tree(Text(label, style="bold"))
    _add_tree_value(tree, value)
    return tree


def _add_tree_value(tree: Tree, value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(item, (Mapping, list)):
                branch = tree.add(Text(str(key), style="cyan"))
                _add_tree_value(branch, item)
            else:
                tree.add(Text.assemble((f"{key}: ", "cyan"), (_scalar(item), "")))
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            branch = tree.add(Text(f"[{index}]", style="cyan"))
            _add_tree_value(branch, item)
        return
    tree.add(Text(_scalar(value)))


def _scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
