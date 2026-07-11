from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

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
    OperationalStateProjector,
    OperationalStateScope,
)
from blackcell.kernel import EventStore, IdempotencyConflict

NOW = datetime(2026, 7, 11, 12, tzinfo=UTC)
STREAM_ID = "observations:repository"
SCOPE = OperationalStateScope("repository", STREAM_ID)


def test_correction_is_append_only_idempotent_and_projects_explicit_lineage(
    tmp_path: Path,
) -> None:
    store = EventStore(tmp_path / "kernel.sqlite3")
    original_event = _record_observation(store)
    handler = IngestCorrectionHandler(store, clock=lambda: NOW + timedelta(hours=2))
    command = _correction_command()

    first = handler.handle(command)
    assert handler.handle(command) == first
    assert len(store) == 2
    assert store.get(original_event.event_id) == original_event

    event = first[0]
    assert event.event_type == "observation.corrected"
    assert event.stream_sequence == 2
    assert event.global_position == 2
    assert event.effective_at == NOW + timedelta(hours=1)
    assert event.payload["correction_schema_version"] == "observation-correction/v1"
    assert event.payload["supersedes_claim_ids"] == ("claim:status:1",)
    assert event.payload["reason"] == "authoritative status correction"

    state = OperationalStateProjector().replay(store.read_all(), scope=SCOPE)
    assert tuple(claim.value for claim in state.claims) == ("blocked",)
    replacement = state.claims[0]
    assert replacement.claim_id == "claim:status:2"
    assert replacement.correction_id == "correction:status:1"
    assert replacement.supersedes_claim_ids == ("claim:status:1",)
    assert tuple(claim.claim_id for claim in state.superseded_claims) == ("claim:status:1",)
    assert state.applied_corrections[0].replacement_claim_id == "claim:status:2"
    assert state.last_source_stream_sequence == 2

    changed = _correction_command(replacement_value="running")
    with pytest.raises(IdempotencyConflict):
        handler.handle(changed)


def test_ledger_and_effective_time_cutoffs_are_independent(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "kernel.sqlite3")
    _record_observation(store)
    IngestCorrectionHandler(store, clock=lambda: NOW + timedelta(hours=2)).handle(
        _correction_command()
    )
    events = store.read_all()
    projector = OperationalStateProjector()
    before_event = projector.replay(
        events,
        scope=SCOPE,
        as_of_position=1,
        as_of_time=NOW + timedelta(days=1),
    )
    before_effective_time = projector.replay(
        events,
        scope=SCOPE,
        as_of_time=NOW + timedelta(minutes=30),
    )
    after_both = projector.replay(
        events,
        scope=SCOPE,
        as_of_time=NOW + timedelta(hours=1),
    )

    assert tuple(claim.value for claim in before_event.claims) == ("ready",)
    assert before_event.cutoff_global_position == 1
    assert tuple(claim.value for claim in before_effective_time.claims) == ("ready",)
    assert before_effective_time.cutoff_global_position == 2
    assert before_effective_time.last_source_stream_sequence == 2
    assert before_effective_time.applied_corrections == ()
    assert tuple(claim.value for claim in after_both.claims) == ("blocked",)
    assert after_both.effective_time_cutoff == NOW + timedelta(hours=1)


def test_explicit_time_filters_future_facts_while_default_stays_unbounded(
    tmp_path: Path,
) -> None:
    store = EventStore(tmp_path / "kernel.sqlite3")
    future = NOW + timedelta(days=1)
    IngestObservationHandler(store, clock=lambda: NOW).handle(
        _observation_command(value="scheduled", effective_at=future)
    )
    projector = OperationalStateProjector()

    bounded = projector.replay(store.read_all(), scope=SCOPE, as_of_time=NOW)
    unbounded = projector.replay(store.read_all(), scope=SCOPE)

    assert bounded.claims == ()
    assert bounded.cutoff_global_position == 1
    assert bounded.last_source_stream_sequence == 1
    assert bounded.effective_time_cutoff == NOW
    assert tuple(claim.value for claim in unbounded.claims) == ("scheduled",)
    assert unbounded.effective_time_cutoff is None

    with pytest.raises(ValueError, match="timezone-aware"):
        projector.replay(
            store.read_all(),
            scope=SCOPE,
            as_of_time=NOW.replace(tzinfo=None),
        )


