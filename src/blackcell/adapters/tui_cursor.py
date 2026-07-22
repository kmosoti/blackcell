"""Atomic owner-only file storage for disposable TUI projection cursors."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from blackcell.interfaces.tui.cursor import (
    ALPHA_TUI_CURSOR_SCHEMA,
    AlphaTuiCursorCheckpoint,
    AlphaTuiCursorError,
    AlphaTuiCursorFailureCode,
    AlphaTuiCursorWitness,
)

_MAX_CHECKPOINT_BYTES = 4_096
_ROOT_KEYS = frozenset({"schema_version", "endpoint_id", "cursor", "witness"})
_WITNESS_KEYS = frozenset({"cursor", "event_id", "payload_digest"})


@dataclass(frozen=True, slots=True)
class FileAlphaTuiCursorStore:
    root: Path
    expected_uid: int

    @classmethod
    def prepare(
        cls,
        root: Path,
        *,
        expected_uid: int | None = None,
    ) -> FileAlphaTuiCursorStore:
        uid = _current_uid() if expected_uid is None else expected_uid
        _prepare_root(root, expected_uid=uid)
        return cls(root=root, expected_uid=uid)

    def load(self, endpoint_id: str) -> AlphaTuiCursorCheckpoint:
        empty = AlphaTuiCursorCheckpoint(endpoint_id=endpoint_id, cursor=0, witness=None)
        path = self._path(endpoint_id)
        try:
            before = path.lstat()
        except FileNotFoundError:
            return empty
        except OSError as error:
            raise AlphaTuiCursorError(AlphaTuiCursorFailureCode.UNSAFE_STATE_FILE) from error
        if stat.S_ISLNK(before.st_mode):
            raise AlphaTuiCursorError(AlphaTuiCursorFailureCode.UNSAFE_STATE_FILE)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as error:
            raise AlphaTuiCursorError(AlphaTuiCursorFailureCode.UNSAFE_STATE_FILE) from error
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != self.expected_uid
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_dev != before.st_dev
                or metadata.st_ino != before.st_ino
                or not 1 <= metadata.st_size <= _MAX_CHECKPOINT_BYTES
            ):
                raise AlphaTuiCursorError(AlphaTuiCursorFailureCode.UNSAFE_STATE_FILE)
            content = _read_bounded(descriptor)
        finally:
            os.close(descriptor)
        checkpoint = _decode_checkpoint(content)
        if checkpoint.endpoint_id != endpoint_id:
            raise AlphaTuiCursorError(AlphaTuiCursorFailureCode.ENDPOINT_MISMATCH)
        return checkpoint

    def save(self, checkpoint: AlphaTuiCursorCheckpoint) -> None:
        if not isinstance(checkpoint, AlphaTuiCursorCheckpoint):
            raise AlphaTuiCursorError(AlphaTuiCursorFailureCode.INVALID_CHECKPOINT)
        current = self.load(checkpoint.endpoint_id)
        if current.cursor > checkpoint.cursor:
            raise AlphaTuiCursorError(AlphaTuiCursorFailureCode.CURSOR_REGRESSION)
        if current.cursor == checkpoint.cursor:
            if current == checkpoint:
                return
            raise AlphaTuiCursorError(AlphaTuiCursorFailureCode.INVALID_CHECKPOINT)
        content = _encode_checkpoint(checkpoint)
        target = self._path(checkpoint.endpoint_id)
        descriptor = -1
        temporary = ""
        try:
            descriptor, temporary = tempfile.mkstemp(
                prefix=f".{checkpoint.endpoint_id}.",
                suffix=".tmp",
                dir=self.root,
            )
            os.fchmod(descriptor, 0o600)
            _write_all(descriptor, content)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.replace(temporary, target)
            temporary = ""
            _sync_directory(self.root)
        except OSError as error:
            raise AlphaTuiCursorError(AlphaTuiCursorFailureCode.IO_FAILED) from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary:
                with suppress(OSError):
                    os.unlink(temporary)

    def _path(self, endpoint_id: str) -> Path:
        AlphaTuiCursorCheckpoint(endpoint_id=endpoint_id, cursor=0, witness=None)
        return self.root / f"{endpoint_id}.json"


def _prepare_root(root: Path, *, expected_uid: int) -> None:
    if not isinstance(root, Path) or not root.is_absolute() or ".." in root.parts:
        raise AlphaTuiCursorError(AlphaTuiCursorFailureCode.UNSAFE_STATE_DIRECTORY)
    try:
        parent = root.parent
        parent_metadata = parent.lstat()
        if (
            stat.S_ISLNK(parent_metadata.st_mode)
            or not stat.S_ISDIR(parent_metadata.st_mode)
            or parent.resolve(strict=True) != parent
            or parent_metadata.st_mode & 0o022
        ):
            raise AlphaTuiCursorError(AlphaTuiCursorFailureCode.UNSAFE_STATE_DIRECTORY)
        try:
            metadata = root.lstat()
        except FileNotFoundError:
            root.mkdir(mode=0o700, parents=False, exist_ok=False)
            os.chmod(root, 0o700)
            metadata = root.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != expected_uid
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or root.resolve(strict=True) != root
        ):
            raise AlphaTuiCursorError(AlphaTuiCursorFailureCode.UNSAFE_STATE_DIRECTORY)
    except AlphaTuiCursorError:
        raise
    except OSError as error:
        raise AlphaTuiCursorError(AlphaTuiCursorFailureCode.UNSAFE_STATE_DIRECTORY) from error


def _read_bounded(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    remaining = _MAX_CHECKPOINT_BYTES + 1
    while remaining:
        chunk = os.read(descriptor, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    content = b"".join(chunks)
    if not 1 <= len(content) <= _MAX_CHECKPOINT_BYTES:
        raise AlphaTuiCursorError(AlphaTuiCursorFailureCode.UNSAFE_STATE_FILE)
    return content


def _decode_checkpoint(content: bytes) -> AlphaTuiCursorCheckpoint:
    try:
        value = json.loads(content)
        if not isinstance(value, dict) or frozenset(value) != _ROOT_KEYS:
            raise ValueError
        witness_value = value["witness"]
        witness = None
        if witness_value is not None:
            if not isinstance(witness_value, dict) or frozenset(witness_value) != _WITNESS_KEYS:
                raise ValueError
            witness = AlphaTuiCursorWitness(
                cursor=_integer(witness_value["cursor"]),
                event_id=_text(witness_value["event_id"]),
                payload_digest=_text(witness_value["payload_digest"]),
            )
        if _text(value["schema_version"]) != ALPHA_TUI_CURSOR_SCHEMA:
            raise ValueError
        checkpoint = AlphaTuiCursorCheckpoint(
            endpoint_id=_text(value["endpoint_id"]),
            cursor=_integer(value["cursor"]),
            witness=witness,
        )
        if _encode_checkpoint(checkpoint) != content:
            raise ValueError
        return checkpoint
    except AlphaTuiCursorError:
        raise
    except (json.JSONDecodeError, KeyError, TypeError, UnicodeDecodeError, ValueError) as error:
        raise AlphaTuiCursorError(AlphaTuiCursorFailureCode.INVALID_CHECKPOINT) from error


def _encode_checkpoint(checkpoint: AlphaTuiCursorCheckpoint) -> bytes:
    witness: dict[str, object] | None = None
    if checkpoint.witness is not None:
        witness = {
            "cursor": checkpoint.witness.cursor,
            "event_id": checkpoint.witness.event_id,
            "payload_digest": checkpoint.witness.payload_digest,
        }
    document = {
        "cursor": checkpoint.cursor,
        "endpoint_id": checkpoint.endpoint_id,
        "schema_version": ALPHA_TUI_CURSOR_SCHEMA,
        "witness": witness,
    }
    content = json.dumps(document, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return f"{content}\n".encode("ascii")


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError
    return value


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError
    return value


def _write_all(descriptor: int, content: bytes) -> None:
    remaining = memoryview(content)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError
        remaining = remaining[written:]


def _sync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _current_uid() -> int:
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        raise AlphaTuiCursorError(AlphaTuiCursorFailureCode.UNSAFE_STATE_DIRECTORY)
    return cast("int", getuid())


__all__ = ["FileAlphaTuiCursorStore"]
