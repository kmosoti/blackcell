from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from blackcell.domains.repository import (
    CLAIMS_RECORDED,
    CORRECTION_RECORDED,
    Claim,
    ClaimBatch,
    ClaimCorrection,
    EpistemicStatus,
    EvidenceRef,
    ProjectionError,
    RepositoryProjector,
    RepositorySemanticEvent,
    SourceReliability,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _claim(
    claim_id: str,
    value: str | bool | None,
    *,
    subject: str = "task:T1",
    predicate: str = "status",
    at: datetime = T0,
    status: EpistemicStatus = EpistemicStatus.OBSERVED,
    group: str | None = "task:T1:status",
) -> Claim:
    return Claim(
        claim_id=claim_id,
        subject=subject,
        predicate=predicate,
        value=value,
        epistemic_status=status,
        source_reliability=SourceReliability.AUTHORITATIVE,
        evidence=(EvidenceRef(f"event:{claim_id}", "test"),),
        observed_at=at,
        effective_at=at,
        conflict_group=group,
    )


def _event(sequence: int, *claims: Claim) -> RepositorySemanticEvent:
    return RepositorySemanticEvent(
        event_id=f"event:{sequence}",
        sequence=sequence,
        kind=CLAIMS_RECORDED,
        source="test",
        occurred_at=max((claim.observed_at for claim in claims), default=T0),
        payload=ClaimBatch(tuple(claims)),
    )


def test_projection_preserves_conflicting_claims_and_unknowns() -> None:
    open_claim = _claim("open", "open")
    closed_claim = _claim("closed", "closed", at=T0 + timedelta(minutes=1))
    unknown = _claim(
        "branch-unknown",
        None,
        subject="repository",
        predicate="git.branch",
        status=EpistemicStatus.UNKNOWN,
        group="repository:git.branch",
    )

    state = RepositoryProjector().project(
        (_event(2, closed_claim), _event(1, open_claim, unknown)),
        as_of_time=T0 + timedelta(hours=1),
    )

    assert {claim.claim_id for claim in state.claims} == {"open", "closed", "branch-unknown"}
    assert len(state.conflicts) == 1
    assert {claim.value for claim in state.conflicts[0].claims} == {"open", "closed"}
    assert state.unknowns == (unknown,)


def test_effective_time_correction_does_not_rewrite_prior_projection() -> None:
    original = _claim("original", "open")
    replacement = _claim("replacement", "closed", at=T0 + timedelta(days=1))
    correction = ClaimCorrection(
        correction_id="correction:1",
        supersedes_claim_ids=(original.claim_id,),
        replacement=replacement,
        effective_at=T0 + timedelta(days=1),
        reason="authoritative task record was corrected",
    )
    correction_event = RepositorySemanticEvent(
        event_id="event:2",
        sequence=2,
        kind=CORRECTION_RECORDED,
        source="test",
        occurred_at=T0 + timedelta(hours=1),
        payload=correction,
    )
    projector = RepositoryProjector()

    before_effective_time = projector.project(
        (_event(1, original), correction_event),
        as_of_time=T0 + timedelta(hours=12),
    )
    after_effective_time = projector.project(
        (_event(1, original), correction_event),
        as_of_time=T0 + timedelta(days=2),
    )
    before_correction_sequence = projector.project(
        (_event(1, original), correction_event),
        as_of_sequence=1,
        as_of_time=T0 + timedelta(days=2),
    )

    assert before_effective_time.claims == (original,)
    assert after_effective_time.claims == (replacement,)
    assert after_effective_time.superseded_claims == (original,)
    assert before_correction_sequence.claims == (original,)


@dataclass(frozen=True)
class _KernelEnvelope:
    event_id: str
    stream_sequence: int
    event_type: str
    recorded_at: datetime
    effective_at: datetime
    payload: dict[str, object]


def test_projector_accepts_canonical_kernel_event_attributes_and_serialized_claims() -> None:
    envelope = _KernelEnvelope(
        event_id="kernel:1",
        stream_sequence=7,
        event_type="ObservationRecorded",
        recorded_at=T0,
        effective_at=T0,
        payload={
            "domain": "repository",
            "claims": [
                {
                    "claim_id": "serialized",
                    "subject": "repository",
                    "predicate": "git.clean",
                    "value": True,
                    "epistemic_status": "observed",
                    "source_reliability": "authoritative",
                    "evidence": [],
                    "observed_at": T0.isoformat(),
                    "effective_at": T0.isoformat(),
                    "conflict_group": "repository:git.clean",
                }
            ],
        },
    )

    state = RepositoryProjector().project((envelope,))

    assert state.as_of_sequence == 7
    assert state.claims[0].claim_id == "serialized"


def test_projection_rejects_claim_id_reuse_instead_of_last_write_wins() -> None:
    first = _claim("same-id", "open")
    second = _claim("same-id", "closed", at=T0 + timedelta(minutes=1))

    with pytest.raises(ProjectionError, match="reused"):
        RepositoryProjector().project((_event(1, first), _event(2, second)))

