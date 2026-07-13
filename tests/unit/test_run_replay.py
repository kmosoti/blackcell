from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from blackcell.adapters.persistence.sqlite.run_replay import KernelRunReplayAdapter
from blackcell.features.authorize_action import AuthorizationOutcome
from blackcell.features.execute_affordance import ExecutionEvidenceJournal, SideEffectClass
from blackcell.features.replay_run import (
    ReplayClassification,
    ReplayIntegrityStage,
    ReplayProjectionStage,
    ReplayRun,
    ReplayRunHandler,
    ReplayVerificationStatus,
    RunReplayReport,
)
from blackcell.features.request_decision import DecisionEvidenceJournal
from blackcell.kernel import ArtifactStore, EventEnvelope, EventIntegrityError, EventStore
from blackcell.kernel._json import json_digest
from blackcell.workflows.run_protocol import (
    INITIAL_STATE_RECORDED,
    RUN_WORKFLOW_VERSION_V1,
    RUN_WORKFLOW_VERSION_V2,
    RunOutcome,
    run_stream_id,
)
from tests.unit import test_daily_operator_v2 as workflow_v2
from tests.unit import test_run_protocol as protocol_v1
from tests.unit import test_run_records_v2 as protocol_v2


class NoDecisionEvidence:
    def get_request(self, request_id: str):
        del request_id
        return None

    def get_preparation(self, request_id: str):
        del request_id
        return None

    def get_attempt(self, request_id: str):
        del request_id
        return None

    def get_terminal(self, request_id: str):
        del request_id
        return None


class NoExecutionEvidence:
    def get(self, idempotency_key: str):
        del idempotency_key
        return None

    def get_by_authorization(self, decision_id: str):
        del decision_id
        return None

    def get_by_invocation(self, invocation_id: str):
        del invocation_id
        return None

    def get_entry_by_invocation(self, invocation_id: str):
        del invocation_id
        return None

    def get_preparation(self, execution_identity_digest: str):
        del execution_identity_digest
        return None

    def list_entries(self, *, after_position=0, limit=None, status=None):
        del after_position, limit, status
        return ()


class InvalidEnvelopeStore(EventStore):
    def read_stream(
        self,
        stream_id: str,
        *,
        after_sequence: int = 0,
        limit: int | None = None,
    ) -> tuple[EventEnvelope, ...]:
        del stream_id, after_sequence, limit
        raise EventIntegrityError("stored payload hash is invalid")


def test_v1_completed_replay_verifies_artifacts_and_reports_states_not_recorded(
    tmp_path: Path,
) -> None:
    recorder, events, artifacts, frame = protocol_v1._started(tmp_path)
    protocol_v1._record_control(recorder, frame, outcome=AuthorizationOutcome.DENY)
    recorder.complete("run:1", RunOutcome.DENIED)

    report = _replay_v1(events, artifacts, "run:1")

    assert report.protocol_version == RUN_WORKFLOW_VERSION_V1
    assert report.classification is ReplayClassification.COMPLETED
    assert report.outcome == RunOutcome.DENIED.value
    assert report.artifacts and all(item.verified for item in report.artifacts)
    assert tuple(item.status for item in report.projections) == (
        ReplayVerificationStatus.NOT_RECORDED,
        ReplayVerificationStatus.NOT_RECORDED,
    )


def test_v1_failed_and_interrupted_histories_are_distinct(tmp_path: Path) -> None:
    failed_root = tmp_path / "failed"
    failed_root.mkdir()
    recorder, events, artifacts, _ = protocol_v1._started(failed_root)
    recorder.fail("run:1", phase="decision", error_type="RuntimeError")
    failed = _replay_v1(events, artifacts, "run:1")

    interrupted_root = tmp_path / "interrupted"
    interrupted_root.mkdir()
    _, prefix_events, prefix_artifacts, _ = protocol_v1._started(interrupted_root)
    interrupted = _replay_v1(prefix_events, prefix_artifacts, "run:1")

    assert failed.classification is ReplayClassification.FAILED
    assert failed.outcome == RunOutcome.FAILED.value
    assert interrupted.classification is ReplayClassification.INTERRUPTED
    assert interrupted.outcome is None


