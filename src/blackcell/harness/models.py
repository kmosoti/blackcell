from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AgentSpec:
    key: str
    role: str
    objective: str
    sandbox: str


@dataclass(frozen=True, slots=True)
class PlanStep:
    key: str
    summary: str
    uses: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class HarnessPlan:
    goal: str
    agents: tuple[AgentSpec, ...]
    steps: tuple[PlanStep, ...]


@dataclass(frozen=True, slots=True)
class TraceEvent:
    index: int
    kind: str
    message: str


@dataclass(frozen=True, slots=True)
class RunTrace:
    runtime: str
    status: str
    events: tuple[TraceEvent, ...]
