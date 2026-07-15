from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from blackcell.features.ingest_observation import (
    CorrectionInput,
    EvidencePointer,
    IngestCorrection,
    IngestCorrectionHandler,
    IngestObservation,
    IngestObservationHandler,
    ObservationInput,
    ObservedClaim,
)
from blackcell.features.project_operational_state import (
    EpistemicStatus,
    OperationalStateFold,
    OperationalStateProjector,
    OperationalStateScope,
    ProjectOperationalState,
    ProjectOperationalStateHandler,
    UnknownReason,
)
from blackcell.kernel import CheckpointStore, EventEnvelope, EventStore, ProjectionRunner

NOW = datetime(2026, 7, 11, 15, tzinfo=UTC)
STREAM_ID = "observations:expiry"
SCOPE = OperationalStateScope("repository", STREAM_ID)


def test_optional_expiry_preserves_old_event_payload_and_validates_time(
    tmp_path: Path,
) -> None:
    no_expiry = _observation_input("claim:plain", "ready", effective_at=NOW)
    expiring = _observation_input(
        "claim:expiring",
        "running",
        effective_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )
    store = EventStore(tmp_path / "kernel.sqlite3")
    events = IngestObservationHandler(store, clock=lambda: NOW).handle(
        IngestObservation(
            STREAM_ID,
            0,
            "operator",
            "fixture",
            "run:expiry",
            (no_expiry, expiring),
        )
    )
    plain_claims = events[0].payload["claims"]
    expiring_claims = events[1].payload["claims"]
    assert isinstance(plain_claims, tuple)
    assert isinstance(expiring_claims, tuple)
    plain_payload = cast("Mapping[str, object]", plain_claims[0])
    expiring_payload = cast("Mapping[str, object]", expiring_claims[0])

    assert "expires_at" not in plain_payload
    assert expiring_payload["expires_at"] == (NOW + timedelta(hours=1)).isoformat()

    with pytest.raises(ValueError, match="timezone-aware"):
        ObservedClaim(
            "claim:bad",
            "project:blackcell",
            "status",
            "ready",
            expires_at=NOW.replace(tzinfo=None),
        )
    with pytest.raises(ValueError, match="cannot precede"):
        _observation_input(
            "claim:bad",
            "ready",
            effective_at=NOW,
            expires_at=NOW - timedelta(seconds=1),
        )
    with pytest.raises(ValueError, match="replacement expires_at"):
        CorrectionInput(
            "correction:bad-expiry",
            NOW + timedelta(hours=1),
            ("claim:plain",),
            ObservedClaim(
                "claim:replacement",
                "project:blackcell",
                "status",
                "blocked",
                expires_at=NOW,
            ),
            "invalid expiry",
            (EvidencePointer(locator="fixture://correction"),),
        )


def test_expired_latest_value_becomes_unknown_without_reviving_older_value(
    tmp_path: Path,
) -> None:
    store = EventStore(tmp_path / "kernel.sqlite3")
    handler = IngestObservationHandler(store, clock=lambda: NOW)
    handler.handle(
        IngestObservation(
            STREAM_ID,
            0,
            "operator",
            "repository-scan",
            "run:expiry",
            (
                _observation_input("claim:old", "ready", effective_at=NOW),
                _observation_input(
                    "claim:new",
                    "blocked",
                    effective_at=NOW + timedelta(minutes=1),
                    expires_at=NOW + timedelta(hours=1),
                ),
            ),
        )
    )
    projector = OperationalStateProjector()

    before = projector.replay(
        store.read_all(),
        scope=SCOPE,
        as_of_time=NOW + timedelta(minutes=30),
    )
    expired = projector.replay(
        store.read_all(),
        scope=SCOPE,
        as_of_time=NOW + timedelta(hours=1),
    )
    compatibility = projector.replay(store.read_all(), scope=SCOPE)

    assert tuple(claim.value for claim in before.claims) == ("blocked",)
    assert tuple(claim.value for claim in compatibility.claims) == ("blocked",)
    assert len(expired.claims) == 1
    unknown = expired.claims[0]
    assert unknown.claim_id == "claim:new"
    assert unknown.value is None
    assert unknown.confidence == 0.0
    assert unknown.epistemic_status is EpistemicStatus.UNKNOWN
    assert unknown.unknown_reason is UnknownReason.EXPIRED
    assert unknown.source_event_id == expired.expired_claims[0].source_event_id
    assert expired.expired_claims[0].value == "blocked"
    assert "ready" not in {claim.value for claim in expired.claims}
    assert expired.conflicts == ()


