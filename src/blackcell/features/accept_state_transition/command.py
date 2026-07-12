from __future__ import annotations

from dataclasses import dataclass

from blackcell.features.accept_state_transition.models import (
    AuthorizationReference,
    EvaluationReference,
    ExecutionReference,
    ProposalReference,
    TransitionEventReference,
    TransitionStateView,
)


@dataclass(frozen=True, slots=True)
class AcceptStateTransition:
    """Request an evidence-scoped transition attestation.

    Raw DTO/reference constructors, derived IDs, and strict codecs prove internal content
    consistency only; they do not prove that any referenced artifact or event exists.  The
    canonical workflow binder must verify every owner artifact and ledger reference before this
    command crosses the feature boundary.  The v2 writer and replay path remain inactive until
    that binder is composed and verified.  This slice then deterministically validates
    cross-identities and computes only deltas supported by definitive evaluation evidence.
    """

    run_id: str
    initial_state: TransitionStateView
    outcome_state: TransitionStateView | None
    proposal: ProposalReference
    authorization: AuthorizationReference
    execution: ExecutionReference | None
    evaluation: EvaluationReference
    triggering_events: tuple[TransitionEventReference, ...] = ()

    def __post_init__(self) -> None:
        if not self.run_id.strip():
            raise ValueError("run_id must not be empty")
        events = tuple(sorted(self.triggering_events))
        ids = tuple(item.event_id for item in events)
        positions = tuple(item.global_position for item in events)
        if len(ids) != len(set(ids)) or len(positions) != len(set(positions)):
            raise ValueError("triggering event ids and global positions must be unique")
        object.__setattr__(self, "triggering_events", events)


__all__ = ["AcceptStateTransition"]
