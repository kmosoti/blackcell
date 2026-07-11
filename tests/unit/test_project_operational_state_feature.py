from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from blackcell.domains.repository import RepositoryProjector
from blackcell.features.project_operational_state import (
    OperationalStateProjector,
    OperationalStateScope,
)
from blackcell.kernel import EventEnvelope, EventStore

NOW = datetime(2026, 7, 10, 12, tzinfo=UTC)


def observation(
    sequence: int,
    value: str | int | bool,
    *,
    effective_at: datetime = NOW,
    confidence: float = 0.8,
) -> EventEnvelope:
    return EventEnvelope.create(
        stream_id="observations:daily",
        stream_sequence=sequence,
        event_type="observation.recorded",
        actor="operator",
        source="fixture",
        payload={
            "claims": [
                {
                    "subject": "project:blackcell",
                    "predicate": "status",
                    "value": value,
                    "confidence": confidence,
                }
            ]
        },
        recorded_at=NOW,
        effective_at=effective_at,
        correlation_id="run:daily",
    )


def scoped_observation(
    stream_id: str,
    sequence: int,
    value: str,
    *,
    domain: str = "repository",
    effective_at: datetime = NOW,
    source: str = "fixture",
    claim_id: str | None = None,
) -> EventEnvelope:
    return EventEnvelope.create(
        stream_id=stream_id,
        stream_sequence=sequence,
        event_type="observation.recorded",
        actor="operator",
        source=source,
        payload={
            "domain": domain,
            "claims": [
                {
                    "claim_id": claim_id or f"claim:{domain}:{stream_id}:{sequence}",
                    "subject": "project:blackcell",
                    "predicate": "status",
                    "value": value,
                    "confidence": 0.8,
                }
            ],
        },
        recorded_at=NOW,
        effective_at=effective_at,
        correlation_id="run:scope-test",
    )


def test_projection_accepts_legacy_observation_event_spelling(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "kernel.sqlite3")
    legacy = observation(1, "ready")
    legacy = EventEnvelope.create(
        stream_id=legacy.stream_id,
        stream_sequence=legacy.stream_sequence,
        event_type="ObservationRecorded",
        actor=legacy.actor,
        source=legacy.source,
        payload=legacy.payload,
        recorded_at=legacy.recorded_at,
        effective_at=legacy.effective_at,
        correlation_id=legacy.correlation_id,
    )
    store.append(legacy, expected_sequence=0)

    assert OperationalStateProjector().replay(store.read_all()).claims[0].value == "ready"


def test_projection_preserves_confidence_time_and_provenance(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "kernel.sqlite3")
    stored = store.append(observation(1, "ready", confidence=0.7), expected_sequence=0)

    state = OperationalStateProjector().replay(store.read_all())

    assert state.last_global_position == 1
    assert state.conflicts == ()
    assert state.claims_for("project:blackcell", "status") == state.claims
    claim = state.claims[0]
    assert (claim.value, claim.confidence) == ("ready", 0.7)
    assert (claim.source_event_id, claim.source, claim.actor) == (
        stored.event_id,
        "fixture",
        "operator",
    )
    assert claim.claim_id.startswith(f"{stored.event_id}#claim:")
    assert (claim.domain, claim.stream_id, claim.stream_sequence, claim.global_position) == (
        "repository",
        "observations:daily",
        1,
        1,
    )
    assert state.scope == OperationalStateScope("repository", "observations:daily")
    assert state.cutoff_global_position == state.last_global_position == 1
    assert state.last_source_stream_sequence == 1


def test_equal_time_disagreement_remains_an_explicit_conflict(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "kernel.sqlite3")
    store.append_many(
        (observation(1, "ready"), observation(2, "blocked")),
        expected_sequences={"observations:daily": 0},
    )

    state = OperationalStateProjector().replay(store.read_all())

    assert {claim.value for claim in state.claims} == {"ready", "blocked"}
    assert len(state.conflicts) == 1
    assert set(state.conflicts[0].values) == {"ready", "blocked"}


def test_equal_time_json_distinct_booleans_and_numbers_conflict(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "kernel.sqlite3")
    store.append_many(
        (observation(1, True), observation(2, 1)),
        expected_sequences={"observations:daily": 0},
    )

    state = OperationalStateProjector().replay(store.read_all())

    assert state.conflicts[0].values == (True, 1)


def test_newer_evidence_supersedes_candidates_and_older_evidence_is_ignored(
    tmp_path: Path,
) -> None:
    store = EventStore(tmp_path / "kernel.sqlite3")
    store.append_many(
        (
            observation(1, "ready"),
            observation(2, "blocked", effective_at=NOW + timedelta(minutes=1)),
            observation(3, "stale", effective_at=NOW - timedelta(minutes=1)),
        ),
        expected_sequences={"observations:daily": 0},
    )

    state = OperationalStateProjector().replay(store.read_all())

    assert tuple(claim.value for claim in state.claims) == ("blocked",)
    assert state.conflicts == ()


