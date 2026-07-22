from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import cast

from blackcell.adapters.persistence.sqlite import SQLiteOrchestrationScheduler
from blackcell.bootstrap.alpha_runtime import AlphaRuntimeApiService
from blackcell.bootstrap.repository import compose_repository_runtime
from blackcell.config import RuntimeSecurityConfig
from blackcell.features.ingest_observation import (
    EvidencePointer,
    IngestObservation,
    IngestObservationHandler,
    ObservationInput,
    ObservedClaim,
)
from blackcell.features.replay_run import RunReplayReport
from blackcell.interfaces.http.alpha_contracts import (
    AlphaCancelRunRequest,
    AlphaEventPageResponse,
    AlphaIntentRequest,
    AlphaIntentResponse,
    AlphaPlanRequest,
    AlphaPlanResponse,
    AlphaProjectRequest,
    AlphaProjectResponse,
    AlphaReplayResponse,
    AlphaRunRequest,
    AlphaRunResponse,
)
from blackcell.interfaces.http.contracts import (
    ApprovalRequest,
    ContextResponse,
    EvaluationResponse,
    EventPageResponse,
    EventResponse,
    HealthResponse,
    ObservationIngestRequest,
    ObservationIngestResponse,
    OrchestrationApprovalResponse,
    OrchestrationNodeResponse,
    OrchestrationRunResponse,
    ReplayArtifactResponse,
    ReplayFindingResponse,
    ReplayProjectionResponse,
    ReplayResponse,
    RunResponse,
    RunSubmissionRequest,
)
from blackcell.interfaces.http.ports import (
    AlphaRuntimeApiPort,
    RuntimeApiError,
    RuntimeApiFailureCode,
    RuntimeApiPort,
)
from blackcell.kernel import (
    ArtifactQuotaExceededError,
    ArtifactStore,
    ConcurrencyError,
    EventEnvelope,
    EventStore,
    KernelError,
)
from blackcell.operator import (
    CanonicalOperatorRunResult,
    RepositoryOperator,
)
from blackcell.operator.serialization import jsonable
from blackcell.orchestration import (
    OrchestrationApproval,
    OrchestrationApprovalConflict,
    OrchestrationRole,
    OrchestrationRunConflict,
    OrchestrationRunSnapshot,
    OrchestrationSchedulerError,
    OrchestrationSchedulerPort,
)
from blackcell.runtime import StorageQuotaPort
from blackcell.workflows.run_protocol import EVALUATION_RECORDED
from blackcell.workflows.telemetry import WorkflowTelemetry


