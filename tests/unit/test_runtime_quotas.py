from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from blackcell.config import RuntimePaths
from blackcell.interfaces.http import SlidingWindowRequestQuota
from blackcell.kernel import ArtifactQuotaExceededError, ArtifactStore
from blackcell.runtime import RuntimeStorageQuota


def test_sliding_request_quota_has_exact_boundary_and_recovers_after_window() -> None:
    now = [100.0]
    quota = SlidingWindowRequestQuota(2, monotonic_clock=lambda: now[0])

    assert quota.consume()
    assert quota.consume()
    assert not quota.consume()
    now[0] = 160.0
    assert quota.consume()


def test_sliding_request_quota_serializes_concurrent_admission() -> None:
    quota = SlidingWindowRequestQuota(10, monotonic_clock=lambda: 100.0)

    with ThreadPoolExecutor(max_workers=20) as executor:
        accepted = tuple(executor.map(lambda _: quota.consume(), range(100)))

    assert sum(accepted) == 10


def test_active_storage_quota_reserves_mutation_headroom_and_excludes_backups(
    tmp_path: Path,
) -> None:
    paths = RuntimePaths.prepare(str(tmp_path / "data"))
    quota = RuntimeStorageQuota(paths, max_active_bytes=100, mutation_reserve_bytes=10)
    artifact = paths.artifact_root / "active.bin"
    artifact.write_bytes(b"a" * 90)

    assert quota.active_bytes() == 90
    assert quota.has_mutation_capacity()

    (paths.backup_root / "off-volume-copy.bin").write_bytes(b"b" * 1_000)
    assert quota.active_bytes() == 90

    artifact.write_bytes(b"a" * 91)
    assert not quota.has_mutation_capacity()


def test_active_storage_quota_fails_closed_on_symlinked_entries(tmp_path: Path) -> None:
    paths = RuntimePaths.prepare(str(tmp_path / "data"))
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    (paths.artifact_root / "linked").symlink_to(outside)
    quota = RuntimeStorageQuota(paths, max_active_bytes=100, mutation_reserve_bytes=10)

    assert not quota.has_mutation_capacity()


def test_artifact_quota_is_exact_shared_and_duplicate_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    database = tmp_path / "kernel.sqlite3"
    first = ArtifactStore(root, database_path=database, max_total_bytes=4)
    reference = first.put_bytes(b"same")
    second = ArtifactStore(root, database_path=database, max_total_bytes=4)

    assert second.put_bytes(b"same") == reference
    with pytest.raises(ArtifactQuotaExceededError) as caught:
        second.put_bytes(b"new")

    assert str(caught.value) == "artifact-quota-exceeded"


def test_artifact_quota_serializes_competing_store_instances(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    database = tmp_path / "kernel.sqlite3"
    ArtifactStore(root, database_path=database, max_total_bytes=10)

    def write(value: bytes) -> bool:
        store = ArtifactStore(root, database_path=database, max_total_bytes=10)
        try:
            store.put_bytes(value)
        except ArtifactQuotaExceededError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as executor:
        accepted = tuple(executor.map(write, (b"a" * 6, b"b" * 6)))

    assert sum(accepted) == 1
