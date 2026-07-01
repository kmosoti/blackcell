import os
from typing import Any

import httpx

from blackcell.config.models import BlackcellConfig, ProjectRef, RepositoryRef
from blackcell.models import IssueRef, ProjectItemRef, PullRequestRef
from blackcell.providers.base import CreateIssueRequest, CreatePullRequestRequest

GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"
ISSUE_KEY_MARKER_PREFIX = "<!-- blackcell:issue-key "
PR_ISSUE_KEY_MARKER_PREFIX = "<!-- blackcell:pr-issue-key "


class GitHubApiError(RuntimeError):
    pass


class GitHubProjectsProvider:
    name = "github"

    def __init__(
        self,
        config: BlackcellConfig,
        *,
        token: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._config = config
        self._token = token or os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
        self._client = client or httpx.Client(timeout=20)

    def create_issue(self, request: CreateIssueRequest) -> IssueRef:
        repository_id = self._config.repository.node_id or self._repository_id()
        data = self._graphql(
            """
            mutation($repoId: ID!, $projectId: ID!, $title: String!, $body: String!) {
              createIssue(input: {
                repositoryId: $repoId
                projectV2Ids: [$projectId]
                title: $title
                body: $body
              }) {
                issue {
                  id
                  number
                  title
                  url
                  state
                  body
                  repository {
                    owner { login }
                    name
                  }
                }
              }
            }
            """,
            {
                "repoId": repository_id,
                "projectId": self._config.project.id,
                "title": request.title,
                "body": request.body,
            },
        )
        return _issue_ref(data["createIssue"]["issue"])

    def read_issue(self, number: int) -> IssueRef:
        data = self._graphql(
            """
            query($owner: String!, $repo: String!, $number: Int!) {
              repository(owner: $owner, name: $repo) {
                issue(number: $number) {
                  id
                  number
                  title
                  url
                  state
                  body
                  repository {
                    owner { login }
                    name
                  }
                }
              }
            }
            """,
            {
                "owner": self._config.repository.owner,
                "repo": self._config.repository.name,
                "number": number,
            },
        )
        return _issue_ref(data["repository"]["issue"])

    def read_issue_by_id(self, issue_id: str) -> IssueRef | None:
        data = self._graphql(
            """
            query($issueId: ID!) {
              node(id: $issueId) {
                ... on Issue {
                  id
                  number
                  title
                  url
                  state
                  body
                  repository {
                    owner { login }
                    name
                  }
                }
              }
            }
            """,
            {"issueId": issue_id},
        )
        issue = data.get("node")
        if not isinstance(issue, dict):
            return None
        return _issue_ref(issue)

    def list_repository_issues(self, *, first: int = 100) -> list[IssueRef]:
        issues: list[IssueRef] = []
        cursor: str | None = None
        remaining = first

        while remaining > 0:
            page_size = min(remaining, 100)
            data = self._graphql(
                """
                query($owner: String!, $repo: String!, $first: Int!, $after: String) {
                  repository(owner: $owner, name: $repo) {
                    issues(
                      first: $first
                      after: $after
                      states: [OPEN, CLOSED]
                      orderBy: {field: UPDATED_AT, direction: DESC}
                    ) {
                      nodes {
                        id
                        number
                        title
                        url
                        state
                        body
                        repository {
                          owner { login }
                          name
                        }
                      }
                      pageInfo {
                        hasNextPage
                        endCursor
                      }
                    }
                  }
                }
                """,
                {
                    "owner": self._config.repository.owner,
                    "repo": self._config.repository.name,
                    "first": page_size,
                    "after": cursor,
                },
            )
            connection = data["repository"]["issues"]
            nodes = connection.get("nodes") or []
            issues.extend(_issue_ref(issue) for issue in nodes)
            remaining -= len(nodes)

            page_info = connection.get("pageInfo") or {}
            cursor = page_info.get("endCursor")
            if not page_info.get("hasNextPage") or not nodes:
                break

        return issues

    def find_issues_by_blackcell_marker(self, issue_key: str) -> list[IssueRef]:
        return [
            issue
            for issue in self.list_repository_issues(first=100)
            if _has_blackcell_issue_marker(issue.body or "", issue_key)
        ]

    def find_issues_by_exact_title(self, title: str) -> list[IssueRef]:
        return [issue for issue in self.list_repository_issues(first=100) if issue.title == title]

    def update_issue(self, *, issue_id: str, title: str, body: str) -> IssueRef:
        data = self._graphql(
            """
            mutation($issueId: ID!, $title: String!, $body: String!) {
              updateIssue(input: {
                id: $issueId
                title: $title
                body: $body
              }) {
                issue {
                  id
                  number
                  title
                  url
                  state
                  body
                  repository {
                    owner { login }
                    name
                  }
                }
              }
            }
            """,
            {"issueId": issue_id, "title": title, "body": body},
        )
        return _issue_ref(data["updateIssue"]["issue"])

    def list_project_items(self, *, first: int = 20) -> list[ProjectItemRef]:
        items: list[ProjectItemRef] = []
        cursor: str | None = None
        remaining = first

        while remaining > 0:
            page_size = min(remaining, 100)
            data = self._graphql(
                """
                query($projectId: ID!, $first: Int!, $after: String) {
                  node(id: $projectId) {
                    ... on ProjectV2 {
                      id
                      number
                      title
                      url
                      items(first: $first, after: $after) {
                        nodes {
                          id
                          type
                          isArchived
                          content {
                            __typename
                            ... on Issue {
                              id
                              title
                              url
                            }
                            ... on PullRequest {
                              id
                              title
                              url
                            }
                            ... on DraftIssue {
                              title
                              body
                            }
                          }
                        }
                        pageInfo {
                          hasNextPage
                          endCursor
                        }
                      }
                    }
                  }
                }
                """,
                {"projectId": self._config.project.id, "first": page_size, "after": cursor},
            )

            project = data["node"]
            project_ref = ProjectRef(
                id=project["id"],
                number=project["number"],
                title=project["title"],
                url=project["url"],
            )
            connection = project["items"]
            nodes = connection.get("nodes") or []
            items.extend(_project_item_ref(project_ref, item) for item in nodes)
            remaining -= len(nodes)

            page_info = connection.get("pageInfo") or {}
            cursor = page_info.get("endCursor")
            if not page_info.get("hasNextPage") or not nodes:
                break

        return items

    def add_project_item_by_id(self, content_id: str) -> ProjectItemRef:
        try:
            data = self._graphql(
                """
                mutation($projectId: ID!, $contentId: ID!) {
                  addProjectV2ItemById(input: {
                    projectId: $projectId
                    contentId: $contentId
                  }) {
                    item {
                      id
                      type
                      isArchived
                      content {
                        __typename
                        ... on Issue {
                          id
                          title
                          url
                        }
                        ... on PullRequest {
                          id
                          title
                          url
                        }
                        ... on DraftIssue {
                          title
                          body
                        }
                      }
                    }
                  }
                }
                """,
                {"projectId": self._config.project.id, "contentId": content_id},
            )
        except GitHubApiError as error:
            existing = self._existing_project_item(content_id, error)
            if existing is not None:
                return existing
            raise
        return _project_item_ref(self._config.project, data["addProjectV2ItemById"]["item"])

    def _existing_project_item(
        self,
        content_id: str,
        error: GitHubApiError,
    ) -> ProjectItemRef | None:
        if "Content already exists in this project" not in str(error):
            return None
        for item in self.list_project_items(first=100):
            if item.content_id == content_id:
                return item
        return None

    def read_pull_request_by_id(self, pull_request_id: str) -> PullRequestRef | None:
        data = self._graphql(
            """
            query($pullRequestId: ID!) {
              node(id: $pullRequestId) {
                ... on PullRequest {
                  id
                  number
                  title
                  url
                  state
                  isDraft
                  baseRefName
                  headRefName
                  headRefOid
                  body
                  repository {
                    owner { login }
                    name
                  }
                }
              }
            }
            """,
            {"pullRequestId": pull_request_id},
        )
        pull_request = data.get("node")
        if not isinstance(pull_request, dict):
            return None
        return _pull_request_ref(pull_request)

    def list_repository_pull_requests(self, *, first: int = 100) -> list[PullRequestRef]:
        pull_requests: list[PullRequestRef] = []
        cursor: str | None = None
        remaining = first

        while remaining > 0:
            page_size = min(remaining, 100)
            data = self._graphql(
                """
                query($owner: String!, $repo: String!, $first: Int!, $after: String) {
                  repository(owner: $owner, name: $repo) {
                    pullRequests(
                      first: $first
                      after: $after
                      states: [OPEN]
                      orderBy: {field: UPDATED_AT, direction: DESC}
                    ) {
                      nodes {
                        id
                        number
                        title
                        url
                        state
                        isDraft
                        baseRefName
                        headRefName
                        headRefOid
                        body
                        repository {
                          owner { login }
                          name
                        }
                      }
                      pageInfo {
                        hasNextPage
                        endCursor
                      }
                    }
                  }
                }
                """,
                {
                    "owner": self._config.repository.owner,
                    "repo": self._config.repository.name,
                    "first": page_size,
                    "after": cursor,
                },
            )
            connection = data["repository"]["pullRequests"]
            nodes = connection.get("nodes") or []
            pull_requests.extend(_pull_request_ref(pull_request) for pull_request in nodes)
            remaining -= len(nodes)

            page_info = connection.get("pageInfo") or {}
            cursor = page_info.get("endCursor")
            if not page_info.get("hasNextPage") or not nodes:
                break

        return pull_requests

    def find_pull_requests_by_blackcell_marker(self, issue_key: str) -> list[PullRequestRef]:
        return [
            pull_request
            for pull_request in self.list_repository_pull_requests(first=100)
            if _has_blackcell_pull_request_marker(pull_request.body or "", issue_key)
        ]

    def find_pull_requests_by_head(self, head_ref_name: str) -> list[PullRequestRef]:
        return [
            pull_request
            for pull_request in self.list_repository_pull_requests(first=100)
            if pull_request.head_ref_name == head_ref_name
        ]

    def create_pull_request(self, request: CreatePullRequestRequest) -> PullRequestRef:
        repository_id = self._config.repository.node_id or self._repository_id()
        data = self._graphql(
            """
            mutation(
              $repoId: ID!
              $baseRefName: String!
              $headRefName: String!
              $title: String!
              $body: String!
              $draft: Boolean!
            ) {
              createPullRequest(input: {
                repositoryId: $repoId
                baseRefName: $baseRefName
                headRefName: $headRefName
                title: $title
                body: $body
                draft: $draft
              }) {
                pullRequest {
                  id
                  number
                  title
                  url
                  state
                  isDraft
                  baseRefName
                  headRefName
                  headRefOid
                  body
                  repository {
                    owner { login }
                    name
                  }
                }
              }
            }
            """,
            {
                "repoId": repository_id,
                "baseRefName": request.base_ref_name,
                "headRefName": request.head_ref_name,
                "title": request.title,
                "body": request.body,
                "draft": request.draft,
            },
        )
        return _pull_request_ref(data["createPullRequest"]["pullRequest"])

    def update_pull_request(self, *, pull_request_id: str, title: str, body: str) -> PullRequestRef:
        data = self._graphql(
            """
            mutation($pullRequestId: ID!, $title: String!, $body: String!) {
              updatePullRequest(input: {
                pullRequestId: $pullRequestId
                title: $title
                body: $body
              }) {
                pullRequest {
                  id
                  number
                  title
                  url
                  state
                  isDraft
                  baseRefName
                  headRefName
                  headRefOid
                  body
                  repository {
                    owner { login }
                    name
                  }
                }
              }
            }
            """,
            {"pullRequestId": pull_request_id, "title": title, "body": body},
        )
        return _pull_request_ref(data["updatePullRequest"]["pullRequest"])

    def mark_pull_request_ready_for_review(self, pull_request_id: str) -> PullRequestRef:
        data = self._graphql(
            """
            mutation($pullRequestId: ID!) {
              markPullRequestReadyForReview(input: {
                pullRequestId: $pullRequestId
              }) {
                pullRequest {
                  id
                  number
                  title
                  url
                  state
                  isDraft
                  baseRefName
                  headRefName
                  headRefOid
                  body
                  repository {
                    owner { login }
                    name
                  }
                }
              }
            }
            """,
            {"pullRequestId": pull_request_id},
        )
        return _pull_request_ref(data["markPullRequestReadyForReview"]["pullRequest"])

    def _repository_id(self) -> str:
        data = self._graphql(
            """
            query($owner: String!, $repo: String!) {
              repository(owner: $owner, name: $repo) {
                id
              }
            }
            """,
            {"owner": self._config.repository.owner, "repo": self._config.repository.name},
        )
        return data["repository"]["id"]

    def _graphql(self, query: str, variables: dict[str, object]) -> dict[str, Any]:
        if not self._token:
            raise GitHubApiError("GITHUB_TOKEN or GH_TOKEN is required for GitHub API calls")

        response = self._client.post(
            GITHUB_GRAPHQL_URL,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
            },
            json={"query": query, "variables": variables},
        )
        response.raise_for_status()
        payload = response.json()

        if errors := payload.get("errors"):
            raise GitHubApiError(str(errors))

        data = payload.get("data")
        if not isinstance(data, dict):
            raise GitHubApiError("GitHub GraphQL response did not include data")
        return data


