from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Self, cast

from blackcell.features.build_context.models import (
    ContextClaimIdentity,
    ContextEvidence,
    ContextFrame,
    ContextOmission,
    ContextOmissionReason,
    ContextOmissionStage,
    serialize_context_frame,
)
from blackcell.features.build_context.storage import (
    ContextFrameConflictError,
    ContextFrameIntegrityError,
    ContextFrameSchemaError,
)
from blackcell.kernel import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactStore,
    JsonScalar,
)
from blackcell.kernel._json import bytes_digest, canonical_json_bytes
from blackcell.kernel.database import connect

_INDEX_SCHEMA_VERSION = 1
_FRAME_SCHEMA_VERSION = "context-frame/v3"
_OMISSION_SCHEMA_VERSION = "context-omission/v2"
_MEDIA_TYPE = "application/vnd.blackcell.context-frame+json"
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

_FRAME_KEYS = frozenset(
    {
        "schema_version",
        "task_id",
        "objective",
        "generated_at",
        "source_packet_id",
        "source_packet_purpose",
        "source_selection_id",
        "state_domain",
        "state_stream_id",
        "state_global_position",
        "state_stream_position",
        "source_claim_identities",
        "evidence",
        "provenance_event_ids",
        "omissions",
        "model_payload_characters",
    }
)
_CLAIM_IDENTITY_KEYS = frozenset({"source_event_id", "claim_id"})
_EVIDENCE_KEYS = frozenset(
    {
        "claim_id",
        "subject",
        "predicate",
        "value",
        "confidence",
        "effective_at",
        "freshness_seconds",
        "stale",
        "source_event_id",
        "domain",
        "stream_id",
        "stream_sequence",
        "global_position",
        "relevance_score",
        "selection_reasons",
        "conflicted",
    }
)
_OMISSION_KEYS = frozenset(
    {
        "schema_version",
        "claim_id",
        "subject",
        "predicate",
        "value",
        "confidence",
        "effective_at",
        "freshness_seconds",
        "stale",
        "source_event_id",
        "domain",
        "stream_id",
        "stream_sequence",
        "global_position",
        "relevance_score",
        "selection_reasons",
        "conflicted",
        "stage",
        "reason",
        "model_payload_characters",
        "source_omission_id",
        "source_omission_schema_version",
    }
)


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
        data = serialize_context_frame(frame).encode("utf-8")
        actual_frame_id = bytes_digest(data)
        if actual_frame_id != frame.frame_id:
            raise ContextFrameConflictError(
                f"ContextFrame {frame.frame_id!r} does not identify its canonical content"
            )

        try:
            reference = self._artifacts.put_bytes(
                data,
                media_type=_MEDIA_TYPE,
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
        try:
            payload = json.loads(data.decode("utf-8"))
            canonical = canonical_json_bytes(payload)
        except (TypeError, ValueError, UnicodeDecodeError) as error:
            raise ContextFrameIntegrityError(
                f"ContextFrame artifact {frame_id!r} is not canonical JSON"
            ) from error
        if canonical != data:
            raise ContextFrameIntegrityError(
                f"ContextFrame artifact {frame_id!r} is not canonical JSON"
            )
        frame = _decode_frame(payload, expected_frame_id=frame_id)
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


def _decode_frame(value: object, *, expected_frame_id: str) -> ContextFrame:
    payload = _require_mapping(value, keys=_FRAME_KEYS, label="ContextFrame")
    schema_version = _require_string(payload["schema_version"], label="schema_version")
    if schema_version != _FRAME_SCHEMA_VERSION:
        raise ContextFrameSchemaError(
            f"unsupported ContextFrame schema {schema_version!r}; "
            f"expected {_FRAME_SCHEMA_VERSION!r}"
        )
    generated_at = _require_datetime(payload["generated_at"], label="generated_at")
    state_stream_id = _require_optional_string(payload["state_stream_id"], label="state_stream_id")
    raw_identities = payload["source_claim_identities"]
    if not isinstance(raw_identities, list):
        raise ContextFrameIntegrityError("source_claim_identities must be a JSON array")
    identities = tuple(
        _decode_claim_identity(item, index=index) for index, item in enumerate(raw_identities)
    )
    raw_evidence = payload["evidence"]
    if not isinstance(raw_evidence, list):
        raise ContextFrameIntegrityError("ContextFrame evidence must be a JSON array")
    evidence = tuple(_decode_evidence(item, index=index) for index, item in enumerate(raw_evidence))
    raw_omissions = payload["omissions"]
    if not isinstance(raw_omissions, list):
        raise ContextFrameIntegrityError("ContextFrame omissions must be a JSON array")
    omissions = tuple(
        _decode_omission(item, index=index) for index, item in enumerate(raw_omissions)
    )
    provenance_event_ids = _require_string_tuple(
        payload["provenance_event_ids"], label="provenance_event_ids"
    )
    try:
        frame = ContextFrame(
            task_id=_require_string(payload["task_id"], label="task_id"),
            objective=_require_string(payload["objective"], label="objective"),
            generated_at=generated_at,
            source_packet_id=_require_string(payload["source_packet_id"], label="source_packet_id"),
            source_packet_purpose=_require_string(
                payload["source_packet_purpose"], label="source_packet_purpose"
            ),
            source_selection_id=_require_string(
                payload["source_selection_id"], label="source_selection_id"
            ),
            state_domain=_require_string(payload["state_domain"], label="state_domain"),
            state_stream_id=state_stream_id,
            state_global_position=_require_non_negative_int(
                payload["state_global_position"], label="state_global_position"
            ),
            state_stream_position=_require_non_negative_int(
                payload["state_stream_position"], label="state_stream_position"
            ),
            source_claim_identities=identities,
            evidence=evidence,
            provenance_event_ids=provenance_event_ids,
            omissions=omissions,
            model_payload_characters=_require_non_negative_int(
                payload["model_payload_characters"], label="model_payload_characters"
            ),
            schema_version=schema_version,
        )
    except ValueError as error:
        raise ContextFrameIntegrityError("payload violates the ContextFrame contract") from error
    if frame.frame_id != expected_frame_id:
        raise ContextFrameIntegrityError(
            f"ContextFrame digest mismatch: expected {expected_frame_id!r}, got {frame.frame_id!r}"
        )
    return frame


def _decode_claim_identity(value: object, *, index: int) -> ContextClaimIdentity:
    label = f"source_claim_identities[{index}]"
    payload = _require_mapping(value, keys=_CLAIM_IDENTITY_KEYS, label=label)
    try:
        return ContextClaimIdentity(
            source_event_id=_require_string(
                payload["source_event_id"], label=f"{label} source_event_id"
            ),
            claim_id=_require_string(payload["claim_id"], label=f"{label} claim_id"),
        )
    except ValueError as error:
        raise ContextFrameIntegrityError(f"{label} violates its contract") from error


def _decode_evidence(value: object, *, index: int) -> ContextEvidence:
    label = f"ContextFrame evidence[{index}]"
    payload = _require_mapping(value, keys=_EVIDENCE_KEYS, label=label)
    confidence = _require_confidence(payload["confidence"], label=f"{label} confidence")
    return ContextEvidence(
        claim_id=_require_string(payload["claim_id"], label=f"{label} claim_id"),
        subject=_require_string(payload["subject"], label=f"{label} subject"),
        predicate=_require_string(payload["predicate"], label=f"{label} predicate"),
        value=_require_json_scalar(payload["value"], label=f"{label} value"),
        confidence=confidence,
        effective_at=_require_datetime(payload["effective_at"], label=f"{label} effective_at"),
        freshness_seconds=_require_non_negative_int(
            payload["freshness_seconds"], label=f"{label} freshness_seconds"
        ),
        stale=_require_bool(payload["stale"], label=f"{label} stale"),
        source_event_id=_require_string(
            payload["source_event_id"], label=f"{label} source_event_id"
        ),
        domain=_require_string(payload["domain"], label=f"{label} domain"),
        stream_id=_require_string(payload["stream_id"], label=f"{label} stream_id"),
        stream_sequence=_require_positive_int(
            payload["stream_sequence"], label=f"{label} stream_sequence"
        ),
        global_position=_require_positive_int(
            payload["global_position"], label=f"{label} global_position"
        ),
        relevance_score=_require_int(payload["relevance_score"], label=f"{label} relevance_score"),
        selection_reasons=_require_string_tuple(
            payload["selection_reasons"], label=f"{label} selection_reasons"
        ),
        conflicted=_require_bool(payload["conflicted"], label=f"{label} conflicted"),
    )


def _decode_omission(value: object, *, index: int) -> ContextOmission:
    label = f"ContextFrame omission[{index}]"
    payload = _require_mapping(value, keys=_OMISSION_KEYS, label=label)
    schema_version = _require_string(payload["schema_version"], label=f"{label} schema_version")
    if schema_version != _OMISSION_SCHEMA_VERSION:
        raise ContextFrameSchemaError(
            f"unsupported ContextOmission schema {schema_version!r}; "
            f"expected {_OMISSION_SCHEMA_VERSION!r}"
        )
    try:
        return ContextOmission(
            claim_id=_require_string(payload["claim_id"], label=f"{label} claim_id"),
            subject=_require_string(payload["subject"], label=f"{label} subject"),
            predicate=_require_string(payload["predicate"], label=f"{label} predicate"),
            value=_require_json_scalar(payload["value"], label=f"{label} value"),
            confidence=_require_confidence(payload["confidence"], label=f"{label} confidence"),
            effective_at=_require_datetime(payload["effective_at"], label=f"{label} effective_at"),
            freshness_seconds=_require_non_negative_int(
                payload["freshness_seconds"], label=f"{label} freshness_seconds"
            ),
            stale=_require_bool(payload["stale"], label=f"{label} stale"),
            source_event_id=_require_string(
                payload["source_event_id"], label=f"{label} source_event_id"
            ),
            domain=_require_string(payload["domain"], label=f"{label} domain"),
            stream_id=_require_string(payload["stream_id"], label=f"{label} stream_id"),
            stream_sequence=_require_positive_int(
                payload["stream_sequence"], label=f"{label} stream_sequence"
            ),
            global_position=_require_positive_int(
                payload["global_position"], label=f"{label} global_position"
            ),
            relevance_score=_require_int(
                payload["relevance_score"], label=f"{label} relevance_score"
            ),
            selection_reasons=_require_string_tuple(
                payload["selection_reasons"], label=f"{label} selection_reasons"
            ),
            conflicted=_require_bool(payload["conflicted"], label=f"{label} conflicted"),
            stage=_require_enum(ContextOmissionStage, payload["stage"], label=f"{label} stage"),
            reason=_require_enum(ContextOmissionReason, payload["reason"], label=f"{label} reason"),
            model_payload_characters=_require_optional_int(
                payload["model_payload_characters"],
                label=f"{label} model_payload_characters",
            ),
            source_omission_id=_require_optional_string(
                payload["source_omission_id"], label=f"{label} source_omission_id"
            ),
            source_omission_schema_version=_require_optional_string(
                payload["source_omission_schema_version"],
                label=f"{label} source_omission_schema_version",
            ),
            schema_version=schema_version,
        )
    except ValueError as error:
        raise ContextFrameIntegrityError(
            f"{label} violates the ContextOmission contract"
        ) from error


def _require_mapping(
    value: object,
    *,
    keys: frozenset[str],
    label: str,
) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ContextFrameIntegrityError(f"{label} must be a JSON object")
    actual = frozenset(value)
    if actual != keys:
        missing = sorted(keys - actual)
        unexpected = sorted(actual - keys)
        raise ContextFrameIntegrityError(
            f"{label} fields do not match its schema; missing={missing}, unexpected={unexpected}"
        )
    return cast("Mapping[str, object]", value)


def _require_string(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContextFrameIntegrityError(f"{label} must be a non-empty string")
    return value


def _require_optional_string(value: object, *, label: str) -> str | None:
    if value is None:
        return None
    return _require_string(value, label=label)


def _require_string_tuple(value: object, *, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ContextFrameIntegrityError(f"{label} must be a JSON array")
    return tuple(_require_string(item, label=f"{label} item") for item in value)


def _require_datetime(value: object, *, label: str) -> datetime:
    text = _require_string(value, label=label)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise ContextFrameIntegrityError(f"{label} must be an ISO 8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContextFrameIntegrityError(f"{label} must be timezone-aware")
    if parsed.isoformat() != text:
        raise ContextFrameIntegrityError(f"{label} must use canonical ISO 8601 formatting")
    return parsed


def _require_int(value: object, *, label: str) -> int:
    if type(value) is not int:
        raise ContextFrameIntegrityError(f"{label} must be an integer")
    return value


def _require_non_negative_int(value: object, *, label: str) -> int:
    parsed = _require_int(value, label=label)
    if parsed < 0:
        raise ContextFrameIntegrityError(f"{label} must be non-negative")
    return parsed


def _require_positive_int(value: object, *, label: str) -> int:
    parsed = _require_int(value, label=label)
    if parsed < 1:
        raise ContextFrameIntegrityError(f"{label} must be positive")
    return parsed


def _require_optional_int(value: object, *, label: str) -> int | None:
    if value is None:
        return None
    return _require_int(value, label=label)


def _require_bool(value: object, *, label: str) -> bool:
    if type(value) is not bool:
        raise ContextFrameIntegrityError(f"{label} must be a boolean")
    return value


def _require_confidence(value: object, *, label: str) -> float:
    if type(value) is not float or not 0.0 <= value <= 1.0:
        raise ContextFrameIntegrityError(f"{label} must be a float from zero to one")
    return value


def _require_enum[EnumT](enum_type: type[EnumT], value: object, *, label: str) -> EnumT:
    text = _require_string(value, label=label)
    try:
        return enum_type(text)
    except (TypeError, ValueError) as error:
        raise ContextFrameIntegrityError(f"{label} is not recognized") from error


def _require_json_scalar(value: object, *, label: str) -> JsonScalar:
    if value is None or type(value) in (bool, int, float, str):
        try:
            canonical_json_bytes({"value": value})
        except (TypeError, ValueError) as error:
            raise ContextFrameIntegrityError(f"{label} must be a finite JSON scalar") from error
        return cast("JsonScalar", value)
    raise ContextFrameIntegrityError(f"{label} must be a JSON scalar")


def _validate_digest(value: str, *, label: str) -> None:
    hexadecimal = value.removeprefix("sha256:")
    if not value.startswith("sha256:") or len(hexadecimal) != 64:
        raise ContextFrameIntegrityError(f"{label} is not a SHA-256 digest")
    try:
        int(hexadecimal, 16)
    except ValueError as error:
        raise ContextFrameIntegrityError(f"{label} is not a SHA-256 digest") from error
