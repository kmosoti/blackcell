import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from blackcell.cli.app import app
from blackcell.cli.output import OutputMode, OutputRenderer
from tests.cli_runner import CycloptsCliRunner

runner = CycloptsCliRunner()


class _State(StrEnum):
    READY = "ready"


@dataclass(frozen=True, slots=True)
class _ModernPayload:
    observed_at: datetime
    state: _State
    labels: frozenset[str]


def test_output_renderer_serializes_runtime_types() -> None:
    renderer = OutputRenderer(mode=OutputMode.JSON)
    with renderer.console.capture() as capture:
        renderer.emit(
            _ModernPayload(
                datetime(2026, 7, 9, 12, tzinfo=UTC),
                _State.READY,
                frozenset({"b", "a"}),
            )
        )

    payload = json.loads(capture.get())
    assert payload == {
        "labels": ["a", "b"],
        "observed_at": "2026-07-09T12:00:00+00:00",
        "state": "ready",
    }


def test_bench_list_jsonl_outputs_one_record_per_line() -> None:
    result = runner.invoke(app, ["--jsonl", "bench", "list"], catch_exceptions=False)

    assert result.exit_code == 0
    records = [json.loads(line) for line in result.stdout.splitlines()]
    assert records
    assert records[0]["scenarios"][0]["scenario_id"] == "dependencies-before-change"


def test_bench_list_renders_rich_when_requested() -> None:
    result = runner.invoke(app, ["--rich", "bench", "list"], catch_exceptions=False)

    assert result.exit_code == 0
    assert "OperatorBench Scenarios" in result.stdout
