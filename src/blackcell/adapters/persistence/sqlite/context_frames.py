from __future__ import annotations

import sqlite3
from pathlib import Path
from types import TracebackType
from typing import Self

from blackcell.features.build_context.artifacts import (
    CONTEXT_FRAME_MEDIA_TYPE,
    decode_context_frame,
    encode_context_frame,
)
from blackcell.features.build_context.models import ContextFrame
from blackcell.features.build_context.storage import (
    ContextFrameConflictError,
    ContextFrameIntegrityError,
    ContextFrameSchemaError,
)
from blackcell.kernel import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactStore,
)
from blackcell.kernel._json import bytes_digest
from blackcell.kernel.database import connect

_INDEX_SCHEMA_VERSION = 1
_MIGRATION_TABLE = "context_frame_index_schema_migrations"

_MIGRATION_SCHEMA = f"""
create table if not exists {_MIGRATION_TABLE} (
    version integer primary key,
    applied_at text not null
)
"""
_INDEX_SCHEMA = """
create table if not exists context_frame_index (
    frame_id text primary key check(length(frame_id) > 0),
    schema_version text not null check(length(schema_version) > 0),
    task_id text not null check(length(task_id) > 0),
    generated_at text not null,
    foreign key(frame_id) references kernel_artifacts(digest)
)
"""


