from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from blackcell.domains.repository import (
    Claim,
    ClaimCorrection,
    EpistemicStatus,
    SourceReliability,
)
from blackcell.operator import (
    RepositoryOperator,
    RepositoryStatusReader,
    RepositoryStatusSnapshot,
)
from blackcell.workflows.run_protocol import RUN_WORKFLOW_VERSION_V2

NOW = datetime(2026, 7, 13, 12, tzinfo=UTC)


class CountingStatusReader(RepositoryStatusReader):
    def __init__(self, repo_root: Path) -> None:
        super().__init__(repo_root, clock=lambda: NOW)
        self.calls = 0

    def read(self):
        self.calls += 1
        return super().read()


class InvalidStatusReader(RepositoryStatusReader):
    def __init__(self, repo_root: Path) -> None:
        super().__init__(repo_root, clock=lambda: NOW)
        self.calls = 0

    def read(self) -> RepositoryStatusSnapshot:
        self.calls += 1
        return RepositoryStatusSnapshot(
            False,
            False,
            0,
            "sha256:" + "0" * 64,
            NOW,
        )


def test_public_operator_delegates_to_verified_daily_operator_v2(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    database = repo / ".blackcell" / "kernel.sqlite3"
    reader = CountingStatusReader(repo)
    operator = RepositoryOperator(
        repo,
        database_path=database,
        status_reader=reader,
        clock=lambda: NOW,
    )

    result = operator.run()

    assert result.status == "completed"
    assert result.outcome == "executed"
    assert result.workflow_version == RUN_WORKFLOW_VERSION_V2
    assert result.authorization_outcome == "allow"
    assert result.execution_status == "succeeded"
    assert result.evaluation_verdict == "pass"
    assert result.transition_recorded
    assert result.run_event_count >= 17
    assert result.artifact_count >= 12
    assert reader.calls == 3

    context = operator.context(result.run_id)
    replay = operator.replay(result.run_id)
    state = operator.current_state()
    assert context.frame_id == result.context_frame_id
    assert context.payload["schema_version"] == "context-frame/v4"
    assert replay.run_id == result.run_id
    assert replay.classification.value == "completed"
    assert all(item.verified for item in replay.artifacts)
    assert state.scope.stream_id == result.repository_stream_id
    assert state.claims_for("repository", "git.valid")[0].value is True
    assert reader.calls == 3
    assert not any(event.event_type.startswith("operator.") for event in replay.events)


def test_status_output_paths_are_not_persisted_as_run_evidence(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    sensitive_name = "customer-secret-path.txt"
    (repo / sensitive_name).write_text("fixture\n", encoding="utf-8")
    operator = RepositoryOperator(
        repo,
        database_path=repo / ".blackcell" / "kernel.sqlite3",
        clock=lambda: NOW,
    )

    result = operator.run()
    replay = operator.replay(result.run_id)
    encoded_events = repr([event.payload for event in replay.events]).encode()
    artifact_bytes = b"\n".join(
        operator.artifacts.get_bytes(item.digest) for item in replay.artifacts
    )

    assert sensitive_name.encode() not in encoded_events
    assert sensitive_name.encode() not in artifact_bytes


def test_symbolic_repository_constraint_denies_without_execution_or_outcome_read(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path)
    reader = InvalidStatusReader(repo)
    operator = RepositoryOperator(
        repo,
        database_path=repo / ".blackcell" / "kernel.sqlite3",
        status_reader=reader,
        clock=lambda: NOW,
    )

    result = operator.run()

    assert result.status == "completed"
    assert result.outcome == "denied"
    assert result.authorization_outcome == "deny"
    assert result.execution_status is None
    assert result.evaluation_verdict == "not-evaluated"
    assert not result.transition_recorded
    assert reader.calls == 1


def test_codex_route_requires_an_explicit_model_before_storage_creation(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path)
    database = repo / ".blackcell" / "kernel.sqlite3"

    try:
        RepositoryOperator(repo, database_path=database, model="codex")
    except ValueError as error:
        assert "--codex-model is required" in str(error)
    else:  # pragma: no cover - assertion helper
        raise AssertionError("Codex route accepted no model ID")

    assert not database.exists()


def test_current_state_corrections_use_the_canonical_ingestion_path(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    operator = RepositoryOperator(
        repo,
        database_path=repo / ".blackcell" / "kernel.sqlite3",
        clock=lambda: NOW,
    )
    operator.run()
    original = operator.current_state().claims_for("repository", "git.clean")[0]
    replacement = Claim(
        claim_id="claim:human-clean-correction",
        subject=original.subject,
        predicate=original.predicate,
        value=True,
        epistemic_status=EpistemicStatus.OBSERVED,
        source_reliability=SourceReliability.AUTHORITATIVE,
        evidence=(),
        observed_at=NOW,
        effective_at=NOW,
    )

    event = operator.append_correction(
        ClaimCorrection(
            correction_id="correction:human-clean",
            supersedes_claim_ids=(original.claim_id,),
            replacement=replacement,
            effective_at=NOW,
            reason="Human verified the intended state.",
        )
    )

    state = operator.current_state()
    assert event.event_type == "observation.corrected"
    assert original.claim_id in {item.claim_id for item in state.superseded_claims}
    assert state.claims_for("repository", "git.clean")[0].claim_id == replacement.claim_id


def test_default_storage_remains_inside_git_metadata(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    operator = RepositoryOperator(repo, clock=lambda: NOW)

    operator.run()

    assert operator.database_path == repo / ".git" / "blackcell" / "kernel.sqlite3"
    assert operator.database_path.is_file()
    assert not (repo / ".blackcell").exists()


def test_latest_run_and_direct_lookup_are_repository_scoped_in_shared_storage(
    tmp_path: Path,
) -> None:
    left_repo = _repository(tmp_path, name="left")
    right_repo = _repository(tmp_path, name="right")
    database = tmp_path / "shared" / "kernel.sqlite3"
    artifacts = tmp_path / "shared" / "artifacts"
    left = RepositoryOperator(
        left_repo,
        database_path=database,
        artifact_root=artifacts,
        clock=lambda: NOW,
    )
    right = RepositoryOperator(
        right_repo,
        database_path=database,
        artifact_root=artifacts,
        clock=lambda: NOW,
    )

    left_run = left.run()
    right_run = right.run()

    assert left.context().run_id == left_run.run_id
    assert right.context().run_id == right_run.run_id
    with pytest.raises(LookupError, match="does not belong"):
        left.replay(right_run.run_id)


def _repository(tmp_path: Path, *, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    (repo / "README.md").write_text("# Fixture\n", encoding="utf-8")
    subprocess.run(
        ["git", "init", "--quiet"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return repo
