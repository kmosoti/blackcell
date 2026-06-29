"""Read-only GitHub echo verification."""

import time
from typing import Any

from blackcell.backends.repository import RepositoryReader
from blackcell.config.model import BlackcellConfig
from blackcell.contracts.errors import ConflictFailure
from blackcell.contracts.markers import item_marker
from blackcell.contracts.plan import PlanSpec


class VerificationService:
    def __init__(self, config: BlackcellConfig, github: RepositoryReader) -> None:
        self.config = config
        self.github = github

    def github_readiness(self) -> dict[str, Any]:
        owner = self.config.repository.owner
        name = self.config.repository.name
        repository = self.github.repository(owner, name)
        protection = self.github.branch_protection(
            owner, name, self.config.repository.default_branch
        )
        rulesets = self.github.repository_rulesets(owner, name)
        executor = self.github.collaborator_permission(
            owner, name, self.config.identity.executor_github_login
        )
        return {
            "repository": repository.get("full_name"),
            "private": repository.get("private"),
            "default_branch": repository.get("default_branch"),
            "permissions": repository.get("permissions"),
            "branch_protected": protection is not None or bool(rulesets),
            "branch_protection": protection,
            "rulesets": rulesets,
            "executor": {
                "login": self.config.identity.executor_github_login,
                "permission": (executor or {}).get("permission"),
                "role_name": (executor or {}).get("role_name"),
            },
        }

    def verify_echoes(
        self,
        plan: PlanSpec,
        *,
        timeout_seconds: float = 0,
        poll_interval: float = 2,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        deadline = time.monotonic() + timeout_seconds
        while True:
            issues = self.github.issues(self.config.repository.owner, self.config.repository.name)
            verified: list[dict[str, Any]] = []
            pending: list[str] = []
            for item in plan.work_items:
                marker = item_marker(plan, item)
                matches = [issue for issue in issues if marker in (issue.get("body") or "")]
                if len(matches) > 1:
                    raise ConflictFailure(
                        "Multiple GitHub Issue echoes contain an assignment marker.",
                        details={"item_key": item.key, "count": len(matches)},
                    )
                if not matches:
                    pending.append(item.key)
                    continue
                issue = matches[0]
                if issue.get("title") != item.title:
                    raise ConflictFailure(
                        "GitHub Issue echo title does not match the Linear assignment.",
                        details={
                            "item_key": item.key,
                            "expected": item.title,
                            "actual": issue.get("title"),
                        },
                    )
                verified.append(
                    {
                        "item_key": item.key,
                        "number": issue["number"],
                        "title": issue["title"],
                        "url": issue["html_url"],
                    }
                )
            if not pending or time.monotonic() >= deadline:
                return verified, pending
            time.sleep(poll_interval)
