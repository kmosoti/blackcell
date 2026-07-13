from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import cast
from urllib.parse import quote
from uuid import uuid4

from blackcell.config import RuntimePaths

_BUNDLE_SCHEMA = "blackcell-recovery-bundle/v1"
_MANIFEST_NAME = "manifest.json"
_DATABASE_NAME = "kernel.sqlite3"
_ARTIFACT_PREFIX = Path("artifacts") / "blobs"
_COPY_CHUNK_BYTES = 1024 * 1024
_MAX_MANIFEST_BYTES = 64 * 1024 * 1024


class RecoveryFailureCode(StrEnum):
    INVALID_PATH = "invalid-recovery-path"
    ACTIVE_STATE_UNAVAILABLE = "active-state-unavailable"
    INVALID_BUNDLE = "invalid-recovery-bundle"
    BACKUP_FAILED = "backup-failed"
    RESTORE_TARGET_EXISTS = "restore-target-exists"
    RESTORE_FAILED = "restore-failed"
    RETENTION_FAILED = "retention-failed"


class RecoveryError(RuntimeError):
    def __init__(self, code: RecoveryFailureCode) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True, slots=True)
class RecoveryBundleInfo:
    bundle_path: Path
    backup_id: str
    created_at: datetime
    database_digest: str
    database_bytes: int
    schema_version: int
    event_highwater: int
    artifact_count: int
    artifact_bytes: int


@dataclass(frozen=True, slots=True)
class RestoreInfo:
    target_path: Path
    backup_id: str
    event_highwater: int
    artifact_count: int


@dataclass(frozen=True, slots=True)
class _ArtifactEntry:
    digest: str
    relative_path: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class _VerifiedBundle:
    info: RecoveryBundleInfo
    artifacts: tuple[_ArtifactEntry, ...]


