from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from blackcell.adapters.models import (
    CODEX_CLI_ADAPTER_ID,
    CodexCliModelAdapter,
    GatewayDecisionAdapter,
)
from blackcell.adapters.models.codex_cli import (
    CODEX_CLI_DEFAULT_INPUT_TOKEN_BUDGET,
    estimate_codex_cli_input_tokens,
)
from blackcell.adapters.persistence.sqlite import (
    KernelRunReplayAdapter,
    SQLiteDecisionAttemptJournal,
    SQLiteExecutionJournal,
)
from blackcell.adapters.persistence.sqlite.run_records_v2 import KernelFeedbackRunRecorder
from blackcell.adapters.repository import (
    REPOSITORY_MODEL_ADAPTER_ID,
    REPOSITORY_OUTCOME_CONTRACT_VERSION,
    REPOSITORY_OUTCOME_OBSERVER_ID,
    REPOSITORY_STATUS_ADAPTER_ID,
    RepositoryRecordedModelAdapter,
    RepositoryStatusExecutionAdapter,
    RepositoryStatusOutcomeObserver,
    RepositoryStatusReader,
    validated_git_directory,
)
from blackcell.features.evaluate_outcome import OutcomeEvaluator
from blackcell.features.execute_affordance import AffordanceExecutionHandler
from blackcell.features.ingest_observation import (
    IngestCorrectionHandler,
    IngestObservationHandler,
)
from blackcell.features.project_operational_state import ProjectOperationalStateHandler
from blackcell.features.replay_run import ReplayRunHandler
from blackcell.features.request_decision import RequestDecisionHandler
from blackcell.gateway import (
    DataClassification,
    GatewayProfile,
    ModelCapability,
    ModelGateway,
)
from blackcell.gateway.ports import ModelAdapter
from blackcell.kernel import ArtifactStore, CheckpointStore, EventStore
from blackcell.operator.facade import (
    DEFAULT_RECORDED_TOKEN_BUDGET,
    RepositoryOperator,
    RepositoryOperatorConfiguration,
)
from blackcell.operator.status import RepositoryStatusPort
from blackcell.workflows import DailyOperatorV2Workflow, WorkflowTelemetry
from blackcell.workflows.outcome_evidence import OutcomeEvidenceWriter

Clock = Callable[[], datetime]
_RECORDED_MODEL_INPUT_TOKENS = 256


@dataclass(frozen=True, slots=True)
class RepositoryRuntimeComponents:
    """Explicit capabilities assembled for one repository runtime."""

    operator: RepositoryOperator
    events: EventStore
    artifacts: ArtifactStore
    database_path: Path
    artifact_root: Path


def default_repository_database_path(repo_root: Path | str) -> Path:
    root = Path(repo_root).resolve()
    return validated_git_directory(root) / "blackcell" / "kernel.sqlite3"


