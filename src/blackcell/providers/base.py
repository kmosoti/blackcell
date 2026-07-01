from dataclasses import dataclass
from typing import Protocol

from blackcell.models import IssueRef, ProjectItemRef, PullRequestRef


@dataclass(frozen=True, slots=True)
class CreateIssueRequest:
    title: str
    body: str


@dataclass(frozen=True, slots=True)
class CreatePullRequestRequest:
    title: str
    body: str
    base_ref_name: str
    head_ref_name: str
    draft: bool = True


class ProjectProvider(Protocol):
    name: str

    def create_issue(self, request: CreateIssueRequest) -> IssueRef:
        raise NotImplementedError

    def read_issue(self, number: int) -> IssueRef:
        raise NotImplementedError

    def read_issue_by_id(self, issue_id: str) -> IssueRef | None:
        raise NotImplementedError

    def list_repository_issues(self, *, first: int = 100) -> list[IssueRef]:
        raise NotImplementedError

    def find_issues_by_blackcell_marker(self, issue_key: str) -> list[IssueRef]:
        raise NotImplementedError

    def find_issues_by_exact_title(self, title: str) -> list[IssueRef]:
        raise NotImplementedError

    def update_issue(self, *, issue_id: str, title: str, body: str) -> IssueRef:
        raise NotImplementedError

    def list_project_items(self, *, first: int = 20) -> list[ProjectItemRef]:
        raise NotImplementedError

    def add_project_item_by_id(self, content_id: str) -> ProjectItemRef:
        raise NotImplementedError

    def read_pull_request_by_id(self, pull_request_id: str) -> PullRequestRef | None:
        raise NotImplementedError

    def list_repository_pull_requests(self, *, first: int = 100) -> list[PullRequestRef]:
        raise NotImplementedError

    def find_pull_requests_by_blackcell_marker(self, issue_key: str) -> list[PullRequestRef]:
        raise NotImplementedError

    def find_pull_requests_by_head(self, head_ref_name: str) -> list[PullRequestRef]:
        raise NotImplementedError

    def create_pull_request(self, request: CreatePullRequestRequest) -> PullRequestRef:
        raise NotImplementedError

    def update_pull_request(self, *, pull_request_id: str, title: str, body: str) -> PullRequestRef:
        raise NotImplementedError

    def mark_pull_request_ready_for_review(self, pull_request_id: str) -> PullRequestRef:
        raise NotImplementedError