def test_expired_source_is_unknown_while_independent_source_remains_observed(
    tmp_path: Path,
) -> None:
    store = EventStore(tmp_path / "kernel.sqlite3")
    IngestObservationHandler(store, clock=lambda: NOW).handle(
        IngestObservation(
            STREAM_ID,
            0,
            "operator",
            "source:a",
            "run:expiry",
            (
                _observation_input(
                    "claim:a",
                    "blocked",
                    effective_at=NOW,
                    expires_at=NOW + timedelta(minutes=5),
                ),
            ),
        )
    )
    IngestObservationHandler(store, clock=lambda: NOW).handle(
        IngestObservation(
            STREAM_ID,
            1,
            "operator",
            "source:b",
            "run:expiry",
            (_observation_input("claim:b", "ready", effective_at=NOW),),
        )
    )

    state = OperationalStateProjector().replay(
        store.read_all(),
        scope=SCOPE,
        as_of_time=NOW + timedelta(minutes=5),
    )

    assert {(claim.source, claim.epistemic_status, claim.value) for claim in state.claims} == {
        ("source:a", EpistemicStatus.UNKNOWN, None),
        ("source:b", EpistemicStatus.OBSERVED, "ready"),
    }
    assert state.conflicts == ()


def test_expired_correction_replacement_does_not_restore_superseded_fact(
    tmp_path: Path,
) -> None:
    store = EventStore(tmp_path / "kernel.sqlite3")
    IngestObservationHandler(store, clock=lambda: NOW).handle(
        IngestObservation(
            STREAM_ID,
            0,
            "operator",
            "repository-scan",
            "run:expiry",
            (_observation_input("claim:original", "ready", effective_at=NOW),),
        )
    )
    replacement = ObservedClaim(
        "claim:replacement",
        "project:blackcell",
        "status",
        "blocked",
        expires_at=NOW + timedelta(hours=2),
    )
    IngestCorrectionHandler(store, clock=lambda: NOW).handle(
        IngestCorrection(
            STREAM_ID,
            1,
            "operator",
            "operator-correction",
            "run:expiry",
            (
                CorrectionInput(
                    "correction:1",
                    NOW + timedelta(hours=1),
                    ("claim:original",),
                    replacement,
                    "authoritative correction",
                    (EvidencePointer(locator="fixture://correction"),),
                ),
            ),
        )
    )

    state = OperationalStateProjector().replay(
        store.read_all(),
        scope=SCOPE,
        as_of_time=NOW + timedelta(hours=3),
    )

    assert tuple(claim.claim_id for claim in state.claims) == ("claim:replacement",)
    assert state.claims[0].epistemic_status is EpistemicStatus.UNKNOWN
    assert tuple(claim.claim_id for claim in state.superseded_claims) == ("claim:original",)
    assert tuple(claim.claim_id for claim in state.expired_claims) == ("claim:replacement",)


