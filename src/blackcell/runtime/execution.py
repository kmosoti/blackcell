"""Typed operation execution pipeline and explicit cross-cutting aspects."""

from collections.abc import Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic_ns
from typing import Any, Protocol
from uuid import uuid4

from blackcell.contracts.errors import BlackcellError, ConflictFailure
from blackcell.contracts.facade import Credential, OperationSpec
from blackcell.contracts.result import ResultEnvelope
from blackcell.ledger.sqlite import EventType
from blackcell.ledger.store import ChronicleStore
from blackcell.runtime.observability import EventSink, NullEventSink


@dataclass(frozen=True, slots=True, kw_only=True)
class PendingOutcome:
    code: str
    message: str
    recovery: str
    data: Mapping[str, Any]


@dataclass(frozen=True, slots=True, kw_only=True)
class OperationContext:
    spec: OperationSpec
    correlation_id: str
    plan_id: str | None
    started_at: datetime
    started_ns: int


current_operation: ContextVar[OperationContext | None] = ContextVar(
    "blackcell_current_operation",
    default=None,
)


class OperationAspect(Protocol):
    def before(self, context: OperationContext) -> None: ...

    def after(self, context: OperationContext, result: ResultEnvelope) -> None: ...

    def failed(self, context: OperationContext, error: BlackcellError) -> None: ...


class CredentialAspect:
    def __init__(self, prepare: Callable[[Credential], None]) -> None:
        self._prepare = prepare

    def before(self, context: OperationContext) -> None:
        for credential in sorted(context.spec.credentials):
            self._prepare(credential)

    def after(self, context: OperationContext, result: ResultEnvelope) -> None:
        del context, result

    def failed(self, context: OperationContext, error: BlackcellError) -> None:
        del context, error


class AnomalyAspect:
    def __init__(self, chronicle: ChronicleStore) -> None:
        self._chronicle = chronicle

    def before(self, context: OperationContext) -> None:
        del context

    def after(self, context: OperationContext, result: ResultEnvelope) -> None:
        del context, result

    def failed(self, context: OperationContext, error: BlackcellError) -> None:
        if not isinstance(error, ConflictFailure):
            return
        plan_id = str(error.details.get("plan_id") or context.plan_id or "BCP-0000")
        self._chronicle.append(
            EventType.ANOMALY_DETECTED,
            plan_id,
            {
                "code": error.code,
                "message": error.message,
                "details": error.details,
                "operation": context.spec.name,
                "correlation_id": context.correlation_id,
            },
        )


class StructuredEventAspect:
    def __init__(self, sink: EventSink | None = None) -> None:
        self._sink = sink or NullEventSink()

    def before(self, context: OperationContext) -> None:
        self._emit(context, "operation.started")

    def after(self, context: OperationContext, result: ResultEnvelope) -> None:
        self._emit(
            context,
            "operation.completed",
            status=result.status,
            exit_class=result.exit_class,
        )

    def failed(self, context: OperationContext, error: BlackcellError) -> None:
        self._emit(
            context,
            "operation.failed",
            status="error",
            exit_class=error.exit_class.name.lower(),
            error_code=error.code,
        )

    def _emit(self, context: OperationContext, event: str, **fields: Any) -> None:
        self._sink.emit(
            {
                "event": event,
                "occurred_at": datetime.now(UTC).isoformat(),
                "correlation_id": context.correlation_id,
                "operation": context.spec.name,
                "facade": context.spec.facade,
                "authority": context.spec.authority,
                "effect": context.spec.effect,
                "plan_id": context.plan_id,
                "duration_ms": round((monotonic_ns() - context.started_ns) / 1_000_000, 3),
                **fields,
            }
        )


class OperationExecutor:
    def __init__(self, aspects: Sequence[OperationAspect] = ()) -> None:
        self._aspects = tuple(aspects)

    def execute(
        self,
        spec: OperationSpec,
        callback: Callable[[], Mapping[str, Any] | PendingOutcome],
        *,
        plan_id: str | None = None,
    ) -> ResultEnvelope:
        context = OperationContext(
            spec=spec,
            correlation_id=str(uuid4()),
            plan_id=plan_id,
            started_at=datetime.now(UTC),
            started_ns=monotonic_ns(),
        )
        token = current_operation.set(context)
        try:
            for aspect in self._aspects:
                aspect.before(context)
            outcome = callback()
            result = (
                ResultEnvelope.pending(
                    outcome.code,
                    outcome.message,
                    outcome.recovery,
                    dict(outcome.data),
                )
                if isinstance(outcome, PendingOutcome)
                else ResultEnvelope.ok(dict(outcome))
            )
            result = result.with_context(
                operation=spec.name,
                facade=spec.facade,
                correlation_id=context.correlation_id,
            )
            for aspect in reversed(self._aspects):
                aspect.after(context, result)
            return result
        except BlackcellError as error:
            for aspect in reversed(self._aspects):
                aspect.failed(context, error)
            return ResultEnvelope.from_error(error).with_context(
                operation=spec.name,
                facade=spec.facade,
                correlation_id=context.correlation_id,
            )
        finally:
            current_operation.reset(token)
