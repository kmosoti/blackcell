from datetime import UTC, datetime, timedelta

from blackcell.context import LatestNBaselineRenderer, RawEventBaselineRenderer
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

T0 = datetime(2026, 3, 2, tzinfo=UTC)


def _events() -> tuple[RepositorySemanticEvent, ...]:
    events = []
    for index in range(1, 4):
        at = T0 + timedelta(minutes=index)
        claim = Claim(
            f"claim:{index}",
            f"task:T{index}",
            "status",
            "open",
            EpistemicStatus.OBSERVED,
            SourceReliability.AUTHORITATIVE,
            (EvidenceRef(f"event:{index}", "test"),),
            at,
            at,
        )
        events.append(
            RepositorySemanticEvent(
                f"event:{index}", index, CLAIMS_RECORDED, "test", at, ClaimBatch((claim,))
            )
        )
    return tuple(events)


def test_raw_and_latest_n_baselines_are_deterministic_and_bounded() -> None:
    events = _events()
    raw = RawEventBaselineRenderer().render(tuple(reversed(events)))
    state = RepositoryProjector().project(events)
    latest = LatestNBaselineRenderer(2).render(state)

    assert raw.included_references == ("1", "2", "3")
    assert latest.included_references == ("claim:3", "claim:2")
    assert latest.omitted_count == 1
    assert raw.content_id == RawEventBaselineRenderer().render(events).content_id

