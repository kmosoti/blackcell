from dataclasses import dataclass
from datetime import datetime

from blackcell.features.authorize_action.models import ActionProposal, AffordancePolicy


@dataclass(frozen=True, slots=True)
class AuthorizeAction:
    proposal: ActionProposal
    affordance: AffordancePolicy
    evaluated_at: datetime
    context_evidence_event_ids: tuple[str, ...]
    approval_granted: bool = False

    def __post_init__(self) -> None:
        if self.proposal.affordance != self.affordance.name:
            raise ValueError("proposal affordance does not match its policy")
        if self.evaluated_at.tzinfo is None or self.evaluated_at.utcoffset() is None:
            raise ValueError("evaluated_at must be timezone-aware")
        if len(self.context_evidence_event_ids) != len(set(self.context_evidence_event_ids)):
            raise ValueError("context evidence event ids must be unique")
