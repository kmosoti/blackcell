"""Read-only GitHub REST adapter for Linear-created issue echoes."""

from typing import Any

import httpx
from pydantic import SecretStr

from blackcell.contracts.errors import AuthenticationFailure, RemoteFailure


class GitHubRestAdapter:
    def __init__(
        self,
        token: SecretStr | None = None,
        *,
        endpoint: str = "https://api.github.com",
        client: httpx.Client | None = None,
    ) -> None:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token is not None:
            headers["Authorization"] = f"Bearer {token.get_secret_value()}"
        self.endpoint = endpoint.rstrip("/")
        self._owns_client = client is None
        self.client = client or httpx.Client(
            headers=headers,
            timeout=httpx.Timeout(20.0, connect=5.0),
        )

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def repository(self, owner: str, repository: str) -> dict[str, Any]:
        payload = self._get(f"/repos/{owner}/{repository}")
        if not isinstance(payload, dict):
            raise RemoteFailure("GitHub returned malformed repository data.")
        return payload

    def issues(self, owner: str, repository: str) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for page in range(1, 11):
            payload = self._get(
                f"/repos/{owner}/{repository}/issues",
                params={"state": "all", "per_page": 100, "page": page},
            )
            if not isinstance(payload, list):
                raise RemoteFailure("GitHub returned malformed issue data.")
            results.extend(issue for issue in payload if "pull_request" not in issue)
            if len(payload) < 100:
                return results
        raise RemoteFailure("GitHub issue pagination exceeded the safety limit.")

    def find_issues_by_marker(
        self, owner: str, repository: str, marker: str
    ) -> list[dict[str, Any]]:
        return [
            issue for issue in self.issues(owner, repository) if marker in (issue.get("body") or "")
        ]

    def branch_protection(self, owner: str, repository: str, branch: str) -> dict[str, Any] | None:
        return self._optional_dict(f"/repos/{owner}/{repository}/branches/{branch}/protection")

    def collaborator_permission(
        self, owner: str, repository: str, username: str
    ) -> dict[str, Any] | None:
        return self._optional_dict(
            f"/repos/{owner}/{repository}/collaborators/{username}/permission"
        )

    def repository_rulesets(self, owner: str, repository: str) -> list[dict[str, Any]]:
        payload = self._get(f"/repos/{owner}/{repository}/rulesets")
        if not isinstance(payload, list):
            raise RemoteFailure("GitHub returned malformed ruleset data.")
        return payload

    def _optional_dict(self, path: str) -> dict[str, Any] | None:
        try:
            response = self.client.get(f"{self.endpoint}{path}")
            if response.status_code == 404:
                return None
            if response.status_code in {401, 403}:
                raise AuthenticationFailure("GitHub rejected the configured credential.")
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise RemoteFailure("GitHub returned malformed data.")
            return payload
        except AuthenticationFailure:
            raise
        except (httpx.HTTPError, ValueError) as error:
            raise RemoteFailure("GitHub read request failed.") from error

    def _get(
        self, path: str, params: dict[str, str | int] | None = None
    ) -> dict[str, Any] | list[dict[str, Any]]:
        try:
            response = self.client.get(f"{self.endpoint}{path}", params=params)
            if response.status_code in {401, 403}:
                raise AuthenticationFailure("GitHub rejected the configured credential.")
            response.raise_for_status()
            payload: dict[str, Any] | list[dict[str, Any]] = response.json()
            return payload
        except AuthenticationFailure:
            raise
        except (httpx.HTTPError, ValueError) as error:
            raise RemoteFailure("GitHub read request failed.") from error
