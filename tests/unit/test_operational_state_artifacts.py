from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import cast

import pytest

from blackcell.features.project_operational_state import (
    OPERATIONAL_STATE_SNAPSHOT_SCHEMA_VERSION,
    BeliefClaim,
    BeliefConflict,
    BeliefCorrection,
    EpistemicStatus,
    OperationalBeliefState,
    OperationalStateArtifactCodecError,
    OperationalStateFold,
    OperationalStateProjector,
    OperationalStateScope,
    ProjectOperationalState,
    ProjectOperationalStateHandler,
    UnknownReason,
    decode_operational_state_snapshot,
    encode_operational_state_snapshot,
    operational_state_snapshot_digest,
)
from blackcell.kernel import CheckpointStore, EventEnvelope, EventStore, ProjectionCheckpoint
from blackcell.kernel._json import canonical_json_bytes

NOW = datetime(2026, 7, 11, 15, tzinfo=UTC)
STREAM_ID = "observations:snapshot"
SCOPE = OperationalStateScope("repository", STREAM_ID)


def test_snapshot_round_trip_covers_complete_state_and_content_identity() -> None:
    state = _complete_state()

    encoded = encode_operational_state_snapshot(state)
    digest = operational_state_snapshot_digest(state)
    decoded = decode_operational_state_snapshot(
        encoded,
        expected_snapshot_digest=digest,
    )
    payload = json.loads(encoded)

    assert decoded == state
    assert digest == f"sha256:{hashlib.sha256(encoded).hexdigest()}"
    assert payload["schema_version"] == OPERATIONAL_STATE_SNAPSHOT_SCHEMA_VERSION
    assert payload["scope"] == {"domain": "repository", "stream_id": STREAM_ID}
    assert payload["cutoff_global_position"] == 4
    assert payload["last_source_stream_sequence"] == 4
    assert payload["effective_time_cutoff"] == (NOW + timedelta(hours=3)).isoformat()
    assert [item["claim_id"] for item in payload["claims"]] == [
        "claim:replacement",
        "claim:source-b",
        "claim:source-c",
    ]
    assert payload["conflicts"][0]["claim_ids"] == [
        "claim:source-b",
        "claim:source-c",
    ]
    assert payload["superseded_claims"][0]["claim_id"] == "claim:original"
    assert payload["applied_corrections"][0]["correction_id"] == "correction:1"
    assert payload["correction_replacement_claims"][0]["claim_id"] == "claim:replacement"
    assert payload["expired_claims"][0]["claim_id"] == "claim:replacement"
    assert payload["claims"][0]["epistemic_status"] == "unknown"
    assert payload["claims"][0]["unknown_reason"] == "expired"
    assert payload["expired_claims"][0]["epistemic_status"] == "observed"


def test_snapshot_normalizes_offset_equivalent_timestamps_to_utc() -> None:
    state = _complete_state()
    offset = timezone(timedelta(hours=5, minutes=30))
    shifted = _shift_state_to_timezone(state, offset)

    assert shifted == state
    assert encode_operational_state_snapshot(shifted) == encode_operational_state_snapshot(state)
    assert operational_state_snapshot_digest(shifted) == operational_state_snapshot_digest(state)
    decoded = decode_operational_state_snapshot(encode_operational_state_snapshot(shifted))
    assert decoded.effective_time_cutoff is not None
    assert decoded.effective_time_cutoff.utcoffset() == timedelta(0)
    assert all(claim.effective_at.utcoffset() == timedelta(0) for claim in decoded.claims)