class RuntimeApiService(RuntimeApiPort, AlphaRuntimeApiPort):
    """Concrete HTTP-facing adapter over canonical runtime application use cases."""

    def __init__(
        self,
        operator: RepositoryOperator,
        scheduler: OrchestrationSchedulerPort,
        *,
        events: EventStore,
        artifacts: ArtifactStore | None = None,
        alpha_isolation_root: Path | str | None = None,
        storage_quota: StorageQuotaPort | None = None,
    ) -> None:
        if events.path != operator.database_path:
            raise ValueError("runtime API event store does not match the operator database")
        self._operator = operator
        self._events = events
        self._ingestion = IngestObservationHandler(self._events)
        self._scheduler = scheduler
        self._storage_quota = storage_quota
        self._alpha = AlphaRuntimeApiService(
            events,
            operator.repo_root,
            isolation_root=alpha_isolation_root,
            artifacts=artifacts,
        )

    @classmethod
    def from_config(
        cls,
        config: RuntimeSecurityConfig,
        *,
        repository_root: Path | str,
        workflow_telemetry: WorkflowTelemetry | None = None,
        artifact_max_total_bytes: int | None = None,
        alpha_isolation_root: Path | str | None = None,
        storage_quota: StorageQuotaPort | None = None,
    ) -> RuntimeApiService:
        database_path = config.paths.ensure_database_file()
        components = compose_repository_runtime(
            Path(repository_root),
            database_path=database_path,
            artifact_root=config.paths.artifact_root,
            workflow_telemetry=workflow_telemetry,
            artifact_max_total_bytes=artifact_max_total_bytes,
        )
        return cls(
            components.operator,
            SQLiteOrchestrationScheduler(database_path),
            events=components.events,
            artifacts=components.artifacts,
            alpha_isolation_root=alpha_isolation_root,
            storage_quota=storage_quota,
        )

    def readiness(self) -> HealthResponse:
        try:
            self._events.read_all(after_position=0, limit=1)
            if self._storage_quota is not None and not self._storage_quota.has_mutation_capacity():
                return HealthResponse(status="not-ready")
        except Exception:
            return HealthResponse(status="not-ready")
        return HealthResponse(status="ready")

    def register_alpha_project(
        self,
        request: AlphaProjectRequest,
        *,
        principal_id: str,
    ) -> AlphaProjectResponse:
        _principal(principal_id)
        self._require_storage()
        return _translate(lambda: self._alpha.register_project(request, principal_id=principal_id))

    def accept_alpha_intent(
        self,
        request: AlphaIntentRequest,
        *,
        principal_id: str,
    ) -> AlphaIntentResponse:
        _principal(principal_id)
        self._require_storage()
        return _translate(lambda: self._alpha.accept_intent(request, principal_id=principal_id))

    def accept_alpha_plan(
        self,
        request: AlphaPlanRequest,
        *,
        principal_id: str,
    ) -> AlphaPlanResponse:
        _principal(principal_id)
        self._require_storage()
        return _translate(lambda: self._alpha.accept_plan(request, principal_id=principal_id))

    def submit_alpha_run(
        self,
        request: AlphaRunRequest,
        *,
        principal_id: str,
    ) -> AlphaRunResponse:
        _principal(principal_id)
        self._require_storage()
        return _translate(lambda: self._alpha.submit_run(request, principal_id=principal_id))

    def inspect_alpha_run(self, run_id: str) -> AlphaRunResponse:
        return _translate(lambda: self._alpha.inspect_run(run_id))

    def cancel_alpha_run(
        self,
        run_id: str,
        request: AlphaCancelRunRequest,
        *,
        principal_id: str,
    ) -> AlphaRunResponse:
        _principal(principal_id)
        self._require_storage()
        return _translate(
            lambda: self._alpha.cancel_run(run_id, request, principal_id=principal_id)
        )

    def list_alpha_events(
        self,
        *,
        after_cursor: int,
        limit: int,
    ) -> AlphaEventPageResponse:
        return _translate(lambda: self._alpha.list_events(after_cursor=after_cursor, limit=limit))

    def replay_alpha_run(self, run_id: str) -> AlphaReplayResponse:
        return _translate(lambda: self._alpha.replay_run(run_id))

    def ingest_observations(
        self,
        request: ObservationIngestRequest,
        *,
        principal_id: str,
    ) -> ObservationIngestResponse:
        _principal(principal_id)
        self._require_storage()

        def operation() -> ObservationIngestResponse:
            events = self._ingestion.handle(
                IngestObservation(
                    stream_id=request.stream_id,
                    expected_sequence=request.expected_sequence,
                    actor=principal_id,
                    source=request.source,
                    correlation_id=request.correlation_id,
                    causation_id=request.causation_id,
                    domain=request.domain,
                    observations=tuple(
                        ObservationInput(
                            observation_id=observation.observation_id,
                            effective_at=observation.effective_at,
                            claims=tuple(
                                ObservedClaim(
                                    claim_id=claim.claim_id,
                                    subject=claim.subject,
                                    predicate=claim.predicate,
                                    value=claim.value,
                                    confidence=claim.confidence,
                                    expires_at=claim.expires_at,
                                )
                                for claim in observation.claims
                            ),
                            evidence=tuple(
                                EvidencePointer(
                                    locator=evidence.locator,
                                    artifact_id=evidence.artifact_id,
                                    digest=evidence.digest,
                                )
                                for evidence in observation.evidence
                            ),
                            idempotency_key=observation.idempotency_key,
                        )
                        for observation in request.observations
                    ),
                )
            )
            return ObservationIngestResponse(
                stream_id=request.stream_id,
                event_ids=tuple(item.event_id for item in events),
                first_sequence=events[0].stream_sequence,
                last_sequence=events[-1].stream_sequence,
            )

        return _translate(operation)

    def submit_run(
        self,
        request: RunSubmissionRequest,
        *,
        principal_id: str,
    ) -> RunResponse:
        _principal(principal_id)
        self._require_storage()
        return _translate(
            lambda: _run_response(
                self._operator.run(
                    objective=request.objective,
                    approval_granted=request.approval_granted,
                    token_budget=request.token_budget,
                    character_budget=request.character_budget,
                )
            )
        )

    def inspect_run(self, run_id: str) -> RunResponse:
        return _translate(
            lambda: _run_response_from_replay(
                self._operator.replay(run_id),
                self._operator.repository_stream_id,
            )
        )

    def inspect_context(self, run_id: str) -> ContextResponse:
        def operation() -> ContextResponse:
            context = self._operator.context(run_id)
            payload = jsonable(context.payload)
            if not isinstance(payload, dict):
                raise TypeError("context payload must normalize to an object")
            return ContextResponse(
                run_id=context.run_id,
                frame_id=context.frame_id,
                artifact_digest=context.artifact_digest,
                payload=cast("dict[str, object]", payload),
            )

        return _translate(operation)

    def replay_run(self, run_id: str) -> ReplayResponse:
        return _translate(lambda: _replay_response(self._operator.replay(run_id)))

    def inspect_evaluation(self, run_id: str) -> EvaluationResponse:
        def operation() -> EvaluationResponse:
            replay = self._operator.replay(run_id)
            event = next(
                (item for item in replay.events if item.event_type == EVALUATION_RECORDED),
                None,
            )
            if event is None:
                raise LookupError("evaluation not recorded")
            artifact = _mapping(event.payload.get("artifact"))
            return EvaluationResponse(
                run_id=run_id,
                evaluation_id=_text(event.payload, "evaluation_id"),
                evaluation_spec_id=_text(event.payload, "evaluation_spec_id"),
                verdict=_text(event.payload, "verdict"),
                artifact_digest=_text(artifact, "digest"),
            )

        return _translate(operation)

    def list_events(self, *, after_position: int, limit: int) -> EventPageResponse:
        def operation() -> EventPageResponse:
            events = self._events.read_all(after_position=after_position, limit=limit)
            values = tuple(_event_response(item) for item in events)
            next_position = values[-1].global_position if values else after_position
            return EventPageResponse(
                after_position=after_position,
                limit=limit,
                events=values,
                next_after_position=next_position,
            )

        return _translate(operation)

    def inspect_orchestration(self, run_id: str) -> OrchestrationRunResponse:
        return _translate(lambda: _orchestration_response(self._scheduler.inspect(run_id)))

    def record_orchestration_approval(
        self,
        run_id: str,
        node_id: str,
        request: ApprovalRequest,
        *,
        principal_id: str,
    ) -> OrchestrationApprovalResponse:
        _principal(principal_id)
        self._require_storage()
        return _translate(
            lambda: _approval_response(
                self._scheduler.record_approval(
                    run_id,
                    node_id,
                    OrchestrationRole(request.role),
                    principal_id=principal_id,
                    approved=request.approved,
                )
            )
        )

    def _require_storage(self) -> None:
        if self._storage_quota is not None and not self._storage_quota.has_mutation_capacity():
            raise RuntimeApiError(RuntimeApiFailureCode.STORAGE_QUOTA_EXCEEDED)


