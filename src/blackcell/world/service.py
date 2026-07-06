from __future__ import annotations

import subprocess
from pathlib import Path

from blackcell.config import find_repo_root
from blackcell.world.models import Belief, Expectation, Fact, Observation, Surprise, WorldSnapshot


def observe_repo(start: Path | None = None) -> WorldSnapshot:
    from blackcell.runtime import list_runtime_adapters

    root = find_repo_root(start)
    branch = _git_output(root, "git", "branch", "--show-current")
    entries = {
        "README.md": (root / "README.md").exists(),
        "docs": (root / "docs").exists(),
        "src": (root / "src").exists(),
        "tests": (root / "tests").exists(),
        "pyproject.toml": (root / "pyproject.toml").exists(),
    }
    is_dirty = bool(_git_output(root, "git", "status", "--short"))
    adapters = list_runtime_adapters()
    available_adapters = tuple(adapter.name for adapter in adapters if adapter.available)

    observations = (
        *(
            Observation(
                key=f"repo:{name}",
                kind="presence",
                message=f"{name} is {'present' if present else 'missing'}",
                path=name,
            )
            for name, present in entries.items()
        ),
        Observation(
            key="repo:git-status",
            kind="workspace",
            message="workspace has local modifications" if is_dirty else "workspace is clean",
        ),
        *(
            Observation(
                key=f"runtime:{adapter.name}",
                kind="runtime",
                message=f"{adapter.name} runtime is "
                + ("available" if adapter.available else "unavailable"),
            )
            for adapter in adapters
        ),
    )

    facts = (
        *(
            Fact(subject="repo", predicate="has_path", object=name, source=f"observe:{name}")
            for name, present in entries.items()
            if present
        ),
        Fact(
            subject="repo",
            predicate="workspace_state",
            object="dirty" if is_dirty else "clean",
            source="observe:git-status",
        ),
        *(
            Fact(
                subject="runtime",
                predicate="has_adapter",
                object=adapter,
                source="observe:runtime",
            )
            for adapter in available_adapters
        ),
    )

    beliefs = _beliefs(entries, available_adapters)
    expectations = _expectations(entries)
    surprises = _surprises(entries)
    return WorldSnapshot(
        repo_root=root,
        branch=branch,
        observations=observations,
        facts=facts,
        beliefs=beliefs,
        expectations=expectations,
        surprises=surprises,
    )


def _beliefs(entries: dict[str, bool], available_adapters: tuple[str, ...]) -> tuple[Belief, ...]:
    beliefs: list[Belief] = []
    if entries["pyproject.toml"] and entries["src"]:
        beliefs.append(
            Belief(
                key="belief:python-package",
                status="supported",
                summary="The repository can host a Python package-oriented harness.",
                evidence=("repo:pyproject.toml", "repo:src"),
            )
        )
    if entries["README.md"] and entries["docs"]:
        beliefs.append(
            Belief(
                key="belief:docs-split",
                status="supported",
                summary=(
                    "The repository can keep a concise README and deeper documentation in docs/."
                ),
                evidence=("repo:README.md", "repo:docs"),
            )
        )
    beliefs.append(
        Belief(
            key="belief:runtime-agnostic",
            status="supported",
            summary="BlackCell can expose runtime adapters generically: "
            + ", ".join(available_adapters),
            evidence=tuple(f"runtime:{adapter}" for adapter in available_adapters),
        )
    )
    return tuple(beliefs)


def _expectations(entries: dict[str, bool]) -> tuple[Expectation, ...]:
    expectations = [
        Expectation(
            key="expectation:docs-boundary",
            summary=(
                "README should stay focused while architecture and research notes live in docs/."
            ),
            rationale="A concise repo entrypoint reduces conceptual drag.",
        )
    ]
    if entries["tests"]:
        expectations.append(
            Expectation(
                key="expectation:verification",
                summary="New harness slices should remain covered by unit tests.",
                rationale="The repo already has an established pytest workflow.",
            )
        )
    return tuple(expectations)


def _surprises(entries: dict[str, bool]) -> tuple[Surprise, ...]:
    surprises: list[Surprise] = []
    if not entries["docs"]:
        surprises.append(
            Surprise(
                key="surprise:docs-missing",
                summary="Detailed docs are missing.",
                expected="docs/ present",
                observed="docs/ absent",
                severity="warning",
            )
        )
    return tuple(surprises)


def _git_output(root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            args,
            cwd=root,
            capture_output=True,
            check=True,
            text=True,
        )
    except OSError, subprocess.CalledProcessError:
        return None
    value = result.stdout.strip()
    return value or None
