from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from blackcell.adapters.persistence.sqlite.context_frames import ArtifactContextFrameStore
from blackcell.adapters.persistence.sqlite.run_records import KernelRunRecorder
from blackcell.features.authorize_action import (
    ActionProposal,
    AuthorizationDecision,
    AuthorizationFinding,
    AuthorizationOutcome,
)
from blackcell.features.authorize_action.artifacts import encode_action_proposal
from blackcell.features.build_context import ContextFrame, serialize_context_frame
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
from blackcell.kernel import (
    ArtifactStore,
    ConcurrencyError,
    EventEnvelope,
    EventStore,
)
from blackcell.kernel._json import canonical_json_bytes, json_digest
from blackcell.workflows.run_protocol import (
    AUTHORIZATION_DECIDED,
    CONTEXT_RECORDED,
    RUN_COMPLETED,
    RUN_EVENT_SCHEMA_VERSION,
    RUN_STARTED,
    RUN_TRACE_MEDIA_TYPE,
    TRACE_RECORDED,
    RunAlreadyExists,
    RunArtifactLink,
    RunIdentityConflict,
    RunInterrupted,
    RunOutcome,
    RunProtocolIntegrityError,
    RunStart,
    run_stream_id,
)

NOW = datetime(2026, 7, 11, 14, tzinfo=UTC)
DIGEST = f"sha256:{'1' * 64}"
CONTEXT_MEDIA_TYPE = "application/vnd.blackcell.context-frame+json"
EXECUTION_MEDIA_TYPE = "application/vnd.blackcell.execution-result+json"

EventMutation = Callable[[tuple[EventEnvelope, ...]], tuple[EventEnvelope, ...]]


class _TamperingStore(EventStore):
    mutation: EventMutation | None = None

    def read_stream(
        self,
        stream_id: str,
        *,
        after_sequence: int = 0,
        limit: int | None = None,
    ) -> tuple[EventEnvelope, ...]:
        events = super().read_stream(
            stream_id,
            after_sequence=after_sequence,
            limit=limit,
        )
        if self.mutation is None or not events:
            return events
        return self.mutation(events)


class _RacingStageStore(EventStore):
    raced = False

    def append(self, event: EventEnvelope, *, expected_sequence: int) -> EventEnvelope:
        if event.event_type == CONTEXT_RECORDED and not self.raced:
            self.raced = True
            EventStore(self.path).append(event, expected_sequence=expected_sequence)
        return super().append(event, expected_sequence=expected_sequence)


class _VanishingStartRaceStore(EventStore):
    def append(self, event: EventEnvelope, *, expected_sequence: int) -> EventEnvelope:
        if event.event_type == RUN_STARTED:
            raise ConcurrencyError(event.stream_id, expected_sequence, expected_sequence + 1)
        return super().append(event, expected_sequence=expected_sequence)


