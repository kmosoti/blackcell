from datetime import UTC, datetime, timedelta

from blackcell.context import DeterministicContextProjector
from blackcell.domains.repository import (
    CLAIMS_RECORDED,
    Claim,
    ClaimBatch,
    EpistemicStatus,
    EvidenceRef,
    RepositoryProjector,
    RepositorySemanticEvent,
    SourceReliability,
)

T0 = datetime(2026, 3, 1, tzinfo=UTC)


def _claim(
    claim_id: str,
    subject: str,
    predicate: str,
    value: str | bool | None,
    *,
    status: EpistemicStatus = EpistemicStatus.OBSERVED,
    group: str | None = None,
    offset: int = 0,
    source: str = "test",
) -> Claim:
    at = T0 + timedelta(minutes=offset)
    return Claim(
        claim_id,
        subject,
        predicate,
        value,
        status,
        SourceReliability.AUTHORITATIVE,
        (EvidenceRef(f"event:{claim_id}", source),),
        at,
        at,
        conflict_group=group,
    )


def _state(*claims: Claim):
    events = tuple(
        RepositorySemanticEvent(
            f"event:{index}",
            index,
            CLAIMS_RECORDED,
            "test",
            claim.observed_at,
            ClaimBatch((claim,)),
        )
        for index, claim in enumerate(claims, start=1)
    )
    return RepositoryProjector().project(events, as_of_time=T0 + timedelta(hours=1))


def test_context_frame_is_stable_and_preserves_selected_conflict_atomically() -> None:
    state = _state(
        _claim("open", "task:T1", "status", "open", group="task:T1:status"),
        _claim(
            "closed",
            "task:T1",
            "status",
            "closed",
            group="task:T1:status",
            offset=1,
            source="other",
        ),
        _claim(
            "unknown",
            "task:T1",
            "owner",
            None,
            status=EpistemicStatus.UNKNOWN,
            group="task:T1:owner",
        ),
        _claim("noise", "path:README.md", "present", True),
    )
    projector = DeterministicContextProjector()

    first = projector.project(
        state,
        objective="Resolve task T1 status",
        constraints=("read-only", "cite evidence"),
        available_affordances=("git_status", "inspect_file"),
    )
    second = projector.project(
        state,
        objective="Resolve task T1 status",
        constraints=("cite evidence", "read-only"),
        available_affordances=("inspect_file", "git_status"),
    )

    assert first.frame_id == second.frame_id
    assert {claim.claim_id for claim in first.conflicts[0].claims} == {"open", "closed"}
    assert first.unknowns[0].claim_id == "unknown"
    assert first.estimated_tokens <= first.token_budget
    assert "selected=objective-conflict" in first.rendered_context


def test_context_projection_enforces_budget_and_records_omissions() -> None:
    state = _state(
        *(
            _claim(
                f"claim-{index}",
                f"task:T{index}",
                "status",
                "a-very-long-status-value" * 3,
                offset=index,
            )
            for index in range(10)
        )
    )

    frame = DeterministicContextProjector().project(
        state,
        objective="Inspect task status",
        character_budget=700,
        token_budget=175,
    )

    assert len(frame.rendered_context) <= 700
    assert frame.estimated_tokens <= 175
    assert frame.omission_summary.omitted_claim_count > 0