def test_snapshot_decoder_rejects_schema_shape_types_identity_and_forgery() -> None:
    state = _complete_state()
    encoded = encode_operational_state_snapshot(state)
    original = json.loads(encoded)

    extra = {**original, "unexpected": None}
    missing = dict(original)
    del missing["expired_claims"]
    forged_type = _copy_payload(original)
    forged_type["cutoff_global_position"] = True
    forged_status = _copy_payload(original)
    status_claims = cast("list[dict[str, object]]", forged_status["claims"])
    status_claims[0]["epistemic_status"] = "UNKNOWN"
    forged_conflict = _copy_payload(original)
    conflicts = cast("list[dict[str, object]]", forged_conflict["conflicts"])
    conflict_claim_ids = cast("list[str]", conflicts[0]["claim_ids"])
    conflict_claim_ids[0] = "claim:forged"
    forged_replacement = _copy_payload(original)
    corrections = cast(
        "list[dict[str, object]]",
        forged_replacement["applied_corrections"],
    )
    corrections[0]["replacement_claim_id"] = "claim:forged"
    duplicate_claim = _copy_payload(original)
    duplicate_claims = cast("list[dict[str, object]]", duplicate_claim["claims"])
    duplicate_claims.append(dict(duplicate_claims[1]))
    duplicate_correction = _copy_payload(original)
    duplicate_corrections = cast(
        "list[dict[str, object]]",
        duplicate_correction["applied_corrections"],
    )
    duplicate_corrections.append(dict(duplicate_corrections[0]))
    duplicate_conflict = _copy_payload(original)
    duplicate_conflicts = cast(
        "list[dict[str, object]]",
        duplicate_conflict["conflicts"],
    )
    duplicate_conflicts.append(dict(duplicate_conflicts[0]))
    missing_replacement = _copy_payload(original)
    missing_replacement["correction_replacement_claims"] = []
    extra_replacement = _copy_payload(original)
    extra_replacements = cast(
        "list[dict[str, object]]",
        extra_replacement["correction_replacement_claims"],
    )
    extra_replacements.append(dict(extra_replacements[0]))

    for payload in (
        extra,
        missing,
        forged_type,
        forged_status,
        forged_conflict,
        forged_replacement,
        duplicate_claim,
        duplicate_correction,
        duplicate_conflict,
        missing_replacement,
        extra_replacement,
    ):
        with pytest.raises(OperationalStateArtifactCodecError):
            decode_operational_state_snapshot(canonical_json_bytes(payload))

    with pytest.raises(OperationalStateArtifactCodecError, match="canonical JSON"):
        decode_operational_state_snapshot(b" " + encoded)
    with pytest.raises(OperationalStateArtifactCodecError, match="digest"):
        decode_operational_state_snapshot(
            encoded,
            expected_snapshot_digest="sha256:" + "0" * 64,
        )


def test_snapshot_decoder_rejects_unsupported_malformed_and_nested_shapes() -> None:
    original = json.loads(encode_operational_state_snapshot(_complete_state()))
    unsupported = _copy_payload(original)
    unsupported["schema_version"] = "operational-state-snapshot/v999"
    claims_object = _copy_payload(original)
    claims_object["claims"] = {}
    unaligned_conflict = _copy_payload(original)
    conflicts = cast("list[dict[str, object]]", unaligned_conflict["conflicts"])
    values = cast("list[object]", conflicts[0]["values"])
    values.pop()

    for payload in (unsupported, claims_object, unaligned_conflict):
        with pytest.raises(OperationalStateArtifactCodecError):
            decode_operational_state_snapshot(canonical_json_bytes(payload))
    with pytest.raises(OperationalStateArtifactCodecError, match="UTF-8 canonical JSON"):
        decode_operational_state_snapshot(b"{not-json")


def test_snapshot_decoder_rejects_noncanonical_domain_order_and_timestamps() -> None:
    encoded = encode_operational_state_snapshot(_complete_state())
    reordered = _copy_payload(json.loads(encoded))
    reordered_claims = cast("list[dict[str, object]]", reordered["claims"])
    reordered_claims.reverse()
    noncanonical_time = _copy_payload(json.loads(encoded))
    timed_claims = cast("list[dict[str, object]]", noncanonical_time["claims"])
    timed_claims[0]["effective_at"] = "2026-07-11T16:00:00Z"

    for payload in (reordered, noncanonical_time):
        with pytest.raises(OperationalStateArtifactCodecError, match="canonical ordering"):
            decode_operational_state_snapshot(canonical_json_bytes(payload))


