import os
from typing import Any

import httpx

from blackcell.config.models import BlackcellConfig, ProjectRef, RepositoryRef
from blackcell.models import (
    IssueRef,
    ProjectFieldOptionRef,
    ProjectFieldRef,
    ProjectItemFieldValueRef,
    ProjectItemRef,
    PullRequestRef,
)
from blackcell.providers.base import (
    CreateIssueRequest,
    CreateProjectFieldRequest,
    CreatePullRequestRequest,
    ProjectFieldValue,
)

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
                          fieldValues(first: 50) {
                            nodes {
                              __typename
                              ... on ProjectV2ItemFieldTextValue {
                                text
                                field {
                                  ... on ProjectV2FieldCommon {
                                    id
                                    name
                                  }
                                }
                              }
                              ... on ProjectV2ItemFieldNumberValue {
                                number
                                field {
                                  ... on ProjectV2FieldCommon {
                                    id
                                    name
                                  }
                                }
                              }
                              ... on ProjectV2ItemFieldSingleSelectValue {
                                optionId
                                name
                                field {
                                  ... on ProjectV2FieldCommon {
                                    id
                                    name
                                  }
                                }
                              }
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
                      fieldValues(first: 50) {
                        nodes {
                          __typename
                          ... on ProjectV2ItemFieldTextValue {
                            text
                            field {
                              ... on ProjectV2FieldCommon {
                                id
                                name
                              }
                            }
                          }
                          ... on ProjectV2ItemFieldNumberValue {
                            number
                            field {
                              ... on ProjectV2FieldCommon {
                                id
                                name
                              }
                            }
                          }
                          ... on ProjectV2ItemFieldSingleSelectValue {
                            optionId
                            name
                            field {
                              ... on ProjectV2FieldCommon {
                                id
                                name
                              }
                            }
                          }
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

    def archive_project_item(self, item_id: str) -> None:
        self._graphql(
            """
            mutation($projectId: ID!, $itemId: ID!) {
              archiveProjectV2Item(input: {
                projectId: $projectId
                itemId: $itemId
              }) {
                item {
                  id
                }
              }
            }
            """,
            {"projectId": self._config.project.id, "itemId": item_id},
        )

    def list_project_fields(self, *, first: int = 50) -> list[ProjectFieldRef]:
        fields: list[ProjectFieldRef] = []
        cursor: str | None = None
        remaining = first

        while remaining > 0:
            page_size = min(remaining, 100)
            data = self._graphql(
                """
                query($projectId: ID!, $first: Int!, $after: String) {
                  node(id: $projectId) {
                    ... on ProjectV2 {
                      fields(first: $first, after: $after) {
                        nodes {
                          __typename
                          ... on ProjectV2Field {
                            id
                            name
                            dataType
                          }
                          ... on ProjectV2SingleSelectField {
                            id
                            name
                            dataType
                            options {
                              id
                              name
                              color
                              description
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
            connection = data["node"]["fields"]
            nodes = connection.get("nodes") or []
            fields.extend(_project_field_ref(field) for field in nodes if field)
            remaining -= len(nodes)

            page_info = connection.get("pageInfo") or {}
            cursor = page_info.get("endCursor")
            if not page_info.get("hasNextPage") or not nodes:
                break

        return fields

    def create_project_field(self, request: CreateProjectFieldRequest) -> ProjectFieldRef:
        data = self._graphql(
            """
            mutation(
              $projectId: ID!
              $name: String!
              $dataType: ProjectV2CustomFieldType!
              $singleSelectOptions: [ProjectV2SingleSelectFieldOptionInput!]
            ) {
              createProjectV2Field(input: {
                projectId: $projectId
                name: $name
                dataType: $dataType
                singleSelectOptions: $singleSelectOptions
              }) {
                projectV2Field {
                  __typename
                  ... on ProjectV2Field {
                    id
                    name
                    dataType
                  }
                  ... on ProjectV2SingleSelectField {
                    id
                    name
                    dataType
                    options {
                      id
                      name
                      color
                      description
                    }
                  }
                }
              }
            }
            """,
            {
                "projectId": self._config.project.id,
                "name": request.name,
                "dataType": request.data_type,
                "singleSelectOptions": _single_select_option_inputs(request.single_select_options)
                if request.data_type == "SINGLE_SELECT"
                else None,
            },
        )
        return _project_field_ref(data["createProjectV2Field"]["projectV2Field"])

    def update_project_single_select_field_options(
        self,
        field: ProjectFieldRef,
        option_names: tuple[str, ...],
    ) -> ProjectFieldRef:
        option_inputs: list[dict[str, object]] = []
        existing_by_name = {option.name: option for option in field.options}
        for option_name in option_names:
            existing = existing_by_name.get(option_name)
            option_input = _single_select_option_input(option_name)
            if existing is not None:
                if existing.id is not None:
                    option_input["id"] = existing.id
                option_input["color"] = existing.color
                option_input["description"] = existing.description
            option_inputs.append(option_input)

        data = self._graphql(
            """
            mutation(
              $fieldId: ID!
              $singleSelectOptions: [ProjectV2SingleSelectFieldOptionInput!]
            ) {
              updateProjectV2Field(input: {
                fieldId: $fieldId
                singleSelectOptions: $singleSelectOptions
              }) {
                projectV2Field {
                  __typename
                  ... on ProjectV2SingleSelectField {
                    id
                    name
                    dataType
                    options {
                      id
                      name
                      color
                      description
                    }
                  }
                }
              }
            }
            """,
            {"fieldId": field.id, "singleSelectOptions": option_inputs},
        )
        return _project_field_ref(data["updateProjectV2Field"]["projectV2Field"])

    def update_project_item_field_value(
        self,
        *,
        item_id: str,
        field_id: str,
        value: ProjectFieldValue,
    ) -> None:
        self._graphql(
            """
            mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $value: ProjectV2FieldValue!) {
              updateProjectV2ItemFieldValue(input: {
                projectId: $projectId
                itemId: $itemId
                fieldId: $fieldId
                value: $value
              }) {
                projectV2Item {
                  id
                }
              }
            }
            """,
            {
                "projectId": self._config.project.id,
                "itemId": item_id,
                "fieldId": field_id,
                "value": value.to_graphql_value(),
            },
        )

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


def _project_field_ref(data: dict[str, Any]) -> ProjectFieldRef:
    return ProjectFieldRef(
        id=data["id"],
        name=data["name"],
        data_type=data["dataType"],
        options=tuple(
            ProjectFieldOptionRef(
                id=option.get("id"),
                name=option["name"],
                color=option.get("color") or _option_color(option["name"]),
                description=option.get("description") or "",
            )
            for option in data.get("options") or []
        ),
    )


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
        field_values=tuple(
            field_value
            for value in (data.get("fieldValues") or {}).get("nodes", [])
            if (field_value := _project_item_field_value_ref(value)) is not None
        ),
    )


def _project_item_field_value_ref(data: dict[str, Any]) -> ProjectItemFieldValueRef | None:
    field = data.get("field") or {}
    field_id = field.get("id")
    field_name = field.get("name")
    if field_id is None or field_name is None:
        return None

    type_name = data.get("__typename")
    if type_name == "ProjectV2ItemFieldTextValue":
        return ProjectItemFieldValueRef(
            field_id=field_id,
            field_name=field_name,
            type="text",
            text=data.get("text"),
        )
    if type_name == "ProjectV2ItemFieldNumberValue":
        return ProjectItemFieldValueRef(
            field_id=field_id,
            field_name=field_name,
            type="number",
            number=data.get("number"),
        )
    if type_name == "ProjectV2ItemFieldSingleSelectValue":
        return ProjectItemFieldValueRef(
            field_id=field_id,
            field_name=field_name,
            type="single_select",
            option_id=data.get("optionId"),
            option_name=data.get("name"),
        )
    return None


def _single_select_option_inputs(option_names: tuple[str, ...]) -> list[dict[str, object]]:
    return [_single_select_option_input(option_name) for option_name in option_names]


def _single_select_option_input(option_name: str) -> dict[str, object]:
    return {
        "name": option_name,
        "color": _option_color(option_name),
        "description": "",
    }


def _option_color(option_name: str) -> str:
    return {
        "Backlog": "GRAY",
        "Todo": "BLUE",
        "In Progress": "YELLOW",
        "Review Required": "PURPLE",
        "Done": "GREEN",
        "P0": "RED",
        "P1": "ORANGE",
        "P2": "YELLOW",
        "P3": "GREEN",
        "feature": "BLUE",
        "bug": "RED",
        "refactor": "PURPLE",
        "chore": "GRAY",
    }.get(option_name, "GRAY")
