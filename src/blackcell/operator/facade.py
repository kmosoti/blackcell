from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from blackcell.adapters.models import (
    CODEX_CLI_ADAPTER_ID,
    CodexCliModelAdapter,
    GatewayDecisionAdapter,
)
from blackcell.adapters.persistence.sqlite import (
    KernelRunReplayAdapter,
    SQLiteDecisionAttemptJournal,
    SQLiteExecutionJournal,
)
from blackcell.adapters.persistence.sqlite.run_records_v2 import KernelFeedbackRunRecorder
from blackcell.domains.repository import ClaimCorrection
from blackcell.features.authorize_action import AffordancePolicy
from blackcell.features.build_context import (
    BuildContext,
    decode_context_frame,
    serialize_context_frame,
)
from blackcell.features.derive_signal_packet import DeriveSignalPacket
from blackcell.features.evaluate_outcome import (
    EvaluationCriterion,
    EvaluationSpec,
    OutcomeEvaluator,
)
from blackcell.features.execute_affordance import (
    AffordanceDefinition,
    AffordanceExecutionHandler,
    SideEffectClass,
)
from blackcell.features.ingest_observation import (
    CorrectionInput,
    EvidencePointer,
    IngestCorrection,
    IngestCorrectionHandler,
    IngestObservation,
    IngestObservationHandler,
    ObservationInput,
    ObservedClaim,
)
from blackcell.features.project_operational_state import (
    OperationalBeliefState,
    OperationalStateScope,
    ProjectOperationalState,
    ProjectOperationalStateHandler,
)
from blackcell.features.replay_run import ReplayRun, ReplayRunHandler, RunReplayReport
from blackcell.features.request_decision import (
    DecisionBudget,
    DecisionCapability,
    DecisionClassification,
    DecisionLocality,
    DecisionRequirements,
    RequestDecisionHandler,
)
from blackcell.features.retrieve_evidence import EvidenceKey, RetrieveEvidence
from blackcell.features.solve_constraints import (
    ConstraintDefinition,
    ConstraintOperator,
    SolveConstraints,
)
from blackcell.gateway import (
    DataClassification,
    GatewayProfile,
    ModelCapability,
    ModelGateway,
)
from blackcell.gateway.ports import ModelAdapter
from blackcell.kernel import (
    ArtifactStore,
    CheckpointStore,
    EventEnvelope,
    EventStore,
    new_event_id,
)
from blackcell.operator.models import CanonicalOperatorRunResult, StoredContextFrame
from blackcell.operator.repository_adapters import (
    REPOSITORY_MODEL_ADAPTER_ID,
    REPOSITORY_OUTCOME_CONTRACT_VERSION,
    REPOSITORY_OUTCOME_OBSERVER_ID,
    REPOSITORY_STATUS_ADAPTER_ID,
    RepositoryRecordedModelAdapter,
    RepositoryStatusExecutionAdapter,
    RepositoryStatusOutcomeObserver,
    RepositoryStatusReader,
)
from blackcell.operator.service import _validated_git_directory
from blackcell.workflows import (
    DailyOperatorV2Request,
    DailyOperatorV2Workflow,
    WorkflowTelemetry,
)
from blackcell.workflows.outcome_evidence import OutcomeEvidenceWriter
from blackcell.workflows.run_protocol import (
    AUTHORIZATION_DECIDED,
    CONTEXT_RECORDED,
    EVALUATION_RECORDED,
    EXECUTION_RECORDED,
    RUN_STARTED,
    STATE_TRANSITION_RECORDED,
    run_stream_id,
)

DEFAULT_OBJECTIVE = "Inspect current repository readiness through one read-only diagnostic."
DEFAULT_CONSTRAINTS = (
    "The repository must remain a valid Git worktree.",
    "Only the declared read-only repository inspection may execute.",
)

Clock = Callable[[], datetime]


