from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from threading import Event
from types import MappingProxyType
from typing import cast

from blackcell.adapters.persistence.sqlite import SQLiteOrchestrationScheduler
from blackcell.adapters.telemetry import RuntimeTelemetry
from blackcell.bootstrap.role_dag import (
    EXECUTE_HANDLER,
    PLAN_HANDLER,
    PLAN_SCHEMA,
    REVIEW_HANDLER,
    REVIEW_SCHEMA,
    RUN_SCHEMA,
    SUMMARY_SCHEMA,
    SYNTHESIZE_HANDLER,
    VERIFICATION_SCHEMA,
    VERIFY_HANDLER,
)
from blackcell.config import RuntimeProcessConfig
from blackcell.features.replay_run import ReplayClassification
from blackcell.kernel import JsonInput
from blackcell.operator import DEFAULT_OBJECTIVE, RepositoryOperator
from blackcell.operator.serialization import jsonable
from blackcell.orchestration import (
    NodeStatus,
    NodeUsage,
    OrchestrationLeaseConflict,
    OrchestrationNodeLease,
    OrchestrationResultConflict,
    OrchestrationSchedulerPort,
)
from blackcell.runtime import RuntimeStorageQuota, StorageQuotaPort
from blackcell.workflows.run_protocol import MODEL_RESPONDED

_FAILURE_CODE = re.compile(r"[a-z0-9][a-z0-9._-]{0,99}\Z")


class WorkerHandlerError(RuntimeError):
    def __init__(self, code: str) -> None:
        if not _FAILURE_CODE.fullmatch(code):
            raise ValueError("worker failure code must be bounded")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class WorkerInput:
    input_name: str
    source_node_id: str
    source_schema: str
    result_digest: str


@dataclass(frozen=True, slots=True)
class WorkerWork:
    lease: OrchestrationNodeLease
    inputs: Mapping[str, WorkerInput]


@dataclass(frozen=True, slots=True)
class HandlerOutcome:
    result_digest: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_microusd: int = 0


WorkerHandler = Callable[[WorkerWork], HandlerOutcome]


@dataclass(frozen=True, slots=True)
class HandlerRegistration:
    output_schema: str
    handler: WorkerHandler


