from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from blackcell.adapters.persistence.sqlite.context_frames import ArtifactContextFrameStore
from blackcell.adapters.persistence.sqlite.run_records import KernelRunRecorder
from blackcell.features.authorize_action import (
    ActionProposal,
    AuthorizationDecision,
    AuthorizationFinding,
    AuthorizationOutcome,
)
from blackcell.features.build_context import ContextFrame
from blackcell.features.execute_affordance import (
    ExecutionResult,
    ExecutionStatus,
    serialize_execution_result,
)
from blackcell.features.solve_constraints import (
    ConstraintEvaluation,
    ConstraintOutcome,
    ConstraintProof,
)
from blackcell.kernel import ArtifactStore, EventEnvelope, EventStore
from blackcell.kernel._json import json_digest
from blackcell.workflows.run_protocol import (
    AUTHORIZATION_DECIDED,
    CONSTRAINTS_EVALUATED,
    CONTEXT_RECORDED,
    EXECUTION_RECORDED,
    PROPOSAL_RECORDED,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_STARTED,
    TRACE_RECORDED,
    RunAlreadyExists,
    RunIdentityConflict,
    RunInterrupted,
    RunOutcome,
    RunProtocolIntegrityError,
    RunStart,
    run_stream_id,
)

NOW = datetime(2026, 7, 11, 14, tzinfo=UTC)
DIGEST = f"sha256:{'1' * 64}"


class _RacingStartStore(EventStore):
    raced = False

    def append(self, event: EventEnvelope, *, expected_sequence: int) -> EventEnvelope:
        if event.event_type == RUN_STARTED and not self.raced:
            self.raced = True
            EventStore(self.path).append(event, expected_sequence=0)
        return super().append(event, expected_sequence=expected_sequence)


class _FailingTerminalStore(EventStore):
    def append(self, event: EventEnvelope, *, expected_sequence: int) -> EventEnvelope:
        if event.event_type == RUN_FAILED:
            raise RuntimeError("terminal storage failure")
        return super().append(event, expected_sequence=expected_sequence)


def test_recorder_persists_verified_executed_chain_and_causal_trace(tmp_path: Path) -> None:
    recorder, events, artifacts, frame = _started(tmp_path)
    proposal, _, decision = _record_control(recorder, frame)
    result = _execution(proposal, decision)
    execution_ref = artifacts.put_bytes(
        serialize_execution_result(result).encode("utf-8"),
        media_type="application/vnd.blackcell.execution-result+json",
        encoding="utf-8",
    )
    assert execution_ref.digest == result.result_id

    recorder.record_execution("run:1", result)
    terminal = recorder.complete("run:1", RunOutcome.EXECUTED)

    stored = events.read_stream(run_stream_id("run:1"))
    assert tuple(item.event_type for item in stored) == (
        RUN_STARTED,
        CONTEXT_RECORDED,
        PROPOSAL_RECORDED,
        CONSTRAINTS_EVALUATED,
        AUTHORIZATION_DECIDED,
        EXECUTION_RECORDED,
        TRACE_RECORDED,
        RUN_COMPLETED,
    )
    assert all(item.correlation_id == "run:1" for item in stored)
    assert stored[0].causation_id is None
    assert tuple(item.causation_id for item in stored[1:]) == tuple(
        item.event_id for item in stored[:-1]
    )
    assert terminal.terminal_event == stored[-1]
    trace_digest = terminal.trace_event.payload["artifact"]["digest"]
    trace = artifacts.get_json(str(trace_digest))
    assert trace["schema_version"] == "run-trace/v1"
    assert trace["outcome"] == "executed"
    assert len(trace["entries"]) == 6
    assert trace["entries"][-1]["artifact_digest"] == result.result_id
    for event in stored[1:-1]:
        link = event.payload["artifact"]
        assert artifacts.verify(str(link["digest"]))


def test_denial_is_a_completed_safety_outcome_without_execution(tmp_path: Path) -> None:
    recorder, events, _, frame = _started(tmp_path)
    _record_control(recorder, frame, outcome=AuthorizationOutcome.DENY)

    recorder.complete("run:1", RunOutcome.DENIED)

    stored = events.read_stream(run_stream_id("run:1"))
    assert EXECUTION_RECORDED not in {item.event_type for item in stored}
    assert stored[-1].event_type == RUN_COMPLETED
    assert stored[-1].payload["outcome"] == "denied"