class RepositoryOperator:
    """Product facade for the canonical Daily Operator v2 repository workflow."""

    def __init__(
        self,
        repo_root: Path | str,
        *,
        database_path: Path | str | None = None,
        artifact_root: Path | str | None = None,
        model: Literal["recorded", "codex"] = "recorded",
        codex_model: str | None = None,
        status_reader: RepositoryStatusReader | None = None,
        clock: Clock = lambda: datetime.now(UTC),
        workflow_telemetry: WorkflowTelemetry | None = None,
    ) -> None:
        if model not in {"recorded", "codex"}:
            raise ValueError(f"unsupported repository operator model route: {model!r}")
        if model == "codex" and (codex_model is None or not codex_model.strip()):
            raise ValueError("--codex-model is required when --model=codex")
        if model == "recorded" and codex_model is not None:
            raise ValueError("--codex-model is only valid when --model=codex")

        self.repo_root = Path(repo_root).resolve()
        git_directory = _validated_git_directory(self.repo_root)
        self.database_path = (
            Path(database_path)
            if database_path is not None
            else git_directory / "blackcell" / "kernel.sqlite3"
        )
        self.artifact_root = (
            Path(artifact_root)
            if artifact_root is not None
            else self.database_path.parent / "artifacts"
        )
        self._clock = clock
        self._model_route = model
        self._codex_model = codex_model
        root_digest = hashlib.sha256(str(self.repo_root).encode()).hexdigest()[:20]
        self.repository_stream_id = f"repository:{root_digest}"

        self.events = EventStore(self.database_path)
        self.artifacts = ArtifactStore(self.artifact_root, database_path=self.database_path)
        self._decision_journal = SQLiteDecisionAttemptJournal(
            self.artifact_root,
            database_path=self.database_path,
        )
        self._execution_journal = SQLiteExecutionJournal(
            self.artifact_root,
            database_path=self.database_path,
        )
        self._state = ProjectOperationalStateHandler(
            self.events,
            CheckpointStore(self.database_path),
        )
        self._status = status_reader or RepositoryStatusReader(self.repo_root, clock=clock)
        execution_adapter = RepositoryStatusExecutionAdapter(self._status, self.artifacts)
        outcome_observer = RepositoryStatusOutcomeObserver(self._status, self.artifacts)
        model_adapter, profile = self._model_configuration()
        gateway = ModelGateway(
            (profile,),
            {model_adapter.adapter_id: model_adapter},
            clock=clock,
        )
        recorder = KernelFeedbackRunRecorder(
            self.events,
            self.artifacts,
            self._decision_journal,
            self._execution_journal,
            clock=clock,
        )
        self._workflow = DailyOperatorV2Workflow(
            history=self.events,
            artifacts=self.artifacts,
            state=self._state,
            ingestion=IngestObservationHandler(self.events, clock=clock),
            runs=recorder,
            decisions=RequestDecisionHandler(
                GatewayDecisionAdapter(gateway, clock=clock),
                self._decision_journal,
                clock=clock,
            ),
            execution=AffordanceExecutionHandler(
                {execution_adapter.adapter_id: execution_adapter},
                self._execution_journal,
                clock=clock,
            ),
            execution_journal=self._execution_journal,
            outcome_observer=outcome_observer,
            outcome_evidence=OutcomeEvidenceWriter(self.events, clock=clock),
            evaluator=OutcomeEvaluator(clock=clock),
            telemetry=workflow_telemetry,
        )
        replay_adapter = KernelRunReplayAdapter(
            self.events,
            self.artifacts,
            self._decision_journal,
            self._execution_journal,
        )
        self._replay = ReplayRunHandler(
            replay_adapter,
            replay_adapter,
            replay_adapter,
            replay_adapter,
        )

    @staticmethod
    def default_database_path(repo_root: Path | str) -> Path:
        root = Path(repo_root).resolve()
        return _validated_git_directory(root) / "blackcell" / "kernel.sqlite3"

    def run(
        self,
        *,
        objective: str = DEFAULT_OBJECTIVE,
        approval_granted: bool = False,
        token_budget: int = 2_000,
        character_budget: int = 8_000,
    ) -> CanonicalOperatorRunResult:
        if not objective.strip():
            raise ValueError("operator objective must not be empty")
        if isinstance(token_budget, bool) or not isinstance(token_budget, int) or token_budget < 1:
            raise ValueError("operator token budget must be a positive integer")
        if (
            isinstance(character_budget, bool)
            or not isinstance(character_budget, int)
            or character_budget < 1
        ):
            raise ValueError("operator character budget must be a positive integer")
        request = self._request(
            objective=objective,
            approval_granted=approval_granted,
            token_budget=token_budget,
            character_budget=character_budget,
        )
        self._workflow.run(request)
        report = self.replay(request.run_id)
        return _run_result(report, self.repository_stream_id)

    def current_state(self, *, as_of_time: datetime | None = None) -> OperationalBeliefState:
        return self._state.handle(
            ProjectOperationalState(
                OperationalStateScope("repository", self.repository_stream_id),
                as_of_time=as_of_time or self._now(),
            )
        )

    def context(self, run_id: str | None = None) -> StoredContextFrame:
        resolved_run_id = run_id or self._latest_run_id()
        events = self._run_events(resolved_run_id)
        event = next((item for item in events if item.event_type == CONTEXT_RECORDED), None)
        if event is None:
            raise LookupError(f"run {resolved_run_id!r} has no recorded ContextFrame")
        artifact = _artifact_link(event)
        frame_id = _payload_text(event.payload, "frame_id")
        frame = decode_context_frame(
            self.artifacts.get_bytes(artifact["digest"], verify=True),
            expected_frame_id=frame_id,
        )
        payload = json.loads(serialize_context_frame(frame))
        if not isinstance(payload, Mapping):  # pragma: no cover - codec invariant
            raise TypeError("stored ContextFrame payload must be an object")
        return StoredContextFrame(
            run_id=resolved_run_id,
            frame_id=frame.frame_id,
            artifact_digest=artifact["digest"],
            payload=cast("Mapping[str, Any]", payload),
        )

    def replay(self, run_id: str | None = None) -> RunReplayReport:
        resolved_run_id = run_id or self._latest_run_id()
        self._run_events(resolved_run_id)
        return self._replay.handle(ReplayRun(resolved_run_id))

    def append_correction(
        self,
        correction: ClaimCorrection,
        *,
        actor: str = "human-operator",
        source: str = "human-correction",
    ) -> EventEnvelope:
        if not actor.strip() or not source.strip():
            raise ValueError("correction actor and source must be non-empty")
        replacement = correction.replacement
        evidence = tuple(
            EvidencePointer(
                locator=item.locator or f"blackcell://legacy-evidence/{item.event_id}",
                artifact_id=item.artifact_id,
                digest=item.digest,
            )
            for item in correction.evidence
        ) or (EvidencePointer(locator="blackcell://human-correction"),)
        command = IngestCorrection(
            stream_id=self.repository_stream_id,
            expected_sequence=self.events.current_sequence(self.repository_stream_id),
            actor=actor,
            source=source,
            correlation_id=correction.correction_id,
            corrections=(
                CorrectionInput(
                    correction_id=correction.correction_id,
                    effective_at=correction.effective_at,
                    supersedes_claim_ids=correction.supersedes_claim_ids,
                    replacement=ObservedClaim(
                        claim_id=replacement.claim_id,
                        subject=replacement.subject,
                        predicate=replacement.predicate,
                        value=replacement.value,
                        expires_at=replacement.expires_at,
                    ),
                    reason=correction.reason,
                    evidence=evidence,
                    idempotency_key=correction.correction_id,
                ),
            ),
        )
        return IngestCorrectionHandler(self.events, clock=self._clock).handle(command)[0]

    def _model_configuration(self) -> tuple[ModelAdapter, GatewayProfile]:
        if self._model_route == "recorded":
            adapter = RepositoryRecordedModelAdapter()
            return adapter, GatewayProfile(
                "repository-reason-recorded",
                ModelCapability.REASON,
                REPOSITORY_MODEL_ADAPTER_ID,
                "repository-baseline/v1",
                0,
                True,
                True,
                DataClassification.SECRET,
                32_000,
                4_096,
                0,
            )
        adapter = CodexCliModelAdapter()
        return adapter, GatewayProfile(
            "repository-reason-codex",
            ModelCapability.REASON,
            CODEX_CLI_ADAPTER_ID,
            cast("str", self._codex_model),
            0,
            False,
            False,
            DataClassification.PRIVATE,
            32_000,
            4_096,
            0,
        )

    def _request(
        self,
        *,
        objective: str,
        approval_granted: bool,
        token_budget: int,
        character_budget: int,
    ) -> DailyOperatorV2Request:
        snapshot = self._status.read()
        observed_at = snapshot.observed_at
        run_id = new_event_id()
        observation = ObservationInput(
            observation_id=new_event_id(),
            effective_at=observed_at,
            claims=(
                ObservedClaim(new_event_id(), "repository", "git.valid", snapshot.valid),
                ObservedClaim(new_event_id(), "repository", "git.clean", snapshot.clean),
            ),
            evidence=(EvidencePointer(digest=snapshot.output_digest),),
            idempotency_key=f"repository-status:{run_id}",
        )
        read_only = AffordancePolicy(
            "inspect_repository",
            True,
            evidence_action=True,
        )
        execution = AffordanceDefinition(
            "inspect_repository",
            REPOSITORY_STATUS_ADAPTER_ID,
            SideEffectClass.READ_ONLY,
            10,
        )
        local = self._model_route == "recorded"
        return DailyOperatorV2Request(
            run_id=run_id,
            ingestion=IngestObservation(
                stream_id=self.repository_stream_id,
                expected_sequence=self.events.current_sequence(self.repository_stream_id),
                actor="repository-operator",
                source="repository.git-status/v1",
                correlation_id=run_id,
                observations=(observation,),
                domain="repository",
            ),
            initial_effective_time_cutoff=observed_at,
            signal=DeriveSignalPacket("daily-repository-inspection", observed_at, 300),
            retrieval=RetrieveEvidence(
                objective,
                (
                    EvidenceKey("repository", "git.valid"),
                    EvidenceKey("repository", "git.clean"),
                ),
                8,
            ),
            context=BuildContext(
                f"task:{run_id}",
                objective,
                observed_at,
                character_budget,
            ),
            constraints=SolveConstraints(
                observed_at,
                (
                    ConstraintDefinition(
                        "constraint:repository-valid",
                        "repository must remain a valid Git worktree",
                        "repository",
                        "git.valid",
                        ConstraintOperator.EQUALS,
                        (True,),
                        1.0,
                        300,
                    ),
                ),
            ),
            evaluation_spec=EvaluationSpec(
                "repository-inspection-success",
                objective,
                (
                    EvaluationCriterion(
                        "criterion:repository-valid",
                        "repository",
                        "git.valid",
                        True,
                        1.0,
                        True,
                    ),
                ),
            ),
            gateway_requirements=DecisionRequirements(
                f"decision:{run_id}",
                "node:repository-inspection",
                DecisionCapability.REASON,
                DecisionClassification.PRIVATE,
                DecisionLocality.LOCAL_ONLY if local else DecisionLocality.REMOTE_ALLOWED,
                DecisionBudget(token_budget, min(token_budget, 512), 120_000, 0),
                min(token_budget, 256),
                local,
                observed_at,
            ),
            authorization_affordance=read_only,
            execution_affordance=execution,
            invocation_id=f"invocation:{run_id}",
            idempotency_key=f"execution:{run_id}",
            expected_observer_id=REPOSITORY_OUTCOME_OBSERVER_ID,
            expected_observer_contract_version=REPOSITORY_OUTCOME_CONTRACT_VERSION,
            approval_granted=approval_granted,
        )

    def _latest_run_id(self) -> str:
        events = self.events.read_all(after_position=0)
        for event in reversed(events):
            if (
                event.event_type == RUN_STARTED
                and event.payload.get("observation_stream_id") == self.repository_stream_id
            ):
                return _payload_text(event.payload, "run_id")
        raise LookupError("no operator run exists for this repository")

    def _run_events(self, run_id: str) -> tuple[EventEnvelope, ...]:
        events = self.events.read_stream(run_stream_id(run_id))
        if not events:
            raise LookupError(f"operator run {run_id!r} does not exist")
        start = events[0]
        if (
            start.event_type != RUN_STARTED
            or start.payload.get("observation_stream_id") != self.repository_stream_id
        ):
            raise LookupError(f"operator run {run_id!r} does not belong to this repository")
        return events

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("operator clock must return a timezone-aware timestamp")
        return value.astimezone(UTC)


