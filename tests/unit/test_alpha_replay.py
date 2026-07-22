from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import cast

from blackcell.adapters.execution.text_changes import TextChangeExecutor
from blackcell.adapters.execution.worktree import GitWorktreeLifecycle
from blackcell.bootstrap.alpha_runtime import AlphaRuntimeApiService
from blackcell.bootstrap.alpha_worker import AlphaRuntimeWorker, AlphaWorkerPolicy
from blackcell.kernel import ArtifactNotFoundError, ArtifactRef, ArtifactStore, EventStore
from blackcell.kernel._json import canonical_json_bytes
from blackcell.orchestration.alpha_artifacts import ALPHA_OUTCOME_MEDIA_TYPE
from blackcell.orchestration.alpha_replay import (
    AlphaArtifactReplayStatus,
    AlphaReplayCheckExpectation,
    AlphaReplayFindingCode,
    AlphaReplayNodeExpectation,
    verify_alpha_run_artifacts,
)
from tests.unit.test_alpha_worker import (
    RecordingAcceptance,
    ReplacingProvider,
    _git,
    _git_text,
    _runtime,
    _submit,
)


def test_completed_worker_run_replays_complete_artifact_chain_live_free(
    tmp_path: Path,
) -> None:
    runtime, events, artifacts, repository, isolation, _, provider, acceptance = _completed_writer(
        tmp_path
    )
    provider_calls = len(provider.calls)
    acceptance_calls = len(acceptance.commands)

    first = runtime.replay_run("run-1")
    reopened = AlphaRuntimeApiService(
        EventStore(events.path),
        repository,
        isolation_root=isolation,
        artifacts=ArtifactStore(artifacts.root, database_path=events.path),
    )
    second = reopened.replay_run("run-1")

    assert first == second
    assert first.schema_version == "alpha-replay/v2"
    assert first.artifact_integrity == "verified"
    assert first.verification.lifecycle_status == "not-started"
    assert first.verification.artifact_integrity == "not-applicable"
    assert first.verification.verdict is None
    assert first.findings == ()
    assert all(artifact.verified for artifact in first.artifacts)
    assert {artifact.role for artifact in first.artifacts} == {
        "outcome",
        "context",
        "proposal",
        "provider",
        "effect",
        "check-command",
        "check-result",
        "check-stdout",
        "check-stderr",
    }
    assert len(events) == first.processed_events
    assert len(provider.calls) == provider_calls == 1
    assert len(acceptance.commands) == acceptance_calls == 1


def test_replay_reports_missing_linked_artifact(tmp_path: Path) -> None:
    _, events, artifacts, repository, isolation, outcome, _, _ = _completed_writer(tmp_path)
    context_digest = _linked_digest(outcome, "context_artifact")
    reader = _MissingArtifactReader(artifacts, context_digest)
    replay = AlphaRuntimeApiService(
        EventStore(events.path),
        repository,
        isolation_root=isolation,
        artifacts=reader,
    ).replay_run("run-1")

    assert replay.artifact_integrity == "failed"
    assert replay.findings[0].code == "alpha-replay-artifact-missing"
    assert replay.findings[0].role == "context"
    assert replay.findings[0].artifact_digest == context_digest


def test_replay_reports_changed_artifact_bytes(tmp_path: Path) -> None:
    runtime, _, artifacts, _, _, outcome, _, _ = _completed_writer(tmp_path)
    context_digest = _linked_digest(outcome, "context_artifact")
    artifacts.path_for(context_digest).write_bytes(b"changed-after-recording")

    replay = runtime.replay_run("run-1")

    assert replay.artifact_integrity == "failed"
    assert replay.findings[0].code == "alpha-replay-artifact-integrity-failed"
    assert replay.findings[0].role == "context"
    assert replay.findings[0].artifact_digest == context_digest


def test_replay_reports_unavailable_store_and_absent_terminal_outcome_as_inconclusive(
    tmp_path: Path,
) -> None:
    _, events, artifacts, repository, isolation, _, _, _ = _completed_writer(tmp_path)
    unavailable = AlphaRuntimeApiService(
        EventStore(events.path),
        repository,
        isolation_root=isolation,
    ).replay_run("run-1")

    absent = verify_alpha_run_artifacts(
        artifacts,
        run_id="run-absent",
        nodes=(_absent_failed_expectation(),),
    )

    assert unavailable.artifact_integrity == "inconclusive"
    assert unavailable.findings[0].code == "alpha-replay-artifact-store-unavailable"
    assert absent.status is AlphaArtifactReplayStatus.INCONCLUSIVE
    assert absent.findings[0].code is AlphaReplayFindingCode.OUTCOME_REFERENCE_ABSENT


