from blackcell.config import BlackcellConfig, ProjectRef, RepositoryRef
from blackcell.control_plane.rendering import has_blackcell_issue_marker
from blackcell.models import IssueRef, ProjectItemRef
from blackcell.providers import CreateIssueRequest, ProviderRegistry


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