def test_projection_rejects_missing_reused_cross_key_and_backdated_targets(
    tmp_path: Path,
) -> None:
    missing_store = EventStore(tmp_path / "missing.sqlite3")
    IngestCorrectionHandler(missing_store, clock=lambda: NOW).handle(
        _correction_command(expected_sequence=0)
    )
    with pytest.raises(ValueError, match="earlier claims in scope"):
        OperationalStateProjector().replay(missing_store.read_all(), scope=SCOPE)

    cross_key_store = EventStore(tmp_path / "cross-key.sqlite3")
    _record_observation(cross_key_store)
    IngestCorrectionHandler(cross_key_store, clock=lambda: NOW).handle(
        _correction_command(replacement_predicate="owner")
    )
    with pytest.raises(ValueError, match="same fact key"):
        OperationalStateProjector().replay(cross_key_store.read_all(), scope=SCOPE)

    reused_store = EventStore(tmp_path / "reused.sqlite3")
    _record_observation(reused_store)
    IngestCorrectionHandler(reused_store, clock=lambda: NOW).handle(
        _correction_command(
            replacement_claim_id="claim:other",
            supersedes=("claim:status:1",),
        )
    )
    IngestObservationHandler(reused_store, clock=lambda: NOW).handle(
        _observation_command(
            claim_id="claim:other",
            expected_sequence=2,
            value="duplicate",
        )
    )
    with pytest.raises(ValueError, match="claim id 'claim:other' was reused"):
        OperationalStateProjector().replay(reused_store.read_all(), scope=SCOPE)

    backdated_store = EventStore(tmp_path / "backdated.sqlite3")
    _record_observation(backdated_store, effective_at=NOW + timedelta(hours=2))
    IngestCorrectionHandler(backdated_store, clock=lambda: NOW).handle(
        _correction_command(effective_at=NOW + timedelta(hours=1))
    )
    with pytest.raises(ValueError, match="before a target claim"):
        OperationalStateProjector().replay(backdated_store.read_all(), scope=SCOPE)


def test_chained_corrections_require_new_claim_identity(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "kernel.sqlite3")
    _record_observation(store)
    handler = IngestCorrectionHandler(store, clock=lambda: NOW + timedelta(hours=3))
    handler.handle(_correction_command())
    handler.handle(
        _correction_command(
            correction_id="correction:status:2",
            expected_sequence=2,
            supersedes=("claim:status:2",),
            replacement_claim_id="claim:status:3",
            replacement_value="running",
            effective_at=NOW + timedelta(hours=2),
        )
    )

    state = OperationalStateProjector().replay(store.read_all(), scope=SCOPE)

    assert tuple(claim.claim_id for claim in state.claims) == ("claim:status:3",)
    assert tuple(claim.claim_id for claim in state.superseded_claims) == (
        "claim:status:1",
        "claim:status:2",
    )
    assert tuple(item.correction_id for item in state.applied_corrections) == (
        "correction:status:1",
        "correction:status:2",
    )


def test_correction_contract_rejects_ambiguous_or_unsupported_inputs() -> None:
    pointer = EvidencePointer(locator="fixture://correction")
    replacement = ObservedClaim(
        "claim:status:2",
        "project:blackcell",
        "status",
        "blocked",
    )

    with pytest.raises(ValueError, match="at least one claim"):
        CorrectionInput("correction:1", NOW, (), replacement, "reason", (pointer,))
    with pytest.raises(ValueError, match="unique"):
        CorrectionInput(
            "correction:1",
            NOW,
            ("claim:1", "claim:1"),
            replacement,
            "reason",
            (pointer,),
        )
    with pytest.raises(ValueError, match="new claim id"):
        CorrectionInput(
            "correction:1",
            NOW,
            (replacement.claim_id,),
            replacement,
            "reason",
            (pointer,),
        )
    with pytest.raises(ValueError, match="explicit evidence"):
        CorrectionInput("correction:1", NOW, ("claim:1",), replacement, "reason", ())
    with pytest.raises(ValueError, match="timezone-aware"):
        CorrectionInput(
            "correction:1",
            NOW.replace(tzinfo=None),
            ("claim:1",),
            replacement,
            "reason",
            (pointer,),
        )


def _record_observation(
    store: EventStore,
    *,
    effective_at: datetime = NOW,
):
    return IngestObservationHandler(store, clock=lambda: NOW).handle(
        _observation_command(effective_at=effective_at)
    )[0]


def _observation_command(
    *,
    claim_id: str = "claim:status:1",
    expected_sequence: int = 0,
    value: str = "ready",
    effective_at: datetime = NOW,
) -> IngestObservation:
    observation = ObservationInput(
        observation_id=f"observation:{claim_id}",
        effective_at=effective_at,
        claims=(
            ObservedClaim(
                claim_id,
                "project:blackcell",
                "status",
                value,
                0.9,
            ),
        ),
        evidence=(EvidencePointer(locator=f"fixture://{claim_id}"),),
    )
    return IngestObservation(
        STREAM_ID,
        expected_sequence,
        "operator",
        "repository-scan",
        "run:state-test",
        (observation,),
    )


def _correction_command(
    *,
    correction_id: str = "correction:status:1",
    expected_sequence: int = 1,
    supersedes: tuple[str, ...] = ("claim:status:1",),
    replacement_claim_id: str = "claim:status:2",
    replacement_predicate: str = "status",
    replacement_value: str = "blocked",
    effective_at: datetime = NOW + timedelta(hours=1),
) -> IngestCorrection:
    correction = CorrectionInput(
        correction_id=correction_id,
        effective_at=effective_at,
        supersedes_claim_ids=supersedes,
        replacement=ObservedClaim(
            replacement_claim_id,
            "project:blackcell",
            replacement_predicate,
            replacement_value,
            1.0,
        ),
        reason="authoritative status correction",
        evidence=(EvidencePointer(locator=f"fixture://{correction_id}"),),
    )
    return IngestCorrection(
        STREAM_ID,
        expected_sequence,
        "operator",
        "operator-correction",
        "run:state-test",
        (correction,),
    )