def test_v2_success_replays_exact_states_and_writes_nothing(tmp_path: Path) -> None:
    scenario = protocol_v2.RunFixture.create(tmp_path)
    scenario.through_success()
    scenario.recorder.complete(protocol_v2.RUN_ID, RunOutcome.EXECUTED)
    decision = protocol_v2._required(scenario.decision, "decision stage")
    before = (
        scenario.events.read_all(),
        scenario.decision_journal.get_request(decision.request_record.request.request_id),
        scenario.decision_journal.get_preparation(decision.request_record.request.request_id),
        scenario.decision_journal.get_attempt(decision.request_record.request.request_id),
        scenario.decision_journal.get_terminal(decision.request_record.request.request_id),
        scenario.execution_journal.list_entries(),
        _blob_snapshot(scenario.artifacts),
    )

    report = _replay(
        scenario.events,
        scenario.artifacts,
        scenario.decision_journal,
        scenario.execution_journal,
        protocol_v2.RUN_ID,
    )

    after = (
        scenario.events.read_all(),
        scenario.decision_journal.get_request(decision.request_record.request.request_id),
        scenario.decision_journal.get_preparation(decision.request_record.request.request_id),
        scenario.decision_journal.get_attempt(decision.request_record.request.request_id),
        scenario.decision_journal.get_terminal(decision.request_record.request.request_id),
        scenario.execution_journal.list_entries(),
        _blob_snapshot(scenario.artifacts),
    )
    assert before == after
    assert report.protocol_version == RUN_WORKFLOW_VERSION_V2
    assert report.classification is ReplayClassification.COMPLETED
    assert report.outcome == RunOutcome.EXECUTED.value
    assert report.artifacts and all(item.verified for item in report.artifacts)
    assert tuple((item.stage, item.status) for item in report.projections) == (
        (ReplayProjectionStage.INITIAL, ReplayVerificationStatus.VERIFIED),
        (ReplayProjectionStage.OUTCOME, ReplayVerificationStatus.VERIFIED),
    )


@pytest.mark.parametrize(
    ("branch", "classification", "outcome", "outcome_state"),
    (
        (
            "deny",
            ReplayClassification.COMPLETED,
            RunOutcome.DENIED,
            ReplayVerificationStatus.NOT_RECORDED,
        ),
        (
            "approval",
            ReplayClassification.COMPLETED,
            RunOutcome.APPROVAL_REQUIRED,
            ReplayVerificationStatus.NOT_RECORDED,
        ),
        (
            "unknown",
            ReplayClassification.COMPLETED,
            RunOutcome.REQUIRES_RECONCILIATION,
            ReplayVerificationStatus.NOT_RECORDED,
        ),
        (
            "execution-failed",
            ReplayClassification.COMPLETED,
            RunOutcome.EXECUTION_FAILED,
            ReplayVerificationStatus.VERIFIED,
        ),
        (
            "model-failed",
            ReplayClassification.FAILED,
            RunOutcome.FAILED,
            ReplayVerificationStatus.NOT_RECORDED,
        ),
    ),
)
def test_v2_terminal_branches_preserve_history_classification_and_material_outcome(
    tmp_path: Path,
    branch: str,
    classification: ReplayClassification,
    outcome: RunOutcome,
    outcome_state: ReplayVerificationStatus,
) -> None:
    fixture = workflow_v2.WorkflowFixture.create(
        tmp_path,
        gateway=(workflow_v2.Gateway(fail_route=True) if branch == "model-failed" else None),
        adapter=(
            workflow_v2.ExecutionAdapter(unknown=True)
            if branch == "unknown"
            else workflow_v2.ExecutionAdapter(succeeds=False)
            if branch == "execution-failed"
            else None
        ),
    )
    request = workflow_v2._request(
        initial_status="blocked" if branch == "deny" else "ready",
        side_effect_class=(
            SideEffectClass.REVERSIBLE if branch == "approval" else SideEffectClass.READ_ONLY
        ),
    )
    fixture.workflow.run(request)

    report = _replay(
        fixture.events,
        fixture.artifacts,
        fixture.decision_journal,
        fixture.execution_journal,
        workflow_v2.RUN_ID,
    )

    assert report.classification is classification
    assert report.outcome == outcome.value
    assert report.projections[0].status is ReplayVerificationStatus.VERIFIED
    assert report.projections[1].status is outcome_state


