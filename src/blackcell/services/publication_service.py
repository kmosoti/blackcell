"""Publication identity preflight with no repository mutation."""

from blackcell.backends.publication import PublicationBackend
from blackcell.config.model import BlackcellConfig
from blackcell.contracts.errors import PolicyFailure
from blackcell.contracts.publication import (
    InvariantCheck,
    PublicationPreflight,
    PublicationSnapshot,
    PublicationStage,
)


class PublicationService:
    def __init__(self, config: BlackcellConfig, backend: PublicationBackend) -> None:
        self.config = config
        self.backend = backend

    def preflight(self, stage: PublicationStage) -> PublicationPreflight:
        snapshot = self.backend.snapshot(stage)
        checks = self._checks(snapshot)
        report = PublicationPreflight(
            stage=stage,
            ready=all(check.passed for check in checks),
            checks=tuple(checks),
            snapshot=snapshot,
        )
        if not report.ready:
            raise PolicyFailure(
                "Publication identity preflight failed.",
                recovery="Correct the failed identity or branch checks before publishing.",
                details={
                    "stage": stage,
                    "checks": [
                        check.model_dump(mode="json") for check in report.checks if not check.passed
                    ],
                },
            )
        return report

    def _checks(self, snapshot: PublicationSnapshot) -> list[InvariantCheck]:
        publication = self.config.publication
        repository = self.config.repository
        executor = self.config.identity.executor_github_login
        commit_name = executor
        expected_repository = f"{repository.owner}/{repository.name}"
        checks = [
            _check(
                "branch.not_default",
                snapshot.branch != repository.default_branch,
                f"not {repository.default_branch}",
                snapshot.branch,
            ),
            _check(
                "branch.prefix",
                snapshot.branch.startswith(publication.branch_prefix),
                f"{publication.branch_prefix}*",
                snapshot.branch,
            ),
            _check(
                "git.config.name",
                snapshot.configured_identity.name == commit_name,
                commit_name,
                snapshot.configured_identity.name,
            ),
            _check(
                "git.config.email",
                snapshot.configured_identity.email == publication.commit_email,
                publication.commit_email,
                snapshot.configured_identity.email,
            ),
        ]
        if snapshot.stage is PublicationStage.COMMIT:
            return checks

        head = snapshot.head
        push_target = snapshot.push_target
        if head is None or push_target is None:
            raise PolicyFailure("Publication backend omitted push-stage metadata.")
        checks.extend(
            [
                _check(
                    "commit.author.name",
                    head.author.name == commit_name,
                    commit_name,
                    head.author.name,
                ),
                _check(
                    "commit.author.email",
                    head.author.email == publication.commit_email,
                    publication.commit_email,
                    head.author.email,
                ),
                _check(
                    "push.remote",
                    push_target.remote == publication.push_remote,
                    publication.push_remote,
                    push_target.remote,
                ),
                _check(
                    "push.ssh_host",
                    push_target.host == publication.push_ssh_host,
                    publication.push_ssh_host,
                    push_target.host,
                ),
                _check(
                    "push.repository",
                    push_target.repository == expected_repository,
                    expected_repository,
                    push_target.repository,
                ),
            ]
        )
        if snapshot.stage is PublicationStage.PUSH:
            return checks

        checks.append(
            _check(
                "github.login",
                snapshot.github_login == executor,
                executor,
                snapshot.github_login,
            )
        )
        pull_request = snapshot.pull_request
        if pull_request is None:
            return checks
        checks.extend(
            [
                _check(
                    "pull_request.author",
                    pull_request.author_login == executor,
                    executor,
                    pull_request.author_login,
                ),
                _check(
                    "pull_request.draft",
                    pull_request.is_draft is publication.require_draft_pr,
                    str(publication.require_draft_pr).lower(),
                    str(pull_request.is_draft).lower(),
                ),
                _check(
                    "pull_request.state",
                    pull_request.state == "OPEN",
                    "OPEN",
                    pull_request.state,
                ),
                _check(
                    "pull_request.base",
                    pull_request.base_branch == repository.default_branch,
                    repository.default_branch,
                    pull_request.base_branch,
                ),
                _check(
                    "pull_request.head",
                    pull_request.head_branch == snapshot.branch,
                    snapshot.branch,
                    pull_request.head_branch,
                ),
            ]
        )
        return checks


def _check(invariant: str, passed: bool, expected: str, actual: str | None) -> InvariantCheck:
    return InvariantCheck(
        invariant=invariant,
        passed=passed,
        expected=expected,
        actual=actual,
    )
