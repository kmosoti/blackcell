from datetime import UTC, datetime, timedelta

from blackcell.context import SignalPacketProjector
from blackcell.domains.repository import RepositoryProjector
from blackcell.domains.repository.adapter import observe_repository


def test_signal_packet_is_deterministic_provenance_linked_and_not_a_context_frame(
    tmp_path,
) -> None:
    (tmp_path / "README.md").write_text("# Fixture\n", encoding="utf-8")
    now = datetime(2026, 7, 9, 12, tzinfo=UTC)
    events = observe_repository(
        tmp_path,
        paths=("README.md", "missing.txt"),
        observed_at=now,
    )
    state = RepositoryProjector().project(events, as_of_time=now + timedelta(minutes=6))

    first = SignalPacketProjector().project(state)
    second = SignalPacketProjector().project(state)

    assert first == second
    assert first.packet_id.startswith("signal:")
    assert first.state_id == state.state_id
    assert first.claim_ids == tuple(claim.claim_id for claim in state.current_claims)
    assert first.measurements[0].name == "claims.current"
    assert first.measurements[0].evidence_ids
    assert not hasattr(first, "rendered_context")
