from __future__ import annotations

import contextvars
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from blackcell.telemetry.policy import ContentPolicy
from blackcell.telemetry.types import SpanEvent, SpanExporter, SpanRecord, SpanStatus

_SPAN_NAME = re.compile(r"^blackcell\.[a-z][a-z0-9_.-]*$")


class SpanNames:
    OBSERVE = "blackcell.observe"
    PROJECT_STATE = "blackcell.state.project"
    BUILD_CONTEXT = "blackcell.context.build"
    MODEL_DECIDE = "blackcell.model.decide"
    POLICY_EVALUATE = "blackcell.policy.evaluate"
    AFFORDANCE_EXECUTE = "blackcell.affordance.execute"
    OUTCOME_OBSERVE = "blackcell.outcome.observe"
    EVALUATION_GRADE = "blackcell.evaluation.grade"
    TRANSITION_COMMIT = "blackcell.transition.commit"


WallClock = Callable[[], datetime]
MonotonicClock = Callable[[], float]


class SpanHandle:
    def __init__(
        self,
        recorder: TraceRecorder,
        *,
        trace_id: str,
        span_id: str,
        parent_span_id: str | None,
        name: str,
        correlation_ids: Mapping[str, str],
        attributes: Mapping[str, Any] | None,
    ) -> None:
        self._recorder = recorder
        self.trace_id = trace_id
        self.span_id = span_id
        self.parent_span_id = parent_span_id
        self.name = name
        self.correlation_ids = dict(correlation_ids)
        self._attributes: dict[str, Any] = dict(attributes or {})
        self._events: list[tuple[str, datetime, Mapping[str, Any]]] = []
        self._started_at = recorder._wall_clock()
        self._started_tick = recorder._monotonic_clock()
        self._ended = False

    def set_attribute(self, key: str, value: Any) -> None:
        if self._ended:
            raise RuntimeError("span has already ended")
        self._attributes[key] = value

    def add_event(self, name: str, attributes: Mapping[str, Any] | None = None) -> None:
        if self._ended:
            raise RuntimeError("span has already ended")
        if not name:
            raise ValueError("event name must not be empty")
        self._events.append((name, self._recorder._wall_clock(), dict(attributes or {})))

    def end(self, *, status: SpanStatus = SpanStatus.OK) -> SpanRecord:
        if self._ended:
            raise RuntimeError("span has already ended")
        self._ended = True
        ended_at = self._recorder._wall_clock()
        duration_ms = max(0.0, (self._recorder._monotonic_clock() - self._started_tick) * 1000)
        policy = self._recorder.content_policy
        events = tuple(
            SpanEvent(
                policy.sanitize_text(name),
                timestamp.isoformat(),
                policy.sanitize(attributes),
            )
            for name, timestamp, attributes in self._events
        )
        record = SpanRecord(
            trace_id=policy.sanitize_text(self.trace_id),
            span_id=self.span_id,
            parent_span_id=(
                None if self.parent_span_id is None else policy.sanitize_text(self.parent_span_id)
            ),
            name=self.name,
            started_at=self._started_at.isoformat(),
            ended_at=ended_at.isoformat(),
            duration_ms=duration_ms,
            status=status,
            correlation_ids=policy.sanitize_text_mapping(self.correlation_ids),
            attributes=policy.sanitize(self._attributes),
            events=events,
        )
        self._recorder._commit(record)
        return record


class TraceRecorder:
    """Small internal trace recorder with no telemetry backend dependency."""

    def __init__(
        self,
        *,
        content_policy: ContentPolicy | None = None,
        exporters: Sequence[SpanExporter] = (),
        wall_clock: WallClock | None = None,
        monotonic_clock: MonotonicClock = time.monotonic,
        max_records: int | None = None,
    ) -> None:
        if max_records is not None and (
            isinstance(max_records, bool) or not isinstance(max_records, int) or max_records < 0
        ):
            raise ValueError("max_records must be a non-negative integer or None")
        self.content_policy = content_policy or ContentPolicy()
        self._exporters = tuple(exporters)
        self._wall_clock = wall_clock or (lambda: datetime.now(UTC))
        self._monotonic_clock = monotonic_clock
        self._max_records = max_records
        self._records: list[SpanRecord] = []
        self._export_errors: list[str] = []
        self._lock = threading.Lock()
        self._current: contextvars.ContextVar[SpanHandle | None] = contextvars.ContextVar(
            f"blackcell_current_span_{id(self)}", default=None
        )

    @contextmanager
    def span(
        self,
        name: str,
        *,
        trace_id: str | None = None,
        parent_span_id: str | None = None,
        correlation_ids: Mapping[str, str] | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> Iterator[SpanHandle]:
        _validate_span_name(name)
        parent = self._current.get()
        if parent is not None:
            resolved_trace_id = trace_id or parent.trace_id
            resolved_parent_id = parent_span_id or parent.span_id
        else:
            resolved_trace_id = trace_id or uuid.uuid4().hex
            resolved_parent_id = parent_span_id
        handle = SpanHandle(
            self,
            trace_id=resolved_trace_id,
            span_id=uuid.uuid4().hex[:16],
            parent_span_id=resolved_parent_id,
            name=name,
            correlation_ids=_validate_correlation_ids(correlation_ids or {}),
            attributes=attributes,
        )
        token = self._current.set(handle)
        try:
            yield handle
        except BaseException as error:
            handle.set_attribute("error.type", type(error).__name__)
            handle.set_attribute("error.message", str(error))
            handle.end(status=SpanStatus.ERROR)
            raise
        else:
            handle.end()
        finally:
            self._current.reset(token)

    def records(self, *, trace_id: str | None = None) -> tuple[SpanRecord, ...]:
        with self._lock:
            records = tuple(self._records)
        if trace_id is None:
            return records
        return tuple(record for record in records if record.trace_id == trace_id)

    def export_errors(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._export_errors)

    def clear(self) -> None:
        with self._lock:
            self._records.clear()
            self._export_errors.clear()

    def _commit(self, record: SpanRecord) -> None:
        with self._lock:
            if self._max_records != 0:
                self._records.append(record)
                if self._max_records is not None:
                    del self._records[: -self._max_records]
        for exporter in self._exporters:
            try:
                exporter.export(record)
            except Exception as error:  # exporter failure must not fail the controlled action
                with self._lock:
                    self._export_errors.append(type(error).__name__)
                    del self._export_errors[:-128]


def _validate_span_name(name: str) -> None:
    if not _SPAN_NAME.fullmatch(name):
        raise ValueError("span names must use the stable blackcell.* namespace")


def _validate_correlation_ids(value: Mapping[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, item in value.items():
        if not key or not isinstance(key, str):
            raise ValueError("correlation id names must be non-empty strings")
        if not item or not isinstance(item, str):
            raise ValueError("correlation id values must be non-empty strings")
        result[key] = item
    return result
