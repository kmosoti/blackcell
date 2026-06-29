"""Provider-neutral facade and invariant classification."""

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum


class Facade(StrEnum):
    PROFILE = "profile"
    SCHEMA = "schema"
    DIRECTIVE = "directive"
    OPERATION = "operation"
    ASSIGNMENT = "assignment"
    ECHO = "echo"
    RECON = "recon"
    WORKFLOW = "workflow"
    CHRONICLE = "chronicle"
    ANOMALY = "anomaly"
    PUBLICATION = "publication"
    SYSTEM = "system"


class Authority(StrEnum):
    BLACKCELL = "blackcell"
    LINEAR = "linear"
    GITHUB = "github"
    LOCAL_GIT = "local_git"
    CROSS_SYSTEM = "cross_system"


class Effect(StrEnum):
    READ = "read"
    APPEND = "append"
    RECONCILE = "reconcile"
    MUTATE = "mutate"


class Credential(StrEnum):
    LINEAR = "linear"
    GITHUB = "github"


class InvariantAspect(StrEnum):
    INPUT = "input"
    AUTHENTICATION = "authentication"
    IDENTITY = "identity"
    AUTHORITY = "authority"
    STATE = "state"
    IMMUTABILITY = "immutability"
    IDEMPOTENCY = "idempotency"
    OUTPUT = "output"
    OBSERVABILITY = "observability"
    PUBLICATION_IDENTITY = "publication_identity"


class InvariantGroup(StrEnum):
    SCHEMA = "schema"
    AUTHORITY = "authority"
    CREDENTIAL = "credential"
    IDENTITY = "identity"
    LIFECYCLE = "lifecycle"
    DIGEST = "digest"
    PROJECT_WORKFLOW = "project_workflow"
    PROJECT_PRESENTATION = "project_presentation"
    ASSIGNMENT_CONTRACT = "assignment_contract"
    ECHO_CONTRACT = "echo_contract"
    PUBLICATION_IDENTITY = "publication_identity"
    OUTPUT = "output"
    OBSERVABILITY = "observability"


@dataclass(frozen=True, slots=True, kw_only=True)
class OperationSpec:
    """The invariant contract for one public BlackCell operation."""

    name: str
    facade: Facade
    authority: Authority
    effect: Effect
    aspects: frozenset[InvariantAspect]
    invariant_groups: frozenset[InvariantGroup]
    credentials: frozenset[Credential] = frozenset()


def _infer_invariant_groups(aspects: Iterable[InvariantAspect]) -> frozenset[InvariantGroup]:
    normalized = frozenset(aspects)
    groups = {
        InvariantGroup.SCHEMA: {InvariantAspect.INPUT},
        InvariantGroup.AUTHORITY: {InvariantAspect.AUTHORITY},
        InvariantGroup.CREDENTIAL: {InvariantAspect.AUTHENTICATION},
        InvariantGroup.IDENTITY: {
            InvariantAspect.IDENTITY,
            InvariantAspect.AUTHENTICATION,
        },
        InvariantGroup.LIFECYCLE: {InvariantAspect.STATE},
        InvariantGroup.DIGEST: {InvariantAspect.IMMUTABILITY},
        InvariantGroup.ASSIGNMENT_CONTRACT: {InvariantAspect.IDEMPOTENCY},
        InvariantGroup.OUTPUT: {InvariantAspect.OUTPUT},
        InvariantGroup.OBSERVABILITY: {InvariantAspect.OBSERVABILITY},
        InvariantGroup.PUBLICATION_IDENTITY: {InvariantAspect.PUBLICATION_IDENTITY},
    }
    if {
        InvariantAspect.INPUT,
        InvariantAspect.AUTHENTICATION,
        InvariantAspect.IDENTITY,
        InvariantAspect.AUTHORITY,
    }.issubset(normalized):
        groups[InvariantGroup.PROJECT_WORKFLOW] = {InvariantAspect.STATE}
        groups[InvariantGroup.PROJECT_PRESENTATION] = {InvariantAspect.STATE}
    return frozenset(group for group, members in groups.items() if normalized.intersection(members))


def operation(
    name: str,
    facade: Facade,
    authority: Authority,
    effect: Effect,
    *aspects: InvariantAspect,
    credentials: tuple[Credential, ...] = (),
    invariant_groups: tuple[InvariantGroup, ...] | None = None,
) -> OperationSpec:
    """Build an immutable operation contract without repeating baseline aspects."""
    resolved_aspects = frozenset(
        {
            InvariantAspect.OUTPUT,
            InvariantAspect.OBSERVABILITY,
            *aspects,
        }
    )
    if invariant_groups is None:
        resolved_groups = _infer_invariant_groups(resolved_aspects)
    else:
        resolved_groups = frozenset(
            {
                InvariantGroup.OUTPUT,
                InvariantGroup.OBSERVABILITY,
                *invariant_groups,
            }
        )
    return OperationSpec(
        name=name,
        facade=facade,
        authority=authority,
        effect=effect,
        aspects=resolved_aspects,
        invariant_groups=resolved_groups,
        credentials=frozenset(credentials),
    )