class RuntimeWorker:
    """One-at-a-time local worker over the durable fenced scheduler."""

    def __init__(
        self,
        operator: RepositoryOperator,
        scheduler: OrchestrationSchedulerPort,
        config: RuntimeProcessConfig,
        *,
        handlers: Mapping[str, HandlerRegistration] | None = None,
        stop_event: Event | None = None,
        monotonic_clock: Callable[[], float] = time.monotonic,
        shutdown: Callable[[], None] | None = None,
        storage_quota: StorageQuotaPort | None = None,
    ) -> None:
        self._operator = operator
        self._artifacts = operator.artifacts
        self._scheduler = scheduler
        self._config = config
        self.stop_event = stop_event or Event()
        self._monotonic_clock = monotonic_clock
        self._shutdown = shutdown or (lambda: None)
        self._storage_quota = storage_quota
        builtins = {
            PLAN_HANDLER: HandlerRegistration(PLAN_SCHEMA, self._plan),
            EXECUTE_HANDLER: HandlerRegistration(RUN_SCHEMA, self._execute),
            REVIEW_HANDLER: HandlerRegistration(REVIEW_SCHEMA, self._review),
            VERIFY_HANDLER: HandlerRegistration(VERIFICATION_SCHEMA, self._verify),
            SYNTHESIZE_HANDLER: HandlerRegistration(SUMMARY_SCHEMA, self._synthesize),
        }
        self._handlers = MappingProxyType(dict(builtins if handlers is None else handlers))

    @classmethod
    def from_config(
        cls,
        config: RuntimeProcessConfig,
        *,
        stop_event: Event | None = None,
    ) -> RuntimeWorker:
        telemetry = RuntimeTelemetry.from_config(config)
        try:
            database_path = config.security.paths.ensure_database_file()
            operator = RepositoryOperator(
                config.repository_root,
                database_path=database_path,
                artifact_root=config.security.paths.artifact_root,
                workflow_telemetry=telemetry.workflow,
                artifact_max_total_bytes=config.quota.artifact_max_total_bytes,
            )
            scheduler = SQLiteOrchestrationScheduler(database_path)
        except Exception:
            telemetry.shutdown()
            raise
        return cls(
            operator,
            scheduler,
            config,
            stop_event=stop_event,
            shutdown=telemetry.shutdown,
            storage_quota=RuntimeStorageQuota(
                config.security.paths,
                max_active_bytes=config.quota.active_storage_max_bytes,
                mutation_reserve_bytes=config.quota.mutation_reserve_bytes,
            ),
        )

    def serve(self, *, once: bool = False) -> int:
        try:
            while not self.stop_event.is_set():
                worked = self.run_once()
                if once:
                    return 0 if worked else 3
                if not worked:
                    self.stop_event.wait(self._config.worker_poll_milliseconds / 1_000)
            return 0
        finally:
            self._shutdown()

    def run_once(self) -> bool:
        if self._storage_quota is not None and not self._storage_quota.has_mutation_capacity():
            return False
        self._scheduler.recover_expired()
        if self.stop_event.is_set():
            return False
        lease = self._scheduler.acquire(
            self._config.worker_id,
            lease_seconds=self._config.worker_lease_seconds,
        )
        if lease is None:
            return False
        started = self._monotonic_clock()
        try:
            registration = self._handlers.get(lease.node.handler)
            if registration is None:
                raise WorkerHandlerError("handler-unavailable")
            if registration.output_schema != lease.node.output_schema:
                raise WorkerHandlerError("handler-contract-mismatch")
            work = WorkerWork(lease, self._resolve_inputs(lease))
            outcome = registration.handler(work)
            usage = _usage(outcome, _elapsed_ms(started, self._monotonic_clock()))
            if usage.exceeds(lease.node.budget):
                self._scheduler.fail(
                    lease,
                    failure_code="budget-exceeded",
                    usage=usage,
                )
            else:
                self._artifacts.verify(outcome.result_digest)
                self._scheduler.complete(
                    lease,
                    result_digest=outcome.result_digest,
                    output_schema=registration.output_schema,
                    usage=usage,
                )
        except WorkerHandlerError as error:
            self._fail(lease, error.code, started)
        except OrchestrationLeaseConflict, OrchestrationResultConflict:
            pass
        except Exception:
            self._fail(lease, "handler-failed", started)
        return True

    def _fail(self, lease: OrchestrationNodeLease, code: str, started: float) -> None:
        usage = NodeUsage(latency_ms=_elapsed_ms(started, self._monotonic_clock()))
        with suppress(OrchestrationLeaseConflict, OrchestrationResultConflict):
            self._scheduler.fail(lease, failure_code=code, usage=usage)

    def _resolve_inputs(self, lease: OrchestrationNodeLease) -> Mapping[str, WorkerInput]:
        snapshot = self._scheduler.inspect(lease.run_id)
        nodes = {item.node_id: item for item in snapshot.nodes}
        values: dict[str, WorkerInput] = {}
        for binding in lease.node.inputs:
            source = nodes.get(binding.source_node_id)
            if (
                source is None
                or source.status is not NodeStatus.SUCCEEDED
                or source.result_digest is None
            ):
                raise WorkerHandlerError("input-unavailable")
            values[binding.input_name] = WorkerInput(
                binding.input_name,
                binding.source_node_id,
                binding.source_schema,
                source.result_digest,
            )
        return MappingProxyType(values)

    def _plan(self, work: WorkerWork) -> HandlerOutcome:
        if work.inputs:
            raise WorkerHandlerError("invalid-input")
        reference = self._artifacts.put_json(
            {
                "schema_version": PLAN_SCHEMA,
                "objective": DEFAULT_OBJECTIVE,
                "model_route": "recorded",
            }
        )
        return HandlerOutcome(reference.digest)

    def _execute(self, work: WorkerWork) -> HandlerOutcome:
        plan = self._input_artifact(work, "plan", PLAN_SCHEMA)
        objective = _text(plan, "objective")
        result = self._operator.run(objective=objective)
        reference = self._artifacts.put_json(cast("JsonInput", jsonable(result)))
        replay = self._operator.replay(result.run_id)
        response = next(
            (item for item in replay.events if item.event_type == MODEL_RESPONDED),
            None,
        )
        if response is None:
            raise WorkerHandlerError("usage-unavailable")
        return HandlerOutcome(
            reference.digest,
            input_tokens=_integer(response.payload, "input_tokens"),
            output_tokens=_integer(response.payload, "output_tokens"),
            cost_microusd=_integer(response.payload, "cost_microusd"),
        )

    def _review(self, work: WorkerWork) -> HandlerOutcome:
        run = self._input_artifact(work, "run", RUN_SCHEMA)
        approved = (
            run.get("status") == ReplayClassification.COMPLETED.value
            and run.get("evaluation_verdict") == "pass"
            and run.get("transition_recorded") is True
        )
        findings = [] if approved else ["canonical-run-not-accepted"]
        reference = self._artifacts.put_json(
            {
                "schema_version": REVIEW_SCHEMA,
                "run_id": _text(run, "run_id"),
                "approved": approved,
                "findings": findings,
                "run_artifact_digest": work.inputs["run"].result_digest,
            }
        )
        return HandlerOutcome(reference.digest)

    def _verify(self, work: WorkerWork) -> HandlerOutcome:
        run = self._input_artifact(work, "run", RUN_SCHEMA)
        review = self._input_artifact(work, "review", REVIEW_SCHEMA)
        run_id = _text(run, "run_id")
        replay = self._operator.replay(run_id)
        verified = (
            replay.classification.value == run.get("status")
            and replay.finding is None
            and all(item.verified for item in replay.artifacts)
            and review.get("approved") is True
        )
        reference = self._artifacts.put_json(
            {
                "schema_version": VERIFICATION_SCHEMA,
                "run_id": run_id,
                "verified": verified,
                "classification": replay.classification.value,
                "artifact_count": len(replay.artifacts),
                "run_artifact_digest": work.inputs["run"].result_digest,
                "review_artifact_digest": work.inputs["review"].result_digest,
            }
        )
        return HandlerOutcome(reference.digest)

    def _synthesize(self, work: WorkerWork) -> HandlerOutcome:
        review = self._input_artifact(work, "review", REVIEW_SCHEMA)
        verification = self._input_artifact(work, "verification", VERIFICATION_SCHEMA)
        accepted = review.get("approved") is True and verification.get("verified") is True
        reference = self._artifacts.put_json(
            {
                "schema_version": SUMMARY_SCHEMA,
                "run_id": _text(verification, "run_id"),
                "accepted": accepted,
                "review_artifact_digest": work.inputs["review"].result_digest,
                "verification_artifact_digest": work.inputs["verification"].result_digest,
            }
        )
        return HandlerOutcome(reference.digest)

    def _input_artifact(
        self,
        work: WorkerWork,
        name: str,
        schema_version: str,
    ) -> Mapping[str, object]:
        value = work.inputs.get(name)
        if value is None or value.source_schema != schema_version:
            raise WorkerHandlerError("invalid-input")
        payload = self._artifacts.get_json(value.result_digest)
        if not isinstance(payload, Mapping) or payload.get("schema_version") != schema_version:
            raise WorkerHandlerError("invalid-input-artifact")
        return cast("Mapping[str, object]", payload)


def _usage(outcome: HandlerOutcome, elapsed_ms: int) -> NodeUsage:
    try:
        return NodeUsage(
            outcome.input_tokens,
            outcome.output_tokens,
            elapsed_ms,
            outcome.cost_microusd,
        )
    except (TypeError, ValueError) as error:
        raise WorkerHandlerError("invalid-handler-usage") from error


def _elapsed_ms(started: float, ended: float) -> int:
    return max(0, int((ended - started) * 1_000))


def _text(value: Mapping[str, object], field: str) -> str:
    item = value.get(field)
    if not isinstance(item, str) or not item.strip():
        raise WorkerHandlerError("invalid-input-artifact")
    return item


def _integer(value: Mapping[str, object], field: str) -> int:
    item = value.get(field)
    if isinstance(item, bool) or not isinstance(item, int) or item < 0:
        raise WorkerHandlerError("invalid-input-artifact")
    return item


__all__ = [
    "HandlerOutcome",
    "HandlerRegistration",
    "RuntimeWorker",
    "WorkerHandler",
    "WorkerHandlerError",
    "WorkerInput",
    "WorkerWork",
]
