from __future__ import annotations

import stat
from pathlib import Path
from typing import Protocol

from blackcell.config import RuntimePaths


class StorageQuotaPort(Protocol):
    def has_mutation_capacity(self) -> bool: ...


class RuntimeStorageQuota:
    """Fail-closed admission over active SQLite and artifact bytes.

    This is deliberately not a filesystem hard quota. It reserves bounded
    headroom before runtime mutations while ArtifactStore separately serializes
    its exact byte ceiling in SQLite.
    """

    def __init__(
        self,
        paths: RuntimePaths,
        *,
        max_active_bytes: int,
        mutation_reserve_bytes: int,
    ) -> None:
        if (
            isinstance(max_active_bytes, bool)
            or not isinstance(max_active_bytes, int)
            or max_active_bytes < 1
            or isinstance(mutation_reserve_bytes, bool)
            or not isinstance(mutation_reserve_bytes, int)
            or not 0 < mutation_reserve_bytes < max_active_bytes
        ):
            raise ValueError("invalid active-storage quota")
        self._paths = paths
        self._maximum = max_active_bytes
        self._reserve = mutation_reserve_bytes

    def active_bytes(self) -> int:
        total = 0
        for path in (
            self._paths.database_path,
            Path(f"{self._paths.database_path}-wal"),
            Path(f"{self._paths.database_path}-shm"),
        ):
            total += _regular_file_size(path)
        total += _tree_bytes(self._paths.artifact_root)
        return total

    def has_mutation_capacity(self) -> bool:
        try:
            return self.active_bytes() + self._reserve <= self._maximum
        except OSError, RuntimeError:
            return False


def _regular_file_size(path: Path) -> int:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return 0
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError("unsafe active-storage entry")
    return metadata.st_size


def _tree_bytes(root: Path) -> int:
    try:
        metadata = root.lstat()
    except FileNotFoundError:
        return 0
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError("unsafe active-storage root")
    entries = tuple(root.iterdir())
    total = 0
    pending = list(entries)
    while pending:
        path = pending.pop()
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise RuntimeError("unsafe active-storage entry")
        if stat.S_ISDIR(metadata.st_mode):
            pending.extend(path.iterdir())
        elif stat.S_ISREG(metadata.st_mode):
            total += metadata.st_size
        else:
            raise RuntimeError("unsafe active-storage entry")
    return total


__all__ = [
    "RuntimeStorageQuota",
    "StorageQuotaPort",
]
