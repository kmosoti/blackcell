from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from blackcell.adapters.recovery import (
    LocalRecoveryService,
    RecoveryError,
    RecoveryFailureCode,
)
from blackcell.bootstrap.process import main
from blackcell.config import BACKUP_RETENTION_COUNT_ENV, DATA_DIR_ENV, RuntimePaths
from blackcell.kernel import ArtifactStore, EventEnvelope, EventStore


def test_backup_is_verifiable_tamper_evident_and_retains_only_verified_count(
    tmp_path: Path,
) -> None:
    paths, _ = _state(tmp_path / "data")
    identifiers = iter(("1" * 32, "2" * 32, "3" * 32))
    times = iter(
        datetime(2026, 7, 13, 12, 0, tzinfo=UTC) + timedelta(minutes=index) for index in range(3)
    )
    recovery = LocalRecoveryService(
        paths,
        clock=lambda: next(times),
        identifier=lambda: next(identifiers),
    )

    first = recovery.create_backup(retention_count=2)
    second = recovery.create_backup(retention_count=2)
    third = recovery.create_backup(retention_count=2)

    assert not first.bundle_path.exists()
    assert tuple(item.backup_id for item in recovery.list_backups()) == (
        second.backup_id,
        third.backup_id,
    )
    artifact = next((second.bundle_path / "artifacts" / "blobs").rglob("?" * 64))
    artifact.write_bytes(b"tampered")

    with pytest.raises(RecoveryError) as caught:
        LocalRecoveryService().verify_bundle(second.bundle_path)

    assert caught.value.code is RecoveryFailureCode.INVALID_BUNDLE
    assert tuple(item.backup_id for item in recovery.list_backups()) == (third.backup_id,)


