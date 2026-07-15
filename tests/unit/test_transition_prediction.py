from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from blackcell.features.predict_transition import (
    DeterministicTransitionPredictor,
    PredictionDisposition,
    PredictionFindingOutcome,
    PredictionTarget,
    PredictTransition,
    ScoreTransitionPrediction,
    TransitionPredictionScorer,
    prediction_payload,
    prediction_score_payload,
)
from blackcell.features.project_operational_state import (
    BeliefClaim,
    BeliefConflict,
    EpistemicStatus,
    OperationalBeliefState,
    OperationalStateScope,
    UnknownReason,
    decode_operational_state_snapshot,
    operational_state_snapshot_digest,
)
from blackcell.kernel import EventEnvelope, JsonScalar
from blackcell.operator import RepositoryOperator
from blackcell.workflows.run_protocol import (
    EXECUTION_RECORDED,
    INITIAL_STATE_RECORDED,
    OUTCOME_STATE_RECORDED,
    PROPOSAL_RECORDED,
)

BASE = datetime(2026, 7, 13, 12, tzinfo=UTC)
DIGEST = "sha256:" + "a" * 64


def test_state_persistence_prediction_is_typed_deterministic_and_advisory() -> None:
    state = _state(
        _claim("ready", True, position=1, predicate="ready"),
        _claim("count", 1, position=2, predicate="count"),
        position=2,
        sequence=2,
    )
    command = PredictTransition(
        state,
        operational_state_snapshot_digest(state),
        DIGEST,
        "inspect",
        (PredictionTarget("project", "count"), PredictionTarget("project", "ready")),
        BASE,
        300,
    )

    first = DeterministicTransitionPredictor().handle(command)
    second = DeterministicTransitionPredictor().handle(command)

    assert first == second
    assert first.prediction_id == second.prediction_id
    assert tuple(item.target.predicate for item in first.facts) == ("count", "ready")
    assert all(item.disposition is PredictionDisposition.PREDICTED for item in first.facts)
    assert all(item.confidence == 0.75 for item in first.facts)
    assert all("state-persistence" in item.assumptions for item in first.facts)
    assert prediction_payload(first)["advisory_only"] is True
    assert "events" not in prediction_payload(first)


def test_unknown_and_conflicted_sources_do_not_invent_predictions() -> None:
    expired = _claim(
        "expired",
        None,
        position=1,
        confidence=0.0,
        status=EpistemicStatus.UNKNOWN,
        unknown_reason=UnknownReason.EXPIRED,
        expires_at=BASE,
    )
    conflict_a = _claim("conflict-a", "a", position=2, predicate="mode")
    conflict_b = _claim("conflict-b", "b", position=3, predicate="mode")
    state = _state(
        expired,
        conflict_a,
        conflict_b,
        conflicts=(
            BeliefConflict(
                "project",
                "mode",
                (conflict_a.source_event_id, conflict_b.source_event_id),
                (conflict_a.claim_id, conflict_b.claim_id),
                ("a", "b"),
            ),
        ),
        expired=(
            _claim(
                "expired",
                "old",
                position=1,
                expires_at=BASE,
            ),
        ),
        position=3,
        sequence=3,
        effective=BASE,
    )

    prediction = DeterministicTransitionPredictor().handle(
        PredictTransition(
            state,
            operational_state_snapshot_digest(state),
            DIGEST,
            "inspect",
            (
                PredictionTarget("project", "expired"),
                PredictionTarget("project", "missing"),
                PredictionTarget("project", "mode"),
            ),
            BASE,
            60,
        )
    )

    assert all(item.disposition is PredictionDisposition.UNKNOWN for item in prediction.facts)
    assert all(item.value is None and item.confidence == 0.0 for item in prediction.facts)
    assumptions = {item.target.predicate: item.assumptions for item in prediction.facts}
    assert assumptions == {
        "expired": ("no-current-observation",),
        "missing": ("no-current-observation",),
        "mode": ("source-state-conflicted",),
    }


