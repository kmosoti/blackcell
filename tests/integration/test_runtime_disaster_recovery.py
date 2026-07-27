from __future__ import annotations

import shutil
import stat
from pathlib import Path

from blackcell.adapters.recovery import LocalRecoveryService
from blackcell.config import RuntimePaths
from blackcell.kernel import ArtifactStore, EventEnvelope, EventStore


def test_external_bundle_restores_kernel_state_after_active_state_loss(
    tmp_path: Path,
) -> None:
    paths = RuntimePaths.prepare(str(tmp_path / "active-data"))
    database = paths.ensure_database_file()
    artifacts = ArtifactStore(paths.artifact_root, database_path=database)
    reference = artifacts.put_text("retained recovery evidence")
    stored = EventStore(database).append(
        EventEnvelope.create(
            stream_id="recovery:integration",
            stream_sequence=1,
            event_type="recovery.evidence-recorded",
            actor="test",
            source="recovery-integration",
            payload={"artifact_digest": reference.digest},
        ),
        expected_sequence=0,
    )

    backup = LocalRecoveryService(paths).create_backup(retention_count=1)
    external = tmp_path / "external-copy" / backup.bundle_path.name
    external.parent.mkdir()
    shutil.copytree(backup.bundle_path, external)

    shutil.rmtree(paths.data_root)
    restored_root = tmp_path / "restored-data"
    restored = LocalRecoveryService().restore_bundle(external, restored_root)
    restored_paths = RuntimePaths.prepare(str(restored_root))
    restored_events = EventStore(restored_paths.database_path).read_all(
        after_position=0,
        limit=1_000,
    )
    restored_artifacts = ArtifactStore(
        restored_paths.artifact_root,
        database_path=restored_paths.database_path,
    )

    assert restored.backup_id == backup.backup_id
    assert restored.event_highwater == stored.global_position
    assert restored_events == (stored,)
    assert restored_artifacts.get_text(reference.digest) == "retained recovery evidence"
    assert restored_artifacts.verify(reference.digest)
    assert stat.S_IMODE(restored_paths.data_root.stat().st_mode) == 0o700
    assert stat.S_IMODE(restored_paths.database_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(restored_paths.backup_root.stat().st_mode) == 0o700
    assert not tuple(restored_paths.backup_root.iterdir())
