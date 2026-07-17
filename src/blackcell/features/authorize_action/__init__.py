"""Fail-closed action authorization over symbolic proofs."""

from blackcell.features.authorize_action.artifacts import (
    ACTION_PROPOSAL_MEDIA_TYPE,
    AUTHORIZATION_DECISION_MEDIA_TYPE,
    AuthorizationArtifactCodecError,
    decode_action_proposal,
    decode_authorization_decision,
    encode_action_proposal,
    encode_authorization_decision,
)
from blackcell.features.authorize_action.command import AuthorizeAction
from blackcell.features.authorize_action.handler import authorize_action
from blackcell.features.authorize_action.models import (
    ActionArgument,
    ActionProposal,
    AffordancePolicy,
    AuthorizationDecision,
    AuthorizationFinding,
    AuthorizationOutcome,
)

__all__ = [
    "ACTION_PROPOSAL_MEDIA_TYPE",
    "AUTHORIZATION_DECISION_MEDIA_TYPE",
    "ActionArgument",
    "ActionProposal",
    "AffordancePolicy",
    "AuthorizationArtifactCodecError",
    "AuthorizationDecision",
    "AuthorizationFinding",
    "AuthorizationOutcome",
    "AuthorizeAction",
    "authorize_action",
    "decode_action_proposal",
    "decode_authorization_decision",
    "encode_action_proposal",
    "encode_authorization_decision",
]