def _run_result(
    replay: RunReplayReport,
    repository_stream_id: str,
) -> CanonicalOperatorRunResult:
    context = next((item for item in replay.events if item.event_type == CONTEXT_RECORDED), None)
    authorization = next(
        (item for item in replay.events if item.event_type == AUTHORIZATION_DECIDED),
        None,
    )
    execution = next(
        (item for item in replay.events if item.event_type == EXECUTION_RECORDED), None
    )
    evaluation = next(
        (item for item in replay.events if item.event_type == EVALUATION_RECORDED), None
    )
    return CanonicalOperatorRunResult(
        run_id=replay.run_id,
        status=replay.classification.value,
        outcome=replay.outcome,
        workflow_version=replay.protocol_version,
        repository_stream_id=repository_stream_id,
        run_stream_id=replay.run_stream_id,
        context_frame_id=None if context is None else _optional_text(context.payload, "frame_id"),
        authorization_outcome=(
            None if authorization is None else _optional_text(authorization.payload, "outcome")
        ),
        execution_status=None if execution is None else _optional_text(execution.payload, "status"),
        evaluation_verdict=(
            None if evaluation is None else _optional_text(evaluation.payload, "verdict")
        ),
        transition_recorded=any(
            item.event_type == STATE_TRANSITION_RECORDED for item in replay.events
        ),
        run_event_count=replay.event_count,
        artifact_count=len(replay.artifacts),
    )


def _artifact_link(event: EventEnvelope) -> Mapping[str, str]:
    value = event.payload.get("artifact")
    if not isinstance(value, Mapping):
        raise TypeError(f"{event.event_type} artifact link must be an object")
    digest = value.get("digest")
    if not isinstance(digest, str) or not digest.strip():
        raise ValueError(f"{event.event_type} artifact link has no digest")
    return {"digest": digest}


def _payload_text(payload: Mapping[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"operator event field {field!r} must be non-empty text")
    return value


def _optional_text(payload: Mapping[str, object], field: str) -> str | None:
    value = payload.get(field)
    return value if isinstance(value, str) and value.strip() else None


__all__ = ["DEFAULT_CONSTRAINTS", "DEFAULT_OBJECTIVE", "RepositoryOperator"]
