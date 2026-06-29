"""Read-only Git and GitHub CLI publication inspection."""

import json
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

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
from blackcell.policy.sync import sanitized_environment

_SCP_REMOTE = re.compile(r"^(?:[^@]+@)?(?P<host>[^:]+):(?P<path>.+)$")


@dataclass(frozen=True, slots=True)
class CommandResult:
    stdout: str
    returncode: int = 0
    stderr: str = ""


class CommandRunner(Protocol):
    def run(self, command: Sequence[str], *, cwd: Path) -> CommandResult: ...


class SubprocessRunner:
    def run(self, command: Sequence[str], *, cwd: Path) -> CommandResult:
        try:
            completed = subprocess.run(
                list(command),
                cwd=cwd,
                check=False,
                capture_output=True,
                text=True,
                env=sanitized_environment(),
                timeout=15,
            )
        except FileNotFoundError as error:
            raise PolicyFailure(
                f"Required publication tool is unavailable: {command[0]}."
            ) from error
        except subprocess.SubprocessError as error:
            raise PolicyFailure("Publication inspection command failed.") from error
        return CommandResult(
            stdout=completed.stdout.strip(),
            stderr=completed.stderr.strip(),
            returncode=completed.returncode,
        )


class LocalPublicationAdapter:
    def __init__(
        self,
        config: BlackcellConfig,
        *,
        root: Path | None = None,
        runner: CommandRunner | None = None,
    ) -> None:
        self.config = config
        self.root = (root or Path.cwd()).resolve()
        self.runner = runner or SubprocessRunner()

    def snapshot(self, stage: PublicationStage) -> PublicationSnapshot:
        branch = self._required(["git", "branch", "--show-current"], "current Git branch")
        identity = GitIdentity(
            name=self._required(["git", "config", "user.name"], "Git user.name"),
            email=self._required(["git", "config", "user.email"], "Git user.email"),
        )
        head = self._head() if stage is not PublicationStage.COMMIT else None
        push_target = self._push_target() if stage is not PublicationStage.COMMIT else None
        github_login = None
        pull_request = None
        if stage is PublicationStage.PULL_REQUEST:
            github_login = self._required(["gh", "api", "user", "--jq", ".login"], "GitHub login")
            pull_request = self._pull_request(branch)
        return PublicationSnapshot(
            stage=stage,
            branch=branch,
            configured_identity=identity,
            head=head,
            push_target=push_target,
            github_login=github_login,
            pull_request=pull_request,
        )

    def _head(self) -> CommitSnapshot:
        value = self._required(
            ["git", "show", "-s", "--format=%H%x00%an%x00%ae", "HEAD"],
            "HEAD commit identity",
        )
        fields = value.split("\0")
        if len(fields) != 3:
            raise PolicyFailure("Git returned malformed HEAD commit identity.")
        return CommitSnapshot(
            sha=fields[0],
            author=GitIdentity(name=fields[1], email=fields[2]),
        )

    def _push_target(self) -> PushTarget:
        remote = self.config.publication.push_remote
        url = self._required(
            ["git", "remote", "get-url", "--push", remote],
            f"push URL for remote {remote}",
        )
        host, repository = _parse_remote(url)
        return PushTarget(
            remote=remote,
            url=url,
            host=host,
            repository=repository,
        )

    def _pull_request(self, branch: str) -> PullRequestSnapshot | None:
        raw = self._required(
            [
                "gh",
                "pr",
                "list",
                "--head",
                branch,
                "--state",
                "all",
                "--limit",
                "2",
                "--json",
                "number,author,isDraft,state,baseRefName,headRefName,url",
            ],
            "pull request metadata",
        )
        try:
            payloads = json.loads(raw)
            if not isinstance(payloads, list):
                raise TypeError
            if not payloads:
                return None
            if len(payloads) > 1:
                raise PolicyFailure(
                    "Multiple pull requests exist for the publication branch.",
                    details={"branch": branch, "count": len(payloads)},
                )
            payload = payloads[0]
            return PullRequestSnapshot(
                number=payload["number"],
                author_login=payload["author"]["login"],
                is_draft=payload["isDraft"],
                state=payload["state"],
                base_branch=payload["baseRefName"],
                head_branch=payload["headRefName"],
                url=payload["url"],
            )
        except PolicyFailure:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise PolicyFailure("GitHub CLI returned malformed pull request metadata.") from error

    def _required(self, command: Sequence[str], description: str) -> str:
        result = self.runner.run(command, cwd=self.root)
        if result.returncode or not result.stdout:
            raise PolicyFailure(
                f"Unable to inspect {description}.",
                details={"command": list(command), "returncode": result.returncode},
            )
        return result.stdout


def _parse_remote(url: str) -> tuple[str, str]:
    match = _SCP_REMOTE.match(url)
    if match:
        host = match.group("host")
        path = match.group("path")
    else:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        path = parsed.path.lstrip("/")
    repository = path.removesuffix(".git").strip("/")
    if not host or not repository:
        raise PolicyFailure(
            "Git push remote URL is not a recognized SSH or HTTPS repository URL.",
            details={"url": url},
        )
    return host, repository
