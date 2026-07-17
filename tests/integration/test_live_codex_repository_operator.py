from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from blackcell.bootstrap.repository import compose_repository_runtime

_CODEX_MODEL = os.environ.get("BLACKCELL_CODEX_MODEL")

pytestmark = pytest.mark.skipif(
    not _CODEX_MODEL,
    reason="set BLACKCELL_CODEX_MODEL to run the live Codex repository route",
)


def test_live_codex_repository_route_completes_and_replays_without_source_repo(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "source-repository"
    repo.mkdir()
    (repo / "README.md").write_text("# Live Codex fixture\n", encoding="utf-8")
    subprocess.run(
        ["git", "init", "--quiet"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    runtime = tmp_path / "runtime"
    components = compose_repository_runtime(
        repo,
        database_path=runtime / "kernel.sqlite3",
        artifact_root=runtime / "artifacts",
        model="codex",
        codex_model=_require_codex_model(),
    )

    result = components.operator.run(
        objective="Inspect whether this fixture is a valid Git repository.",
    )

    assert result.status == "completed"
    assert result.outcome == "executed"
    assert result.execution_status == "succeeded"
    first_replay = components.operator.replay(result.run_id)
    assert first_replay.classification.value == "completed"
    assert all(artifact.verified for artifact in first_replay.artifacts)

    shutil.rmtree(repo)

    replay_without_repo = components.operator.replay(result.run_id)
    assert replay_without_repo == first_replay


def _require_codex_model() -> str:
    if _CODEX_MODEL is None:  # pragma: no cover - guarded by the module skip
        raise AssertionError("BLACKCELL_CODEX_MODEL is required")
    return _CODEX_MODEL
