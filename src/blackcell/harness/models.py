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
class LatentTraceSummary:
    state_id: str
    action_id: str
    prediction_id: str
    confidence_label: str
    sample_count: int
    error_id: str
    transition_id: str
    sample_id: str
    recorded_path: str | None = None
    evidence_run_id: str | None = None
    evidence_event_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LatentTraceActionStats:
    action_id: str
    sample_count: int
    mean_semantic_distance: float
    surprise_count: int
    confidence_label: str


@dataclass(frozen=True, slots=True)
class RunTrace:
    runtime: str
    status: str
    events: tuple[TraceEvent, ...]
    latent: LatentTraceSummary | None = None
    latent_stats: tuple[LatentTraceActionStats, ...] = ()
    ledger_path: str | None = None
    ledger_run_id: str | None = None