def test_snapshot_codec_rejects_duplicate_correction_target_lineage() -> None:
    state = _complete_state()
    first = state.applied_corrections[0]
    duplicate_target = replace(
        first,
        correction_id="correction:duplicate-target",
        replacement_claim_id="claim:replacement:duplicate",
        source_event_id="event:duplicate-target",
        stream_sequence=3,
        global_position=3,
    )
    duplicate_replacement = replace(
        state.correction_replacement_claims[0],
        claim_id="claim:replacement:duplicate",
        correction_id="correction:duplicate-target",
        source_event_id="event:duplicate-target",
        stream_sequence=3,
        global_position=3,
    )

    with pytest.raises(ValueError, match="more than once"):
        replace(
            state,
            applied_corrections=(first, duplicate_target),
            correction_replacement_claims=(
                state.correction_replacement_claims[0],
                duplicate_replacement,
            ),
        )


def test_snapshot_codec_rejects_forged_or_inconsistent_correction_lineage() -> None:
    state = _complete_state()
    correction = state.applied_corrections[0]
    with pytest.raises(ValueError, match="exactly follow"):
        replace(
            state,
            applied_corrections=(replace(correction, replacement_claim_id="claim:absent"),),
        )
    with pytest.raises(ValueError, match="provenance"):
        replace(
            state,
            applied_corrections=(replace(correction, actor="different-actor"),),
        )
    with pytest.raises(ValueError, match="target"):
        replace(
            state,
            superseded_claims=(replace(state.superseded_claims[0], predicate="owner"),),
        )


def test_state_rejects_ordinary_claim_forged_into_a_correction_event() -> None:
    state = _complete_state()
    replacement = state.correction_replacement_claims[0]
    rogue = replace(
        replacement,
        claim_id="claim:rogue-ordinary",
        correction_id=None,
        supersedes_claim_ids=(),
        expires_at=None,
    )

    with pytest.raises(ValueError, match="correction event fingerprint"):
        replace(state, claims=(*state.claims, rogue))


def test_snapshot_round_trips_valid_omitted_nonselected_correction_replacement(
    tmp_path: Path,
) -> None:
    store = EventStore(tmp_path / "kernel.sqlite3")
    store.append_many(
        (
            _observation(1, "ready"),
            _correction_event(),
            _same_source_observation_after_correction(),
        ),
        expected_sequences={STREAM_ID: 0},
    )
    state = OperationalStateProjector().replay(store.read_all(), scope=SCOPE)

    represented_ids = {
        claim.claim_id for claim in (*state.claims, *state.superseded_claims, *state.expired_claims)
    }
    assert state.applied_corrections[0].replacement_claim_id == "claim:replacement"
    assert "claim:replacement" not in represented_ids
    assert tuple(claim.claim_id for claim in state.correction_replacement_claims) == (
        "claim:replacement",
    )
    assert tuple(claim.claim_id for claim in state.claims) == ("claim:latest",)
    assert tuple(claim.claim_id for claim in state.superseded_claims) == ("claim:1",)
    encoded = encode_operational_state_snapshot(state)
    assert decode_operational_state_snapshot(encoded) == state

    latest = state.claims[0]
    with pytest.raises(ValueError, match="represented correction claims"):
        replace(state, claims=(replace(latest, claim_id="claim:replacement"),))
    with pytest.raises(ValueError, match="represented correction claims"):
        replace(
            state,
            claims=(
                replace(
                    latest,
                    correction_id="correction:rogue",
                    supersedes_claim_ids=("claim:1",),
                ),
            ),
        )


