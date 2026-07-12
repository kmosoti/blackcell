from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from blackcell.adapters.persistence.sqlite import ArtifactContextFrameStore
from blackcell.features.build_context import (
    ContextClaimIdentity,
    ContextEvidence,
    ContextFrame,
    ContextFrameConflictError,
    ContextFrameIntegrityError,
    ContextFrameSchemaError,
    ContextOmission,
    ContextOmissionReason,
    ContextOmissionStage,
    serialize_context_evidence,
    serialize_context_frame,
)
from blackcell.features.retrieve_evidence import EvidenceOmission, EvidenceOmissionReason
from blackcell.kernel import ArtifactStore, JsonScalar
from blackcell.kernel._json import bytes_digest, canonical_json_bytes

NOW = datetime(2026, 7, 10, 17, tzinfo=UTC)
DOMAIN = "project"


def test_store_round_trips_complete_frame_lineage_and_exact_retry(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    frame = _frame("task:daily", value="blocked")
    assert (
        frame.frame_id == "sha256:28218eca1badbb54f7399de300eff66abe5384a25d83aab5f377dc0ede8ef40c"
    )

    with ArtifactContextFrameStore(root) as store:
        assert store.put(frame) == frame
        assert store.put(frame) == frame
        assert store.get(frame.frame_id) == frame
        assert len(store) == 1

    with ArtifactContextFrameStore(root) as reopened:
        assert reopened.put(frame) == frame
        loaded = reopened.get(frame.frame_id)
        assert reopened.get("sha256:" + "f" * 64) is None
        assert len(reopened) == 1

    assert loaded == frame
    assert loaded is not None
    assert loaded.schema_version == "context-frame/v3"
    assert loaded.state_domain == DOMAIN
    assert loaded.state_stream_id == _stream_id("task:daily")
    assert loaded.state_global_position == 20
    assert loaded.state_stream_position == 4
    assert tuple(item.claim_id for item in loaded.evidence) == (
        "claim:task:daily:1",
        "claim:task:daily:2",
    )
    assert tuple(item.stream_sequence for item in loaded.evidence) == (1, 2)
    assert tuple(item.global_position for item in loaded.evidence) == (10, 11)
    assert loaded.source_claim_identities == frame.source_claim_identities
    assert loaded.omissions == frame.omissions
    assert loaded.omitted_evidence_count == 2
    assert {item.stage for item in loaded.omissions} == {
        ContextOmissionStage.RETRIEVAL,
        ContextOmissionStage.CONTEXT_PROJECTION,
    }

    artifact = ArtifactStore(root)
    data = artifact.get_bytes(frame.frame_id)
    assert bytes_digest(data) == frame.frame_id
    assert json.loads(data)["schema_version"] == "context-frame/v3"
    with sqlite3.connect(root / "kernel.sqlite3") as connection:
        indexed = connection.execute(
            "select frame_id, schema_version, task_id from context_frame_index"
        ).fetchall()
        columns = {row[1] for row in connection.execute("pragma table_info(context_frame_index)")}
        artifacts = connection.execute(
            "select digest from kernel_artifacts where digest = ?",
            (frame.frame_id,),
        ).fetchall()
    assert indexed == [(frame.frame_id, "context-frame/v3", frame.task_id)]
    assert "payload_json" not in columns
    assert artifacts == [(frame.frame_id,)]


def test_store_rejects_same_identity_with_different_payload_before_writing(
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifacts"
    frame = _frame("task:daily", value="blocked")
    collision = replace(frame, objective="silently changed objective")
    object.__setattr__(collision, "frame_id", frame.frame_id)

    with ArtifactContextFrameStore(root) as store:
        store.put(frame)

        with pytest.raises(ContextFrameConflictError, match="canonical content"):
            store.put(collision)

        assert store.get(frame.frame_id) == frame
        assert len(store) == 1

    with sqlite3.connect(root / "kernel.sqlite3") as connection:
        artifact_count = connection.execute("select count(*) from kernel_artifacts").fetchone()[0]
    assert artifact_count == 1


def test_exact_retry_repairs_an_unindexed_artifact(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    frame = _frame("task:daily", value="blocked")
    artifact = ArtifactStore(root)
    reference = artifact.put_bytes(
        serialize_context_frame(frame).encode("utf-8"),
        media_type="application/vnd.blackcell.context-frame+json",
        encoding="utf-8",
    )
    assert reference.digest == frame.frame_id

    with ArtifactContextFrameStore(root) as store:
        assert store.get(frame.frame_id) is None
        assert store.put(frame) == frame
        assert store.get(frame.frame_id) == frame
        assert len(store) == 1


def test_store_rejects_corrupted_artifact_bytes(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    frame = _frame("task:daily", value="blocked")

    with ArtifactContextFrameStore(root) as store:
        store.put(frame)
        artifact_path = ArtifactStore(root).path_for(frame.frame_id)
        artifact_path.write_bytes(artifact_path.read_bytes() + b"\n")

        with pytest.raises(ContextFrameIntegrityError, match="missing or corrupt"):
            store.get(frame.frame_id)
        with pytest.raises(ContextFrameIntegrityError, match=r"artifact.*corrupt"):
            store.put(frame)


def test_store_reconstructs_scope_and_cutoff_invariants_from_artifact(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    frame = _frame("task:daily", value="blocked")
    with ArtifactContextFrameStore(root) as store:
        store.put(frame)
        artifacts = ArtifactStore(root)
        payload = json.loads(artifacts.get_text(frame.frame_id))
        # Keep the model-payload character count stable while moving one claim
        # outside the declared frame domain.
        payload["evidence"][0]["domain"] = "foreign"
        data = canonical_json_bytes(payload)
        forged = artifacts.put_bytes(
            data,
            media_type="application/vnd.blackcell.context-frame+json",
            encoding="utf-8",
        )
        _insert_index(
            store.database_path,
            forged.digest,
            schema_version=payload["schema_version"],
            task_id=payload["task_id"],
            generated_at=payload["generated_at"],
        )

        with pytest.raises(ContextFrameIntegrityError, match="ContextFrame contract"):
            store.get(forged.digest)


def test_store_rejects_unsupported_frame_artifact_schema(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    frame = _frame("task:daily", value="blocked")
    with ArtifactContextFrameStore(root) as store:
        payload = json.loads(serialize_context_frame(frame))
        payload["schema_version"] = "context-frame/v99"
        artifact = ArtifactStore(root).put_bytes(
            canonical_json_bytes(payload),
            media_type="application/vnd.blackcell.context-frame+json",
            encoding="utf-8",
        )
        _insert_index(
            store.database_path,
            artifact.digest,
            schema_version=payload["schema_version"],
            task_id=payload["task_id"],
            generated_at=payload["generated_at"],
        )

        with pytest.raises(ContextFrameSchemaError, match="unsupported ContextFrame schema"):
            store.get(artifact.digest)


def test_v3_decoder_rejects_v4_fields_without_changing_the_frozen_digest(
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifacts"
    frame = _frame("task:daily", value="blocked")
    assert (
        frame.frame_id == "sha256:28218eca1badbb54f7399de300eff66abe5384a25d83aab5f377dc0ede8ef40c"
    )

    with ArtifactContextFrameStore(root) as store:
        payload = json.loads(serialize_context_frame(frame))
        payload["state_effective_time"] = None
        artifact = ArtifactStore(root).put_bytes(
            canonical_json_bytes(payload),
            media_type="application/vnd.blackcell.context-frame+json",
            encoding="utf-8",
        )
        _insert_index(
            store.database_path,
            artifact.digest,
            schema_version="context-frame/v3",
            task_id=frame.task_id,
            generated_at=frame.generated_at.isoformat(),
        )

        with pytest.raises(ContextFrameIntegrityError, match="fields do not match"):
            store.get(artifact.digest)


def test_store_lists_bound_and_unbound_frames_deterministically(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    frames = (
        _frame("task:z", value=True, generated_at=NOW + timedelta(seconds=2)),
        _frame("task:a", value=False, generated_at=NOW + timedelta(seconds=1)),
        _unbound_frame("task:empty"),
    )

    with ArtifactContextFrameStore(root) as store:
        for frame in reversed(frames):
            store.put(frame)

        listed = store.list_frames()

    assert tuple(frame.frame_id for frame in listed) == tuple(
        sorted(frame.frame_id for frame in frames)
    )
    assert next(frame for frame in listed if frame.task_id == "task:empty").state_stream_id is None


def test_store_context_manager_closes_and_close_is_idempotent(tmp_path: Path) -> None:
    store = ArtifactContextFrameStore(tmp_path / "artifacts")
    frame = _frame("task:daily", value="blocked")
    with store:
        store.put(frame)

    store.close()
    with pytest.raises(RuntimeError, match="is closed"):
        store.get(frame.frame_id)
    with pytest.raises(RuntimeError, match="is closed"):
        store.list_frames()


def test_newer_index_schema_is_rejected_before_index_ddl(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    store = ArtifactContextFrameStore(root)
    database_path = store.database_path
    store.close()
    with sqlite3.connect(database_path) as connection:
        connection.execute("drop table context_frame_index")
        connection.execute(
            """
            insert into context_frame_index_schema_migrations(version, applied_at)
            values (2, '2026-07-10T17:00:00+00:00')
            """
        )

    with pytest.raises(ContextFrameSchemaError, match="newer than supported"):
        ArtifactContextFrameStore(root)

    with sqlite3.connect(database_path) as connection:
        recreated = connection.execute(
            "select 1 from sqlite_master where type = 'table' and name = 'context_frame_index'"
        ).fetchone()
    assert recreated is None


def _frame(
    task_id: str,
    *,
    value: JsonScalar,
    generated_at: datetime = NOW,
) -> ContextFrame:
    evidence = (
        _evidence(task_id, 1, "status", value, global_position=10),
        _evidence(task_id, 2, "owner", "kennedy", global_position=11),
    )
    omissions = _omissions(task_id)
    identities = tuple(
        sorted(
            ContextClaimIdentity(item.source_event_id, item.claim_id)
            for item in (*evidence, *omissions)
        )
    )
    model_payload = "\n".join(serialize_context_evidence(item) for item in evidence)
    return ContextFrame(
        task_id=task_id,
        objective="inspect current project status",
        generated_at=generated_at,
        source_packet_id=f"packet:{task_id}",
        source_packet_purpose="daily-operator",
        source_selection_id=f"selection:{task_id}",
        state_domain=DOMAIN,
        state_stream_id=_stream_id(task_id),
        state_global_position=20,
        state_stream_position=4,
        source_claim_identities=identities,
        evidence=evidence,
        provenance_event_ids=tuple(item.source_event_id for item in evidence),
        omissions=omissions,
        model_payload_characters=len(model_payload),
    )


def _unbound_frame(task_id: str) -> ContextFrame:
    return ContextFrame(
        task_id=task_id,
        objective="inspect empty state",
        generated_at=NOW,
        source_packet_id=f"packet:{task_id}",
        source_packet_purpose="daily-operator",
        source_selection_id=f"selection:{task_id}",
        state_domain=DOMAIN,
        state_stream_id=None,
        state_global_position=20,
        state_stream_position=0,
        source_claim_identities=(),
        evidence=(),
        provenance_event_ids=(),
        omissions=(),
        model_payload_characters=0,
    )


def _evidence(
    task_id: str,
    sequence: int,
    predicate: str,
    value: JsonScalar,
    *,
    global_position: int,
) -> ContextEvidence:
    return ContextEvidence(
        claim_id=f"claim:{task_id}:{sequence}",
        subject="project:blackcell",
        predicate=predicate,
        value=value,
        confidence=0.9,
        effective_at=NOW - timedelta(seconds=30),
        freshness_seconds=30,
        stale=False,
        source_event_id=f"event:{task_id}:{sequence}",
        domain=DOMAIN,
        stream_id=_stream_id(task_id),
        stream_sequence=sequence,
        global_position=global_position,
        relevance_score=110,
        selection_reasons=("objective-overlap",),
        conflicted=False,
    )


def _omissions(task_id: str) -> tuple[ContextOmission, ...]:
    source = EvidenceOmission(
        claim_id=f"claim:{task_id}:3",
        subject="project:blackcell",
        predicate="priority",
        value="low",
        confidence=0.8,
        effective_at=NOW - timedelta(seconds=60),
        freshness_seconds=60,
        stale=False,
        source_event_id=f"event:{task_id}:3",
        domain=DOMAIN,
        stream_id=_stream_id(task_id),
        stream_sequence=3,
        global_position=12,
        score=0,
        reasons=(),
        conflicted=False,
        reason=EvidenceOmissionReason.IRRELEVANT,
    )
    return (
        ContextOmission(
            claim_id=source.claim_id,
            subject=source.subject,
            predicate=source.predicate,
            value=source.value,
            confidence=source.confidence,
            effective_at=source.effective_at,
            freshness_seconds=source.freshness_seconds,
            stale=source.stale,
            source_event_id=source.source_event_id,
            domain=source.domain,
            stream_id=source.stream_id,
            stream_sequence=source.stream_sequence,
            global_position=source.global_position,
            relevance_score=source.score,
            selection_reasons=source.reasons,
            conflicted=source.conflicted,
            stage=ContextOmissionStage.RETRIEVAL,
            reason=ContextOmissionReason.IRRELEVANT,
            source_omission_id=source.omission_id,
            source_omission_schema_version=source.schema_version,
        ),
        ContextOmission(
            claim_id=f"claim:{task_id}:4",
            subject="project:blackcell",
            predicate="reviewer",
            value="kennedy",
            confidence=0.8,
            effective_at=NOW - timedelta(seconds=60),
            freshness_seconds=60,
            stale=False,
            source_event_id=f"event:{task_id}:4",
            domain=DOMAIN,
            stream_id=_stream_id(task_id),
            stream_sequence=4,
            global_position=13,
            relevance_score=100,
            selection_reasons=("objective-overlap",),
            conflicted=False,
            stage=ContextOmissionStage.CONTEXT_PROJECTION,
            reason=ContextOmissionReason.CHARACTER_BUDGET,
            model_payload_characters=321,
        ),
    )


def _stream_id(task_id: str) -> str:
    return f"observations:{task_id}"


def _insert_index(
    database_path: Path,
    frame_id: str,
    *,
    schema_version: object,
    task_id: object,
    generated_at: object,
) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            insert into context_frame_index(frame_id, schema_version, task_id, generated_at)
            values (?, ?, ?, ?)
            """,
            (frame_id, schema_version, task_id, generated_at),
        )