class _FailOnceCompletedStore(EventStore):
    failed = False

    def append(self, event: EventEnvelope, *, expected_sequence: int) -> EventEnvelope:
        if event.event_type == RUN_COMPLETED and not self.failed:
            self.failed = True
            raise RuntimeError("simulated terminal commit failure")
        return super().append(event, expected_sequence=expected_sequence)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("run_id", " ", "run_id"),
        ("actor", "", "actor"),
        ("task_id", "\t", "task_id"),
        ("objective", " ", "objective"),
        ("domain", "", "domain"),
        ("observation_stream_id", " ", "observation_stream_id"),
        ("request_digest", "sha256:not-a-digest", "request_digest"),
        ("request_digest", f"sha256:{'g' * 64}", "request_digest"),
    ),
)
def test_run_start_rejects_ambiguous_identity_fields(
    field: str,
    value: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        replace(_start(), **{field: value})


def test_run_artifact_link_and_stream_identity_are_strict() -> None:
    valid = RunArtifactLink(
        DIGEST,
        "application/json",
        "utf-8",
        7,
        "fixture/v1",
        "fixture:1",
    )
    assert valid.as_payload() == {
        "digest": DIGEST,
        "media_type": "application/json",
        "encoding": "utf-8",
        "size_bytes": 7,
        "schema_version": "fixture/v1",
        "logical_id": "fixture:1",
    }

    with pytest.raises(ValueError, match="digest"):
        replace(valid, digest="sha256:bad")
    with pytest.raises(ValueError, match="media_type"):
        replace(valid, media_type=" ")
    with pytest.raises(ValueError, match="encoding"):
        replace(valid, encoding="")
    with pytest.raises(ValueError, match="non-negative"):
        replace(valid, size_bytes=-1)
    with pytest.raises(ValueError, match="run_id"):
        run_stream_id(" ")


def test_recorder_requires_one_shared_kernel_database(tmp_path: Path) -> None:
    events = EventStore(tmp_path / "events.sqlite3")
    artifacts = ArtifactStore(
        tmp_path / "artifacts",
        database_path=tmp_path / "artifacts.sqlite3",
    )

    with pytest.raises(ValueError, match="same kernel database"):
        KernelRunRecorder(events, artifacts)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("task_id", "task:other", "task"),
        ("objective", "different objective", "objective"),
        ("state_domain", "other-domain", "domain"),
    ),
)
def test_context_must_belong_to_the_started_request(
    tmp_path: Path,
    field: str,
    value: str,
    message: str,
) -> None:
    recorder, events, _, _ = _recorder(tmp_path)
    recorder.start(_start())

    with pytest.raises(RunProtocolIntegrityError, match=message):
        recorder.record_context("run:1", replace(_frame(), **{field: value}))

    assert events.current_sequence(run_stream_id("run:1")) == 1


def test_control_artifacts_must_follow_the_context_and_each_other(tmp_path: Path) -> None:
    recorder, events, artifacts, database = _recorder(tmp_path)
    frame = _start_with_context(recorder, artifacts, database)
    proposal = _proposal(frame)

    with pytest.raises(RunProtocolIntegrityError, match="different ContextFrame"):
        recorder.record_proposal("run:1", replace(proposal, context_frame_id=DIGEST))
    recorder.record_proposal("run:1", proposal)

    evaluation = _evaluation(frame)
    with pytest.raises(RunProtocolIntegrityError, match="different ContextFrame"):
        recorder.record_constraints("run:1", replace(evaluation, context_frame_id=DIGEST))
    recorder.record_constraints("run:1", evaluation)

    decision = _decision(proposal, evaluation, AuthorizationOutcome.ALLOW)
    with pytest.raises(RunProtocolIntegrityError, match="different proposal"):
        recorder.record_authorization("run:1", replace(decision, proposal_id="proposal:other"))
    with pytest.raises(RunProtocolIntegrityError, match="constraint evaluation"):
        recorder.record_authorization(
            "run:1",
            replace(decision, constraint_evaluation_id=DIGEST),
        )
    recorder.record_authorization("run:1", decision)

    assert events.current_sequence(run_stream_id("run:1")) == 5


def test_stage_and_terminal_entry_points_fail_closed(tmp_path: Path) -> None:
    recorder, events, _, _ = _recorder(tmp_path)
    frame = _frame()

    with pytest.raises(RunProtocolIntegrityError, match="has not started"):
        recorder.record_context("run:1", frame)

    recorder.start(_start())
    with pytest.raises(RunProtocolIntegrityError, match=r"requires 'run\.context-recorded'"):
        recorder.record_proposal("run:1", _proposal(frame))
    with pytest.raises(RunProtocolIntegrityError, match="before authorization"):
        recorder.complete("run:1", RunOutcome.DENIED)
    with pytest.raises(ValueError, match=r"use fail\(\)"):
        recorder.complete("run:1", RunOutcome.FAILED)
    with pytest.raises(ValueError, match="must not be empty"):
        recorder.fail("run:1", phase=" ", error_type="RuntimeError")
    with pytest.raises(ValueError, match="must not be empty"):
        recorder.fail("run:1", phase="context", error_type="")

    assert events.current_sequence(run_stream_id("run:1")) == 1


