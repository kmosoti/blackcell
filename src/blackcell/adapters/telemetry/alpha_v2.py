"""OTel-ready metadata projection for alpha-v2 event correlations."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from blackcell.kernel import EventEnvelope
from blackcell.kernel._json import thaw_json
from blackcell.telemetry import TraceRecorder


class AlphaV2TraceObserver:
    """Project one durable event into a sanitized metadata-only trace span."""

    def __init__(self, recorder: TraceRecorder) -> None:
        self._recorder = recorder

    def record(self, event: EventEnvelope) -> None:
        payload = cast("Mapping[str, object]", thaw_json(event.payload))  # pragma: no mutate
        run_id = _text(payload.get("run_id")) or event.correlation_id
        correlations = {
            "run_id": run_id,
            **{
                name: value
                for name in ("plan_id", "task_id", "workspace_id")
                if (value := _text(payload.get(name))) is not None
            },
        }
        attempt = payload.get("attempt")
        attributes: dict[str, object] = {
            "event.type": event.event_type,
            "event.id": event.event_id,
            "event.sequence": event.stream_sequence,
            "event.payload_digest": event.payload_hash,
        }
        if isinstance(attempt, int) and not isinstance(attempt, bool):
            attributes["attempt"] = attempt
        for name in ("route", "status", "reason"):
            if (value := _text(payload.get(name))) is not None:
                attributes[name] = value
        for name in ("input_tokens", "output_tokens", "cost_microusd"):
            value = payload.get(name)
            attributes[f"usage.{name}.known"] = isinstance(value, int) and not isinstance(
                value, bool
            )
            if isinstance(value, int) and not isinstance(value, bool):
                attributes[f"usage.{name}"] = value
        latency = payload.get("latency_ms")
        if isinstance(latency, int) and not isinstance(latency, bool):
            attributes["usage.latency_ms"] = latency
        with self._recorder.span(
            "blackcell.alpha.v2.event",
            trace_id=run_id,
            correlation_ids=correlations,
            attributes=attributes,
        ):
            pass


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


__all__ = ["AlphaV2TraceObserver"]
