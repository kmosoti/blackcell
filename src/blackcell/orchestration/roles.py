from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from blackcell.gateway import DataClassification, LocalityPolicy, ModelCapability
from blackcell.orchestration.models import DagNode, NodeSideEffect, OrchestrationRole


@dataclass(frozen=True, slots=True)
class RoleProfile:
    role: OrchestrationRole
    allowed_capabilities: tuple[ModelCapability, ...]
    maximum_classification: DataClassification
    allowed_localities: tuple[LocalityPolicy, ...]
    deterministic_required: bool
    may_execute: bool
    may_approve: bool
    may_synthesize: bool


ROLE_PROFILES: Mapping[OrchestrationRole, RoleProfile] = MappingProxyType(
    {
        OrchestrationRole.PLANNER: RoleProfile(
            OrchestrationRole.PLANNER,
            (ModelCapability.REASON,),
            DataClassification.PRIVATE,
            (LocalityPolicy.LOCAL_ONLY, LocalityPolicy.REMOTE_ALLOWED),
            False,
            False,
            False,
            False,
        ),
        OrchestrationRole.EXECUTOR: RoleProfile(
            OrchestrationRole.EXECUTOR,
            (ModelCapability.CODE, ModelCapability.REASON),
            DataClassification.PRIVATE,
            (LocalityPolicy.LOCAL_ONLY, LocalityPolicy.REMOTE_ALLOWED),
            False,
            True,
            False,
            False,
        ),
        OrchestrationRole.REVIEWER: RoleProfile(
            OrchestrationRole.REVIEWER,
            (ModelCapability.REVIEW,),
            DataClassification.PRIVATE,
            (LocalityPolicy.LOCAL_ONLY, LocalityPolicy.REMOTE_ALLOWED),
            False,
            False,
            True,
            False,
        ),
        OrchestrationRole.VERIFIER: RoleProfile(
            OrchestrationRole.VERIFIER,
            (ModelCapability.VERIFY,),
            DataClassification.SECRET,
            (LocalityPolicy.LOCAL_ONLY,),
            True,
            False,
            True,
            False,
        ),
        OrchestrationRole.SYNTHESIZER: RoleProfile(
            OrchestrationRole.SYNTHESIZER,
            (ModelCapability.REASON,),
            DataClassification.INTERNAL,
            (LocalityPolicy.LOCAL_ONLY, LocalityPolicy.REMOTE_ALLOWED),
            False,
            False,
            False,
            True,
        ),
    }
)


class RolePolicyError(ValueError):
    pass


def validate_role_policy(node: DagNode) -> None:
    profile = ROLE_PROFILES[node.role]
    capability = node.model_capability
    if capability is not None and capability not in profile.allowed_capabilities:
        raise RolePolicyError(
            f"role {node.role.value!r} cannot request capability {capability.value!r}"
        )
    if capability is not None and node.classification > profile.maximum_classification:
        raise RolePolicyError("node classification exceeds its role gateway policy")
    if capability is not None and node.locality not in profile.allowed_localities:
        raise RolePolicyError("node locality exceeds its role gateway policy")
    if (
        capability is not None
        and profile.deterministic_required
        and not node.deterministic_required
    ):
        raise RolePolicyError("node role requires deterministic gateway routing")
    if node.side_effect is NodeSideEffect.IRREVERSIBLE:
        raise RolePolicyError("irreversible nodes require authority outside the scheduler")
    if node.side_effect is not NodeSideEffect.NONE and not profile.may_execute:
        raise RolePolicyError("only executor nodes may declare an execution side effect")
    if node.required_approvals:
        allowed = {OrchestrationRole.REVIEWER, OrchestrationRole.VERIFIER}
        if any(role not in allowed for role in node.required_approvals):
            raise RolePolicyError("only reviewer or verifier roles may approve scheduled work")
        if node.role in node.required_approvals:
            raise RolePolicyError("a node role cannot approve its own work")
    if node.side_effect is NodeSideEffect.REVERSIBLE and not node.required_approvals:
        raise RolePolicyError("reversible execution requires reviewer or verifier approval")
    if node.role is OrchestrationRole.SYNTHESIZER and node.handler == "override-denial":
        raise RolePolicyError("a synthesizer cannot override a symbolic denial")


def role_profile(role: OrchestrationRole) -> RoleProfile:
    return ROLE_PROFILES[role]


__all__ = [
    "ROLE_PROFILES",
    "RolePolicyError",
    "RoleProfile",
    "role_profile",
    "validate_role_policy",
]