def test_state_rejects_duplicate_or_extra_correction_lineage_identities() -> None:
    state = _complete_state()
    first_correction = state.applied_corrections[0]
    first_replacement = state.correction_replacement_claims[0]
    second_target = replace(state.superseded_claims[0], claim_id="claim:target:2")

    duplicate_id_replacement = replace(
        first_replacement,
        correction_id="correction:2",
        supersedes_claim_ids=(second_target.claim_id,),
        source_event_id="event:5",
        stream_sequence=5,
        global_position=5,
    )
    duplicate_id_correction = replace(
        first_correction,
        correction_id="correction:2",
        supersedes_claim_ids=(second_target.claim_id,),
        source_event_id="event:5",
        stream_sequence=5,
        global_position=5,
    )
    with pytest.raises(ValueError, match="replacement claim ids must be unique"):
        replace(
            state,
            cutoff_global_position=5,
            last_source_stream_sequence=5,
            superseded_claims=(*state.superseded_claims, second_target),
            applied_corrections=(first_correction, duplicate_id_correction),
            correction_replacement_claims=(first_replacement, duplicate_id_replacement),
        )

    same_event_replacement = replace(
        first_replacement,
        claim_id="claim:replacement:2",
        correction_id="correction:2",
        supersedes_claim_ids=(second_target.claim_id,),
    )
    same_event_correction = replace(
        first_correction,
        correction_id="correction:2",
        replacement_claim_id=same_event_replacement.claim_id,
        supersedes_claim_ids=(second_target.claim_id,),
    )
    with pytest.raises(ValueError, match="distinct event"):
        replace(
            state,
            superseded_claims=(*state.superseded_claims, second_target),
            applied_corrections=(first_correction, same_event_correction),
            correction_replacement_claims=(first_replacement, same_event_replacement),
        )

    with pytest.raises(ValueError, match="exactly match applied correction targets"):
        replace(state, superseded_claims=())
    unknown_replacement = replace(
        first_replacement,
        value=None,
        confidence=0.0,
        epistemic_status=EpistemicStatus.UNKNOWN,
        unknown_reason=UnknownReason.EXPIRED,
    )
    with pytest.raises(ValueError, match="retain observed evidence"):
        replace(state, correction_replacement_claims=(unknown_replacement,))


def test_snapshot_codec_rejects_derived_origins_and_future_effective_evidence() -> None:
    state = _complete_state()
    original = state.superseded_claims[0]
    forged_unknown_origin = replace(
        original,
        value=None,
        confidence=0.0,
        expires_at=NOW,
        epistemic_status=EpistemicStatus.UNKNOWN,
        unknown_reason=UnknownReason.EXPIRED,
    )
    with pytest.raises(ValueError, match="observed evidence"):
        replace(state, superseded_claims=(forged_unknown_origin,))
    with pytest.raises(ValueError, match="effective-time cutoff"):
        replace(state, effective_time_cutoff=NOW)


def test_handler_projects_exact_historical_prefix_without_regressing_checkpoint(
    tmp_path: Path,
) -> None:
    path = tmp_path / "kernel.sqlite3"
    store = EventStore(path)
    store.append_many(
        (
            _observation(1, "ready"),
            _observation(2, "blocked"),
            _observation(3, "running"),
        ),
        expected_sequences={STREAM_ID: 0},
    )
    checkpoints = CheckpointStore(path)
    handler = ProjectOperationalStateHandler(store, checkpoints)
    fold = OperationalStateFold(SCOPE)

    current = handler.handle(ProjectOperationalState(SCOPE))
    historical = handler.handle(ProjectOperationalState(SCOPE, as_of_position=1))
    origin = handler.handle(ProjectOperationalState(SCOPE, as_of_position=0))
    checkpoint = checkpoints.load(fold.name, fold.version)

    assert current.cutoff_global_position == 3
    assert tuple(claim.value for claim in current.claims) == ("running",)
    assert historical.cutoff_global_position == 1
    assert historical.last_source_stream_sequence == 1
    assert tuple(claim.value for claim in historical.claims) == ("ready",)
    assert origin.scope == SCOPE
    assert origin.cutoff_global_position == origin.last_source_stream_sequence == 0
    assert origin.claims == origin.conflicts == ()
    assert checkpoint is not None and checkpoint.last_global_position == 3

    with pytest.raises(ValueError, match="exact, complete"):
        handler.handle(ProjectOperationalState(SCOPE, as_of_position=4))


