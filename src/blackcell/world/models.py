from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Observation:
    key: str
    kind: str
    message: str
    path: str | None = None


@dataclass(frozen=True, slots=True)
class Fact:
    subject: str
    predicate: str
    object: str
    source: str
    confidence: float = 1.0


@dataclass(frozen=True, slots=True)
class Belief:
    key: str
    status: str
    summary: str
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Expectation:
    key: str
    summary: str
    rationale: str


@dataclass(frozen=True, slots=True)
class Surprise:
    key: str
    summary: str
    expected: str
    observed: str
    severity: str = "info"


@dataclass(frozen=True, slots=True)
class WorldSnapshot:
    repo_root: Path
    branch: str | None
    observations: tuple[Observation, ...]
    facts: tuple[Fact, ...]
    beliefs: tuple[Belief, ...]
    expectations: tuple[Expectation, ...]
    surprises: tuple[Surprise, ...]