def test_raw_checkpoint_resumes_rematerializes_and_is_disposable(tmp_path: Path) -> None:
    event_path = tmp_path / "kernel.sqlite3"
    store = EventStore(event_path)
    IngestObservationHandler(store, clock=lambda: NOW).handle(
        IngestObservation(
            STREAM_ID,
            0,
            "operator",
            "repository-scan",
            "run:expiry",
            (
                _observation_input(
                    "claim:1",
                    "ready",
                    effective_at=NOW,
                    expires_at=NOW + timedelta(hours=1),
                ),
            ),
        )
    )
    _append_foreign_event(store)
    checkpoints = CheckpointStore(event_path)
    handler = ProjectOperationalStateHandler(store, checkpoints)
    fold = OperationalStateFold(SCOPE)

    initial = handler.handle(ProjectOperationalState(SCOPE, NOW + timedelta(minutes=30)))
    checkpoint = checkpoints.load(fold.name, fold.version)
    assert checkpoint is not None
    assert checkpoint.last_global_position == 2
    raw = ProjectionRunner().replay(fold, (), checkpoint=checkpoint).state
    assert raw.claims[0].epistemic_status is EpistemicStatus.OBSERVED
    assert all(claim.unknown_reason is None for claim in raw.claims)

    IngestObservationHandler(store, clock=lambda: NOW).handle(
        IngestObservation(
            STREAM_ID,
            1,
            "operator",
            "repository-scan",
            "run:expiry",
            (
                _observation_input(
                    "claim:2",
                    "blocked",
                    effective_at=NOW + timedelta(minutes=10),
                    expires_at=NOW + timedelta(hours=2),
                ),
            ),
        )
    )
    resumed = handler.handle(ProjectOperationalState(SCOPE, NOW + timedelta(hours=3)))
    direct = OperationalStateProjector().replay(
        store.read_all(),
        scope=SCOPE,
        as_of_time=NOW + timedelta(hours=3),
    )
    rematerialized = handler.handle(ProjectOperationalState(SCOPE, NOW + timedelta(minutes=30)))
    rebuilt = ProjectOperationalStateHandler(
        store,
        CheckpointStore(tmp_path / "disposable.sqlite3"),
    ).handle(ProjectOperationalState(SCOPE, NOW + timedelta(hours=3)))

    assert initial.cutoff_global_position == 2
    assert resumed == direct == rebuilt
    assert resumed.cutoff_global_position == 3
    assert resumed.claims[0].claim_id == "claim:2"
    assert resumed.claims[0].epistemic_status is EpistemicStatus.UNKNOWN
    assert rematerialized.claims[0].claim_id == "claim:2"
    assert rematerialized.claims[0].epistemic_status is EpistemicStatus.OBSERVED
    updated = checkpoints.load(fold.name, fold.version)
    assert updated is not None and updated.last_global_position == 3


def test_raw_fold_codec_rejects_scope_and_derived_state_forgery(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "kernel.sqlite3")
    IngestObservationHandler(store, clock=lambda: NOW).handle(
        IngestObservation(
            STREAM_ID,
            0,
            "operator",
            "fixture",
            "run:expiry",
            (_observation_input("claim:1", "ready", effective_at=NOW),),
        )
    )
    fold = OperationalStateFold(SCOPE)
    raw = fold.apply(fold.initial_state(), store.read_all()[0])
    payload = fold.dump_state(raw)

    assert fold.load_state(payload) == raw
    with pytest.raises(ValueError, match="fields do not match"):
        fold.load_state({**payload, "derived_unknowns": []})
    foreign = OperationalStateFold(OperationalStateScope("personal", STREAM_ID))
    with pytest.raises(ValueError, match="different scope"):
        foreign.load_state(payload)

    unknown = replace(
        raw.claims[0],
        value=None,
        confidence=0.0,
        epistemic_status=EpistemicStatus.UNKNOWN,
        unknown_reason=UnknownReason.EXPIRED,
        expires_at=NOW,
    )
    with pytest.raises(ValueError, match="cannot contain derived unknown"):
        replace(raw, claims=(unknown,))
    with pytest.raises(TypeError, match="epistemic_status"):
        replace(raw.claims[0], epistemic_status=cast("EpistemicStatus", "unknown"))
    with pytest.raises(TypeError, match="unknown_reason"):
        replace(raw.claims[0], unknown_reason=cast("UnknownReason", "expired"))


def _observation_input(
    claim_id: str,
    value: str,
    *,
    effective_at: datetime,
    expires_at: datetime | None = None,
) -> ObservationInput:
    return ObservationInput(
        observation_id=f"observation:{claim_id}",
        effective_at=effective_at,
        claims=(
            ObservedClaim(
                claim_id,
                "project:blackcell",
                "status",
                value,
                0.9,
                expires_at,
            ),
        ),
        evidence=(EvidencePointer(locator=f"fixture://{claim_id}"),),
    )


def _append_foreign_event(store: EventStore) -> EventEnvelope:
    return store.append(
        EventEnvelope.create(
            stream_id="telemetry:foreign",
            stream_sequence=1,
            event_type="telemetry.recorded",
            actor="fixture",
            source="fixture",
            payload={"status": "ok"},
            recorded_at=NOW,
            correlation_id="run:foreign",
        ),
        expected_sequence=0,
    )