def test_v2_valid_opening_prefix_is_interrupted_without_live_work(tmp_path: Path) -> None:
    scenario = protocol_v2.RunFixture.create(tmp_path)
    scenario.recorder.open(scenario.request)

    report = _replay(
        scenario.events,
        scenario.artifacts,
        scenario.decision_journal,
        scenario.execution_journal,
        protocol_v2.RUN_ID,
    )

    assert report.classification is ReplayClassification.INTERRUPTED
    assert report.outcome is None
    assert tuple(item.status for item in report.projections) == (
        ReplayVerificationStatus.NOT_RECORDED,
        ReplayVerificationStatus.NOT_RECORDED,
    )


@pytest.mark.parametrize("prefix", ("model-attempt", "execution-journal"))
def test_v2_uncertain_durable_prefix_remains_interrupted(
    tmp_path: Path,
    prefix: str,
) -> None:
    scenario = protocol_v2.RunFixture.create(tmp_path)
    scenario.through_context()
    if prefix == "model-attempt":
        scenario.journal_decision(record_prefix_before_terminal=True)
    else:
        scenario.through_decision()
        scenario.journal_execution()

    report = _replay(
        scenario.events,
        scenario.artifacts,
        scenario.decision_journal,
        scenario.execution_journal,
        protocol_v2.RUN_ID,
    )

    assert report.classification is ReplayClassification.INTERRUPTED
    assert report.outcome is None


def test_protocol_corruption_is_reported_without_reentering_a_writer(tmp_path: Path) -> None:
    scenario = protocol_v2.RunFixture.create(tmp_path)
    scenario.through_success()
    scenario.recorder.complete(protocol_v2.RUN_ID, RunOutcome.EXECUTED)
    tampered = protocol_v2.TamperingStore(scenario.database)
    tampered.mutation = lambda events: (
        events[0],
        replace(events[1], causation_id="event:wrong"),
        *events[2:],
    )

    report = _replay(
        tampered,
        scenario.artifacts,
        scenario.decision_journal,
        scenario.execution_journal,
        protocol_v2.RUN_ID,
    )

    assert report.classification is ReplayClassification.CORRUPT
    assert report.finding is not None
    assert report.finding.stage is ReplayIntegrityStage.PROTOCOL
    assert report.finding.code == "run-protocol-invalid"


def test_invalid_stored_event_envelope_is_classified_before_protocol_decode(
    tmp_path: Path,
) -> None:
    database = tmp_path / "kernel.sqlite3"
    events = InvalidEnvelopeStore(database)
    artifacts = ArtifactStore(tmp_path / "artifacts", database_path=database)

    report = _replay(
        events,
        artifacts,
        NoDecisionEvidence(),
        NoExecutionEvidence(),
        "run:corrupt",
    )

    assert report.classification is ReplayClassification.CORRUPT
    assert report.events == ()
    assert report.finding is not None
    assert report.finding.stage is ReplayIntegrityStage.PROTOCOL
    assert report.finding.code == "event-envelope-invalid"