def test_scoring_distinguishes_match_mismatch_missing_conflict_and_unscored() -> None:
    source = _state(
        _claim("match", True, position=1, predicate="match"),
        _claim("mismatch", 1, position=2, predicate="mismatch"),
        _claim("missing", "present", position=3, predicate="missing"),
        _claim("conflict", "a", position=4, predicate="conflict"),
        position=4,
        sequence=4,
    )
    prediction = DeterministicTransitionPredictor().handle(
        PredictTransition(
            source,
            operational_state_snapshot_digest(source),
            DIGEST,
            "act",
            tuple(
                PredictionTarget("project", predicate)
                for predicate in ("match", "mismatch", "missing", "conflict", "unknown")
            ),
            BASE,
            60,
        )
    )
    actual_conflict_a = _claim("actual-a", "a", position=7, predicate="conflict")
    actual_conflict_b = _claim("actual-b", "b", position=8, predicate="conflict")
    actual = _state(
        _claim("actual-match", True, position=5, predicate="match"),
        _claim("actual-mismatch", True, position=6, predicate="mismatch"),
        actual_conflict_a,
        actual_conflict_b,
        conflicts=(
            BeliefConflict(
                "project",
                "conflict",
                (actual_conflict_a.source_event_id, actual_conflict_b.source_event_id),
                (actual_conflict_a.claim_id, actual_conflict_b.claim_id),
                ("a", "b"),
            ),
        ),
        position=8,
        sequence=8,
        effective=BASE + timedelta(seconds=30),
    )

    score = TransitionPredictionScorer().handle(
        ScoreTransitionPrediction(
            prediction,
            actual,
            operational_state_snapshot_digest(actual),
            BASE + timedelta(seconds=31),
        )
    )
    outcomes = {item.target.predicate: item.outcome for item in score.findings}

    assert outcomes == {
        "conflict": PredictionFindingOutcome.ACTUAL_CONFLICT,
        "match": PredictionFindingOutcome.MATCH,
        "mismatch": PredictionFindingOutcome.MISMATCH,
        "missing": PredictionFindingOutcome.ACTUAL_MISSING,
        "unknown": PredictionFindingOutcome.PREDICTION_UNKNOWN,
    }
    assert score.matched_count == 1
    assert score.scored_count == 2
    assert score.exact_match_rate == 0.5
    assert score.brier_score == pytest.approx(0.3125)
    assert prediction_score_payload(score)["scored_count"] == 2


def test_canonical_scalar_equality_keeps_boolean_distinct_from_integer() -> None:
    source = _state(_claim("value", True, position=1), position=1, sequence=1)
    prediction = DeterministicTransitionPredictor().handle(
        PredictTransition(
            source,
            operational_state_snapshot_digest(source),
            DIGEST,
            "act",
            (PredictionTarget("project", "value"),),
            BASE,
            60,
        )
    )
    actual = _state(
        _claim("actual", 1, position=2),
        position=2,
        sequence=2,
        effective=BASE + timedelta(seconds=1),
    )

    score = TransitionPredictionScorer().handle(
        ScoreTransitionPrediction(
            prediction,
            actual,
            operational_state_snapshot_digest(actual),
            BASE + timedelta(seconds=2),
        )
    )

    assert score.findings[0].outcome is PredictionFindingOutcome.MISMATCH


def test_scoring_rejects_same_or_different_scope_and_non_outcome_state() -> None:
    source = _state(_claim("value", True, position=1), position=1, sequence=1)
    prediction = DeterministicTransitionPredictor().handle(
        PredictTransition(
            source,
            operational_state_snapshot_digest(source),
            DIGEST,
            "act",
            (PredictionTarget("project", "value"),),
            BASE,
            60,
        )
    )

    with pytest.raises(ValueError, match="later same-stream"):
        TransitionPredictionScorer().handle(
            ScoreTransitionPrediction(
                prediction,
                source,
                operational_state_snapshot_digest(source),
                BASE + timedelta(seconds=1),
            )
        )
    other = _state(
        _claim("value", True, position=2, stream="repository:other"),
        position=2,
        sequence=2,
        stream="repository:other",
        effective=BASE + timedelta(seconds=1),
    )
    with pytest.raises(ValueError, match="scope differs"):
        TransitionPredictionScorer().handle(
            ScoreTransitionPrediction(
                prediction,
                other,
                operational_state_snapshot_digest(other),
                BASE + timedelta(seconds=2),
            )
        )