def _translate[ResultT](operation: Callable[[], ResultT]) -> ResultT:
    try:
        return operation()
    except RuntimeApiError:
        raise
    except ArtifactQuotaExceededError as error:
        raise RuntimeApiError(RuntimeApiFailureCode.STORAGE_QUOTA_EXCEEDED) from error
    except (ConcurrencyError, OrchestrationApprovalConflict, OrchestrationRunConflict) as error:
        raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT) from error
    except LookupError as error:
        raise RuntimeApiError(RuntimeApiFailureCode.NOT_FOUND) from error
    except (TypeError, ValueError) as error:
        raise RuntimeApiError(RuntimeApiFailureCode.INVALID_REQUEST) from error
    except KernelError, OrchestrationSchedulerError:
        raise


def _principal(value: str) -> None:
    if not value.strip() or len(value) > 200:
        raise RuntimeApiError(RuntimeApiFailureCode.INVALID_REQUEST)


def _run_response(result: CanonicalOperatorRunResult) -> RunResponse:
    return RunResponse(
        run_id=result.run_id,
        status=result.status,
        outcome=result.outcome,
        workflow_version=result.workflow_version,
        repository_stream_id=result.repository_stream_id,
        run_stream_id=result.run_stream_id,
        context_frame_id=result.context_frame_id,
        authorization_outcome=result.authorization_outcome,
        execution_status=result.execution_status,
        evaluation_verdict=result.evaluation_verdict,
        transition_recorded=result.transition_recorded,
        run_event_count=result.run_event_count,
        artifact_count=result.artifact_count,
    )


def _run_response_from_replay(
    replay: RunReplayReport,
    repository_stream_id: str,
) -> RunResponse:
    by_type = {item.event_type: item for item in replay.events}
    context = by_type.get("run.context-recorded")
    authorization = by_type.get("run.authorization-decided")
    execution = by_type.get("run.execution-recorded")
    evaluation = by_type.get(EVALUATION_RECORDED)
    return RunResponse(
        run_id=replay.run_id,
        status=replay.classification.value,
        outcome=replay.outcome,
        workflow_version=replay.protocol_version,
        repository_stream_id=repository_stream_id,
        run_stream_id=replay.run_stream_id,
        context_frame_id=_optional_text(context, "frame_id"),
        authorization_outcome=_optional_text(authorization, "outcome"),
        execution_status=_optional_text(execution, "status"),
        evaluation_verdict=_optional_text(evaluation, "verdict"),
        transition_recorded="run.state-transition-recorded" in by_type,
        run_event_count=replay.event_count,
        artifact_count=len(replay.artifacts),
    )


