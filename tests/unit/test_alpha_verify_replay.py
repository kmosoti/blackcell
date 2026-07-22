from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import cast

from blackcell.bootstrap.alpha_verify_worker import AlphaVerificationSchedulerPort
from blackcell.kernel import ArtifactRef, ArtifactStore, EventEnvelope, EventStore
from blackcell.orchestration.alpha_verify import AlphaVerificationStatus
from blackcell.orchestration.alpha_verify_replay import (
    AlphaVerificationReplayFindingCode,
    AlphaVerificationReplayLifecycle,
    replay_alpha_verification,
)
from tests.unit.test_alpha_verify_worker import (
    NOW,
    CrashingVerifier,
    FailingCompletionScheduler,
    StatusVerifier,
    _completed_clear_review,
    _worker,
)


class MetadataMismatchReader:
    def __init__(self, delegate: ArtifactStore) -> None:
        self.delegate = delegate
        self.database_path = delegate.database_path

    def stat(self, digest: str | ArtifactRef) -> ArtifactRef:
        return replace(self.delegate.stat(digest), media_type="application/json")

    def get_bytes(self, digest: str | ArtifactRef, *, verify: bool = True) -> bytes:
        return self.delegate.get_bytes(digest, verify=verify)


class MissingSourceEventReader:
    def __init__(self, delegate: EventStore, missing_event_id: str) -> None:
        self.delegate = delegate
        self.missing_event_id = missing_event_id

    def read_stream(self, stream_id: str) -> tuple[EventEnvelope, ...]:
        return self.delegate.read_stream(stream_id)

    def get(self, event_id: str) -> EventEnvelope | None:
        if event_id == self.missing_event_id:
            return None
        return self.delegate.get(event_id)


def test_verification_replay_validates_completed_report_and_restart(tmp_path: Path) -> None:
    source, scheduler, artifacts = _completed_clear_review(tmp_path)
    result = _worker(
        source,
        scheduler,
        artifacts,
        StatusVerifier(AlphaVerificationStatus.PASS),
    ).run_once()
    assert result.status == "verification-completed"

    first = replay_alpha_verification(source.events, artifacts, run_id="run-1")
    reopened_events = EventStore(source.events.path)
    second = replay_alpha_verification(
        reopened_events,
        ArtifactStore(artifacts.root, database_path=reopened_events.path),
        run_id="run-1",
    )
    api_replay = source.execution.replay_run("run-1")

    assert first == second
    assert first.lifecycle_status is AlphaVerificationReplayLifecycle.COMPLETED
    assert first.verdict is AlphaVerificationStatus.PASS
    assert first.failure_code is None
    assert first.artifact_integrity.value == "verified"
    assert first.finding_code is None
    assert first.report_artifact_digest == result.report_artifact_digest
    assert first.report_size_bytes is not None
    assert first.report_media_type == "application/vnd.blackcell.alpha-verification-report+json"
    assert first.report_encoding == "utf-8"
    assert first.matrix_digest is not None
    assert first.processed_events == 2
    assert api_replay.verification.lifecycle_status == "completed"
    assert api_replay.verification.verdict == "pass"
    assert api_replay.verification.artifact_integrity == "verified"
    assert api_replay.verification.evidence_digest == first.evidence_digest