def test_execution_requires_allow_and_the_exact_authorization(tmp_path: Path) -> None:
    denied_path = tmp_path / "denied"
    denied, _, denied_artifacts, denied_database = _recorder(denied_path)
    denied_frame = _start_with_context(denied, denied_artifacts, denied_database)
    denied_proposal, denied_decision = _record_control(
        denied,
        denied_frame,
        AuthorizationOutcome.DENY,
    )
    denied_result = _execution(denied_proposal, denied_decision, ExecutionStatus.SUCCEEDED)

    with pytest.raises(RunProtocolIntegrityError, match="only an allowed"):
        denied.record_execution("run:1", denied_result)

    allowed_path = tmp_path / "allowed"
    allowed, events, artifacts, database = _recorder(allowed_path)
    frame = _start_with_context(allowed, artifacts, database)
    proposal, decision = _record_control(allowed, frame, AuthorizationOutcome.ALLOW)
    result = _execution(proposal, decision, ExecutionStatus.SUCCEEDED)
    _persist_execution(artifacts, result)

    with pytest.raises(RunProtocolIntegrityError, match="different authorization"):
        allowed.record_execution(
            "run:1",
            replace(result, authorization_decision_id="authorization:other"),
        )
    allowed.record_execution("run:1", result)

    assert events.current_sequence(run_stream_id("run:1")) == 6


@pytest.mark.parametrize(
    ("authorization", "status", "outcome"),
    (
        (AuthorizationOutcome.REQUIRE_APPROVAL, None, RunOutcome.APPROVAL_REQUIRED),
        (AuthorizationOutcome.ALLOW, ExecutionStatus.FAILED, RunOutcome.EXECUTION_FAILED),
        (
            AuthorizationOutcome.ALLOW,
            ExecutionStatus.UNKNOWN,
            RunOutcome.REQUIRES_RECONCILIATION,
        ),
    ),
)
def test_fresh_recorder_reconstructs_and_completes_each_bounded_outcome(
    tmp_path: Path,
    authorization: AuthorizationOutcome,
    status: ExecutionStatus | None,
    outcome: RunOutcome,
) -> None:
    recorder, events, artifacts, database = _recorder(tmp_path)
    frame = _start_with_context(recorder, artifacts, database)
    proposal, decision = _record_control(recorder, frame, authorization)
    if status is not None:
        result = _execution(proposal, decision, status)
        _persist_execution(artifacts, result)
        recorder.record_execution("run:1", result)

    restarted = KernelRunRecorder(events, artifacts, clock=lambda: NOW)
    terminal = restarted.complete("run:1", outcome)

    stored = events.read_stream(run_stream_id("run:1"))
    assert terminal.terminal_event == stored[-1]
    assert stored[-1].payload["outcome"] == outcome.value
    assert stored[-2].event_type == TRACE_RECORDED
    with pytest.raises(RunAlreadyExists):
        KernelRunRecorder(events, artifacts, clock=lambda: NOW).start(_start())


def test_nonterminal_redelivery_and_identity_collision_are_classified_after_restart(
    tmp_path: Path,
) -> None:
    recorder, events, artifacts, _ = _recorder(tmp_path)
    recorder.start(_start())
    restarted = KernelRunRecorder(events, artifacts, clock=lambda: NOW)

    with pytest.raises(RunInterrupted, match="explicit recovery"):
        restarted.start(_start())
    with pytest.raises(RunIdentityConflict, match="different request digest"):
        restarted.start(replace(_start(), request_digest=json_digest({"request": "two"})))


def test_stage_race_commits_once_and_surfaces_optimistic_concurrency(tmp_path: Path) -> None:
    database = tmp_path / "kernel.sqlite3"
    events = _RacingStageStore(database)
    artifacts = ArtifactStore(tmp_path / "artifacts", database_path=database)
    recorder = KernelRunRecorder(events, artifacts, clock=lambda: NOW)
    recorder.start(_start())
    frame = _frame()
    _persist_frame(artifacts, database, frame)

    with pytest.raises(ConcurrencyError):
        recorder.record_context("run:1", frame)

    stored = events.read_stream(run_stream_id("run:1"))
    assert tuple(event.event_type for event in stored) == (RUN_STARTED, CONTEXT_RECORDED)


