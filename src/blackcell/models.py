from dataclasses import dataclass

from blackcell.config.models import ProjectRef, RepositoryRef


@dataclass(frozen=True, slots=True)
class IssueRef:
    id: str
    number: int
    title: str
    url: str
    state: str
    repository: RepositoryRef
    body: str | None = None


@dataclass(frozen=True, slots=True)
class PullRequestRef:
    id: str
    number: int
    title: str
    url: str
    state: str
    is_draft: bool
    base_ref_name: str
    head_ref_name: str
    head_ref_oid: str
    repository: RepositoryRef
    body: str | None = None


@dataclass(frozen=True, slots=True)
class ProjectItemRef:
    id: str
    type: str
    is_archived: bool
    project: ProjectRef
    content_id: str | None = None
    content_title: str | None = None
    content_url: str | None = None
    content_type: str | None = None