def test_newer_same_source_supersedes_while_independent_sources_stay_concurrent(
    tmp_path: Path,
) -> None:
    store = EventStore(tmp_path / "kernel.sqlite3")
    stream_id = "observations:repository"
    store.append_many(
        (
            scoped_observation(
                stream_id,
                1,
                "ready",
                source="repository-scan",
                effective_at=NOW,
            ),
            scoped_observation(
                stream_id,
                2,
                "blocked",
                source="operator-report",
                effective_at=NOW + timedelta(minutes=1),
            ),
            scoped_observation(
                stream_id,
                3,
                "running",
                source="repository-scan",
                effective_at=NOW + timedelta(minutes=2),
            ),
        ),
        expected_sequences={stream_id: 0},
    )

    state = OperationalStateProjector().replay(
        store.read_all(),
        scope=OperationalStateScope("repository", stream_id),
    )

    assert {(claim.source, claim.value) for claim in state.claims} == {
        ("operator-report", "blocked"),
        ("repository-scan", "running"),
    }
    assert "ready" not in {claim.value for claim in state.claims}
    assert len(state.conflicts) == 1
    assert set(state.conflicts[0].values) == {"blocked", "running"}


def test_projection_rejects_claim_id_reuse_across_event_provenance(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "kernel.sqlite3")
    stream_id = "observations:repository"
    store.append_many(
        (
            scoped_observation(stream_id, 1, "ready", claim_id="claim:stable"),
            scoped_observation(
                stream_id,
                2,
                "ready",
                effective_at=NOW + timedelta(minutes=1),
                claim_id="claim:stable",
            ),
        ),
        expected_sequences={stream_id: 0},
    )

    with pytest.raises(ValueError, match="claim id 'claim:stable' was reused"):
        OperationalStateProjector().replay(
            store.read_all(),
            scope=OperationalStateScope("repository", stream_id),
        )


def test_projection_requires_stored_globally_ordered_events(tmp_path: Path) -> None:
    unstored = observation(1, "ready")
    with pytest.raises(ValueError, match="stored"):
        OperationalStateProjector().replay((unstored,))

    store = EventStore(tmp_path / "kernel.sqlite3")
    first, second = store.append_many(
        (observation(1, "ready"), observation(2, "blocked")),
        expected_sequences={"observations:daily": 0},
    )
    with pytest.raises(ValueError, match="ordered"):
        OperationalStateProjector().replay((second, first))


def test_explicit_scope_isolates_streams_and_domains_at_one_ledger_cutoff(
    tmp_path: Path,
) -> None:
    store = EventStore(tmp_path / "kernel.sqlite3")
    stored = store.append_many(
        (
            scoped_observation("observations:repo-a", 1, "ready"),
            scoped_observation("observations:repo-b", 1, "blocked"),
            scoped_observation(
                "observations:repo-a",
                2,
                "private",
                domain="personal-planning",
            ),
        ),
        expected_sequences={
            "observations:repo-a": 0,
            "observations:repo-b": 0,
        },
    )
    projector = OperationalStateProjector()

    repository_state = projector.replay(
        store.read_all(),
        scope=OperationalStateScope("repository", "observations:repo-a"),
    )
    personal_state = projector.replay(
        store.read_all(),
        scope=OperationalStateScope("personal-planning", "observations:repo-a"),
    )

    assert tuple(claim.value for claim in repository_state.claims) == ("ready",)
    assert repository_state.conflicts == ()
    assert repository_state.cutoff_global_position == 3
    assert repository_state.last_source_stream_sequence == 1
    assert repository_state.claims[0].source_event_id == stored[0].event_id
    assert tuple(claim.value for claim in personal_state.claims) == ("private",)
    assert personal_state.last_source_stream_sequence == 2
    assert personal_state.claims[0].source_event_id == stored[2].event_id

    foreign_claim = replace(repository_state.claims[0], domain="personal-planning")
    with pytest.raises(ValueError, match="declared scope"):
        replace(repository_state, claims=(foreign_claim,))


def test_compatibility_scope_inference_fails_closed_when_input_is_ambiguous(
    tmp_path: Path,
) -> None:
    store = EventStore(tmp_path / "kernel.sqlite3")
    store.append_many(
        (
            scoped_observation("observations:repo", 1, "ready"),
            scoped_observation("observations:personal", 1, "blocked", domain="personal"),
        ),
        expected_sequences={
            "observations:repo": 0,
            "observations:personal": 0,
        },
    )

    with pytest.raises(ValueError, match="scope is ambiguous"):
        OperationalStateProjector().replay(store.read_all())


def test_explicit_unbound_scope_is_rejected_but_empty_replay_can_infer_one() -> None:
    projector = OperationalStateProjector()

    inferred = projector.replay(())

    assert inferred.scope == OperationalStateScope("repository", None)
    assert inferred.claims == inferred.conflicts == ()
    with pytest.raises(ValueError, match=r"explicitly supplied.*must be bound"):
        projector.replay((), scope=OperationalStateScope("repository", None))