def compose_repository_runtime(
    repo_root: Path | str,
    *,
    database_path: Path | str | None = None,
    artifact_root: Path | str | None = None,
    model: Literal["recorded", "codex"] = "recorded",
    codex_model: str | None = None,
    status_reader: RepositoryStatusPort | None = None,
    clock: Clock = lambda: datetime.now(UTC),
    workflow_telemetry: WorkflowTelemetry | None = None,
    artifact_max_total_bytes: int | None = None,
) -> RepositoryRuntimeComponents:
    """Assemble concrete repository runtime dependencies at the bootstrap edge."""

    _validate_model_route(model, codex_model)
    root = Path(repo_root).resolve()
    database = (
        Path(database_path) if database_path is not None else default_repository_database_path(root)
    )
    artifacts_root = (
        Path(artifact_root) if artifact_root is not None else database.parent / "artifacts"
    )

    events = EventStore(database)
    artifacts = ArtifactStore(
        artifacts_root,
        database_path=database,
        max_total_bytes=artifact_max_total_bytes,
    )
    decision_journal = SQLiteDecisionAttemptJournal(
        artifacts_root,
        database_path=database,
        artifact_max_total_bytes=artifact_max_total_bytes,
    )
    execution_journal = SQLiteExecutionJournal(
        artifacts_root,
        database_path=database,
        artifact_max_total_bytes=artifact_max_total_bytes,
    )
    state = ProjectOperationalStateHandler(events, CheckpointStore(database))
    repository_status = status_reader or RepositoryStatusReader(root, clock=clock)
    execution_adapter = RepositoryStatusExecutionAdapter(repository_status, artifacts)
    outcome_observer = RepositoryStatusOutcomeObserver(repository_status, artifacts)
    model_adapter, profile = _model_configuration(model, codex_model)
    gateway = ModelGateway(
        (profile,),
        {model_adapter.adapter_id: model_adapter},
        clock=clock,
    )
    recorder = KernelFeedbackRunRecorder(
        events,
        artifacts,
        decision_journal,
        execution_journal,
        clock=clock,
    )
    workflow = DailyOperatorV2Workflow(
        history=events,
        artifacts=artifacts,
        state=state,
        ingestion=IngestObservationHandler(events, clock=clock),
        runs=recorder,
        decisions=RequestDecisionHandler(
            GatewayDecisionAdapter(gateway, clock=clock),
            decision_journal,
            clock=clock,
        ),
        execution=AffordanceExecutionHandler(
            {execution_adapter.adapter_id: execution_adapter},
            execution_journal,
            clock=clock,
        ),
        execution_journal=execution_journal,
        outcome_observer=outcome_observer,
        outcome_evidence=OutcomeEvidenceWriter(events, clock=clock),
        evaluator=OutcomeEvaluator(clock=clock),
        telemetry=workflow_telemetry,
    )
    replay_adapter = KernelRunReplayAdapter(
        events,
        artifacts,
        decision_journal,
        execution_journal,
    )
    replay = ReplayRunHandler(
        replay_adapter,
        replay_adapter,
        replay_adapter,
        replay_adapter,
    )
    operator = RepositoryOperator(
        root,
        database_path=database,
        artifact_root=artifacts_root,
        events=events,
        artifacts=artifacts,
        status_reader=repository_status,
        state=state,
        correction=IngestCorrectionHandler(events, clock=clock),
        workflow=workflow,
        replay=replay,
        configuration=RepositoryOperatorConfiguration(
            model_local=model == "recorded",
            default_token_budget=(
                DEFAULT_RECORDED_TOKEN_BUDGET
                if model == "recorded"
                else CODEX_CLI_DEFAULT_INPUT_TOKEN_BUDGET
            ),
            input_token_estimator=(
                _recorded_model_input_tokens if model == "recorded" else _codex_model_input_tokens
            ),
            execution_adapter_id=REPOSITORY_STATUS_ADAPTER_ID,
            outcome_observer_id=REPOSITORY_OUTCOME_OBSERVER_ID,
            outcome_observer_contract_version=REPOSITORY_OUTCOME_CONTRACT_VERSION,
        ),
        clock=clock,
    )
    return RepositoryRuntimeComponents(
        operator=operator,
        events=events,
        artifacts=artifacts,
        database_path=database,
        artifact_root=artifacts_root,
    )


def _validate_model_route(model: str, codex_model: str | None) -> None:
    if model not in {"recorded", "codex"}:
        raise ValueError(f"unsupported repository operator model route: {model!r}")
    if model == "codex" and (codex_model is None or not codex_model.strip()):
        raise ValueError("--codex-model is required when --model=codex")
    if model == "recorded" and codex_model is not None:
        raise ValueError("--codex-model is only valid when --model=codex")


def _recorded_model_input_tokens(objective: str, context_character_budget: int) -> int:
    del objective, context_character_budget
    return _RECORDED_MODEL_INPUT_TOKENS


def _codex_model_input_tokens(objective: str, context_character_budget: int) -> int:
    return estimate_codex_cli_input_tokens(
        objective=objective,
        context_character_budget=context_character_budget,
    )


def _model_configuration(
    model: Literal["recorded", "codex"],
    codex_model: str | None,
) -> tuple[ModelAdapter, GatewayProfile]:
    if model == "recorded":
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
        cast("str", codex_model),
        0,
        False,
        False,
        DataClassification.PRIVATE,
        32_000,
        4_096,
        0,
    )


__all__ = [
    "RepositoryRuntimeComponents",
    "compose_repository_runtime",
    "default_repository_database_path",
]
