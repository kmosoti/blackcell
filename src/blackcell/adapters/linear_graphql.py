"""Typed, secret-safe Linear GraphQL transport and adapter."""

import time
from collections.abc import Iterator
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from blackcell.contracts.errors import (
    AuthenticationFailure,
    PermissionFailure,
    RemoteFailure,
)

LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"


class GraphQLErrorItem(BaseModel):
    model_config = ConfigDict(extra="allow")

    message: str
    path: list[str | int] | None = None
    extensions: dict[str, Any] = Field(default_factory=dict)


class GraphQLResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data: dict[str, Any] | None = None
    errors: list[GraphQLErrorItem] = Field(default_factory=list)


class LinearViewer(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    name: str
    email: str


class LinearTeam(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    key: str
    name: str
    archivedAt: str | None = None


class LinearStatus(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    name: str
    type: str
    archivedAt: str | None = None


class LinearLabel(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    name: str
    archivedAt: str | None = None


class LinearProject(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    name: str
    description: str
    content: str | None = None
    icon: str | None = None
    color: str
    url: str
    archivedAt: str | None = None
    status: LinearStatus
    teams: dict[str, Any]
    externalLinks: dict[str, Any]


class LinearExternalLink(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    url: str
    label: str
    archivedAt: str | None = None


class LinearIntegration(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    service: str
    archivedAt: str | None = None
    team: dict[str, Any] | None = None


class LinearIssue(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    identifier: str
    title: str
    description: str | None = None
    url: str


class LinearRelation(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    type: str
    issue: dict[str, Any]
    relatedIssue: dict[str, Any]


class GraphQLExecutor(Protocol):
    def execute(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
        *,
        mutation: bool = False,
    ) -> dict[str, Any]: ...


def _safe_graphql_error_details(item: GraphQLErrorItem) -> dict[str, Any]:
    extensions = item.extensions
    validation_errors = []
    for failure in extensions.get("validationErrors") or []:
        if not isinstance(failure, dict):
            continue
        constraints = failure.get("constraints")
        validation_errors.append(
            {
                "property": failure.get("property"),
                "constraints": (
                    {
                        str(key): str(value)
                        for key, value in constraints.items()
                        if isinstance(value, str)
                    }
                    if isinstance(constraints, dict)
                    else {}
                ),
            }
        )
    return {
        "code": extensions.get("code"),
        "type": extensions.get("type"),
        "user_message": extensions.get("userPresentableMessage"),
        "validation_errors": validation_errors,
    }


class LinearGraphQLTransport:
    def __init__(
        self,
        api_key: SecretStr,
        *,
        endpoint: str = LINEAR_GRAPHQL_URL,
        connect_timeout: float = 5.0,
        read_timeout: float = 20.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._api_key = api_key
        self.endpoint = endpoint
        self._owns_client = client is None
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(
                connect=connect_timeout,
                read=read_timeout,
                write=read_timeout,
                pool=connect_timeout,
            )
        )

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> LinearGraphQLTransport:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def execute(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
        *,
        mutation: bool = False,
    ) -> dict[str, Any]:
        attempts = 1 if mutation else 3
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                response = self.client.post(
                    self.endpoint,
                    headers={
                        "Authorization": self._api_key.get_secret_value(),
                        "Content-Type": "application/json",
                    },
                    json={"query": query, "variables": variables or {}},
                )
                if response.status_code == 401:
                    raise AuthenticationFailure("Linear rejected the configured credential.")
                if response.status_code == 403:
                    raise PermissionFailure(
                        "Linear authenticated the planner but denied this operation."
                    )
                if 400 <= response.status_code < 500 and response.status_code != 429:
                    raise RemoteFailure(
                        "Linear rejected the GraphQL request.",
                        details={"status_code": response.status_code},
                    )
                response.raise_for_status()
                try:
                    envelope = GraphQLResponse.model_validate(response.json())
                except (ValueError, ValidationError) as error:
                    raise RemoteFailure("Linear returned a malformed GraphQL response.") from error
                if envelope.errors:
                    messages = [item.message for item in envelope.errors]
                    provider_details = [
                        _safe_graphql_error_details(item) for item in envelope.errors
                    ]
                    raise RemoteFailure(
                        "Linear returned GraphQL errors.",
                        details={
                            "messages": messages,
                            "partial_data": envelope.data is not None,
                            "provider_details": provider_details,
                        },
                    )
                if envelope.data is None:
                    raise RemoteFailure("Linear returned no GraphQL data.")
                return envelope.data
            except AuthenticationFailure, PermissionFailure, RemoteFailure:
                raise
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as error:
                last_error = error
                if attempt + 1 < attempts:
                    time.sleep(0.25 * (2**attempt))
                    continue
                raise RemoteFailure("Linear request failed or timed out.") from error
        raise RemoteFailure("Linear request failed.") from last_error


class LinearGraphQLAdapter:
    def __init__(self, transport: GraphQLExecutor) -> None:
        self.transport = transport

    def identity_snapshot(self, team_id: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
        data = self.transport.execute(
            """
            query Identity($teamId: String!) {
              viewer { id name email }
              team(id: $teamId) { id key name archivedAt }
            }
            """,
            {"teamId": team_id},
        )
        viewer = self._validate(LinearViewer, data.get("viewer"), "viewer")
        team_data = data.get("team")
        team = self._validate(LinearTeam, team_data, "team") if team_data is not None else None
        return viewer, team

    def project_statuses(self) -> list[dict[str, Any]]:
        nodes = list(
            self._paginate(
                """
                query ProjectStatuses($after: String) {
                  projectStatuses(first: 50, after: $after, includeArchived: false) {
                    nodes { id name type archivedAt }
                    pageInfo { hasNextPage endCursor }
                  }
                }
                """,
                ("projectStatuses",),
            )
        )
        return [self._validate(LinearStatus, node, "project status") for node in nodes]

    def workflow_states(self, team_id: str) -> list[dict[str, Any]]:
        nodes = list(
            self._paginate(
                """
                query WorkflowStates($teamId: String!, $after: String) {
                  team(id: $teamId) {
                    states(first: 50, after: $after, includeArchived: false) {
                      nodes { id name type archivedAt }
                      pageInfo { hasNextPage endCursor }
                    }
                  }
                }
                """,
                ("team", "states"),
                {"teamId": team_id},
            )
        )
        return [self._validate(LinearStatus, node, "workflow state") for node in nodes]

    def issue_labels(self, team_id: str) -> list[dict[str, Any]]:
        nodes = list(
            self._paginate(
                """
                query IssueLabels($teamId: ID!, $after: String) {
                  issueLabels(
                    first: 50,
                    after: $after,
                    includeArchived: false,
                    filter: { team: { id: { eq: $teamId } } }
                  ) {
                    nodes { id name archivedAt }
                    pageInfo { hasNextPage endCursor }
                  }
                }
                """,
                ("issueLabels",),
                {"teamId": team_id},
            )
        )
        return [self._validate(LinearLabel, node, "issue label") for node in nodes]

    def integrations(self) -> list[dict[str, Any]]:
        nodes = list(
            self._paginate(
                """
                query Integrations($after: String) {
                  integrations(first: 50, after: $after, includeArchived: false) {
                    nodes {
                      id service archivedAt
                      team { id key name }
                    }
                    pageInfo { hasNextPage endCursor }
                  }
                }
                """,
                ("integrations",),
            )
        )
        return [self._validate(LinearIntegration, node, "integration") for node in nodes]

    def projects(self, team_id: str) -> list[dict[str, Any]]:
        nodes = list(
            self._paginate(
                """
                query Projects($teamId: String!, $after: String) {
                  team(id: $teamId) {
                    projects(first: 50, after: $after, includeArchived: false) {
                      nodes {
                        id name description content icon color url archivedAt
                        status { id name type }
                        teams(first: 10) { nodes { id key name } }
                        externalLinks(first: 50) {
                          nodes { id url label archivedAt }
                        }
                      }
                      pageInfo { hasNextPage endCursor }
                    }
                  }
                }
                """,
                ("team", "projects"),
                {"teamId": team_id},
            )
        )
        return [self._validate(LinearProject, node, "project") for node in nodes]

    def find_projects_by_marker(self, team_id: str, marker: str) -> list[dict[str, Any]]:
        return [
            project
            for project in self.projects(team_id)
            if marker in (project.get("description") or "")
            or marker in (project.get("content") or "")
        ]

    def create_project(
        self,
        *,
        name: str,
        description: str,
        content: str,
        team_id: str,
        status_id: str,
        icon: str | None,
        color: str,
    ) -> dict[str, Any]:
        input_data = {
            "name": name,
            "description": description,
            "content": content,
            "teamIds": [team_id],
            "statusId": status_id,
            "color": color,
        }
        if icon is not None:
            input_data["icon"] = icon
        data = self.transport.execute(
            """
            mutation CreateProject($input: ProjectCreateInput!) {
              projectCreate(input: $input) {
                success
                project {
                  id name description content icon color url archivedAt
                  status { id name type }
                  teams(first: 10) { nodes { id key name } }
                  externalLinks(first: 50) {
                    nodes { id url label archivedAt }
                  }
                }
              }
            }
            """,
            {"input": input_data},
            mutation=True,
        )
        payload = self._mapping(data.get("projectCreate"), "projectCreate")
        if not payload.get("success") or not payload.get("project"):
            raise RemoteFailure("Linear did not confirm Project creation.")
        return self._validate(LinearProject, payload["project"], "created project")

    def update_project_presentation(
        self,
        project_id: str,
        *,
        description: str,
        content: str,
        icon: str | None,
        color: str,
    ) -> dict[str, Any]:
        input_data = {
            "description": description,
            "content": content,
            "color": color,
        }
        if icon is not None:
            input_data["icon"] = icon
        data = self.transport.execute(
            """
            mutation UpdateProjectPresentation($id: String!, $input: ProjectUpdateInput!) {
              projectUpdate(id: $id, input: $input) {
                success
                project {
                  id name description content icon color url archivedAt
                  status { id name type }
                  teams(first: 10) { nodes { id key name } }
                  externalLinks(first: 50) {
                    nodes { id url label archivedAt }
                  }
                }
              }
            }
            """,
            {
                "id": project_id,
                "input": input_data,
            },
            mutation=True,
        )
        payload = self._mapping(data.get("projectUpdate"), "projectUpdate")
        if not payload.get("success") or not payload.get("project"):
            raise RemoteFailure("Linear did not confirm Project presentation update.")
        return self._validate(LinearProject, payload["project"], "updated project")

    def create_project_external_link(
        self,
        project_id: str,
        *,
        url: str,
        label: str,
    ) -> dict[str, Any]:
        data = self.transport.execute(
            """
            mutation CreateProjectExternalLink($input: EntityExternalLinkCreateInput!) {
              entityExternalLinkCreate(input: $input) {
                success
                entityExternalLink { id url label archivedAt }
              }
            }
            """,
            {"input": {"projectId": project_id, "url": url, "label": label}},
            mutation=True,
        )
        payload = self._mapping(data.get("entityExternalLinkCreate"), "entityExternalLinkCreate")
        if not payload.get("success") or not payload.get("entityExternalLink"):
            raise RemoteFailure("Linear did not confirm Project repository link creation.")
        return self._validate(
            LinearExternalLink,
            payload["entityExternalLink"],
            "created project external link",
        )

    def update_project_external_link(
        self,
        link_id: str,
        *,
        url: str,
        label: str,
    ) -> dict[str, Any]:
        data = self.transport.execute(
            """
            mutation UpdateProjectExternalLink(
              $id: String!,
              $input: EntityExternalLinkUpdateInput!
            ) {
              entityExternalLinkUpdate(id: $id, input: $input) {
                success
                entityExternalLink { id url label archivedAt }
              }
            }
            """,
            {"id": link_id, "input": {"url": url, "label": label}},
            mutation=True,
        )
        payload = self._mapping(data.get("entityExternalLinkUpdate"), "entityExternalLinkUpdate")
        if not payload.get("success") or not payload.get("entityExternalLink"):
            raise RemoteFailure("Linear did not confirm Project repository link update.")
        return self._validate(
            LinearExternalLink,
            payload["entityExternalLink"],
            "updated project external link",
        )

    def project_issues(self, project_id: str) -> list[dict[str, Any]]:
        nodes = list(
            self._paginate(
                """
                query ProjectIssues($projectId: String!, $after: String) {
                  project(id: $projectId) {
                    issues(first: 50, after: $after, includeArchived: false) {
                      nodes {
                        id identifier title description url archivedAt
                        priority
                        parent { id identifier }
                        team { id key name }
                        state { id name type }
                        project { id }
                        labels(first: 50) { nodes { id name } }
                        relations(first: 50) {
                          nodes {
                            id type
                            issue { id identifier }
                            relatedIssue { id identifier }
                          }
                        }
                        inverseRelations(first: 50) {
                          nodes {
                            id type
                            issue { id identifier }
                            relatedIssue { id identifier }
                          }
                        }
                      }
                      pageInfo { hasNextPage endCursor }
                    }
                  }
                }
                """,
                ("project", "issues"),
                {"projectId": project_id},
            )
        )
        return [self._validate(LinearIssue, node, "project issue") for node in nodes]

    def find_issues_by_marker(self, project_id: str, marker: str) -> list[dict[str, Any]]:
        return [
            issue
            for issue in self.project_issues(project_id)
            if marker in (issue.get("description") or "")
        ]

    def team_issues(self, team_id: str) -> list[dict[str, Any]]:
        nodes = list(
            self._paginate(
                """
                query TeamIssues($teamId: String!, $after: String) {
                  team(id: $teamId) {
                    issues(first: 50, after: $after, includeArchived: false) {
                      nodes {
                        id identifier title description url archivedAt
                        priority
                        parent { id identifier }
                        team { id key name }
                        state { id name type }
                        project { id }
                        labels(first: 50) { nodes { id name } }
                        relations(first: 50) {
                          nodes {
                            id type
                            issue { id identifier }
                            relatedIssue { id identifier }
                          }
                        }
                        inverseRelations(first: 50) {
                          nodes {
                            id type
                            issue { id identifier }
                            relatedIssue { id identifier }
                          }
                        }
                      }
                      pageInfo { hasNextPage endCursor }
                    }
                  }
                }
                """,
                ("team", "issues"),
                {"teamId": team_id},
            )
        )
        return [self._validate(LinearIssue, node, "team issue") for node in nodes]

    def find_team_issues_by_marker(self, team_id: str, marker: str) -> list[dict[str, Any]]:
        return [
            issue
            for issue in self.team_issues(team_id)
            if marker in (issue.get("description") or "")
        ]

    def create_issue(
        self,
        *,
        team_id: str,
        project_id: str,
        state_id: str,
        title: str,
        description: str,
        priority: int,
        label_ids: list[str],
        parent_id: str | None,
    ) -> dict[str, Any]:
        data = self.transport.execute(
            """
            mutation CreateIssue($input: IssueCreateInput!) {
              issueCreate(input: $input) {
                success
                issue { id identifier title description url parent { id identifier } }
              }
            }
            """,
            {
                "input": {
                    "teamId": team_id,
                    "projectId": project_id,
                    "stateId": state_id,
                    "title": title,
                    "description": description,
                    "priority": priority,
                    "labelIds": label_ids,
                    "parentId": parent_id,
                }
            },
            mutation=True,
        )
        payload = self._mapping(data.get("issueCreate"), "issueCreate")
        if not payload.get("success") or not payload.get("issue"):
            raise RemoteFailure("Linear did not confirm issue creation.")
        return self._validate(LinearIssue, payload["issue"], "created issue")

    def create_blocking_relation(self, blocker_id: str, blocked_id: str) -> dict[str, Any]:
        data = self.transport.execute(
            """
            mutation CreateRelation($input: IssueRelationCreateInput!) {
              issueRelationCreate(input: $input) {
                success
                issueRelation {
                  id type
                  issue { id identifier }
                  relatedIssue { id identifier }
                }
              }
            }
            """,
            {
                "input": {
                    "type": "blocks",
                    "issueId": blocker_id,
                    "relatedIssueId": blocked_id,
                }
            },
            mutation=True,
        )
        payload = self._mapping(data.get("issueRelationCreate"), "issueRelationCreate")
        if not payload.get("success") or not payload.get("issueRelation"):
            raise RemoteFailure("Linear did not confirm blocking relation creation.")
        return self._validate(LinearRelation, payload["issueRelation"], "created issue relation")

    def issue_relations(self, issue_id: str) -> list[dict[str, Any]]:
        nodes = list(
            self._paginate(
                """
                query IssueRelations($issueId: String!, $after: String) {
                  issue(id: $issueId) {
                    relations(first: 50, after: $after) {
                      nodes {
                        id type
                        issue { id identifier }
                        relatedIssue { id identifier }
                      }
                      pageInfo { hasNextPage endCursor }
                    }
                  }
                }
                """,
                ("issue", "relations"),
                {"issueId": issue_id},
            )
        )
        return [self._validate(LinearRelation, node, "issue relation") for node in nodes]

    def _paginate(
        self,
        query: str,
        path: tuple[str, ...],
        variables: dict[str, Any] | None = None,
    ) -> Iterator[dict[str, Any]]:
        base_variables = dict(variables or {})
        after: str | None = None
        while True:
            data = self.transport.execute(query, {**base_variables, "after": after})
            connection: dict[str, Any] | None = data
            for component in path:
                if connection is None:
                    break
                connection = connection.get(component)
            if connection is None:
                raise RemoteFailure(
                    "Linear response omitted an expected connection.",
                    details={"path": ".".join(path)},
                )
            nodes = connection.get("nodes")
            if not isinstance(nodes, list):
                raise RemoteFailure(
                    "Linear connection omitted nodes.",
                    details={"path": ".".join(path)},
                )
            yield from nodes
            page_info = connection.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                return
            after = page_info.get("endCursor")
            if not after:
                raise RemoteFailure("Linear pagination omitted endCursor.")

    @staticmethod
    def _mapping(value: Any, context: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise RemoteFailure(
                "Linear response omitted a required object.",
                details={"context": context},
            )
        return value

    @staticmethod
    def _validate(model: type[BaseModel], value: Any, context: str) -> dict[str, Any]:
        try:
            return model.model_validate(value).model_dump(mode="json")
        except ValidationError as error:
            failures = [
                {
                    "location": ".".join(str(component) for component in item["loc"]),
                    "type": item["type"],
                }
                for item in error.errors(include_input=False, include_url=False)
            ]
            raise RemoteFailure(
                "Linear response failed typed validation.",
                details={"context": context, "failures": failures},
            ) from error