def test_artifact_corruption_is_reported_as_recorded_evidence_failure(tmp_path: Path) -> None:
    scenario = protocol_v2.RunFixture.create(tmp_path)
    scenario.through_success()
    scenario.recorder.complete(protocol_v2.RUN_ID, RunOutcome.EXECUTED)
    context = next(
        event
        for event in scenario.events.read_stream(run_stream_id(protocol_v2.RUN_ID))
        if event.event_type == "run.context-recorded"
    )
    link = context.payload["artifact"]
    assert isinstance(link, Mapping)
    digest = cast("Mapping[str, object]", link).get("digest")
    assert isinstance(digest, str)
    path = scenario.artifacts.path_for(digest)
    path.write_bytes(path.read_bytes() + b"corrupt")

    report = _replay(
        scenario.events,
        scenario.artifacts,
        scenario.decision_journal,
        scenario.execution_journal,
        protocol_v2.RUN_ID,
    )

    assert report.classification is ReplayClassification.CORRUPT
    assert report.finding is not None
    assert report.finding.stage is ReplayIntegrityStage.ARTIFACT
    assert report.finding.code == "recorded-evidence-invalid"


def test_projection_corruption_is_reported_separately(tmp_path: Path) -> None:
    scenario = protocol_v2.RunFixture.create(tmp_path)
    scenario.through_success()
    scenario.recorder.complete(protocol_v2.RUN_ID, RunOutcome.EXECUTED)
    tampered = protocol_v2.TamperingStore(scenario.database)

    def change_cutoff(events: tuple[EventEnvelope, ...]) -> tuple[EventEnvelope, ...]:
        changed: list[EventEnvelope] = []
        for event in events:
            if event.event_type == INITIAL_STATE_RECORDED:
                payload = dict(event.payload)
                cutoff = payload["cutoff_global_position"]
                assert isinstance(cutoff, int) and not isinstance(cutoff, bool)
                payload["cutoff_global_position"] = cutoff + 1
                event = replace(event, payload=payload, payload_hash=json_digest(payload))
            changed.append(event)
        return tuple(changed)

    tampered.mutation = change_cutoff
    report = _replay(
        tampered,
        scenario.artifacts,
        scenario.decision_journal,
        scenario.execution_journal,
        protocol_v2.RUN_ID,
    )

    assert report.classification is ReplayClassification.CORRUPT
    assert report.finding is not None
    assert report.finding.stage is ReplayIntegrityStage.PROJECTION
    assert report.finding.code == "projection-evidence-invalid"


def test_missing_journal_ownership_is_corrupt_recorded_evidence(tmp_path: Path) -> None:
    scenario = protocol_v2.RunFixture.create(tmp_path)
    scenario.through_success()
    scenario.recorder.complete(protocol_v2.RUN_ID, RunOutcome.EXECUTED)

    report = _replay(
        scenario.events,
        scenario.artifacts,
        NoDecisionEvidence(),
        scenario.execution_journal,
        protocol_v2.RUN_ID,
    )

    assert report.classification is ReplayClassification.CORRUPT
    assert report.finding is not None
    assert report.finding.stage is ReplayIntegrityStage.ARTIFACT
    assert report.finding.code == "recorded-evidence-invalid"


def _replay_v1(
    events: EventStore,
    artifacts: ArtifactStore,
    run_id: str,
) -> RunReplayReport:
    return _replay(
        events,
        artifacts,
        NoDecisionEvidence(),
        NoExecutionEvidence(),
        run_id,
    )


def _replay(
    events: EventStore,
    artifacts: ArtifactStore,
    decisions: DecisionEvidenceJournal,
    executions: ExecutionEvidenceJournal,
    run_id: str,
) -> RunReplayReport:
    adapter = KernelRunReplayAdapter(events, artifacts, decisions, executions)
    return ReplayRunHandler(adapter, adapter, adapter, adapter).handle(ReplayRun(run_id))


def _blob_snapshot(artifacts: ArtifactStore) -> tuple[tuple[str, bytes], ...]:
    return tuple(
        (str(path.relative_to(artifacts.blob_root)), path.read_bytes())
        for path in sorted(artifacts.blob_root.rglob("*"))
        if path.is_file()
    )