def test_verification_replay_distinguishes_not_started_incomplete_and_verifier_error(
    tmp_path: Path,
) -> None:
    incomplete_root = tmp_path / "incomplete"
    incomplete_root.mkdir()
    source, scheduler, artifacts = _completed_clear_review(incomplete_root)
    not_started = replay_alpha_verification(source.events, artifacts, run_id="run-1")
    candidate = source.verification_candidate("run-1")
    scheduler.claim(
        candidate,
        worker_id="verifier-1",
        lease_expires_at=NOW + timedelta(minutes=5),
        claimed_at=NOW,
    )
    claimed = replay_alpha_verification(source.events, artifacts, run_id="run-1")
    scheduler.reconcile(principal_id="verification-supervisor")
    requeued = replay_alpha_verification(source.events, artifacts, run_id="run-1")

    assert not_started.lifecycle_status is AlphaVerificationReplayLifecycle.NOT_STARTED
    assert not_started.artifact_integrity.value == "not-applicable"
    assert claimed.lifecycle_status is AlphaVerificationReplayLifecycle.CLAIMED
    assert claimed.artifact_integrity.value == "not-applicable"
    assert requeued.lifecycle_status is AlphaVerificationReplayLifecycle.REQUEUED
    assert requeued.artifact_integrity.value == "not-applicable"

    verifier_root = tmp_path / "verifier-error"
    verifier_root.mkdir()
    source, scheduler, artifacts = _completed_clear_review(verifier_root)
    result = _worker(source, scheduler, artifacts, CrashingVerifier()).run_once()
    verifier_error = replay_alpha_verification(source.events, artifacts, run_id="run-1")
    assert result.failure_code == "alpha-verifier-failed"
    assert verifier_error.lifecycle_status is AlphaVerificationReplayLifecycle.VERIFIER_ERROR
    assert verifier_error.verdict is None
    assert verifier_error.failure_code == "alpha-verifier-failed"
    assert verifier_error.report_artifact_digest is None
    assert verifier_error.artifact_integrity.value == "not-applicable"

    persistence_root = tmp_path / "persistence-error"
    persistence_root.mkdir()
    source, scheduler, artifacts = _completed_clear_review(persistence_root)
    result = _worker(
        source,
        cast(
            "AlphaVerificationSchedulerPort",
            FailingCompletionScheduler(scheduler),
        ),
        artifacts,
        StatusVerifier(AlphaVerificationStatus.PASS),
    ).run_once()
    persistence_error = replay_alpha_verification(source.events, artifacts, run_id="run-1")
    assert result.failure_code == "alpha-verification-persistence-failed"
    assert persistence_error.lifecycle_status is AlphaVerificationReplayLifecycle.VERIFIER_ERROR
    assert persistence_error.verdict is None
    assert persistence_error.failure_code == "alpha-verification-persistence-failed"
    state = scheduler.inspect("run-1")
    assert state is not None
    assert persistence_error.report_artifact_digest == state.result_artifact_digest
    assert persistence_error.artifact_integrity.value == "verified"


def test_verification_replay_rejects_tamper_metadata_and_source_binding_drift(
    tmp_path: Path,
) -> None:
    tamper_root = tmp_path / "tamper"
    tamper_root.mkdir()
    source, scheduler, artifacts = _completed_clear_review(tamper_root)
    result = _worker(
        source,
        scheduler,
        artifacts,
        StatusVerifier(AlphaVerificationStatus.PASS),
    ).run_once()
    assert result.report_artifact_digest is not None
    artifacts.path_for(result.report_artifact_digest).write_bytes(b"tampered")

    tampered = replay_alpha_verification(source.events, artifacts, run_id="run-1")
    assert tampered.artifact_integrity.value == "failed"
    assert tampered.finding_code is AlphaVerificationReplayFindingCode.REPORT_INTEGRITY_FAILED

    binding_root = tmp_path / "binding"
    binding_root.mkdir()
    source, scheduler, artifacts = _completed_clear_review(binding_root)
    _worker(
        source,
        scheduler,
        artifacts,
        StatusVerifier(AlphaVerificationStatus.PASS),
    ).run_once()
    metadata = replay_alpha_verification(
        source.events,
        MetadataMismatchReader(artifacts),
        run_id="run-1",
    )
    unavailable = replay_alpha_verification(source.events, None, run_id="run-1")
    state = scheduler.inspect("run-1")
    assert state is not None
    source_drift = replay_alpha_verification(
        MissingSourceEventReader(source.events, state.lease.run_event_id),
        artifacts,
        run_id="run-1",
    )

    assert metadata.artifact_integrity.value == "failed"
    assert metadata.finding_code is AlphaVerificationReplayFindingCode.REPORT_METADATA_MISMATCH
    assert unavailable.artifact_integrity.value == "inconclusive"
    assert unavailable.finding_code is AlphaVerificationReplayFindingCode.ARTIFACT_STORE_UNAVAILABLE
    assert source_drift.artifact_integrity.value == "failed"
    assert source_drift.finding_code is AlphaVerificationReplayFindingCode.SOURCE_BINDING_MISMATCH
