import json
from collections.abc import Callable

import httpx

from blackcell.config import BlackcellConfig, ProjectRef, RepositoryRef
from blackcell.providers import CreateIssueRequest
from blackcell.providers.github import GitHubProjectsProvider


def test_create_issue_posts_graphql_and_returns_issue() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "data": {
                    "createIssue": {
                        "issue": {
                            "id": "I_123",
                            "number": 5,
                            "title": "Set up BlackCell branch from scratch",
                            "url": "https://github.com/kmosoti/blackcell/issues/5",
                            "state": "OPEN",
                            "repository": {
                                "owner": {"login": "kmosoti"},
                                "name": "blackcell",
                            },
                        }
                    }
                }
            },
        )

    provider = GitHubProjectsProvider(_config(), token="token", client=_client(handler))

    issue = provider.create_issue(
        CreateIssueRequest(
            title="Set up BlackCell branch from scratch",
            body=(
                "Track IDE setup, project scaffold, CI, and initial BlackCell implementation work."
            ),
        )
    )

    assert issue.number == 5
    assert issue.repository.name_with_owner == "kmosoti/blackcell"
    assert requests[0]["variables"] == {
        "repoId": "R_123",
        "projectId": "PVT_123",
        "title": "Set up BlackCell branch from scratch",
        "body": (
            "Track IDE setup, project scaffold, CI, and initial BlackCell implementation work."
        ),
    }


def test_list_project_items_maps_issue_and_draft_items() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "node": {
                        "id": "PVT_123",
                        "number": 7,
                        "title": "BlackCell",
                        "url": "https://github.com/users/kmosoti/projects/7",
                        "items": {
                            "nodes": [
                                {
                                    "id": "PVTI_issue",
                                    "type": "ISSUE",
                                    "isArchived": False,
                                    "content": {
                                        "__typename": "Issue",
                                        "id": "I_123",
                                        "title": "Set up BlackCell branch from scratch",
                                        "url": "https://github.com/kmosoti/blackcell/issues/5",
                                    },
                                },
                                {
                                    "id": "PVTI_draft",
                                    "type": "DRAFT_ISSUE",
                                    "isArchived": False,
                                    "content": {
                                        "__typename": "DraftIssue",
                                        "title": "Draft",
                                        "body": "Draft body",
                                    },
                                },
                            ]
                        },
                    }
                }
            },
        )

    provider = GitHubProjectsProvider(_config(), token="token", client=_client(handler))

    items = provider.list_project_items()

    assert [item.id for item in items] == ["PVTI_issue", "PVTI_draft"]
    assert items[0].content_id == "I_123"
    assert items[1].content_type == "DraftIssue"


def test_update_issue_posts_graphql_and_returns_issue() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        return httpx.Response(
            200,
            json={
                "data": {
                    "updateIssue": {
                        "issue": {
                            "id": "I_123",
                            "number": 5,
                            "title": "Updated",
                            "body": "Updated body",
                            "url": "https://github.com/kmosoti/blackcell/issues/5",
                            "state": "OPEN",
                            "repository": {
                                "owner": {"login": "kmosoti"},
                                "name": "blackcell",
                            },
                        }
                    }
                }
            },
        )

    provider = GitHubProjectsProvider(_config(), token="token", client=_client(handler))

    issue = provider.update_issue(issue_id="I_123", title="Updated", body="Updated body")

    assert issue.body == "Updated body"
    query = requests[0]["query"]
    assert isinstance(query, str)
    assert "updateIssue" in query
    assert requests[0]["variables"] == {
        "issueId": "I_123",
        "title": "Updated",
        "body": "Updated body",
    }


def test_add_project_item_by_id_posts_graphql_and_returns_project_item() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        return httpx.Response(
            200,
            json={
                "data": {
                    "addProjectV2ItemById": {
                        "item": {
                            "id": "PVTI_123",
                            "type": "ISSUE",
                            "isArchived": False,
                            "content": {
                                "__typename": "Issue",
                                "id": "I_123",
                                "title": "Issue",
                                "url": "https://github.com/kmosoti/blackcell/issues/5",
                            },
                        }
                    }
                }
            },
        )

    provider = GitHubProjectsProvider(_config(), token="token", client=_client(handler))

    item = provider.add_project_item_by_id("I_123")

    assert item.id == "PVTI_123"
    assert item.content_id == "I_123"
    query = requests[0]["query"]
    assert isinstance(query, str)
    assert "addProjectV2ItemById" in query
    assert requests[0]["variables"] == {"projectId": "PVT_123", "contentId": "I_123"}


def test_find_issues_by_blackcell_marker_filters_repository_issues() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "repository": {
                        "issues": {
                            "nodes": [
                                {
                                    "id": "I_123",
                                    "number": 5,
                                    "title": "Managed",
                                    "body": "<!-- blackcell:issue-key BCP-0001 -->",
                                    "url": "https://github.com/kmosoti/blackcell/issues/5",
                                    "state": "OPEN",
                                    "repository": {
                                        "owner": {"login": "kmosoti"},
                                        "name": "blackcell",
                                    },
                                },
                                {
                                    "id": "I_456",
                                    "number": 6,
                                    "title": "Other",
                                    "body": "plain",
                                    "url": "https://github.com/kmosoti/blackcell/issues/6",
                                    "state": "OPEN",
                                    "repository": {
                                        "owner": {"login": "kmosoti"},
                                        "name": "blackcell",
                                    },
                                },
                            ],
                            "pageInfo": {
                                "hasNextPage": False,
                                "endCursor": None,
                            },
                        }
                    }
                }
            },
        )

    provider = GitHubProjectsProvider(_config(), token="token", client=_client(handler))

    issues = provider.find_issues_by_blackcell_marker("BCP-0001")

    assert [issue.id for issue in issues] == ["I_123"]


def _config() -> BlackcellConfig:
    return BlackcellConfig(
        repository=RepositoryRef(owner="kmosoti", name="blackcell", node_id="R_123"),
        project=ProjectRef(id="PVT_123", number=7, title="BlackCell"),
    )


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))
