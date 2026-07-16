from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path

from blackcell.adapters.recovery import LocalRecoveryService
from blackcell.bootstrap.repository import compose_repository_runtime
from blackcell.config import RuntimePaths
from blackcell.kernel import ArtifactStore, EventStore


def test_external_bundle_restores_live_free_replay_after_active_state_loss(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(
        ["git", "init", "--quiet", str(repository)],
        check=True,
        capture_output=True,
        text=True,
    )
    paths = RuntimePaths.prepare(str(tmp_path / "active-data"))
    database = paths.ensure_database_file()
    operator = compose_repository_runtime(
        repository,
        database_path=database,
        artifact_root=paths.artifact_root,
    ).operator
    result = operator.run(objective="Inspect repository recovery readiness.")
    backup = LocalRecoveryService(paths).create_backup(retention_count=1)
    external = tmp_path / "external-copy" / backup.bundle_path.name
    external.parent.mkdir()
    shutil.copytree(backup.bundle_path, external)

    shutil.rmtree(paths.data_root)
    restored_root = tmp_path / "restored-data"
    restored = LocalRecoveryService().restore_bundle(external, restored_root)
    restored_paths = RuntimePaths.prepare(str(restored_root))
    replay_operator = compose_repository_runtime(
        repository,
        database_path=restored_paths.database_path,
        artifact_root=restored_paths.artifact_root,
    ).operator
    repository.rename(tmp_path / "repository-offline")
    replay = replay_operator.replay(result.run_id)

    assert restored.backup_id == backup.backup_id
    assert replay.classification.value == result.status
    assert replay.finding is None
    assert replay.artifacts
    assert all(item.verified for item in replay.artifacts)
    assert (
        EventStore(restored_paths.database_path)
        .read_all(after_position=0, limit=1_000)[-1]
        .global_position
        == backup.event_highwater
    )
    store = ArtifactStore(
        restored_paths.artifact_root,
        database_path=restored_paths.database_path,
    )
    assert all(store.verify(item.digest) for item in replay.artifacts)
    assert stat.S_IMODE(restored_paths.data_root.stat().st_mode) == 0o700
    assert stat.S_IMODE(restored_paths.database_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(restored_paths.backup_root.stat().st_mode) == 0o700
    assert not tuple(restored_paths.backup_root.iterdir())
