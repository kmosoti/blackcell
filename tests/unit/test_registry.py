from blackcell.config import BlackcellConfig, ProjectRef, RepositoryRef
from blackcell.control_plane.rendering import (
    has_blackcell_issue_marker,
    has_blackcell_pull_request_marker,
)
from blackcell.models import IssueRef, ProjectItemRef, PullRequestRef
from blackcell.providers import CreateIssueRequest, CreatePullRequestRequest, ProviderRegistry


class MemoryProvider:
    name = "memory"

    def __init__(self, config: BlackcellConfig) -> None:
        self.config = config

    def create_issue(self, request: CreateIssueRequest) -> IssueRef:
        return IssueRef(
            id="I_memory",
            number=1,
            title=request.title,
            url="https://example.test/1",
            state="OPEN",
            repository=self.config.repository,
        )

    def read_issue(self, number: int) -> IssueRef:
        return IssueRef(
            id="I_memory",
            number=number,
            title="Memory",
            url=f"https://example.test/{number}",
            state="OPEN",
            repository=self.config.repository,
        )

    def read_issue_by_id(self, issue_id: str) -> IssueRef | None:
        if issue_id != "I_memory":
            return None
        return self.read_issue(1)

    def list_repository_issues(self, *, first: int = 100) -> list[IssueRef]:
        return [self.read_issue(1)]

    def find_issues_by_blackcell_marker(self, issue_key: str) -> list[IssueRef]:
        return [
            issue
            for issue in self.list_repository_issues()
            if has_blackcell_issue_marker(issue.body or "", issue_key)
        ]

    def find_issues_by_exact_title(self, title: str) -> list[IssueRef]:
        return [issue for issue in self.list_repository_issues() if issue.title == title]

    def update_issue(self, *, issue_id: str, title: str, body: str) -> IssueRef:
        return IssueRef(
            id=issue_id,
            number=1,
            title=title,
            url="https://example.test/1",
            state="OPEN",
            repository=self.config.repository,
            body=body,
        )

    def read_pull_request_by_id(self, pull_request_id: str) -> PullRequestRef | None:
        if pull_request_id != "PR_memory":
            return None
        return self._pull_request()

    def list_repository_pull_requests(self, *, first: int = 100) -> list[PullRequestRef]:
        return [self._pull_request()][:first]

    def find_pull_requests_by_blackcell_marker(self, issue_key: str) -> list[PullRequestRef]:
        return [
            pull_request
            for pull_request in self.list_repository_pull_requests()
            if has_blackcell_pull_request_marker(pull_request.body or "", issue_key)
        ]

    def find_pull_requests_by_head(self, head_ref_name: str) -> list[PullRequestRef]:
        return [
            pull_request
            for pull_request in self.list_repository_pull_requests()
            if pull_request.head_ref_name == head_ref_name
        ]

    def create_pull_request(self, request: CreatePullRequestRequest) -> PullRequestRef:
        return PullRequestRef(
            id="PR_memory",
            number=1,
            title=request.title,
            url="https://example.test/pull/1",
            state="OPEN",
            is_draft=request.draft,
            base_ref_name=request.base_ref_name,
            head_ref_name=request.head_ref_name,
            head_ref_oid="HEAD",
            repository=self.config.repository,
            body=request.body,
        )

    def update_pull_request(self, *, pull_request_id: str, title: str, body: str) -> PullRequestRef:
        pull_request = self.read_pull_request_by_id(pull_request_id)
        if pull_request is None:
            raise ValueError(f"unknown pull request: {pull_request_id}")
        return PullRequestRef(
            id=pull_request.id,
            number=pull_request.number,
            title=title,
            url=pull_request.url,
            state=pull_request.state,
            is_draft=pull_request.is_draft,
            base_ref_name=pull_request.base_ref_name,
            head_ref_name=pull_request.head_ref_name,
            head_ref_oid=pull_request.head_ref_oid,
            repository=pull_request.repository,
            body=body,
        )

    def mark_pull_request_ready_for_review(self, pull_request_id: str) -> PullRequestRef:
        pull_request = self.read_pull_request_by_id(pull_request_id)
        if pull_request is None:
            raise ValueError(f"unknown pull request: {pull_request_id}")
        return PullRequestRef(
            id=pull_request.id,
            number=pull_request.number,
            title=pull_request.title,
            url=pull_request.url,
            state=pull_request.state,
            is_draft=False,
            base_ref_name=pull_request.base_ref_name,
            head_ref_name=pull_request.head_ref_name,
            head_ref_oid=pull_request.head_ref_oid,
            repository=pull_request.repository,
            body=pull_request.body,
        )

    def list_project_items(self, *, first: int = 20) -> list[ProjectItemRef]:
        return []

    def add_project_item_by_id(self, content_id: str) -> ProjectItemRef:
        return ProjectItemRef(
            id="PVTI_memory",
            type="ISSUE",
            is_archived=False,
            project=self.config.project,
            content_id=content_id,
        )

    def _pull_request(self) -> PullRequestRef:
        return PullRequestRef(
            id="PR_memory",
            number=1,
            title="Memory",
            url="https://example.test/pull/1",
            state="OPEN",
            is_draft=True,
            base_ref_name="main",
            head_ref_name="feature",
            head_ref_oid="HEAD",
            repository=self.config.repository,
        )


def test_registry_creates_registered_provider() -> None:
    config = BlackcellConfig(
        repository=RepositoryRef.parse("kmosoti/blackcell"),
        project=ProjectRef(id="PVT_123", title="BlackCell"),
    )
    registry = ProviderRegistry()
    registry.register(MemoryProvider.name, MemoryProvider)

    provider = registry.create("memory", config)

    assert provider.read_issue(5).number == 5
    assert registry.names() == ["memory"]