def test_observation_v2_cannot_fall_back_to_the_legacy_repository_domain(
    tmp_path: Path,
) -> None:
    store = EventStore(tmp_path / "kernel.sqlite3")
    malformed = observation(1, "ready")
    malformed = EventEnvelope.create(
        stream_id=malformed.stream_id,
        stream_sequence=malformed.stream_sequence,
        event_type=malformed.event_type,
        actor=malformed.actor,
        source=malformed.source,
        payload={
            "observation_schema_version": "observation/v2",
            "claims": malformed.payload["claims"],
        },
        recorded_at=malformed.recorded_at,
        effective_at=malformed.effective_at,
        correlation_id=malformed.correlation_id,
    )
    store.append(malformed, expected_sequence=0)

    with pytest.raises(ValueError, match="requires a domain"):
        OperationalStateProjector().replay(
            store.read_all(),
            scope=OperationalStateScope("repository", "observations:daily"),
        )


def test_historical_position_is_a_complete_ledger_cutoff_not_a_stream_sequence(
    tmp_path: Path,
) -> None:
    store = EventStore(tmp_path / "kernel.sqlite3")
    store.append_many(
        (
            scoped_observation("observations:repo", 1, "ready"),
            scoped_observation("observations:other", 1, "irrelevant"),
            scoped_observation(
                "observations:repo",
                2,
                "blocked",
                effective_at=NOW + timedelta(minutes=1),
            ),
        ),
        expected_sequences={
            "observations:repo": 0,
            "observations:other": 0,
        },
    )
    scope = OperationalStateScope("repository", "observations:repo")
    projector = OperationalStateProjector()

    before_change = projector.replay(store.read_all(), scope=scope, as_of_position=2)
    after_change = projector.replay(store.read_all(), scope=scope)

    assert tuple(claim.value for claim in before_change.claims) == ("ready",)
    assert before_change.cutoff_global_position == 2
    assert before_change.last_source_stream_sequence == 1
    assert tuple(claim.value for claim in after_change.claims) == ("blocked",)
    assert after_change.cutoff_global_position == 3
    assert after_change.last_source_stream_sequence == 2

    with pytest.raises(ValueError, match="exceeds"):
        projector.replay(store.read_all(), scope=scope, as_of_position=4)
    with pytest.raises(ValueError, match="non-negative"):
        projector.replay(store.read_all(), scope=scope, as_of_position=-1)
    with pytest.raises(ValueError, match="complete ledger prefix"):
        projector.replay(store.read_all(after_position=1), scope=scope)


def test_repository_projectors_agree_on_overlapping_current_fact_semantics(
    tmp_path: Path,
) -> None:
    """Characterize shared facts while retaining each projector's richer semantics."""

    store = EventStore(tmp_path / "kernel.sqlite3")
    stream_id = "observations:repository"
    stored = store.append_many(
        (
            repository_compatible_observation(1, "ready", effective_at=NOW),
            repository_compatible_observation(
                2,
                "blocked",
                effective_at=NOW + timedelta(minutes=1),
            ),
        ),
        expected_sequences={stream_id: 0},
    )

    operational = OperationalStateProjector().replay(
        stored,
        scope=OperationalStateScope("repository", stream_id),
    )
    legacy = RepositoryProjector().project(
        stored,
        repository_id=stream_id,
        as_of_sequence=operational.last_source_stream_sequence,
        as_of_time=NOW + timedelta(minutes=2),
    )

    operational_facts = {
        (claim.subject, claim.predicate, claim.value) for claim in operational.claims
    }
    legacy_facts = {(claim.subject, claim.predicate, claim.value) for claim in legacy.claims}
    assert operational_facts == legacy_facts == {("project:blackcell", "status", "blocked")}
    assert operational.claims[0].claim_id == legacy.claims[0].claim_id == "repo-status:2"
    assert operational.last_source_stream_sequence == legacy.as_of_sequence == 2

    # The legacy projector retains superseded Claim objects; the smaller feature
    # projector intentionally exposes only current candidates plus event provenance.
    assert tuple(claim.value for claim in legacy.superseded_claims) == ("ready",)
    assert operational.claims[0].source_event_id == stored[1].event_id


def repository_compatible_observation(
    sequence: int,
    value: str,
    *,
    effective_at: datetime,
) -> EventEnvelope:
    stream_id = "observations:repository"
    claim_id = f"repo-status:{sequence}"
    return EventEnvelope.create(
        stream_id=stream_id,
        stream_sequence=sequence,
        event_type="observation.recorded",
        actor="operator",
        source="repository-scan",
        payload={
            "domain": "repository",
            "claims": [
                {
                    "claim_id": claim_id,
                    "subject": "project:blackcell",
                    "predicate": "status",
                    "value": value,
                    "confidence": 1.0,
                    "epistemic_status": "observed",
                    "source_reliability": "authoritative",
                    "evidence": [
                        {
                            "event_id": f"evidence:{sequence}",
                            "source": "repository-scan",
                            "sequence": sequence,
                        }
                    ],
                    "observed_at": effective_at.isoformat(),
                    "effective_at": effective_at.isoformat(),
                    "conflict_group": "project:blackcell:status",
                }
            ],
        },
        recorded_at=effective_at,
        effective_at=effective_at,
        correlation_id="run:parity",
    )