def test_failure_records_trace_then_terminal_without_raw_error_message(tmp_path: Path) -> None:
    recorder, events, artifacts, _ = _started(tmp_path)

    terminal = recorder.fail("run:1", phase="decision", error_type="RuntimeError")

    stored = events.read_stream(run_stream_id("run:1"))
    assert tuple(item.event_type for item in stored) == (
        RUN_STARTED,
        CONTEXT_RECORDED,
        TRACE_RECORDED,
        RUN_FAILED,
    )
    assert terminal.terminal_event.payload["phase"] == "decision"
    assert "message" not in terminal.terminal_event.payload
    failure_digest = terminal.terminal_event.payload["artifact"]["digest"]
    assert artifacts.get_json(str(failure_digest)) == {
        "schema_version": "run-failure/v1",
        "run_id": "run:1",
        "phase": "decision",
        "error_type": "RuntimeError",
    }


def test_start_is_create_only_and_request_digest_detects_identity_reuse(
    tmp_path: Path,
) -> None:
    recorder, _, _, frame = _started(tmp_path)
    exact = _start()

    with pytest.raises(RunInterrupted):
        recorder.start(exact)
    with pytest.raises(RunIdentityConflict):
        recorder.start(
            RunStart(
                exact.run_id,
                json_digest({"request": "changed"}),
                exact.actor,
                exact.task_id,
                exact.objective,
                exact.domain,
                exact.observation_stream_id,
            )
        )

    _record_control(recorder, frame, outcome=AuthorizationOutcome.DENY)
    recorder.complete("run:1", RunOutcome.DENIED)
    with pytest.raises(RunAlreadyExists):
        recorder.start(exact)
    with pytest.raises(RunAlreadyExists):
        recorder.complete("run:1", RunOutcome.DENIED)


def test_artifact_must_exist_before_context_event_and_corruption_blocks_progress(
    tmp_path: Path,
) -> None:
    database = tmp_path / "kernel.sqlite3"
    artifacts = ArtifactStore(tmp_path / "artifacts", database_path=database)
    events = EventStore(database)
    recorder = KernelRunRecorder(events, artifacts, clock=lambda: NOW)
    recorder.start(_start())
    frame = _frame()

    with pytest.raises(RunProtocolIntegrityError, match="missing or corrupt"):
        recorder.record_context("run:1", frame)
    assert tuple(item.event_type for item in events.read_stream(run_stream_id("run:1"))) == (
        RUN_STARTED,
    )

    _persist_frame(tmp_path, database, frame)
    recorder.record_context("run:1", frame)
    proposal = _proposal(frame)
    proposal_event = recorder.record_proposal("run:1", proposal)
    proposal_path = artifacts.path_for(str(proposal_event.payload["artifact"]["digest"]))
    proposal_path.write_bytes(proposal_path.read_bytes() + b"\n")

    with pytest.raises(RunProtocolIntegrityError, match="missing or corrupt"):
        recorder.record_constraints("run:1", _evaluation(frame))
    assert events.current_sequence(run_stream_id("run:1")) == 3


def test_context_scope_must_match_the_run_observation_scope(tmp_path: Path) -> None:
    database = tmp_path / "kernel.sqlite3"
    artifacts = ArtifactStore(tmp_path / "artifacts", database_path=database)
    events = EventStore(database)
    recorder = KernelRunRecorder(events, artifacts, clock=lambda: NOW)
    recorder.start(_start())
    frame = replace(_frame(), state_stream_id="observations:other")
    _persist_frame(tmp_path, database, frame)

    with pytest.raises(RunProtocolIntegrityError, match="state stream"):
        recorder.record_context("run:1", frame)

    assert events.current_sequence(run_stream_id("run:1")) == 1


def test_completion_outcome_must_match_authorization_and_execution(tmp_path: Path) -> None:
    recorder, _, _, frame = _started(tmp_path)
    _record_control(recorder, frame, outcome=AuthorizationOutcome.DENY)

    with pytest.raises(RunProtocolIntegrityError, match="does not match"):
        recorder.complete("run:1", RunOutcome.EXECUTED)


def test_concurrent_start_is_reclassified_without_entering_a_live_phase(
    tmp_path: Path,
) -> None:
    database = tmp_path / "kernel.sqlite3"
    artifacts = ArtifactStore(tmp_path / "artifacts", database_path=database)
    events = _RacingStartStore(database)
    recorder = KernelRunRecorder(events, artifacts, clock=lambda: NOW)

    with pytest.raises(RunInterrupted):
        recorder.start(_start())

    assert tuple(item.event_type for item in events.read_stream(run_stream_id("run:1"))) == (
        RUN_STARTED,
    )