def test_concurrent_start_that_cannot_be_read_requires_explicit_recovery(
    tmp_path: Path,
) -> None:
    database = tmp_path / "kernel.sqlite3"
    events = _VanishingStartRaceStore(database)
    artifacts = ArtifactStore(tmp_path / "artifacts", database_path=database)
    recorder = KernelRunRecorder(events, artifacts, clock=lambda: NOW)

    with pytest.raises(RunInterrupted, match="not readable"):
        recorder.start(_start())

    assert events.read_stream(run_stream_id("run:1")) == ()


def test_content_address_metadata_collisions_do_not_gain_run_ownership(
    tmp_path: Path,
) -> None:
    context_path = tmp_path / "context"
    context_recorder, context_events, context_artifacts, _ = _recorder(context_path)
    context_recorder.start(_start())
    frame = _frame()
    context_ref = context_artifacts.put_bytes(
        serialize_context_frame(frame).encode("utf-8"),
        media_type="application/json",
        encoding="utf-8",
    )
    assert context_ref.digest == frame.frame_id

    with pytest.raises(RunProtocolIntegrityError, match="incompatible type or encoding"):
        context_recorder.record_context("run:1", frame)
    assert context_events.current_sequence(run_stream_id("run:1")) == 1

    proposal_path = tmp_path / "proposal"
    proposal_recorder, proposal_events, proposal_artifacts, database = _recorder(proposal_path)
    proposal_frame = _start_with_context(proposal_recorder, proposal_artifacts, database)
    proposal = _proposal(proposal_frame)
    proposal_artifacts.put_bytes(
        encode_action_proposal(proposal),
        media_type="application/json",
        encoding="utf-8",
    )

    with pytest.raises(RunProtocolIntegrityError, match="incompatible metadata"):
        proposal_recorder.record_proposal("run:1", proposal)
    assert proposal_events.current_sequence(run_stream_id("run:1")) == 2


@pytest.mark.parametrize(
    ("artifact_change", "message"),
    (
        (None, "lacks an artifact link"),
        ({"encoding": 7}, "encoding"),
        ({"size_bytes": True}, "size"),
        ({"media_type": "application/octet-stream"}, "metadata does not match"),
    ),
)
def test_reconstruction_rejects_malformed_artifact_links(
    tmp_path: Path,
    artifact_change: dict[str, object] | None,
    message: str,
) -> None:
    recorder, events, artifacts, database = _recorder(tmp_path, _TamperingStore)
    _start_with_context(recorder, artifacts, database)
    assert isinstance(events, _TamperingStore)

    def mutate(stored: tuple[EventEnvelope, ...]) -> tuple[EventEnvelope, ...]:
        context = stored[1]
        payload: dict[str, object] = dict(context.payload)
        if artifact_change is None:
            payload.pop("artifact")
        else:
            artifact = payload["artifact"]
            assert isinstance(artifact, Mapping)
            artifact_values = cast("Mapping[str, object]", artifact)
            payload["artifact"] = {**artifact_values, **artifact_change}
        return _replace_at(stored, 1, payload=payload)

    events.mutation = mutate
    with pytest.raises(RunProtocolIntegrityError, match=message):
        recorder.start(_start())

    events.mutation = None
    assert events.current_sequence(run_stream_id("run:1")) == 2


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (
            lambda stored: _replace_at(stored, 1, stream_id="daily-operator-run:other"),
            "different stream",
        ),
        (lambda stored: _replace_at(stored, 1, stream_sequence=3), "not contiguous"),
        (lambda stored: _replace_at(stored, 1, correlation_id="run:other"), "correlation"),
        (
            lambda stored: _replace_at(
                stored,
                1,
                schema_version=RUN_EVENT_SCHEMA_VERSION + 1,
            ),
            "schema version",
        ),
        (
            lambda stored: _replace_payload(stored, 1, run_id="run:other"),
            "payload does not match",
        ),
        (lambda stored: _replace_at(stored, 1, causation_id="event:other"), "causation"),
        (
            lambda stored: _replace_at(stored, 1, event_type=AUTHORIZATION_DECIDED),
            "out of order",
        ),
        (
            lambda stored: _replace_payload(stored, 0, workflow="unsupported"),
            "workflow contract",
        ),
    ),
)
def test_reconstruction_rejects_corrupt_event_grammar(
    tmp_path: Path,
    mutation: EventMutation,
    message: str,
) -> None:
    recorder, events, artifacts, database = _recorder(tmp_path, _TamperingStore)
    _start_with_context(recorder, artifacts, database)
    assert isinstance(events, _TamperingStore)
    events.mutation = mutation

    with pytest.raises(RunProtocolIntegrityError, match=message):
        recorder.start(_start())


