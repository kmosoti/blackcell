import json
from collections.abc import Callable

import httpx

from blackcell.config import BlackcellConfig, ProjectRef, RepositoryRef
from blackcell.providers import CreateIssueRequest, CreatePullRequestRequest
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
                                {
                                    "id": "PVTI_pull",
                                    "type": "PULL_REQUEST",
                                    "isArchived": False,
                                    "content": {
                                        "__typename": "PullRequest",
                                        "id": "PR_123",
                                        "title": "Draft PR",
                                        "url": "https://github.com/kmosoti/blackcell/pull/6",
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

    assert [item.id for item in items] == ["PVTI_issue", "PVTI_draft", "PVTI_pull"]
    assert items[0].content_id == "I_123"
    assert items[1].content_type == "DraftIssue"
    assert items[2].content_id == "PR_123"


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


def test_create_pull_request_posts_graphql_and_returns_pull_request() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "data": {
                    "createPullRequest": {
                        "pullRequest": _pull_request_payload(
                            title="First issue",
                            body="managed body",
                            is_draft=True,
                        )
                    }
                }
            },
        )

    provider = GitHubProjectsProvider(_config(), token="token", client=_client(handler))

    pull_request = provider.create_pull_request(
        CreatePullRequestRequest(
            title="First issue",
            body="managed body",
            base_ref_name="main",
            head_ref_name="feature/bcp-0001",
            draft=True,
        )
    )

    assert pull_request.number == 12
    assert pull_request.is_draft is True
    query = requests[0]["query"]
    assert isinstance(query, str)
    assert "createPullRequest" in query
    assert requests[0]["variables"] == {
        "repoId": "R_123",
        "baseRefName": "main",
        "headRefName": "feature/bcp-0001",
        "title": "First issue",
        "body": "managed body",
        "draft": True,
    }


def test_update_pull_request_posts_graphql_and_returns_pull_request() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "data": {
                    "updatePullRequest": {
                        "pullRequest": _pull_request_payload(
                            title="Updated",
                            body="Updated body",
                            is_draft=True,
                        )
                    }
                }
            },
        )

    provider = GitHubProjectsProvider(_config(), token="token", client=_client(handler))

    pull_request = provider.update_pull_request(
        pull_request_id="PR_123",
        title="Updated",
        body="Updated body",
    )

    assert pull_request.title == "Updated"
    assert pull_request.body == "Updated body"
    query = requests[0]["query"]
    assert isinstance(query, str)
    assert "updatePullRequest" in query
    assert requests[0]["variables"] == {
        "pullRequestId": "PR_123",
        "title": "Updated",
        "body": "Updated body",
    }


def test_mark_pull_request_ready_posts_graphql_and_returns_ready_pull_request() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "data": {
                    "markPullRequestReadyForReview": {
                        "pullRequest": _pull_request_payload(
                            title="First issue",
                            body="managed body",
                            is_draft=False,
                        )
                    }
                }
            },
        )

    provider = GitHubProjectsProvider(_config(), token="token", client=_client(handler))

    pull_request = provider.mark_pull_request_ready_for_review("PR_123")

    assert pull_request.is_draft is False
    query = requests[0]["query"]
    assert isinstance(query, str)
    assert "markPullRequestReadyForReview" in query
    assert requests[0]["variables"] == {"pullRequestId": "PR_123"}


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


def test_find_pull_requests_by_marker_and_head_filters_repository_pull_requests() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "repository": {
                        "pullRequests": {
                            "nodes": [
                                _pull_request_payload(
                                    title="Managed",
                                    body="<!-- blackcell:pr-issue-key BCP-0001 -->",
                                    head_ref_name="feature/bcp-0001",
                                    is_draft=True,
                                ),
                                _pull_request_payload(
                                    id_="PR_456",
                                    number=13,
                                    title="Other",
                                    body="plain",
                                    head_ref_name="feature/other",
                                    is_draft=True,
                                ),
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

    marker_matches = provider.find_pull_requests_by_blackcell_marker("BCP-0001")
    head_matches = provider.find_pull_requests_by_head("feature/bcp-0001")

    assert [pull_request.id for pull_request in marker_matches] == ["PR_123"]
    assert [pull_request.id for pull_request in head_matches] == ["PR_123"]


def _config() -> BlackcellConfig:
    return BlackcellConfig(
        repository=RepositoryRef(owner="kmosoti", name="blackcell", node_id="R_123"),
        project=ProjectRef(id="PVT_123", number=7, title="BlackCell"),
    )


def _pull_request_payload(
    *,
    id_: str = "PR_123",
    number: int = 12,
    title: str,
    body: str,
    head_ref_name: str = "feature/bcp-0001",
    is_draft: bool,
) -> dict[str, object]:
    return {
        "id": id_,
        "number": number,
        "title": title,
        "url": f"https://github.com/kmosoti/blackcell/pull/{number}",
        "state": "OPEN",
        "isDraft": is_draft,
        "baseRefName": "main",
        "headRefName": head_ref_name,
        "headRefOid": "HEAD",
        "body": body,
        "repository": {
            "owner": {"login": "kmosoti"},
            "name": "blackcell",
        },
    }


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))
