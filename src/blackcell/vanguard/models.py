from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class VerificationPlan:
    required: tuple[str, ...] = ()
    conditional: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ExecutorScope:
    allowed_files: tuple[str, ...] = ()
    forbidden: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ChangeSpec:
    change_id: str
    issue_key: str
    intent: str
    non_goals: tuple[str, ...]
    candidate_invariants: tuple[str, ...]
    behavior_contract: tuple[str, ...]
    preserved_contracts: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    verification: VerificationPlan
    executor_scope: ExecutorScope
    escalation_rules: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class QACommand:
    name: str
    command: str
    required: bool
    mutating: bool = False


@dataclass(frozen=True, slots=True)
class QAPlan:
    change_id: str
    issue_key: str
    commands: tuple[QACommand, ...]


@dataclass(frozen=True, slots=True)
class TemplateRecord:
    name: str
    title: str
    body: str
