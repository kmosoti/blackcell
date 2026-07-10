from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePath
from typing import Protocol

from blackcell.domains.repository.events import CLAIMS_RECORDED, RepositorySemanticEvent
from blackcell.domains.repository.models import (
    CheckEvidence,
    Claim,
    ClaimBatch,
    EpistemicStatus,
    EvidenceRef,
    SourceReliability,
    TaskEvidence,
)


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str = ""


class FixedCommandRunner(Protocol):
    def run(self, argv: tuple[str, ...], *, cwd: Path, timeout_seconds: float) -> CommandResult:
        """Run an adapter-owned command. Callers never supply arbitrary argv."""


class SubprocessCommandRunner:
    def run(self, argv: tuple[str, ...], *, cwd: Path, timeout_seconds: float) -> CommandResult:
        try:
            result = subprocess.run(
                argv,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return CommandResult(returncode=127, stdout="", stderr=str(exc))
        return CommandResult(result.returncode, result.stdout, result.stderr)


def observe_file_presence(
    repo_root: Path,
    relative_paths: Iterable[str | Path],
    *,
    observed_at: datetime | None = None,
    starting_sequence: int = 1,
) -> tuple[RepositorySemanticEvent, ...]:
    root = repo_root.resolve()
    at = _at(observed_at)
    events: list[RepositorySemanticEvent] = []
    for offset, requested in enumerate(relative_paths):
        relative, target = _repo_path(root, requested)
        sequence = starting_sequence + offset
        present = target.exists()
        event_id = _id("file-observation", sequence, at, relative, present)
        evidence = EvidenceRef(
            event_id=event_id,
            sequence=sequence,
            source="repository-filesystem",
            locator=relative,
        )
        claim = _claim(
            event_id=event_id,
            subject=f"path:{relative}",
            predicate="present",
            value=present,
            status=EpistemicStatus.OBSERVED,
            reliability=SourceReliability.AUTHORITATIVE,
            evidence=evidence,
            observed_at=at,
            conflict_group=f"path:{relative}:present",
        )
        events.append(_event(event_id, sequence, at, "repository-filesystem", (claim,)))
    return tuple(events)


def observe_git_status(
    repo_root: Path,
    *,
    observed_at: datetime | None = None,
    sequence: int = 1,
    expires_after: timedelta = timedelta(minutes=5),
    runner: FixedCommandRunner | None = None,
) -> RepositorySemanticEvent:
    root = repo_root.resolve()
    at = _at(observed_at)
    command_runner = runner or SubprocessCommandRunner()
    status = command_runner.run(
        ("git", "-c", "color.ui=false", "status", "--porcelain=v1"),
        cwd=root,
        timeout_seconds=5.0,
    )
    branch = command_runner.run(
        ("git", "branch", "--show-current"), cwd=root, timeout_seconds=5.0
    )
    successful = status.returncode == 0
    branch_successful = branch.returncode == 0 and bool(branch.stdout.strip())
    event_id = _id(
        "git-observation",
        sequence,
        at,
        status.returncode,
        status.stdout,
        branch.returncode,
        branch.stdout,
    )
    evidence = EvidenceRef(
        event_id=event_id,
        sequence=sequence,
        source="git",
        locator="git status --porcelain=v1",
    )
    expiry = at + expires_after
    clean = _claim(
        event_id=event_id,
        subject="repository",
        predicate="git.clean",
        value=not bool(status.stdout.strip()) if successful else None,
        status=EpistemicStatus.OBSERVED if successful else EpistemicStatus.UNKNOWN,
        reliability=(
            SourceReliability.AUTHORITATIVE if successful else SourceReliability.UNKNOWN
        ),
        evidence=evidence,
        observed_at=at,
        expires_at=expiry,
        conflict_group="repository:git.clean",
    )
    branch_claim = _claim(
        event_id=event_id,
        subject="repository",
        predicate="git.branch",
        value=branch.stdout.strip() if branch_successful else None,
        status=(EpistemicStatus.OBSERVED if branch_successful else EpistemicStatus.UNKNOWN),
        reliability=(
            SourceReliability.AUTHORITATIVE
            if branch_successful
            else SourceReliability.UNKNOWN
        ),
        evidence=evidence,
        observed_at=at,
        expires_at=expiry,
        conflict_group="repository:git.branch",
    )
    return _event(event_id, sequence, at, "git", (clean, branch_claim))


def adapt_task_evidence(
    evidence: TaskEvidence,
    *,
    observed_at: datetime,
    sequence: int,
    expires_at: datetime | None = None,
) -> RepositorySemanticEvent:
    at = _at(observed_at)
    event_id = _id(
        "task-evidence",
        sequence,
        at,
        evidence.task_id,
        evidence.status,
        evidence.blocked,
        evidence.source,
    )
    ref = EvidenceRef(event_id, evidence.source, sequence=sequence, locator=evidence.task_id)
    subject = f"task:{evidence.task_id}"
    claims = (
        _claim(
            event_id=event_id,
            subject=subject,
            predicate="status",
            value=evidence.status,
            status=EpistemicStatus.REPORTED,
            reliability=evidence.reliability,
            evidence=ref,
            observed_at=at,
            expires_at=expires_at,
            conflict_group=f"{subject}:status",
        ),
        _claim(
            event_id=event_id,
            subject=subject,
            predicate="blocked",
            value=evidence.blocked,
            status=EpistemicStatus.REPORTED,
            reliability=evidence.reliability,
            evidence=ref,
            observed_at=at,
            expires_at=expires_at,
            conflict_group=f"{subject}:blocked",
        ),
    )
    return _event(event_id, sequence, at, evidence.source, claims)


def adapt_check_evidence(
    evidence: CheckEvidence,
    *,
    observed_at: datetime,
    sequence: int,
    expires_at: datetime | None = None,
) -> RepositorySemanticEvent:
    at = _at(observed_at)
    event_id = _id(
        "check-evidence",
        sequence,
        at,
        evidence.name,
        evidence.status,
        evidence.required,
        evidence.source,
    )
    ref = EvidenceRef(event_id, evidence.source, sequence=sequence, locator=evidence.name)
    subject = f"check:{evidence.name}"
    claims = (
        _claim(
            event_id=event_id,
            subject=subject,
            predicate="status",
            value=evidence.status,
            status=EpistemicStatus.OBSERVED,
            reliability=evidence.reliability,
            evidence=ref,
            observed_at=at,
            expires_at=expires_at,
            conflict_group=f"{subject}:status",
        ),
        _claim(
            event_id=event_id,
            subject=subject,
            predicate="required",
            value=evidence.required,
            status=EpistemicStatus.REPORTED,
            reliability=evidence.reliability,
            evidence=ref,
            observed_at=at,
            expires_at=expires_at,
            conflict_group=f"{subject}:required",
        ),
    )
    return _event(event_id, sequence, at, evidence.source, claims)


def observe_repository(
    repo_root: Path,
    *,
    paths: tuple[str, ...] = ("README.md", "pyproject.toml", "src", "tests"),
    observed_at: datetime | None = None,
    starting_sequence: int = 1,
    runner: FixedCommandRunner | None = None,
) -> tuple[RepositorySemanticEvent, ...]:
    at = _at(observed_at)
    files = observe_file_presence(
        repo_root, paths, observed_at=at, starting_sequence=starting_sequence
    )
    git = observe_git_status(
        repo_root,
        observed_at=at,
        sequence=starting_sequence + len(files),
        runner=runner,
    )
    return (*files, git)


def _event(
    event_id: str,
    sequence: int,
    at: datetime,
    source: str,
    claims: tuple[Claim, ...],
) -> RepositorySemanticEvent:
    return RepositorySemanticEvent(
        event_id=event_id,
        sequence=sequence,
        kind=CLAIMS_RECORDED,
        source=source,
        occurred_at=at,
        payload=ClaimBatch(claims),
    )


def _claim(
    *,
    event_id: str,
    subject: str,
    predicate: str,
    value: str | int | float | bool | None,
    status: EpistemicStatus,
    reliability: SourceReliability,
    evidence: EvidenceRef,
    observed_at: datetime,
    conflict_group: str,
    expires_at: datetime | None = None,
) -> Claim:
    claim_id = _id("claim", event_id, subject, predicate, value)
    return Claim(
        claim_id=claim_id,
        subject=subject,
        predicate=predicate,
        value=value,
        epistemic_status=status,
        source_reliability=reliability,
        evidence=(evidence,),
        observed_at=observed_at,
        effective_at=observed_at,
        expires_at=expires_at,
        conflict_group=conflict_group,
    )


def _repo_path(root: Path, requested: str | Path) -> tuple[str, Path]:
    candidate = Path(requested)
    if candidate.is_absolute() or ".." in PurePath(candidate).parts:
        raise ValueError(f"repository path must be relative and contained: {requested}")
    target = (root / candidate).resolve(strict=False)
    try:
        relative = target.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError(f"repository path escapes root: {requested}") from exc
    if relative in ("", "."):
        raise ValueError("repository path must identify a child entry")
    return relative, target


def _at(value: datetime | None) -> datetime:
    result = value or datetime.now(UTC)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("observation time must be timezone-aware")
    return result


def _id(namespace: str, *parts: object) -> str:
    payload = json.dumps(
        [part.isoformat() if isinstance(part, datetime) else part for part in parts],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return f"{namespace}:{hashlib.sha256(payload).hexdigest()}"