def test_replay_rejects_noncanonical_or_source_unbound_outcome(tmp_path: Path) -> None:
    _, _, artifacts, _, _, outcome, _, _ = _completed_writer(tmp_path)
    original = _expectation(outcome, result_digest="sha256:" + "0" * 64)

    noncanonical_ref = artifacts.put_bytes(
        json.dumps(outcome, indent=2, sort_keys=True).encode("utf-8"),
        media_type=ALPHA_OUTCOME_MEDIA_TYPE,
        encoding="utf-8",
    )
    noncanonical = verify_alpha_run_artifacts(
        artifacts,
        run_id="run-1",
        nodes=(replace(original, result_digest=noncanonical_ref.digest),),
    )

    unbound_payload = dict(outcome)
    unbound_payload["node_id"] = "other-node"
    unbound_ref = artifacts.put_bytes(
        canonical_json_bytes(unbound_payload),
        media_type=ALPHA_OUTCOME_MEDIA_TYPE,
        encoding="utf-8",
    )
    unbound = verify_alpha_run_artifacts(
        artifacts,
        run_id="run-1",
        nodes=(replace(original, result_digest=unbound_ref.digest),),
    )

    assert noncanonical.status is AlphaArtifactReplayStatus.FAILED
    assert noncanonical.findings[0].code is AlphaReplayFindingCode.ARTIFACT_NONCANONICAL
    assert unbound.status is AlphaArtifactReplayStatus.FAILED
    assert unbound.findings[0].code is AlphaReplayFindingCode.ARTIFACT_BINDING_MISMATCH


def _completed_writer(
    tmp_path: Path,
    *,
    source_content: str | None = None,
) -> tuple[
    AlphaRuntimeApiService,
    EventStore,
    ArtifactStore,
    Path,
    Path,
    dict[str, object],
    ReplacingProvider,
    RecordingAcceptance,
]:
    runtime, events, artifacts, repository, isolation, base_commit = _runtime(tmp_path)
    if source_content is not None:
        (repository / "src" / "value.py").write_text(source_content)
        _git(repository, "add", "src/value.py")
        _git(repository, "commit", "-m", "expand review evidence")
        base_commit = _git_text(repository, "rev-parse", "HEAD")
    artifacts.put_text("", media_type="text/plain")
    _submit(runtime, repository, base_commit, writer_only=True)
    lifecycle = GitWorktreeLifecycle()
    provider = ReplacingProvider()
    acceptance = RecordingAcceptance(lifecycle)
    worker = AlphaRuntimeWorker(
        runtime=runtime,
        artifacts=artifacts,
        provider=provider,
        change_executor=TextChangeExecutor(lifecycle),
        acceptance=acceptance,
        worktrees=lifecycle,
        policy=AlphaWorkerPolicy("worker-1"),
    )
    result = worker.run_once()
    assert result.status == "node-succeeded"
    assert result.outcome_artifact_digest is not None
    assert len(provider.calls) == 1
    outcome = artifacts.get_json(result.outcome_artifact_digest)
    assert isinstance(outcome, dict)
    return (
        runtime,
        events,
        artifacts,
        repository,
        isolation,
        cast("dict[str, object]", outcome),
        provider,
        acceptance,
    )


def _linked_digest(outcome: Mapping[str, object], field: str) -> str:
    link = outcome[field]
    assert isinstance(link, dict)
    digest = link.get("digest")
    assert isinstance(digest, str)
    return digest


def _expectation(
    outcome: Mapping[str, object],
    *,
    result_digest: str,
) -> AlphaReplayNodeExpectation:
    return AlphaReplayNodeExpectation(
        node_id="write",
        objective="Update the bounded value.",
        constraints=("Only change the admitted file.",),
        depends_on=(),
        repository_write=True,
        effects=("repository-read", "repository-write", "process"),
        allowed_paths=("src/value.py",),
        max_changed_paths=1,
        checks=(
            AlphaReplayCheckExpectation(
                "write-check",
                ("python", "-m", "compileall", "src"),
                0,
                30,
            ),
        ),
        status="succeeded",
        attempt=cast("int", outcome["attempt"]),
        fencing_token=cast("int", outcome["fencing_token"]),
        lease_digest=cast("str", outcome["lease_digest"]),
        worktree_spec_digest=cast("str", outcome["worktree_spec_digest"]),
        base_commit=cast("str", outcome["base_commit"]),
        head_commit=cast("str", outcome["head_commit"]),
        failure_code=None,
        result_digest=result_digest,
        provider_context_digest=_linked_digest(outcome, "context_artifact"),
    )


def _absent_failed_expectation() -> AlphaReplayNodeExpectation:
    return AlphaReplayNodeExpectation(
        node_id="failed",
        objective="Fail before artifact persistence.",
        constraints=(),
        depends_on=(),
        repository_write=False,
        effects=("repository-read", "process"),
        allowed_paths=(),
        max_changed_paths=0,
        checks=(),
        status="failed",
        attempt=1,
        fencing_token=1,
        lease_digest="sha256:" + "a" * 64,
        worktree_spec_digest="sha256:" + "b" * 64,
        base_commit="c" * 40,
        head_commit=None,
        failure_code="alpha-worker-artifact-failed",
        result_digest=None,
        provider_context_digest=None,
    )


class _MissingArtifactReader:
    def __init__(self, delegate: ArtifactStore, missing_digest: str) -> None:
        self._delegate = delegate
        self._missing_digest = missing_digest
        self.database_path = delegate.database_path

    def stat(self, digest: str | ArtifactRef) -> ArtifactRef:
        value = digest.digest if isinstance(digest, ArtifactRef) else digest
        if value == self._missing_digest:
            raise ArtifactNotFoundError("intentionally missing test artifact")
        return self._delegate.stat(digest)

    def get_bytes(self, digest: str | ArtifactRef, *, verify: bool = True) -> bytes:
        return self._delegate.get_bytes(digest, verify=verify)
