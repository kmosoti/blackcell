"""Provider-neutral facade and invariant classification."""

from dataclasses import dataclass
from enum import StrEnum


class Facade(StrEnum):
    PROFILE = "profile"
    DIRECTIVE = "directive"
    OPERATION = "operation"
    ASSIGNMENT = "assignment"
    ECHO = "echo"
    RECON = "recon"
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


@dataclass(frozen=True, slots=True, kw_only=True)
class OperationSpec:
    """The invariant contract for one public BlackCell operation."""

    name: str
    facade: Facade
    authority: Authority
    effect: Effect
    aspects: frozenset[InvariantAspect]
    credentials: frozenset[Credential] = frozenset()


def operation(
    name: str,
    facade: Facade,
    authority: Authority,
    effect: Effect,
    *aspects: InvariantAspect,
    credentials: tuple[Credential, ...] = (),
) -> OperationSpec:
    """Build an immutable operation contract without repeating baseline aspects."""
    return OperationSpec(
        name=name,
        facade=facade,
        authority=authority,
        effect=effect,
        aspects=frozenset(
            {
                InvariantAspect.OUTPUT,
                InvariantAspect.OBSERVABILITY,
                *aspects,
            }
        ),
        credentials=frozenset(credentials),
    )
