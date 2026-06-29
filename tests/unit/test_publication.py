"""Strongly typed read-only publication preflight."""

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from blackcell.adapters.local_publication import (
    CommandResult,
    LocalPublicationAdapter,
)
from blackcell.config.model import BlackcellConfig
from blackcell.contracts.errors import PolicyFailure
from blackcell.contracts.publication import (
    CommitSnapshot,
    GitIdentity,
    PublicationSnapshot,
    PublicationStage,
    PullRequestSnapshot,
    PushTarget,
)
from blackcell.services.publication_service import PublicationService


class FakePublicationBackend:
    def __init__(self, snapshot: PublicationSnapshot) -> None:
        self.value = snapshot

    def snapshot(self, stage: PublicationStage) -> PublicationSnapshot:
        assert stage is self.value.stage
        return self.value


class FakeRunner:
    def __init__(self, results: dict[tuple[str, ...], CommandResult]) -> None:
        self.results = results

    def run(self, command: Sequence[str], *, cwd: Path) -> CommandResult:
        assert cwd.is_absolute()
        return self.results[tuple(command)]


def snapshot(
    config: BlackcellConfig,
    *,
    github_login: str | None = None,
    pr_author: str | None = None,
) -> PublicationSnapshot:
    executor = config.identity.executor_github_login
    return PublicationSnapshot(
        stage=PublicationStage.PULL_REQUEST,
        branch="blackcell/BCP-0001-planner-proof",
        configured_identity=GitIdentity(
            name=executor,
            email=config.publication.commit_email,
        ),
        head=CommitSnapshot(
            sha="abc123",
            author=GitIdentity(
                name=executor,
                email=config.publication.commit_email,
            ),
        ),
        push_target=PushTarget(
            remote="origin",
            url="git@github.com-kz:kmosoti/blackcell.git",
            host="github.com-kz",
            repository="kmosoti/blackcell",
        ),
        github_login=github_login or executor,
        pull_request=PullRequestSnapshot(
            number=2,
            author_login=pr_author or executor,
            is_draft=True,
            state="OPEN",
            base_branch="main",
            head_branch="blackcell/BCP-0001-planner-proof",
            url="https://github.com/kmosoti/blackcell/pull/2",
        ),
    )


def test_publication_preflight_accepts_one_executor_identity(
    config: BlackcellConfig,
) -> None:
    report = PublicationService(
        config,
        FakePublicationBackend(snapshot(config)),
    ).preflight(PublicationStage.PULL_REQUEST)

    assert report.ready is True
    assert all(check.passed for check in report.checks)


@pytest.mark.parametrize(
    ("github_login", "pr_author", "failed_invariant"),
    [
        ("kmosoti", None, "github.login"),
        (None, "kmosoti", "pull_request.author"),
    ],
)
def test_publication_preflight_rejects_owner_executor_mixups(
    config: BlackcellConfig,
    github_login: str | None,
    pr_author: str | None,
    failed_invariant: str,
) -> None:
    service = PublicationService(
        config,
        FakePublicationBackend(snapshot(config, github_login=github_login, pr_author=pr_author)),
    )

    with pytest.raises(PolicyFailure) as failure:
        service.preflight(PublicationStage.PULL_REQUEST)

    assert failure.value.details["checks"][0]["invariant"] == failed_invariant


def test_local_adapter_builds_typed_push_snapshot(config: BlackcellConfig) -> None:
    runner = FakeRunner(
        {
            ("git", "branch", "--show-current"): CommandResult("blackcell/BCP-0001-planner-proof"),
            ("git", "config", "user.name"): CommandResult("kz-harbringer"),
            ("git", "config", "user.email"): CommandResult(
                "290864439+kz-harbringer@users.noreply.github.com"
            ),
            (
                "git",
                "show",
                "-s",
                "--format=%H%x00%an%x00%ae",
                "HEAD",
            ): CommandResult(
                "\0".join(
                    (
                        "abc123",
                        "kz-harbringer",
                        "290864439+kz-harbringer@users.noreply.github.com",
                    )
                )
            ),
            (
                "git",
                "remote",
                "get-url",
                "--push",
                "origin",
            ): CommandResult("git@github.com-kz:kmosoti/blackcell.git"),
        }
    )

    actual = LocalPublicationAdapter(config, root=Path.cwd(), runner=runner).snapshot(
        PublicationStage.PUSH
    )

    assert actual.push_target is not None
    assert actual.push_target.host == "github.com-kz"
    assert actual.push_target.repository == "kmosoti/blackcell"


def test_local_adapter_only_inspects_open_pull_requests(config: BlackcellConfig) -> None:
    branch = "blackcell/BCP-0001-planner-proof"
    pr_command = (
        "gh",
        "pr",
        "list",
        "--head",
        branch,
        "--state",
        "open",
        "--limit",
        "2",
        "--json",
        "number,author,isDraft,state,baseRefName,headRefName,url",
    )
    runner = FakeRunner(
        {
            ("git", "branch", "--show-current"): CommandResult(branch),
            ("git", "config", "user.name"): CommandResult("kz-harbringer"),
            ("git", "config", "user.email"): CommandResult(
                "290864439+kz-harbringer@users.noreply.github.com"
            ),
            (
                "git",
                "show",
                "-s",
                "--format=%H%x00%an%x00%ae",
                "HEAD",
            ): CommandResult(
                "\0".join(
                    (
                        "abc123",
                        "kz-harbringer",
                        "290864439+kz-harbringer@users.noreply.github.com",
                    )
                )
            ),
            (
                "git",
                "remote",
                "get-url",
                "--push",
                "origin",
            ): CommandResult("git@github.com-kz:kmosoti/blackcell.git"),
            ("gh", "api", "user", "--jq", ".login"): CommandResult("kz-harbringer"),
            pr_command: CommandResult(
                json.dumps(
                    [
                        {
                            "number": 2,
                            "author": {"login": "kz-harbringer"},
                            "isDraft": True,
                            "state": "OPEN",
                            "baseRefName": "main",
                            "headRefName": branch,
                            "url": "https://github.com/kmosoti/blackcell/pull/2",
                        }
                    ]
                )
            ),
        }
    )

    actual = LocalPublicationAdapter(config, root=Path.cwd(), runner=runner).snapshot(
        PublicationStage.PULL_REQUEST
    )

    assert actual.pull_request is not None
    assert actual.pull_request.number == 2
