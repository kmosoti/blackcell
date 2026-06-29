"""Narrow read-only repository capabilities."""

from typing import Any, Protocol


class RepositoryReader(Protocol):
    def repository(self, owner: str, repository: str) -> dict[str, Any]: ...

    def issues(self, owner: str, repository: str) -> list[dict[str, Any]]: ...

    def branch_protection(
        self,
        owner: str,
        repository: str,
        branch: str,
    ) -> dict[str, Any] | None: ...

    def collaborator_permission(
        self,
        owner: str,
        repository: str,
        username: str,
    ) -> dict[str, Any] | None: ...

    def repository_rulesets(self, owner: str, repository: str) -> list[dict[str, Any]]: ...