def test_reconstruction_rejects_terminal_and_trace_corruption(tmp_path: Path) -> None:
    recorder, events, artifacts, database = _recorder(tmp_path, _TamperingStore)
    frame = _start_with_context(recorder, artifacts, database)
    _record_control(recorder, frame, AuthorizationOutcome.DENY)
    recorder.complete("run:1", RunOutcome.DENIED)
    assert isinstance(events, _TamperingStore)

    events.mutation = lambda stored: _replace_payload(
        stored,
        len(stored) - 1,
        outcome=RunOutcome.APPROVAL_REQUIRED.value,
    )
    with pytest.raises(RunProtocolIntegrityError, match="terminal outcome"):
        recorder.start(_start())

    events.mutation = lambda stored: _replace_payload(
        stored,
        len(stored) - 1,
        trace_artifact_digest=DIGEST,
    )
    with pytest.raises(RunProtocolIntegrityError, match="trace reference"):
        recorder.start(_start())

    events.mutation = _append_after_terminal
    with pytest.raises(RunProtocolIntegrityError, match="requires the final trace"):
        recorder.start(_start())

    events.mutation = lambda stored: _replace_at(
        stored,
        len(stored) - 2,
        event_type=RUN_COMPLETED,
    )
    with pytest.raises(RunProtocolIntegrityError, match="requires the final trace"):
        recorder.start(_start())

    unrelated = artifacts.put_bytes(
        canonical_json_bytes({"not": "this run prefix"}),
        media_type=RUN_TRACE_MEDIA_TYPE,
        encoding="utf-8",
    )

    def replace_trace_manifest(stored: tuple[EventEnvelope, ...]) -> tuple[EventEnvelope, ...]:
        index = len(stored) - 2
        trace = stored[index]
        payload: dict[str, object] = dict(trace.payload)
        artifact = payload["artifact"]
        assert isinstance(artifact, Mapping)
        artifact_values = cast("Mapping[str, object]", artifact)
        payload["artifact"] = {
            **artifact_values,
            "digest": unrelated.digest,
            "size_bytes": unrelated.size_bytes,
        }
        return _replace_at(stored, index, payload=payload)

    events.mutation = replace_trace_manifest
    with pytest.raises(RunProtocolIntegrityError, match="does not match its event prefix"):
        recorder.start(_start())

    events.mutation = lambda stored: _replace_payload(
        stored,
        len(stored) - 2,
        outcome="unrecognized-outcome",
    )
    with pytest.raises(RunProtocolIntegrityError, match="not recognized"):
        recorder.start(_start())


def test_terminal_commit_retry_reuses_trace_and_rejects_cross_outcome_recovery(
    tmp_path: Path,
) -> None:
    database = tmp_path / "kernel.sqlite3"
    events = _FailOnceCompletedStore(database)
    artifacts = ArtifactStore(tmp_path / "artifacts", database_path=database)
    recorder = KernelRunRecorder(events, artifacts, clock=lambda: NOW)
    frame = _start_with_context(recorder, artifacts, database)
    _record_control(recorder, frame, AuthorizationOutcome.DENY)

    with pytest.raises(RuntimeError, match="terminal commit failure"):
        recorder.complete("run:1", RunOutcome.DENIED)
    assert events.read_stream(run_stream_id("run:1"))[-1].event_type == TRACE_RECORDED

    with pytest.raises(RunProtocolIntegrityError, match="different outcome"):
        recorder.fail("run:1", phase="terminal", error_type="RuntimeError")

    terminal = KernelRunRecorder(events, artifacts, clock=lambda: NOW).complete(
        "run:1",
        RunOutcome.DENIED,
    )
    stored = events.read_stream(run_stream_id("run:1"))
    assert tuple(event.event_type for event in stored).count(TRACE_RECORDED) == 1
    assert terminal.trace_event == stored[-2]
    assert terminal.terminal_event == stored[-1]