def test_explicit_historical_projection_never_reads_or_writes_checkpoints(
    tmp_path: Path,
) -> None:
    store = EventStore(tmp_path / "kernel.sqlite3")
    store.append_many(
        (_observation(1, "ready"), _observation(2, "blocked")),
        expected_sequences={STREAM_ID: 0},
    )
    handler = ProjectOperationalStateHandler(store, _ForbiddenCheckpoints())

    state = handler.handle(ProjectOperationalState(SCOPE, as_of_position=1))

    assert state.cutoff_global_position == 1
    assert tuple(claim.value for claim in state.claims) == ("ready",)


def test_historical_cutoff_validation_preserves_positional_time_compatibility() -> None:
    assert ProjectOperationalState(SCOPE, NOW).as_of_time == NOW
    assert ProjectOperationalState(SCOPE, NOW).as_of_position is None
    with pytest.raises(ValueError, match="non-negative"):
        ProjectOperationalState(SCOPE, as_of_position=-1)
    with pytest.raises(ValueError, match="non-negative"):
        ProjectOperationalState(SCOPE, as_of_position=True)
    with pytest.raises(ValueError, match="integer"):
        ProjectOperationalState(SCOPE, as_of_position=cast("int", 1.5))


def test_state_requires_explicit_and_truthful_expiry_cutoff() -> None:
    state = _complete_state()
    with pytest.raises(ValueError, match="explicit effective-time cutoff"):
        replace(state, effective_time_cutoff=None)
    with pytest.raises(ValueError, match="expire by"):
        replace(state, effective_time_cutoff=NOW + timedelta(minutes=90))

    expiring = _claim(
        claim_id="claim:current-expired",
        value="ready",
        source="source:a",
        stream_sequence=1,
        global_position=1,
        effective_at=NOW,
        expires_at=NOW + timedelta(minutes=1),
    )
    with pytest.raises(ValueError, match="current observed claims"):
        OperationalBeliefState(
            scope=SCOPE,
            claims=(expiring,),
            conflicts=(),
            cutoff_global_position=1,
            last_source_stream_sequence=1,
            effective_time_cutoff=NOW + timedelta(minutes=2),
        )

    future = _claim(
        claim_id="claim:future",
        value="ready",
        source="source:a",
        stream_sequence=1,
        global_position=1,
        effective_at=NOW + timedelta(minutes=2),
    )
    with pytest.raises(ValueError, match="evidence cannot exceed"):
        OperationalBeliefState(
            scope=SCOPE,
            claims=(future,),
            conflicts=(),
            cutoff_global_position=1,
            last_source_stream_sequence=1,
            effective_time_cutoff=NOW + timedelta(minutes=1),
        )


def test_state_event_fingerprints_reject_aliases_and_nonmonotonic_positions() -> None:
    state = _complete_state()
    source_b, source_c = state.claims[1:]

    with pytest.raises(ValueError, match="source_event_id"):
        replace(
            state,
            claims=(state.claims[0], source_b, replace(source_c, source_event_id="event:3")),
        )
    with pytest.raises(ValueError, match="global_position"):
        replace(
            state,
            claims=(
                state.claims[0],
                source_b,
                replace(source_c, global_position=3, stream_sequence=3),
            ),
        )
    with pytest.raises(ValueError, match="source stream position"):
        replace(
            state,
            claims=(state.claims[0], source_b, replace(source_c, stream_sequence=3)),
        )

    nonmonotonic = (
        state.claims[0],
        replace(source_b, global_position=5, stream_sequence=4),
        replace(source_c, global_position=6, stream_sequence=3),
    )
    with pytest.raises(ValueError, match="must be monotonic"):
        replace(
            state,
            claims=nonmonotonic,
            cutoff_global_position=6,
            last_source_stream_sequence=4,
        )
    with pytest.raises(ValueError, match="cannot exceed global"):
        replace(
            state,
            claims=(state.claims[0], source_b, replace(source_c, stream_sequence=5)),
            cutoff_global_position=5,
            last_source_stream_sequence=5,
        )
    with pytest.raises(ValueError, match="cannot exceed the ledger cutoff"):
        replace(state, cutoff_global_position=4, last_source_stream_sequence=5)


