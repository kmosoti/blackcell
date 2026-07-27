import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from blackcell.cli.output import OutputMode, OutputRenderer


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


def test_output_renderer_jsonl_emits_one_record_per_line() -> None:
    renderer = OutputRenderer(mode=OutputMode.JSONL)
    records = (
        _ModernPayload(datetime(2026, 7, 9, 12, tzinfo=UTC), _State.READY, frozenset({"a"})),
        _ModernPayload(datetime(2026, 7, 9, 13, tzinfo=UTC), _State.READY, frozenset({"b"})),
    )
    with renderer.console.capture() as capture:
        renderer.emit_collection("events", records)

    rendered = [json.loads(line) for line in capture.get().splitlines()]
    assert [record["labels"] for record in rendered] == [["a"], ["b"]]


def test_output_renderer_uses_supplied_rich_projection() -> None:
    renderer = OutputRenderer(mode=OutputMode.RICH)
    with renderer.console.capture() as capture:
        renderer.emit_collection("events", (), rich="Execution events")

    assert capture.get().strip() == "Execution events"