def test_terminal_run_rejects_failure_and_material_reentry(tmp_path: Path) -> None:
    recorder, _, artifacts, database = _recorder(tmp_path)
    frame = _start_with_context(recorder, artifacts, database)
    _record_control(recorder, frame, AuthorizationOutcome.DENY)
    recorder.complete("run:1", RunOutcome.DENIED)

    with pytest.raises(RunAlreadyExists, match="terminal"):
        recorder.fail("run:1", phase="complete", error_type="RuntimeError")
    with pytest.raises(RunAlreadyExists, match="terminal"):
        recorder.record_context("run:1", frame)


def _recorder(
    tmp_path: Path,
    event_store_type: type[EventStore] = EventStore,
) -> tuple[KernelRunRecorder, EventStore, ArtifactStore, Path]:
    database = tmp_path / "kernel.sqlite3"
    events = event_store_type(database)
    artifacts = ArtifactStore(tmp_path / "artifacts", database_path=database)
    return KernelRunRecorder(events, artifacts, clock=lambda: NOW), events, artifacts, database


def _start_with_context(
    recorder: KernelRunRecorder,
    artifacts: ArtifactStore,
    database: Path,
) -> ContextFrame:
    recorder.start(_start())
    frame = _frame()
    _persist_frame(artifacts, database, frame)
    recorder.record_context("run:1", frame)
    return frame


def _persist_frame(artifacts: ArtifactStore, database: Path, frame: ContextFrame) -> None:
    with ArtifactContextFrameStore(artifacts.root, database_path=database) as frames:
        assert frames.put(frame) == frame


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


def _record_control(
    recorder: KernelRunRecorder,
    frame: ContextFrame,
    outcome: AuthorizationOutcome,
) -> tuple[ActionProposal, AuthorizationDecision]:
    proposal = _proposal(frame)
    evaluation = _evaluation(frame)
    decision = _decision(proposal, evaluation, outcome)
    recorder.record_proposal("run:1", proposal)
    recorder.record_constraints("run:1", evaluation)
    recorder.record_authorization("run:1", decision)
    return proposal, decision


def _execution(
    proposal: ActionProposal,
    decision: AuthorizationDecision,
    status: ExecutionStatus,
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
        status=status,
        started_at=NOW,
        completed_at=NOW,
        output_digest=None if status is ExecutionStatus.UNKNOWN else DIGEST,
        observed_effects=(),
        error_code="fixture-error" if status is ExecutionStatus.FAILED else None,
        reconciled=status is ExecutionStatus.UNKNOWN,
    )


def _persist_execution(artifacts: ArtifactStore, result: ExecutionResult) -> None:
    reference = artifacts.put_bytes(
        serialize_execution_result(result).encode("utf-8"),
        media_type=EXECUTION_MEDIA_TYPE,
        encoding="utf-8",
    )
    assert reference.digest == result.result_id


def _replace_at(
    events: tuple[EventEnvelope, ...],
    index: int,
    **changes: object,
) -> tuple[EventEnvelope, ...]:
    event = events[index]
    payload = changes.pop("payload", event.payload)
    assert isinstance(payload, Mapping)
    changed = replace(
        event,
        payload=cast("Mapping[str, object]", payload),
        payload_hash=json_digest(payload),
        **changes,
    )
    return (*events[:index], changed, *events[index + 1 :])


def _replace_payload(
    events: tuple[EventEnvelope, ...],
    index: int,
    **changes: object,
) -> tuple[EventEnvelope, ...]:
    payload = {**events[index].payload, **changes}
    return _replace_at(events, index, payload=payload)


def _append_after_terminal(events: tuple[EventEnvelope, ...]) -> tuple[EventEnvelope, ...]:
    terminal = events[-1]
    extra = EventEnvelope.create(
        stream_id=terminal.stream_id,
        stream_sequence=terminal.stream_sequence + 1,
        event_type=AUTHORIZATION_DECIDED,
        schema_version=RUN_EVENT_SCHEMA_VERSION,
        actor=terminal.actor,
        source=terminal.source,
        payload={"run_id": "run:1"},
        recorded_at=NOW,
        effective_at=NOW,
        correlation_id="run:1",
        causation_id=terminal.event_id,
    )
    return (*events, extra)