def test_recovery_cli_is_json_first_and_does_not_require_service_secrets(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    paths, _ = _state(tmp_path / "data")
    monkeypatch.setenv(DATA_DIR_ENV, str(paths.data_root))
    monkeypatch.setenv(BACKUP_RETENTION_COUNT_ENV, "2")
    monkeypatch.delenv("BLACKCELL_API_TOKEN", raising=False)
    monkeypatch.delenv("BLACKCELL_API_TOKEN_FILE", raising=False)

    assert main(("recovery", "backup")) == 0
    backup_payload = json.loads(capsys.readouterr().out)
    bundle = backup_payload["bundle_path"]
    assert backup_payload["operation"] == "backup"

    assert main(("recovery", "list")) == 0
    listed_payload = json.loads(capsys.readouterr().out)
    assert [item["backup_id"] for item in listed_payload["backups"]] == [
        backup_payload["backup_id"]
    ]

    assert main(("recovery", "verify", bundle)) == 0
    verified_payload = json.loads(capsys.readouterr().out)
    assert verified_payload["database_digest"] == backup_payload["database_digest"]

    target = tmp_path / "restored"
    assert main(("recovery", "restore", bundle, str(target))) == 0
    restore_payload = json.loads(capsys.readouterr().out)
    assert restore_payload["target_path"] == str(target)

    secret_path = "relative-customer-secret"
    assert main(("recovery", "verify", secret_path)) == 1
    failure = capsys.readouterr()
    assert failure.out == ""
    assert json.loads(failure.err) == {"error": {"code": "invalid-recovery-path"}}
    assert secret_path not in failure.err


def test_restore_never_overwrites_an_existing_target(tmp_path: Path) -> None:
    paths, _ = _state(tmp_path / "data")
    backup = LocalRecoveryService(paths).create_backup(retention_count=1)
    target = tmp_path / "existing"
    target.mkdir()
    marker = target / "keep"
    marker.write_text("unchanged")

    with pytest.raises(RecoveryError) as caught:
        LocalRecoveryService().restore_bundle(backup.bundle_path, target)

    assert caught.value.code is RecoveryFailureCode.RESTORE_TARGET_EXISTS
    assert marker.read_text() == "unchanged"


def test_verification_rejects_extra_symlinks_and_permissive_modes(tmp_path: Path) -> None:
    paths, _ = _state(tmp_path / "data")
    backup = LocalRecoveryService(paths).create_backup(retention_count=1)
    linked = backup.bundle_path / "unexpected"
    linked.symlink_to(backup.bundle_path / "manifest.json")

    with pytest.raises(RecoveryError) as symlinked:
        LocalRecoveryService().verify_bundle(backup.bundle_path)
    assert symlinked.value.code is RecoveryFailureCode.INVALID_BUNDLE

    linked.unlink()
    manifest = backup.bundle_path / "manifest.json"
    manifest.chmod(0o640)
    with pytest.raises(RecoveryError) as permissive:
        LocalRecoveryService().verify_bundle(backup.bundle_path)
    assert permissive.value.code is RecoveryFailureCode.INVALID_BUNDLE


def test_retention_preserves_the_new_bundle_when_the_clock_moves_backward(
    tmp_path: Path,
) -> None:
    paths, _ = _state(tmp_path / "data")
    identifiers = iter(("a" * 32, "b" * 32))
    times = iter(
        (
            datetime(2026, 7, 13, 12, 0, tzinfo=UTC),
            datetime(2026, 7, 13, 11, 0, tzinfo=UTC),
        )
    )
    recovery = LocalRecoveryService(
        paths,
        clock=lambda: next(times),
        identifier=lambda: next(identifiers),
    )
    first = recovery.create_backup(retention_count=1)
    second = recovery.create_backup(retention_count=1)

    assert not first.bundle_path.exists()
    assert second.bundle_path.is_dir()
    assert recovery.list_backups() == (second,)


def test_online_backup_is_self_consistent_during_concurrent_appends(tmp_path: Path) -> None:
    paths, _ = _state(tmp_path / "data")
    database = paths.database_path
    artifacts = ArtifactStore(paths.artifact_root, database_path=database)
    events = EventStore(database)
    started = threading.Event()
    stop = threading.Event()
    failures: list[BaseException] = []

    def append() -> None:
        sequence = 1
        try:
            while not stop.is_set():
                reference = artifacts.put_bytes(f"concurrent-{sequence}".encode())
                events.append(
                    EventEnvelope.create(
                        stream_id="recovery:concurrent",
                        stream_sequence=sequence,
                        event_type="recovery.concurrent-recorded",
                        actor="test",
                        source="fixture/v1",
                        payload={"artifact_digest": reference.digest},
                    ),
                    expected_sequence=sequence - 1,
                )
                started.set()
                sequence += 1
                time.sleep(0.001)
        except BaseException as error:  # pragma: no cover - surfaced below
            failures.append(error)

    writer = threading.Thread(target=append, daemon=True)
    writer.start()
    assert started.wait(timeout=5)
    try:
        backup = LocalRecoveryService(paths).create_backup(retention_count=1)
    finally:
        stop.set()
        writer.join(timeout=5)

    assert not writer.is_alive()
    assert not failures
    verified = LocalRecoveryService().verify_bundle(backup.bundle_path)
    current_events = events.read_all(after_position=0, limit=10_000)
    current_highwater = current_events[-1].global_position
    assert current_highwater is not None
    assert verified.event_highwater <= current_highwater
    assert verified.artifact_count <= len(tuple((paths.artifact_root / "blobs").rglob("?" * 64)))


def _state(root: Path) -> tuple[RuntimePaths, str]:
    paths = RuntimePaths.prepare(str(root))
    database = paths.ensure_database_file()
    artifacts = ArtifactStore(paths.artifact_root, database_path=database)
    reference = artifacts.put_text("recovery evidence")
    EventStore(database).append(
        EventEnvelope.create(
            stream_id="recovery:test",
            stream_sequence=1,
            event_type="recovery.fixture-recorded",
            actor="test",
            source="fixture/v1",
            payload={"artifact_digest": reference.digest},
        ),
        expected_sequence=0,
    )
    return paths, reference.digest
