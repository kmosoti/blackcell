"""Linear GraphQL transport contract tests using an in-memory HTTP transport."""

from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from blackcell.adapters.linear_graphql import LinearGraphQLAdapter, LinearGraphQLTransport
from blackcell.contracts.errors import (
    AuthenticationFailure,
    PermissionFailure,
    RemoteFailure,
)


def transport_for(handler: httpx.MockTransport) -> LinearGraphQLTransport:
    return LinearGraphQLTransport(
        SecretStr("unit-test-secret"),
        client=httpx.Client(transport=handler),
    )


def test_execute_returns_data_and_sends_expected_auth_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "unit-test-secret"
        return httpx.Response(200, json={"data": {"viewer": {"id": "planner"}}})

    transport = transport_for(httpx.MockTransport(handler))

    assert transport.execute("query { viewer { id } }") == {"viewer": {"id": "planner"}}


@pytest.mark.parametrize(
    ("status_code", "error_type", "code"),
    [
        (401, AuthenticationFailure, "authentication_error"),
        (403, PermissionFailure, "permission_error"),
    ],
)
def test_authentication_failures_are_stable_and_secret_free(
    status_code: int,
    error_type: type[AuthenticationFailure | PermissionFailure],
    code: str,
) -> None:
    handler = httpx.MockTransport(lambda _: httpx.Response(status_code))
    transport = transport_for(handler)

    with pytest.raises(error_type) as captured:
        transport.execute("query { viewer { id } }")

    assert "unit-test-secret" not in str(captured.value)
    assert captured.value.code == code


def test_http_200_graphql_errors_include_partial_data_without_secret() -> None:
    handler = httpx.MockTransport(
        lambda _: httpx.Response(
            200,
            json={
                "data": {"viewer": None},
                "errors": [{"message": "viewer unavailable", "path": ["viewer"]}],
            },
        )
    )
    transport = transport_for(handler)

    with pytest.raises(RemoteFailure) as captured:
        transport.execute("query { viewer { id } }")

    assert captured.value.details == {
        "messages": ["viewer unavailable"],
        "partial_data": True,
        "provider_details": [
            {
                "code": None,
                "type": None,
                "user_message": None,
                "validation_errors": [],
            }
        ],
    }
    assert "unit-test-secret" not in str(captured.value)


@pytest.mark.parametrize("payload", [{"unexpected": True}, ["not", "an", "object"]])
def test_malformed_or_missing_data_is_rejected(payload: object) -> None:
    handler = httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    transport = transport_for(handler)

    with pytest.raises(RemoteFailure):
        transport.execute("query { viewer { id } }")


def test_read_retries_transient_network_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise httpx.ConnectTimeout("temporary", request=request)
        return httpx.Response(200, json={"data": {"ok": True}})

    monkeypatch.setattr("blackcell.adapters.linear_graphql.time.sleep", lambda _: None)
    transport = transport_for(httpx.MockTransport(handler))

    assert transport.execute("query { ok }") == {"ok": True}
    assert attempts == 3


def test_mutation_is_never_blindly_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectTimeout("lost response", request=request)

    monkeypatch.setattr("blackcell.adapters.linear_graphql.time.sleep", lambda _: None)
    transport = transport_for(httpx.MockTransport(handler))

    with pytest.raises(RemoteFailure):
        transport.execute("mutation { createThing }", mutation=True)

    assert attempts == 1