class LocalRecoveryService:
    """Consistent local bundles with explicit verification and non-destructive restore."""

    def __init__(
        self,
        paths: RuntimePaths | None = None,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        identifier: Callable[[], str] = lambda: uuid4().hex,
    ) -> None:
        self._paths = paths
        self._clock = clock
        self._identifier = identifier

    def create_backup(self, *, retention_count: int) -> RecoveryBundleInfo:
        paths = self._require_paths()
        if (
            isinstance(retention_count, bool)
            or not isinstance(retention_count, int)
            or retention_count < 1
        ):
            raise ValueError("retention_count must be a positive integer")
        created_at = self._clock()
        if created_at.tzinfo is None:
            raise ValueError("backup clock must return an aware datetime")
        created_at = created_at.astimezone(UTC)
        backup_id = self._identifier()
        if (
            not isinstance(backup_id, str)
            or len(backup_id) != 32
            or any(character not in "0123456789abcdef" for character in backup_id)
        ):
            raise ValueError("backup identifier must be 32 lowercase hexadecimal characters")
        final = paths.backup_root / f"backup-{backup_id}"
        staging = paths.backup_root / f".backup-{backup_id}.tmp"
        if final.exists() or final.is_symlink() or staging.exists() or staging.is_symlink():
            raise RecoveryError(RecoveryFailureCode.BACKUP_FAILED)

        try:
            _require_active_database(paths.database_path)
            _mkdir_owner(staging)
            snapshot_path = staging / _DATABASE_NAME
            _sqlite_backup(paths.database_path, snapshot_path)
            database_digest, database_bytes = _hash_file(snapshot_path)
            schema_version, event_highwater, artifacts = _snapshot_inventory(snapshot_path)
            artifact_total = 0
            for entry in artifacts:
                source = paths.artifact_root / "blobs" / entry.relative_path
                destination = staging / _ARTIFACT_PREFIX / entry.relative_path
                _copy_verified_file(
                    source,
                    destination,
                    expected_digest=entry.digest,
                    expected_size=entry.size_bytes,
                )
                artifact_total += entry.size_bytes

            manifest = {
                "schema_version": _BUNDLE_SCHEMA,
                "backup_id": backup_id,
                "created_at": created_at.isoformat(),
                "database": {
                    "path": _DATABASE_NAME,
                    "digest": database_digest,
                    "size_bytes": database_bytes,
                    "schema_version": schema_version,
                    "event_highwater": event_highwater,
                },
                "artifacts": {
                    "root": _ARTIFACT_PREFIX.as_posix(),
                    "count": len(artifacts),
                    "size_bytes": artifact_total,
                    "entries": [
                        {
                            "digest": entry.digest,
                            "relative_path": entry.relative_path,
                            "size_bytes": entry.size_bytes,
                        }
                        for entry in artifacts
                    ],
                },
            }
            _write_owner_file(staging / _MANIFEST_NAME, _canonical_json(manifest))
            _fsync_tree_directories(staging)
            verified = self._verify(staging)
            os.rename(staging, final)
            _fsync_directory(paths.backup_root)
            info = _with_bundle_path(verified.info, final)
            self._prune_verified(retention_count, preserve=final)
            return info
        except RecoveryError:
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError) as error:
            raise RecoveryError(RecoveryFailureCode.BACKUP_FAILED) from error
        finally:
            if staging.exists() and not staging.is_symlink():
                shutil.rmtree(staging, ignore_errors=True)

    def list_backups(self) -> tuple[RecoveryBundleInfo, ...]:
        paths = self._require_paths()
        values: list[RecoveryBundleInfo] = []
        try:
            entries = tuple(paths.backup_root.iterdir())
        except OSError as error:
            raise RecoveryError(RecoveryFailureCode.ACTIVE_STATE_UNAVAILABLE) from error
        for path in entries:
            if not path.name.startswith("backup-"):
                continue
            try:
                values.append(self._verify(path).info)
            except RecoveryError:
                continue
        return tuple(sorted(values, key=lambda item: (item.created_at, item.backup_id)))

    def verify_bundle(self, bundle_path: Path | str) -> RecoveryBundleInfo:
        path = _absolute_path(bundle_path)
        return self._verify(path).info

    def restore_bundle(
        self,
        bundle_path: Path | str,
        target_path: Path | str,
    ) -> RestoreInfo:
        verified = self._verify(_absolute_path(bundle_path))
        target = _absolute_path(target_path)
        if target.exists() or target.is_symlink():
            raise RecoveryError(RecoveryFailureCode.RESTORE_TARGET_EXISTS)
        parent = target.parent
        try:
            metadata = parent.lstat()
        except OSError as error:
            raise RecoveryError(RecoveryFailureCode.INVALID_PATH) from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise RecoveryError(RecoveryFailureCode.INVALID_PATH)

        staging = parent / f".{target.name}.{uuid4().hex}.restore"
        lock_path = (
            parent / f".blackcell-restore-{hashlib.sha256(str(target).encode()).hexdigest()}"
        )
        lock_descriptor: int | None = None
        try:
            lock_descriptor = os.open(
                lock_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            if target.exists() or target.is_symlink():
                raise RecoveryError(RecoveryFailureCode.RESTORE_TARGET_EXISTS)
            _mkdir_owner(staging)
            source_database = verified.info.bundle_path / _DATABASE_NAME
            _copy_verified_file(
                source_database,
                staging / _DATABASE_NAME,
                expected_digest=verified.info.database_digest,
                expected_size=verified.info.database_bytes,
            )
            for entry in verified.artifacts:
                _copy_verified_file(
                    verified.info.bundle_path / _ARTIFACT_PREFIX / entry.relative_path,
                    staging / _ARTIFACT_PREFIX / entry.relative_path,
                    expected_digest=entry.digest,
                    expected_size=entry.size_bytes,
                )
            _mkdir_owner(staging / "backups")
            _mkdir_owner(staging / "artifacts")
            _fsync_tree_directories(staging)
            RuntimePaths.prepare(str(staging))
            if target.exists() or target.is_symlink():
                raise RecoveryError(RecoveryFailureCode.RESTORE_TARGET_EXISTS)
            os.rename(staging, target)
            _fsync_directory(parent)
            RuntimePaths.prepare(str(target))
            return RestoreInfo(
                target,
                verified.info.backup_id,
                verified.info.event_highwater,
                verified.info.artifact_count,
            )
        except RecoveryError:
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError) as error:
            raise RecoveryError(RecoveryFailureCode.RESTORE_FAILED) from error
        finally:
            if lock_descriptor is not None:
                os.close(lock_descriptor)
                lock_path.unlink(missing_ok=True)
            if staging.exists() and not staging.is_symlink():
                shutil.rmtree(staging, ignore_errors=True)

    def _require_paths(self) -> RuntimePaths:
        if self._paths is None:
            raise RecoveryError(RecoveryFailureCode.ACTIVE_STATE_UNAVAILABLE)
        return self._paths

    def _verify(self, path: Path) -> _VerifiedBundle:
        try:
            _require_owner_directory(path)
            manifest_path = path / _MANIFEST_NAME
            manifest_bytes = _read_owner_file(manifest_path)
            manifest = json.loads(manifest_bytes)
            if not isinstance(manifest, dict) or _canonical_json(manifest) != manifest_bytes:
                raise ValueError("manifest is not canonical")
            verified = _manifest_info(path, manifest)
            expected_files = {
                _MANIFEST_NAME,
                _DATABASE_NAME,
                *(
                    (_ARTIFACT_PREFIX / entry.relative_path).as_posix()
                    for entry in verified.artifacts
                ),
            }
            expected_directories = {"."}
            for relative in expected_files:
                parent = Path(relative).parent
                while parent != Path("."):
                    expected_directories.add(parent.as_posix())
                    parent = parent.parent
            actual_files, actual_directories = _scan_owner_tree(path)
            if actual_files != expected_files or actual_directories != expected_directories:
                raise ValueError("bundle file inventory differs from manifest")

            database_digest, database_size = _hash_file(path / _DATABASE_NAME)
            if (
                database_digest != verified.info.database_digest
                or database_size != verified.info.database_bytes
            ):
                raise ValueError("database digest differs from manifest")
            schema_version, event_highwater, database_artifacts = _snapshot_inventory(
                path / _DATABASE_NAME
            )
            if (
                schema_version != verified.info.schema_version
                or event_highwater != verified.info.event_highwater
                or database_artifacts != verified.artifacts
            ):
                raise ValueError("database inventory differs from manifest")
            for entry in verified.artifacts:
                digest, size = _hash_file(
                    path / _ARTIFACT_PREFIX / entry.relative_path,
                    require_owner=True,
                )
                if digest != entry.digest or size != entry.size_bytes:
                    raise ValueError("artifact differs from manifest")
            return verified
        except RecoveryError:
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError, json.JSONDecodeError) as error:
            raise RecoveryError(RecoveryFailureCode.INVALID_BUNDLE) from error

    def _prune_verified(self, retention_count: int, *, preserve: Path) -> None:
        try:
            backups = self.list_backups()
            candidates = [info for info in backups if info.bundle_path != preserve]
            for info in candidates[: max(0, len(backups) - retention_count)]:
                shutil.rmtree(info.bundle_path)
            if len(backups) > retention_count:
                _fsync_directory(self._require_paths().backup_root)
        except RecoveryError:
            raise
        except OSError as error:
            raise RecoveryError(RecoveryFailureCode.RETENTION_FAILED) from error


