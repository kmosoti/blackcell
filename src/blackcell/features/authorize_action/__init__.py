"""Fail-closed action authorization over symbolic proofs."""

from blackcell.features.authorize_action.command import AuthorizeAction
from blackcell.features.authorize_action.handler import ActionAuthorizer
from blackcell.features.authorize_action.models import (
    ActionArgument,
    ActionProposal,
    AffordancePolicy,
    AuthorizationDecision,
    AuthorizationFinding,
    AuthorizationOutcome,
)

__all__ = [
    "ActionArgument",
    "ActionAuthorizer",
    "ActionProposal",
    "AffordancePolicy",
    "AuthorizationDecision",
    "AuthorizationFinding",
    "AuthorizationOutcome",
    "AuthorizeAction",
]