def _issue_ref(data: dict[str, Any]) -> IssueRef:
    repository = data["repository"]
    return IssueRef(
        id=data["id"],
        number=data["number"],
        title=data["title"],
        url=data["url"],
        state=data["state"],
        repository=RepositoryRef(owner=repository["owner"]["login"], name=repository["name"]),
        body=data.get("body"),
    )


def _pull_request_ref(data: dict[str, Any]) -> PullRequestRef:
    repository = data["repository"]
    return PullRequestRef(
        id=data["id"],
        number=data["number"],
        title=data["title"],
        url=data["url"],
        state=data["state"],
        is_draft=data["isDraft"],
        base_ref_name=data["baseRefName"],
        head_ref_name=data["headRefName"],
        head_ref_oid=data["headRefOid"],
        repository=RepositoryRef(owner=repository["owner"]["login"], name=repository["name"]),
        body=data.get("body"),
    )


def _has_blackcell_issue_marker(body: str, issue_key: str) -> bool:
    return ISSUE_KEY_MARKER_PREFIX + issue_key + " -->" in body


def _has_blackcell_pull_request_marker(body: str, issue_key: str) -> bool:
    return PR_ISSUE_KEY_MARKER_PREFIX + issue_key + " -->" in body


def _project_item_ref(project: ProjectRef, data: dict[str, Any]) -> ProjectItemRef:
    content = data.get("content") or {}
    return ProjectItemRef(
        id=data["id"],
        type=data["type"],
        is_archived=data["isArchived"],
        project=project,
        content_id=content.get("id"),
        content_title=content.get("title"),
        content_url=content.get("url"),
        content_type=content.get("__typename"),
    )
