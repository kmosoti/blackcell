from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any


class ConfigScope(StrEnum):
    PROJECT = "project"
    GLOBAL = "global"


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    key: str
    description: str
    mode: str
    prompt: str
    permission: dict[str, Any]
    color: str | None = None
    model: str | None = None
    temperature: float | None = None


@dataclass(frozen=True, slots=True)
class AgentCommand:
    key: str
    description: str
    template: str
    agent: str
    subtask: bool = False
    model: str | None = None


@dataclass(frozen=True, slots=True)
class AgentSummary:
    key: str
    mode: str
    description: str
    writes: str


@dataclass(frozen=True, slots=True)
class RenderedAgentArtifact:
    path: str
    kind: str
    body: str
    digest: str
    content: str
    key: str


@dataclass(frozen=True, slots=True)
class AgentArtifactSummary:
    exists: bool
    managed: bool
    digest: str | None = None
    body_digest: str | None = None
    size_bytes: int = 0


@dataclass(frozen=True, slots=True)
class AgentArtifactAction:
    path: str
    action: str
    digest: str
    current: AgentArtifactSummary
    rendered: AgentArtifactSummary
    applied: bool = False
    message: str = ""


@dataclass(frozen=True, slots=True)
class AgentProjectionResult:
    target: str
    scope: str
    operation: str
    dry_run: bool
    drift: bool
    conflicts: bool
    config_root: Path
    actions: tuple[AgentArtifactAction, ...]


@dataclass(frozen=True, slots=True)
class AgentDoctorCheck:
    key: str
    ok: bool
    message: str


@dataclass(frozen=True, slots=True)
class AgentDoctorReport:
    target: str
    scope: str
    config_root: Path
    executable: str | None
    checks: tuple[AgentDoctorCheck, ...]
