from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from blackcell.domains.repository import (
    CheckEvidence,
    CommandResult,
    RepositoryProjector,
    SourceReliability,
    TaskEvidence,
    adapt_check_evidence,
    adapt_task_evidence,
    observe_file_presence,
    observe_git_status,
)

NOW = datetime(2026, 2, 1, tzinfo=UTC)


class _GitRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def run(
        self, argv: tuple[str, ...], *, cwd: Path, timeout_seconds: float
    ) -> CommandResult:
        self.calls.append(argv)
        if argv[-1] == "--show-current":
            return CommandResult(0, "main\n")
        return CommandResult(0, " M README.md\n")


def test_repository_adapters_emit_file_git_task_and_check_claims(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# repo\n", encoding="utf-8")
    runner = _GitRunner()
    events = (
        *observe_file_presence(
            tmp_path, ("README.md", "missing.txt"), observed_at=NOW, starting_sequence=1
        ),
        observe_git_status(tmp_path, observed_at=NOW, sequence=3, runner=runner),
        adapt_task_evidence(
            TaskEvidence("T1", "open", blocked=True), observed_at=NOW, sequence=4
        ),
        adapt_check_evidence(
            CheckEvidence("unit", "failed", reliability=SourceReliability.TRUSTED),
            observed_at=NOW,
            sequence=5,
            expires_at=NOW + timedelta(minutes=10),
        ),
    )

    state = RepositoryProjector().project(events, as_of_time=NOW)

    assert state.find_claims("path:README.md", "present")[0].value is True
    assert state.find_claims("path:missing.txt", "present")[0].value is False
    assert state.find_claims("repository", "git.clean")[0].value is False
    assert state.find_claims("repository", "git.branch")[0].value == "main"
    assert state.find_claims("task:T1", "blocked")[0].value is True
    assert state.find_claims("check:unit", "status")[0].value == "failed"
    assert runner.calls[0] == (
        "git",
        "-c",
        "color.ui=false",
        "status",
        "--porcelain=v1",
    )


def test_file_observer_rejects_paths_outside_repository(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="contained"):
        observe_file_presence(tmp_path, ("../secret",), observed_at=NOW)