def _manifest_info(path: Path, value: Mapping[str, object]) -> _VerifiedBundle:
    if set(value) != {"schema_version", "backup_id", "created_at", "database", "artifacts"}:
        raise ValueError("invalid manifest fields")
    if value["schema_version"] != _BUNDLE_SCHEMA:
        raise ValueError("unsupported bundle schema")
    backup_id = _bounded_text(value["backup_id"], maximum=64)
    if len(backup_id) != 32 or any(character not in "0123456789abcdef" for character in backup_id):
        raise ValueError("invalid backup identifier")
    created_at = datetime.fromisoformat(_bounded_text(value["created_at"], maximum=64))
    if created_at.tzinfo is None:
        raise ValueError("backup timestamp is not aware")
    created_at = created_at.astimezone(UTC)

    database = _mapping(value["database"])
    if set(database) != {"path", "digest", "size_bytes", "schema_version", "event_highwater"}:
        raise ValueError("invalid database manifest")
    if database["path"] != _DATABASE_NAME:
        raise ValueError("invalid database path")
    database_digest = _digest(database["digest"])
    database_bytes = _nonnegative_integer(database["size_bytes"])
    schema_version = _nonnegative_integer(database["schema_version"])
    event_highwater = _nonnegative_integer(database["event_highwater"])

    artifacts = _mapping(value["artifacts"])
    if set(artifacts) != {"root", "count", "size_bytes", "entries"}:
        raise ValueError("invalid artifact manifest")
    if artifacts["root"] != _ARTIFACT_PREFIX.as_posix():
        raise ValueError("invalid artifact root")
    entries_value = artifacts["entries"]
    if not isinstance(entries_value, list):
        raise ValueError("invalid artifact entries")
    entries: list[_ArtifactEntry] = []
    for item in entries_value:
        mapping = _mapping(item)
        if set(mapping) != {"digest", "relative_path", "size_bytes"}:
            raise ValueError("invalid artifact entry")
        digest = _digest(mapping["digest"])
        relative_path = _bounded_text(mapping["relative_path"], maximum=256)
        if relative_path != _artifact_relative_path(digest):
            raise ValueError("artifact path is not content addressed")
        entries.append(
            _ArtifactEntry(digest, relative_path, _nonnegative_integer(mapping["size_bytes"]))
        )
    if entries != sorted(entries, key=lambda item: item.digest):
        raise ValueError("artifact entries are not sorted")
    if len({entry.digest for entry in entries}) != len(entries):
        raise ValueError("artifact entries are not unique")
    artifact_count = _nonnegative_integer(artifacts["count"])
    artifact_bytes = _nonnegative_integer(artifacts["size_bytes"])
    if artifact_count != len(entries) or artifact_bytes != sum(item.size_bytes for item in entries):
        raise ValueError("artifact totals differ from entries")
    return _VerifiedBundle(
        RecoveryBundleInfo(
            path,
            backup_id,
            created_at,
            database_digest,
            database_bytes,
            schema_version,
            event_highwater,
            artifact_count,
            artifact_bytes,
        ),
        tuple(entries),
    )


