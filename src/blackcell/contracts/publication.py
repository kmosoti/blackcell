"""Strongly typed publication identity and workflow contracts."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class PublicationStage(StrEnum):
    COMMIT = "commit"
    PUSH = "push"
    PULL_REQUEST = "pull_request"


class GitIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    email: str


class CommitSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sha: str
    author: GitIdentity


class PushTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    remote: str
    url: str
    host: str
    repository: str


class PullRequestSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    number: int
    author_login: str
    is_draft: bool
    state: str
    base_branch: str
    head_branch: str
    url: str


class PublicationSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    stage: PublicationStage
    branch: str
    configured_identity: GitIdentity
    head: CommitSnapshot | None = None
    push_target: PushTarget | None = None
    github_login: str | None = None
    pull_request: PullRequestSnapshot | None = None


class InvariantCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    invariant: str
    passed: bool
    expected: str
    actual: str | None = None


class PublicationPreflight(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    stage: PublicationStage
    ready: bool
    checks: tuple[InvariantCheck, ...] = Field(default_factory=tuple)
    snapshot: PublicationSnapshot