def test_real_daily_operator_v2_states_and_action_score_without_live_reentry(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    operator = RepositoryOperator(
        repo,
        database_path=tmp_path / "kernel.sqlite3",
        artifact_root=tmp_path / "artifacts",
    )
    result = operator.run()
    replay = operator.replay(result.run_id)
    initial = _recorded_state(operator, replay.events, INITIAL_STATE_RECORDED)
    actual = _recorded_state(operator, replay.events, OUTCOME_STATE_RECORDED)
    proposal = _event(replay.events, PROPOSAL_RECORDED)
    execution = _event(replay.events, EXECUTION_RECORDED)
    generated_at = cast("datetime", initial.effective_time_cutoff)

    prediction = DeterministicTransitionPredictor().handle(
        PredictTransition(
            initial,
            _artifact_digest(_event(replay.events, INITIAL_STATE_RECORDED)),
            cast("str", proposal.payload["action_digest"]),
            cast("str", execution.payload["affordance"]),
            (PredictionTarget("repository", "git.valid"),),
            generated_at,
            300,
        )
    )
    score = TransitionPredictionScorer().handle(
        ScoreTransitionPrediction(
            prediction,
            actual,
            _artifact_digest(_event(replay.events, OUTCOME_STATE_RECORDED)),
            cast("datetime", actual.effective_time_cutoff),
        )
    )

    assert prediction.action_digest == proposal.payload["action_digest"]
    assert prediction.source_snapshot_digest == _artifact_digest(
        _event(replay.events, INITIAL_STATE_RECORDED)
    )
    assert score.actual_snapshot_digest == _artifact_digest(
        _event(replay.events, OUTCOME_STATE_RECORDED)
    )
    assert score.findings[0].outcome is PredictionFindingOutcome.MATCH
    assert score.exact_match_rate == 1.0


def _state(
    *claims: BeliefClaim,
    position: int,
    sequence: int,
    stream: str = "repository:blackcell",
    effective: datetime = BASE,
    conflicts: tuple[BeliefConflict, ...] = (),
    expired: tuple[BeliefClaim, ...] = (),
) -> OperationalBeliefState:
    return OperationalBeliefState(
        OperationalStateScope("repository", stream),
        claims,
        conflicts,
        position,
        sequence,
        effective_time_cutoff=effective,
        expired_claims=expired,
    )


def _claim(
    claim_id: str,
    value: JsonScalar,
    *,
    position: int,
    predicate: str = "value",
    stream: str = "repository:blackcell",
    confidence: float = 1.0,
    status: EpistemicStatus = EpistemicStatus.OBSERVED,
    unknown_reason: UnknownReason | None = None,
    expires_at: datetime | None = None,
) -> BeliefClaim:
    return BeliefClaim(
        claim_id,
        "project",
        predicate,
        value,
        confidence,
        BASE - timedelta(seconds=1),
        BASE - timedelta(seconds=1),
        f"event:{claim_id}",
        "fixture",
        "fixture",
        "run:fixture",
        "repository",
        stream,
        position,
        position,
        expires_at=expires_at,
        epistemic_status=status,
        unknown_reason=unknown_reason,
    )


def _recorded_state(
    operator: RepositoryOperator,
    events: tuple[EventEnvelope, ...],
    event_type: str,
) -> OperationalBeliefState:
    event = _event(events, event_type)
    digest = _artifact_digest(event)
    return decode_operational_state_snapshot(
        operator.artifacts.get_bytes(digest, verify=True),
        expected_snapshot_digest=digest,
    )


def _artifact_digest(event: EventEnvelope) -> str:
    artifact = cast("dict[str, object]", event.payload["artifact"])
    return cast("str", artifact["digest"])


def _event(events: tuple[EventEnvelope, ...], event_type: str) -> EventEnvelope:
    return next(item for item in events if item.event_type == event_type)