def _snapshot_inventory(path: Path) -> tuple[int, int, tuple[_ArtifactEntry, ...]]:
    uri = f"file:{quote(str(path), safe='/')}?mode=ro&immutable=1"
    with closing(sqlite3.connect(uri, uri=True, timeout=30)) as connection:
        connection.execute("pragma query_only = on")
        integrity = connection.execute("pragma integrity_check").fetchall()
        if integrity != [("ok",)]:
            raise ValueError("database integrity check failed")
        connection.execute("pragma foreign_keys = on")
        if connection.execute("pragma foreign_key_check").fetchone() is not None:
            raise ValueError("database foreign key check failed")
        schema_version = int(connection.execute("pragma user_version").fetchone()[0])
        event_highwater = int(
            connection.execute(
                "select coalesce(max(global_position), 0) from kernel_events"
            ).fetchone()[0]
        )
        rows = connection.execute(
            "select digest, relative_path, size_bytes from kernel_artifacts order by digest"
        ).fetchall()
    artifacts = tuple(
        _ArtifactEntry(str(digest), str(relative_path), int(size_bytes))
        for digest, relative_path, size_bytes in rows
    )
    for entry in artifacts:
        _digest(entry.digest)
        if entry.relative_path != _artifact_relative_path(entry.digest) or entry.size_bytes < 0:
            raise ValueError("invalid artifact metadata")
    return schema_version, event_highwater, artifacts


def _sqlite_backup(source_path: Path, destination_path: Path) -> None:
    _mkdir_owner(destination_path.parent)
    source_uri = f"file:{quote(str(source_path), safe='/')}?mode=ro"
    with (
        closing(sqlite3.connect(source_uri, uri=True, timeout=30)) as source,
        closing(sqlite3.connect(destination_path, timeout=30)) as destination,
    ):
        source.backup(destination)
        destination.execute("pragma journal_mode = delete").fetchone()
        destination.execute("pragma synchronous = full")
        destination.commit()
    os.chmod(destination_path, 0o600)
    _fsync_file(destination_path)


def _require_active_database(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise RecoveryError(RecoveryFailureCode.ACTIVE_STATE_UNAVAILABLE) from error
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise RecoveryError(RecoveryFailureCode.ACTIVE_STATE_UNAVAILABLE)


def _absolute_path(value: Path | str) -> Path:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise RecoveryError(RecoveryFailureCode.INVALID_PATH)
    return path


def _require_owner_directory(path: Path) -> None:
    metadata = path.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise ValueError("unsafe bundle directory")


def _mkdir_owner(path: Path) -> None:
    missing: list[Path] = []
    candidate = path
    while not candidate.exists():
        if candidate.is_symlink():
            raise OSError("unsafe directory")
        missing.append(candidate)
        candidate = candidate.parent
    if candidate.is_symlink():
        raise OSError("unsafe directory")
    for directory in reversed(missing):
        directory.mkdir(mode=0o700, exist_ok=False)
        os.chmod(directory, 0o700)
    metadata = path.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
    ):
        raise OSError("unsafe directory")
    os.chmod(path, 0o700)


def _read_owner_file(path: Path) -> bytes:
    metadata = path.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_size > _MAX_MANIFEST_BYTES
    ):
        raise ValueError("unsafe bundle file")
    return path.read_bytes()