def test_completion_fails_closed_for_a_failure_trace_without_authorization(
    tmp_path: Path,
) -> None:
    database = tmp_path / "kernel.sqlite3"
    artifacts = ArtifactStore(tmp_path / "artifacts", database_path=database)
    events = _FailingTerminalStore(database)
    recorder = KernelRunRecorder(events, artifacts, clock=lambda: NOW)
    recorder.start(_start())

    with pytest.raises(RuntimeError, match="terminal storage failure"):
        recorder.fail("run:1", phase="decision", error_type="RuntimeError")
    assert events.read_stream(run_stream_id("run:1"))[-1].event_type == TRACE_RECORDED

    with pytest.raises(RunProtocolIntegrityError, match="without authorization"):
        recorder.complete("run:1", RunOutcome.DENIED)


def _started(
    tmp_path: Path,
) -> tuple[KernelRunRecorder, EventStore, ArtifactStore, ContextFrame]:
    database = tmp_path / "kernel.sqlite3"
    artifact_root = tmp_path / "artifacts"
    artifacts = ArtifactStore(artifact_root, database_path=database)
    events = EventStore(database)
    recorder = KernelRunRecorder(events, artifacts, clock=lambda: NOW)
    recorder.start(_start())
    frame = _frame()
    _persist_frame(tmp_path, database, frame)
    recorder.record_context("run:1", frame)
    return recorder, events, artifacts, frame


def _persist_frame(tmp_path: Path, database: Path, frame: ContextFrame) -> None:
    with ArtifactContextFrameStore(
        tmp_path / "artifacts", database_path=database
    ) as context_frames:
        assert context_frames.put(frame) == frame


def _start() -> RunStart:
    return RunStart(
        "run:1",
        json_digest({"request": "one"}),
        "operator",
        "task:daily",
        "inspect status",
        "repository",
        "observations:daily",
    )


def _frame() -> ContextFrame:
    return ContextFrame(
        task_id="task:daily",
        objective="inspect status",
        generated_at=NOW,
        source_packet_id="packet:1",
        source_packet_purpose="daily",
        source_selection_id="selection:1",
        state_domain="repository",
        state_stream_id="observations:daily",
        state_global_position=0,
        state_stream_position=0,
        source_claim_identities=(),
        evidence=(),
        provenance_event_ids=(),
        omissions=(),
        model_payload_characters=0,
    )


def _record_control(
    recorder: KernelRunRecorder,
    frame: ContextFrame,
    *,
    outcome: AuthorizationOutcome = AuthorizationOutcome.ALLOW,
) -> tuple[ActionProposal, ConstraintEvaluation, AuthorizationDecision]:
    proposal = _proposal(frame)
    evaluation = _evaluation(frame)
    decision = _decision(proposal, evaluation, outcome)
    recorder.record_proposal("run:1", proposal)
    recorder.record_constraints("run:1", evaluation)
    recorder.record_authorization("run:1", decision)
    return proposal, evaluation, decision


def _proposal(frame: ContextFrame) -> ActionProposal:
    return ActionProposal(
        "proposal:1",
        frame.frame_id,
        "inspect",
        (),
        "inspect the repository",
    )


def _evaluation(frame: ContextFrame) -> ConstraintEvaluation:
    proof = ConstraintProof(
        "constraint:1",
        DIGEST,
        ConstraintOutcome.SATISFIED,
        "satisfied",
        "fixture proof",
        ("event:1",),
        NOW,
    )
    return ConstraintEvaluation(frame.frame_id, (proof,), NOW)


def _decision(
    proposal: ActionProposal,
    evaluation: ConstraintEvaluation,
    outcome: AuthorizationOutcome,
) -> AuthorizationDecision:
    return AuthorizationDecision(
        proposal_id=proposal.proposal_id,
        proposal_digest=proposal.proposal_digest,
        context_frame_id=proposal.context_frame_id,
        constraint_evaluation_id=evaluation.evaluation_id,
        authorized_action_digest=proposal.action_digest,
        affordance_policy_digest=DIGEST,
        authorized_read_only=True,
        authorized_external=False,
        authorized_mutates_state=False,
        outcome=outcome,
        findings=(AuthorizationFinding(outcome, outcome.value, "fixture"),),
        evaluated_at=NOW,
        approval_granted=False,
    )


def _execution(
    proposal: ActionProposal,
    decision: AuthorizationDecision,
) -> ExecutionResult:
    return ExecutionResult(
        invocation_id="invocation:1",
        proposal_id=proposal.proposal_id,
        authorization_decision_id=decision.decision_id,
        affordance=proposal.affordance,
        adapter_id="fixture",
        idempotency_key="execution:1",
        authorized_action_digest=proposal.action_digest,
        execution_identity_digest=DIGEST,
        status=ExecutionStatus.SUCCEEDED,
        started_at=NOW,
        completed_at=NOW,
        output_digest=DIGEST,
        observed_effects=(),
        error_code=None,
        reconciled=False,
    )