def _replay_response(replay: RunReplayReport) -> ReplayResponse:
    finding = replay.finding
    return ReplayResponse(
        run_id=replay.run_id,
        run_stream_id=replay.run_stream_id,
        protocol_version=replay.protocol_version,
        classification=replay.classification.value,
        outcome=replay.outcome,
        events=tuple(_event_response(item) for item in replay.events),
        artifacts=tuple(
            ReplayArtifactResponse(
                event_id=item.event_id,
                event_type=item.event_type,
                stream_sequence=item.stream_sequence,
                field=item.field,
                digest=item.digest,
                verified=item.verified,
            )
            for item in replay.artifacts
        ),
        projections=tuple(
            ReplayProjectionResponse(
                stage=item.stage.value,
                status=item.status.value,
                snapshot_digest=item.snapshot_digest,
                cutoff_global_position=item.cutoff_global_position,
                effective_time_cutoff=(
                    None
                    if item.effective_time_cutoff is None
                    else item.effective_time_cutoff.isoformat()
                ),
            )
            for item in replay.projections
        ),
        finding=(
            None
            if finding is None
            else ReplayFindingResponse(
                stage=finding.stage.value,
                code=finding.code,
                message=finding.message,
            )
        ),
    )


def _event_response(event: EventEnvelope) -> EventResponse:
    payload = jsonable(event.payload)
    if not isinstance(payload, dict) or event.global_position is None:
        raise TypeError("persisted event must have an object payload and global position")
    return EventResponse(
        event_id=event.event_id,
        global_position=event.global_position,
        stream_id=event.stream_id,
        stream_sequence=event.stream_sequence,
        event_type=event.event_type,
        schema_version=event.schema_version,
        recorded_at=event.recorded_at.isoformat(),
        effective_at=event.effective_at.isoformat(),
        actor=event.actor,
        source=event.source,
        correlation_id=event.correlation_id,
        causation_id=event.causation_id,
        idempotency_key=event.idempotency_key,
        payload_hash=event.payload_hash,
        payload=cast("dict[str, object]", payload),
    )


def _orchestration_response(snapshot: OrchestrationRunSnapshot) -> OrchestrationRunResponse:
    return OrchestrationRunResponse(
        run_id=snapshot.run_id,
        dag_id=snapshot.definition.dag_id,
        dag_digest=snapshot.definition.dag_digest,
        status=snapshot.status.value,
        submitted_by=snapshot.submitted_by,
        submitted_at=snapshot.submitted_at.isoformat(),
        updated_at=snapshot.updated_at.isoformat(),
        nodes=tuple(
            OrchestrationNodeResponse(
                node_id=item.node_id,
                status=item.status.value,
                attempts=item.attempts,
                fencing_token=item.fencing_token,
                available_at=item.available_at.isoformat(),
                lease_worker_id=item.lease_worker_id,
                lease_expires_at=(
                    None if item.lease_expires_at is None else item.lease_expires_at.isoformat()
                ),
                result_digest=item.result_digest,
                failure_code=item.failure_code,
                input_tokens=item.usage.input_tokens,
                output_tokens=item.usage.output_tokens,
                latency_ms=item.usage.latency_ms,
                cost_microusd=item.usage.cost_microusd,
            )
            for item in snapshot.nodes
        ),
        approvals=tuple(_approval_response(item) for item in snapshot.approvals),
    )


def _approval_response(approval: OrchestrationApproval) -> OrchestrationApprovalResponse:
    return OrchestrationApprovalResponse(
        node_id=approval.node_id,
        role=approval.role.value,
        principal_id=approval.principal_id,
        approved=approval.approved,
        decided_at=approval.decided_at.isoformat(),
        decision_digest=approval.decision_digest,
    )


def _optional_text(event: EventEnvelope | None, field: str) -> str | None:
    if event is None:
        return None
    value = event.payload.get(field)
    return value if isinstance(value, str) and value.strip() else None


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError("event field must be an object")
    return cast("Mapping[str, object]", value)


def _text(value: Mapping[str, object], field: str) -> str:
    item = value.get(field)
    if not isinstance(item, str) or not item.strip():
        raise TypeError("event field must be non-empty text")
    return item


__all__ = ["RuntimeApiService"]