def _write_owner_file(path: Path, data: bytes) -> None:
    _mkdir_owner(path.parent)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)


def _copy_verified_file(
    source: Path,
    destination: Path,
    *,
    expected_digest: str,
    expected_size: int,
) -> None:
    _mkdir_owner(destination.parent)
    source_descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    destination_descriptor: int | None = None
    try:
        source_metadata = os.fstat(source_descriptor)
        if (
            not stat.S_ISREG(source_metadata.st_mode)
            or source_metadata.st_uid != os.getuid()
            or stat.S_IMODE(source_metadata.st_mode) != 0o600
            or source_metadata.st_size != expected_size
        ):
            raise ValueError("unsafe recovery source")
        destination_descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        digest = hashlib.sha256()
        copied = 0
        while chunk := os.read(source_descriptor, _COPY_CHUNK_BYTES):
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(destination_descriptor, view)
                view = view[written:]
            copied += len(chunk)
        actual_digest = f"sha256:{digest.hexdigest()}"
        if actual_digest != expected_digest or copied != expected_size:
            raise ValueError("recovery source failed verification")
        os.fchmod(destination_descriptor, 0o600)
        os.fsync(destination_descriptor)
    finally:
        os.close(source_descriptor)
        if destination_descriptor is not None:
            os.close(destination_descriptor)


def _scan_owner_tree(root: Path) -> tuple[set[str], set[str]]:
    files: set[str] = set()
    directories = {"."}
    pending = [root]
    while pending:
        directory = pending.pop()
        _require_owner_directory(directory)
        for child in directory.iterdir():
            metadata = child.lstat()
            relative = child.relative_to(root).as_posix()
            if stat.S_ISLNK(metadata.st_mode) or metadata.st_uid != os.getuid():
                raise ValueError("unsafe bundle entry")
            if stat.S_ISDIR(metadata.st_mode):
                if stat.S_IMODE(metadata.st_mode) != 0o700:
                    raise ValueError("unsafe bundle directory mode")
                directories.add(relative)
                pending.append(child)
            elif stat.S_ISREG(metadata.st_mode):
                if stat.S_IMODE(metadata.st_mode) != 0o600:
                    raise ValueError("unsafe bundle file mode")
                files.add(relative)
            else:
                raise ValueError("unsupported bundle entry")
    return files, directories


def _hash_file(path: Path, *, require_owner: bool = False) -> tuple[str, int]:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or (
            require_owner
            and (metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o600)
        ):
            raise ValueError("recovery entry is not a regular file")
        digest = hashlib.sha256()
        total = 0
        while chunk := os.read(descriptor, _COPY_CHUNK_BYTES):
            digest.update(chunk)
            total += len(chunk)
        return f"sha256:{digest.hexdigest()}", total
    finally:
        os.close(descriptor)


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_tree_directories(root: Path) -> None:
    directories = [path for path in root.rglob("*") if path.is_dir()]
    for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
        _fsync_directory(directory)
    _fsync_directory(root)


def _artifact_relative_path(digest: str) -> str:
    hexadecimal = digest.removeprefix("sha256:")
    return f"sha256/{hexadecimal[:2]}/{hexadecimal[2:4]}/{hexadecimal}"


def _canonical_json(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("expected an object")
    return cast("Mapping[str, object]", value)


def _bounded_text(value: object, *, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError("expected bounded text")
    return value


def _nonnegative_integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("expected a nonnegative integer")
    return value


def _digest(value: object) -> str:
    digest = _bounded_text(value, maximum=71)
    hexadecimal = digest.removeprefix("sha256:")
    if (
        not digest.startswith("sha256:")
        or len(hexadecimal) != 64
        or any(character not in "0123456789abcdef" for character in hexadecimal)
    ):
        raise ValueError("invalid SHA-256 digest")
    return digest


def _with_bundle_path(info: RecoveryBundleInfo, path: Path) -> RecoveryBundleInfo:
    return RecoveryBundleInfo(
        path,
        info.backup_id,
        info.created_at,
        info.database_digest,
        info.database_bytes,
        info.schema_version,
        info.event_highwater,
        info.artifact_count,
        info.artifact_bytes,
    )


__all__: Sequence[str] = (
    "LocalRecoveryService",
    "RecoveryBundleInfo",
    "RecoveryError",
    "RecoveryFailureCode",
    "RestoreInfo",
)