class PaginatedTransport:
    def __init__(self, *, omit_cursor: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.omit_cursor = omit_cursor

    def execute(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
        *,
        mutation: bool = False,
    ) -> dict[str, Any]:
        del query, mutation
        current = dict(variables or {})
        self.calls.append(current)
        if current["after"] is None:
            page_info: dict[str, Any] = {"hasNextPage": True}
            if not self.omit_cursor:
                page_info["endCursor"] = "cursor-1"
            return {
                "projectStatuses": {
                    "nodes": [{"id": "one", "name": "Proposal", "type": "planned"}],
                    "pageInfo": page_info,
                }
            }
        return {
            "projectStatuses": {
                "nodes": [{"id": "two", "name": "Approved", "type": "planned"}],
                "pageInfo": {"hasNextPage": False, "endCursor": "cursor-2"},
            }
        }


def test_adapter_paginates_every_collection() -> None:
    stub = PaginatedTransport()
    adapter = LinearGraphQLAdapter(stub)

    statuses = adapter.project_statuses()

    assert [status["id"] for status in statuses] == ["one", "two"]
    assert [call["after"] for call in stub.calls] == [None, "cursor-1"]


def test_adapter_rejects_missing_pagination_cursor() -> None:
    adapter = LinearGraphQLAdapter(PaginatedTransport(omit_cursor=True))

    with pytest.raises(RemoteFailure, match="omitted endCursor"):
        adapter.project_statuses()


class MutationTransport:
    def __init__(self) -> None:
        self.query = ""
        self.variables: dict[str, Any] = {}
        self.mutation = False

    def execute(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
        *,
        mutation: bool = False,
    ) -> dict[str, Any]:
        self.query = query
        self.variables = dict(variables or {})
        self.mutation = mutation
        return {
            "entityExternalLinkCreate": {
                "success": True,
                "entityExternalLink": {
                    "id": "link-1",
                    "url": "https://github.com/kmosoti/blackcell",
                    "label": "GitHub repository",
                    "archivedAt": None,
                },
            }
        }


def test_adapter_creates_typed_project_external_link() -> None:
    transport = MutationTransport()
    adapter = LinearGraphQLAdapter(transport)

    link = adapter.create_project_external_link(
        "project-1",
        url="https://github.com/kmosoti/blackcell",
        label="GitHub repository",
    )

    assert transport.mutation is True
    assert "entityExternalLinkCreate" in transport.query
    assert transport.variables == {
        "input": {
            "projectId": "project-1",
            "url": "https://github.com/kmosoti/blackcell",
            "label": "GitHub repository",
        }
    }
    assert link["id"] == "link-1"


def test_graphql_validation_details_exclude_rejected_input_values() -> None:
    handler = httpx.MockTransport(
        lambda _: httpx.Response(
            200,
            json={
                "errors": [
                    {
                        "message": "Argument Validation Error",
                        "extensions": {
                            "code": "INVALID_INPUT",
                            "type": "invalid input",
                            "userPresentableMessage": "icon is not a valid icon.",
                            "validationErrors": [
                                {
                                    "property": "icon",
                                    "value": "must-not-be-returned",
                                    "target": {"icon": "must-not-be-returned"},
                                    "constraints": {"customValidation": "icon is not a valid icon"},
                                }
                            ],
                        },
                    }
                ]
            },
        )
    )
    transport = transport_for(handler)

    with pytest.raises(RemoteFailure) as captured:
        transport.execute("mutation { projectUpdate }", mutation=True)

    details = captured.value.details
    assert details["provider_details"][0]["user_message"] == "icon is not a valid icon."
    assert details["provider_details"][0]["validation_errors"] == [
        {
            "property": "icon",
            "constraints": {"customValidation": "icon is not a valid icon"},
        }
    ]
    assert "must-not-be-returned" not in str(details)


class ProjectMutationTransport:
    def __init__(self) -> None:
        self.variables: dict[str, Any] = {}

    def execute(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
        *,
        mutation: bool = False,
    ) -> dict[str, Any]:
        assert mutation is True
        assert "projectUpdate" in query
        self.variables = dict(variables or {})
        return {
            "projectUpdate": {
                "success": True,
                "project": {
                    "id": "project-1",
                    "name": "BlackCell proof",
                    "description": "summary",
                    "content": "content",
                    "icon": None,
                    "color": "#111827",
                    "url": "https://linear.test/project-1",
                    "archivedAt": None,
                    "status": {"id": "status-1", "name": "Proposal", "type": "backlog"},
                    "teams": {"nodes": [{"id": "team-1"}]},
                    "externalLinks": {"nodes": []},
                },
            }
        }


def test_adapter_omits_unmanaged_project_icon() -> None:
    transport = ProjectMutationTransport()
    adapter = LinearGraphQLAdapter(transport)

    adapter.update_project_presentation(
        "project-1",
        description="summary",
        content="content",
        icon=None,
        color="#111827",
    )

    assert transport.variables["input"] == {
        "description": "summary",
        "content": "content",
        "color": "#111827",
    }


class IntegrationTransport:
    def execute(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
        *,
        mutation: bool = False,
    ) -> dict[str, Any]:
        assert "integrations" in query
        assert variables == {"after": None}
        assert mutation is False
        return {
            "integrations": {
                "nodes": [
                    {
                        "id": "integration-1",
                        "service": "github",
                        "archivedAt": None,
                        "team": None,
                    }
                ],
                "pageInfo": {"hasNextPage": False, "endCursor": "integration-1"},
            }
        }


def test_adapter_reads_active_workspace_integrations() -> None:
    integrations = LinearGraphQLAdapter(IntegrationTransport()).integrations()

    assert integrations == [
        {
            "id": "integration-1",
            "service": "github",
            "archivedAt": None,
            "team": None,
        }
    ]