def test_state_event_fingerprints_allow_multiple_claims_from_one_event() -> None:
    first = _claim(
        claim_id="claim:multi:1",
        value="ready",
        source="source:a",
        stream_sequence=1,
        global_position=1,
        effective_at=NOW,
    )
    second = replace(first, claim_id="claim:multi:2")

    state = OperationalBeliefState(
        scope=SCOPE,
        claims=(first, second),
        conflicts=(),
        cutoff_global_position=1,
        last_source_stream_sequence=1,
    )

    assert state.claims == (first, second)


def _complete_state() -> OperationalBeliefState:
    original = _claim(
        claim_id="claim:original",
        value="ready",
        source="source:a",
        stream_sequence=1,
        global_position=1,
        effective_at=NOW,
    )
    replacement = _claim(
        claim_id="claim:replacement",
        value="blocked",
        source="source:a",
        stream_sequence=2,
        global_position=2,
        effective_at=NOW + timedelta(hours=1),
        expires_at=NOW + timedelta(hours=2),
        correction_id="correction:1",
        supersedes_claim_ids=(original.claim_id,),
    )
    source_b = _claim(
        claim_id="claim:source-b",
        value="running",
        source="source:b",
        stream_sequence=3,
        global_position=3,
        effective_at=NOW + timedelta(hours=1),
    )
    source_c = _claim(
        claim_id="claim:source-c",
        value="stopped",
        source="source:c",
        stream_sequence=4,
        global_position=4,
        effective_at=NOW + timedelta(hours=1),
    )
    unknown = replace(
        replacement,
        value=None,
        confidence=0.0,
        epistemic_status=EpistemicStatus.UNKNOWN,
        unknown_reason=UnknownReason.EXPIRED,
    )
    correction = BeliefCorrection(
        correction_id="correction:1",
        supersedes_claim_ids=(original.claim_id,),
        replacement_claim_id=replacement.claim_id,
        reason="authoritative correction",
        effective_at=replacement.effective_at,
        recorded_at=replacement.recorded_at,
        source_event_id=replacement.source_event_id,
        source=replacement.source,
        actor=replacement.actor,
        correlation_id=replacement.correlation_id,
        domain=replacement.domain,
        stream_id=replacement.stream_id,
        stream_sequence=replacement.stream_sequence,
        global_position=replacement.global_position,
    )
    conflict = BeliefConflict(
        subject="project:blackcell",
        predicate="status",
        source_event_ids=(source_b.source_event_id, source_c.source_event_id),
        claim_ids=(source_b.claim_id, source_c.claim_id),
        values=(source_b.value, source_c.value),
    )
    return OperationalBeliefState(
        scope=SCOPE,
        claims=(unknown, source_b, source_c),
        conflicts=(conflict,),
        cutoff_global_position=4,
        last_source_stream_sequence=4,
        superseded_claims=(original,),
        applied_corrections=(correction,),
        effective_time_cutoff=NOW + timedelta(hours=3),
        expired_claims=(replacement,),
        correction_replacement_claims=(replacement,),
    )


def _claim(
    *,
    claim_id: str,
    value: str,
    source: str,
    stream_sequence: int,
    global_position: int,
    effective_at: datetime,
    expires_at: datetime | None = None,
    correction_id: str | None = None,
    supersedes_claim_ids: tuple[str, ...] = (),
) -> BeliefClaim:
    return BeliefClaim(
        claim_id=claim_id,
        subject="project:blackcell",
        predicate="status",
        value=value,
        confidence=0.9,
        effective_at=effective_at,
        recorded_at=NOW,
        source_event_id=f"event:{global_position}",
        source=source,
        actor="operator",
        correlation_id="run:snapshot",
        domain=SCOPE.domain,
        stream_id=STREAM_ID,
        stream_sequence=stream_sequence,
        global_position=global_position,
        correction_id=correction_id,
        supersedes_claim_ids=supersedes_claim_ids,
        expires_at=expires_at,
    )


