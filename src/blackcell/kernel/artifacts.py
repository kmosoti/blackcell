from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from blackcell.kernel._json import JsonInput, bytes_digest, canonical_json_bytes
from blackcell.kernel.database import connect, initialize_database
from blackcell.kernel.errors import ArtifactIntegrityError, ArtifactNotFoundError
from blackcell.kernel.events import utc_now


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    digest: str
    size_bytes: int
    media_type: str
    encoding: str | None
    created_at: datetime

    @property
    def artifact_id(self) -> str:
        return self.digest


class ArtifactStore:
    """File-backed SHA-256 object store with transactional SQLite metadata."""

    def __init__(self, root: Path | str, *, database_path: Path | str | None = None) -> None:
        self.root = Path(root)
        self.blob_root = self.root / "blobs"
        self.database_path = (
            Path(database_path) if database_path is not None else self.root / "kernel.sqlite3"
        )
        self.blob_root.mkdir(parents=True, exist_ok=True)
        initialize_database(self.database_path)

    def put_bytes(
        self,
        data: bytes,
        *,
        media_type: str = "application/octet-stream",
        encoding: str | None = None,
    ) -> ArtifactRef:
        if not isinstance(data, bytes):
            raise TypeError("artifact data must be bytes")
        if not media_type.strip():
            raise ValueError("media_type must not be empty")
        digest = bytes_digest(data)
        relative_path = self._relative_path(digest)
        destination = self.blob_root / relative_path

        with connect(self.database_path) as connection:
            connection.execute("begin immediate")
            try:
                row = connection.execute(
                    "select * from kernel_artifacts where digest = ?", (digest,)
                ).fetchone()
                if row is not None:
                    reference = _artifact_from_row(row)
                    self._verify_path(destination, digest, reference.size_bytes)
                    connection.commit()
                    return reference

                self._write_once(destination, data, digest)
                created_at = utc_now()
                connection.execute(
                    """
                    insert into kernel_artifacts(
                        digest, algorithm, size_bytes, media_type, encoding,
                        relative_path, created_at
                    ) values (?, 'sha256', ?, ?, ?, ?, ?)
                    """,
                    (
                        digest,
                        len(data),
                        media_type,
                        encoding,
                        relative_path.as_posix(),
                        created_at.isoformat(),
                    ),
                )
                connection.commit()
                return ArtifactRef(digest, len(data), media_type, encoding, created_at)
            except Exception:
                connection.rollback()
                raise

    def put_text(
        self,
        text: str,
        *,
        encoding: str = "utf-8",
        media_type: str = "text/plain",
    ) -> ArtifactRef:
        return self.put_bytes(text.encode(encoding), media_type=media_type, encoding=encoding)

    def put_json(self, value: JsonInput) -> ArtifactRef:
        return self.put_bytes(
            canonical_json_bytes(value),
            media_type="application/json",
            encoding="utf-8",
        )

    def stat(self, digest: str | ArtifactRef) -> ArtifactRef:
        key = _digest_of(digest)
        with connect(self.database_path) as connection:
            row = connection.execute(
                "select * from kernel_artifacts where digest = ?", (key,)
            ).fetchone()
        if row is None:
            raise ArtifactNotFoundError(f"artifact {key!r} does not exist")
        return _artifact_from_row(row)

    def get_bytes(self, digest: str | ArtifactRef, *, verify: bool = True) -> bytes:
        reference = self.stat(digest)
        path = self.blob_root / self._relative_path(reference.digest)
        try:
            data = path.read_bytes()
        except FileNotFoundError as error:
            raise ArtifactIntegrityError(
                f"artifact {reference.digest} metadata exists but its blob is missing"
            ) from error
        if verify:
            actual = bytes_digest(data)
            if actual != reference.digest or len(data) != reference.size_bytes:
                raise ArtifactIntegrityError(
                    f"artifact {reference.digest} failed hash or size verification"
                )
        return data

    def get_text(self, digest: str | ArtifactRef, *, encoding: str | None = None) -> str:
        reference = self.stat(digest)
        codec = encoding or reference.encoding or "utf-8"
        return self.get_bytes(reference).decode(codec)

    def get_json(self, digest: str | ArtifactRef) -> object:
        return json.loads(self.get_text(digest, encoding="utf-8"))

    def verify(self, digest: str | ArtifactRef) -> bool:
        self.get_bytes(digest, verify=True)
        return True

    def path_for(self, digest: str | ArtifactRef) -> Path:
        """Return the verified blob path for local consumers."""

        reference = self.stat(digest)
        path = self.blob_root / self._relative_path(reference.digest)
        self._verify_path(path, reference.digest, reference.size_bytes)
        return path

    @staticmethod
    def _relative_path(digest: str) -> Path:
        _validate_digest(digest)
        hexadecimal = digest.removeprefix("sha256:")
        return Path("sha256") / hexadecimal[:2] / hexadecimal[2:4] / hexadecimal

    @staticmethod
    def _write_once(path: Path, data: bytes, digest: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            ArtifactStore._verify_path(path, digest, len(data))
            return
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _verify_path(path: Path, digest: str, size: int) -> None:
        try:
            data = path.read_bytes()
        except FileNotFoundError as error:
            raise ArtifactIntegrityError(f"artifact blob {digest} is missing") from error
        if len(data) != size or bytes_digest(data) != digest:
            raise ArtifactIntegrityError(f"artifact blob {digest} does not match its address")


def _digest_of(value: str | ArtifactRef) -> str:
    digest = value.digest if isinstance(value, ArtifactRef) else value
    _validate_digest(digest)
    return digest


def _validate_digest(digest: str) -> None:
    hexadecimal = digest.removeprefix("sha256:")
    if not digest.startswith("sha256:") or len(hexadecimal) != 64:
        raise ValueError(f"invalid SHA-256 artifact digest: {digest!r}")
    try:
        int(hexadecimal, 16)
    except ValueError as error:
        raise ValueError(f"invalid SHA-256 artifact digest: {digest!r}") from error


def _artifact_from_row(row: sqlite3.Row) -> ArtifactRef:
    return ArtifactRef(
        digest=str(row["digest"]),
        size_bytes=int(row["size_bytes"]),
        media_type=str(row["media_type"]),
        encoding=None if row["encoding"] is None else str(row["encoding"]),
        created_at=datetime.fromisoformat(str(row["created_at"])),
    )
