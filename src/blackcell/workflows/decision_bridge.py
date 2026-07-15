"""Pure translation from gateway-owned proposals to authorization-owned proposals."""

from __future__ import annotations

from blackcell.features.authorize_action import ActionArgument, ActionProposal
from blackcell.features.request_decision import DecisionProposal


def action_proposal_from_decision(proposal: DecisionProposal) -> ActionProposal:
    """Preserve proposal semantics while changing the owning schema and identity.

    The gateway proposal proves what the model returned.  The action proposal is a
    separate, content-addressed authorization input; their digests are intentionally
    not interchangeable.
    """

    if not isinstance(proposal, DecisionProposal):
        raise TypeError("proposal must be a DecisionProposal")
    return ActionProposal(
        proposal_id=proposal.proposal_id,
        context_frame_id=proposal.context_frame_id,
        affordance=proposal.affordance,
        arguments=tuple(ActionArgument(item.name, item.value) for item in proposal.arguments),
        rationale=proposal.rationale,
        evidence_event_ids=proposal.evidence_event_ids,
    )


__all__ = ["action_proposal_from_decision"]