class ArtifactContextFrameStore:
    """Artifact-backed ContextFrame store with a rebuildable SQLite listing index.

    Canonical frame JSON has exactly one durable owner: :class:`ArtifactStore`.
    The SQLite index records only discovery metadata. An interruption between the
    artifact commit and index commit may leave an inert artifact; an exact retry
    deterministically repairs the missing index entry.
    """

    def __init__(
        self,
        root: Path | str,
        *,
        database_path: Path | str | None = None,
    ) -> None:
        self.root = Path(root)
        self._artifacts = ArtifactStore(self.root, database_path=database_path)
        self.database_path = self._artifacts.database_path
        self._closed = False
        self._initialize_index()

    def __enter__(self) -> Self:
        self._require_open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._closed = True

    def put(self, frame: ContextFrame) -> ContextFrame:
        self._require_open()
        data = encode_context_frame(frame)
        actual_frame_id = bytes_digest(data)
        if actual_frame_id != frame.frame_id:
            raise ContextFrameConflictError(
                f"ContextFrame {frame.frame_id!r} does not identify its canonical content"
            )

        try:
            reference = self._artifacts.put_bytes(
                data,
                media_type=CONTEXT_FRAME_MEDIA_TYPE,
                encoding="utf-8",
            )
        except ArtifactIntegrityError as error:
            raise ContextFrameIntegrityError(
                f"ContextFrame artifact {frame.frame_id!r} is corrupt"
            ) from error
        if reference.digest != frame.frame_id:  # pragma: no cover - content-address invariant
            raise ContextFrameIntegrityError("ArtifactStore returned a different frame digest")

        with connect(self.database_path) as connection:
            connection.execute("begin immediate")
            try:
                row = connection.execute(
                    "select * from context_frame_index where frame_id = ?",
                    (frame.frame_id,),
                ).fetchone()
                metadata = (
                    frame.schema_version,
                    frame.task_id,
                    frame.generated_at.isoformat(),
                )
                if row is not None:
                    stored_metadata = (
                        str(row["schema_version"]),
                        str(row["task_id"]),
                        str(row["generated_at"]),
                    )
                    if stored_metadata != metadata:
                        raise ContextFrameConflictError(
                            f"ContextFrame index {frame.frame_id!r} has conflicting metadata"
                        )
                else:
                    connection.execute(
                        """
                        insert into context_frame_index(
                            frame_id, schema_version, task_id, generated_at
                        ) values (?, ?, ?, ?)
                        """,
                        (frame.frame_id, *metadata),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        stored = self.get(frame.frame_id)
        if stored is None:  # pragma: no cover - committed index invariant
            raise ContextFrameIntegrityError("committed ContextFrame index entry is missing")
        return stored

    def get(self, frame_id: str) -> ContextFrame | None:
        self._require_open()
        _validate_digest(frame_id, label="frame_id")
        with connect(self.database_path) as connection:
            row = connection.execute(
                "select * from context_frame_index where frame_id = ?",
                (frame_id,),
            ).fetchone()
        return None if row is None else self._frame_from_index(row)

    def list_frames(self) -> tuple[ContextFrame, ...]:
        self._require_open()
        with connect(self.database_path) as connection:
            rows = connection.execute(
                "select * from context_frame_index order by frame_id"
            ).fetchall()
        return tuple(self._frame_from_index(row) for row in rows)

    def __len__(self) -> int:
        self._require_open()
        with connect(self.database_path) as connection:
            row = connection.execute("select count(*) as count from context_frame_index").fetchone()
        if row is None:  # pragma: no cover - SQLite aggregate invariant
            raise ContextFrameIntegrityError("SQLite did not return a ContextFrame count")
        return int(row["count"])

    def _frame_from_index(self, row: sqlite3.Row) -> ContextFrame:
        frame_id = str(row["frame_id"])
        _validate_digest(frame_id, label="indexed frame_id")
        try:
            data = self._artifacts.get_bytes(frame_id, verify=True)
        except (ArtifactIntegrityError, ArtifactNotFoundError) as error:
            raise ContextFrameIntegrityError(
                f"ContextFrame artifact {frame_id!r} is missing or corrupt"
            ) from error
        frame = decode_context_frame(data, expected_frame_id=frame_id)
        metadata = (
            str(row["schema_version"]),
            str(row["task_id"]),
            str(row["generated_at"]),
        )
        expected_metadata = (
            frame.schema_version,
            frame.task_id,
            frame.generated_at.isoformat(),
        )
        if metadata != expected_metadata:
            raise ContextFrameIntegrityError(
                f"ContextFrame index {frame_id!r} does not match its artifact"
            )
        return frame

    def _initialize_index(self) -> None:
        with connect(self.database_path) as connection:
            metadata_exists = connection.execute(
                "select 1 from sqlite_master where type = 'table' and name = ?",
                (_MIGRATION_TABLE,),
            ).fetchone()
            if metadata_exists is not None:
                current = _index_schema_version(connection)
                if current > _INDEX_SCHEMA_VERSION:
                    raise ContextFrameSchemaError(
                        f"ContextFrame index schema {current} is newer than supported "
                        f"schema {_INDEX_SCHEMA_VERSION}"
                    )

            connection.execute("begin immediate")
            try:
                connection.execute(_MIGRATION_SCHEMA)
                current = _index_schema_version(connection)
                if current > _INDEX_SCHEMA_VERSION:
                    raise ContextFrameSchemaError(
                        f"ContextFrame index schema {current} is newer than supported "
                        f"schema {_INDEX_SCHEMA_VERSION}"
                    )
                if current < 1:
                    connection.execute(_INDEX_SCHEMA)
                    connection.execute(
                        f"""
                        insert into {_MIGRATION_TABLE}(version, applied_at)
                        values (1, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                        """
                    )
                else:
                    # The supported v1 table is rebuildable from artifacts; ensure
                    # it exists only after the forward-version guard has passed.
                    connection.execute(_INDEX_SCHEMA)
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("ArtifactContextFrameStore is closed")


def _index_schema_version(connection: sqlite3.Connection) -> int:
    row = connection.execute(f"select max(version) as version from {_MIGRATION_TABLE}").fetchone()
    return 0 if row is None or row["version"] is None else int(row["version"])


def _validate_digest(value: str, *, label: str) -> None:
    hexadecimal = value.removeprefix("sha256:")
    if not value.startswith("sha256:") or len(hexadecimal) != 64:
        raise ContextFrameIntegrityError(f"{label} is not a SHA-256 digest")
    try:
        int(hexadecimal, 16)
    except ValueError as error:
        raise ContextFrameIntegrityError(f"{label} is not a SHA-256 digest") from error