def _observation(sequence: int, value: str) -> EventEnvelope:
    return EventEnvelope.create(
        stream_id=STREAM_ID,
        stream_sequence=sequence,
        event_type="observation.recorded",
        actor="operator",
        source="fixture",
        payload={
            "domain": SCOPE.domain,
            "claims": [
                {
                    "claim_id": f"claim:{sequence}",
                    "subject": "project:blackcell",
                    "predicate": "status",
                    "value": value,
                    "confidence": 0.9,
                }
            ],
        },
        recorded_at=NOW,
        effective_at=NOW + timedelta(minutes=sequence),
        correlation_id="run:historical-snapshot",
    )


def _correction_event() -> EventEnvelope:
    return EventEnvelope.create(
        stream_id=STREAM_ID,
        stream_sequence=2,
        event_type="observation.corrected",
        actor="operator",
        source="operator-correction",
        payload={
            "domain": SCOPE.domain,
            "correction_schema_version": "observation-correction/v1",
            "correction_id": "correction:omitted-replacement",
            "reason": "authoritative correction",
            "supersedes_claim_ids": ["claim:1"],
            "replacement": {
                "claim_id": "claim:replacement",
                "subject": "project:blackcell",
                "predicate": "status",
                "value": "blocked",
                "confidence": 1.0,
            },
            "evidence": [{"locator": "fixture://correction"}],
        },
        recorded_at=NOW,
        effective_at=NOW + timedelta(minutes=2),
        correlation_id="run:omitted-replacement",
    )


def _same_source_observation_after_correction() -> EventEnvelope:
    return EventEnvelope.create(
        stream_id=STREAM_ID,
        stream_sequence=3,
        event_type="observation.recorded",
        actor="operator",
        source="operator-correction",
        payload={
            "domain": SCOPE.domain,
            "claims": [
                {
                    "claim_id": "claim:latest",
                    "subject": "project:blackcell",
                    "predicate": "status",
                    "value": "running",
                    "confidence": 0.95,
                }
            ],
        },
        recorded_at=NOW,
        effective_at=NOW + timedelta(minutes=3),
        correlation_id="run:omitted-replacement",
    )


def _copy_payload(value: object) -> dict[str, object]:
    copied = json.loads(json.dumps(value))
    assert isinstance(copied, dict)
    return copied


def _shift_state_to_timezone(
    state: OperationalBeliefState,
    target: timezone,
) -> OperationalBeliefState:
    def shift_claim(claim: BeliefClaim) -> BeliefClaim:
        return replace(
            claim,
            effective_at=claim.effective_at.astimezone(target),
            recorded_at=claim.recorded_at.astimezone(target),
            expires_at=(
                claim.expires_at.astimezone(target) if claim.expires_at is not None else None
            ),
        )

    def shift_correction(correction: BeliefCorrection) -> BeliefCorrection:
        return replace(
            correction,
            effective_at=correction.effective_at.astimezone(target),
            recorded_at=correction.recorded_at.astimezone(target),
        )

    return replace(
        state,
        claims=tuple(shift_claim(claim) for claim in state.claims),
        superseded_claims=tuple(shift_claim(claim) for claim in state.superseded_claims),
        expired_claims=tuple(shift_claim(claim) for claim in state.expired_claims),
        correction_replacement_claims=tuple(
            shift_claim(claim) for claim in state.correction_replacement_claims
        ),
        applied_corrections=tuple(
            shift_correction(correction) for correction in state.applied_corrections
        ),
        effective_time_cutoff=(
            state.effective_time_cutoff.astimezone(target)
            if state.effective_time_cutoff is not None
            else None
        ),
    )


class _ForbiddenCheckpoints:
    def load(
        self,
        projection_name: str,
        projection_version: int,
        *,
        stream_id: str | None = None,
    ) -> ProjectionCheckpoint | None:
        raise AssertionError(
            f"historical projection read checkpoint {projection_name}:{projection_version}:"
            f"{stream_id}"
        )

    def save(
        self,
        checkpoint: ProjectionCheckpoint,
        *,
        expected_position: int | None = None,
    ) -> ProjectionCheckpoint:
        raise AssertionError(
            f"historical projection wrote checkpoint {checkpoint.projection_name}:"
            f"{expected_position}"
        )
