import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import Any


class IssueType(StrEnum):
    FEATURE = "feature"
    BUG = "bug"
    REFACTOR = "refactor"
    CHORE = "chore"


class IssueStatus(StrEnum):
    BACKLOG = "Backlog"
    TODO = "Todo"
    IN_PROGRESS = "In Progress"
    REVIEW_REQUIRED = "Review Required"
    DONE = "Done"


class Priority(StrEnum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class Complexity(IntEnum):
    ONE = 1
    THREE = 3
    FIVE = 5
    EIGHT = 8
    THIRTEEN = 13


class ValidationLevel(StrEnum):
    ERROR = "error"
    WARNING = "warning"


CODEX_AGENT_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]*$"
_CODEX_AGENT_KEY_RE = re.compile(CODEX_AGENT_KEY_PATTERN)


@dataclass(frozen=True, slots=True)
class ValidationMessage:
    level: ValidationLevel
    code: str
    message: str
    path: str


@dataclass(frozen=True, slots=True)
class ValidationResult:
    valid: bool
    errors: tuple[ValidationMessage, ...] = ()
    warnings: tuple[ValidationMessage, ...] = ()

    @classmethod
    def from_messages(cls, messages: Sequence[ValidationMessage]) -> ValidationResult:
        errors = tuple(message for message in messages if message.level is ValidationLevel.ERROR)
        warnings = tuple(
            message for message in messages if message.level is ValidationLevel.WARNING
        )
        return cls(valid=not errors, errors=errors, warnings=warnings)


@dataclass(frozen=True, slots=True)
class ProjectPlan:
    key: str
    name: str
    description: str | None = None


@dataclass(frozen=True, slots=True)
class Roadmap:
    key: str
    title: str
    epics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Epic:
    key: str
    title: str
    roadmap: str
    milestones: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Milestone:
    key: str
    title: str
    epic: str
    target: str | None = None


@dataclass(frozen=True, slots=True)
class GlobalPolicy:
    acceptance_criteria: tuple[str, ...] = ()
    definition_of_ready: tuple[str, ...] = ()
    definition_of_done: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PRPolicy:
    require_issue_link: bool = True
    required_checks: tuple[str, ...] = ()
    merge_strategy: str = "squash"


@dataclass(frozen=True, slots=True)
class NativeAutomation:
    key: str
    name: str
    trigger: str
    action: str
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class AgentWorker:
    key: str
    name: str
    model: str
    owns: tuple[str, ...] = ()
    change_spec: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CodexCliAgent:
    key: str
    name: str
    description: str
    developer_instructions: str
    sandbox_mode: str = "read-only"


@dataclass(frozen=True, slots=True)
class CodexCliWorkflow:
    max_threads: int = 6
    max_depth: int = 1
    agents: tuple[CodexCliAgent, ...] = ()


@dataclass(frozen=True, slots=True)
class AgentWorkflow:
    model: str
    workers: tuple[AgentWorker, ...] = ()
    codex_cli: CodexCliWorkflow | None = None


@dataclass(frozen=True, slots=True)
class IssuePlan:
    key: str
    title: str
    type: IssueType
    status: IssueStatus
    priority: Priority
    complexity: Complexity
    epic: str | None = None
    milestone: str | None = None
    depends_on: tuple[str, ...] = ()
    areas_of_responsibility: tuple[str, ...] = ()
    scope: tuple[str, ...] = ()
    context: tuple[str, ...] = ()
    change_spec: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    definition_of_ready: tuple[str, ...] = ()
    definition_of_done: tuple[str, ...] = ()

    @property
    def kind(self) -> IssueType:
        return self.type

    @property
    def github_title(self) -> str:
        return self.title

    @property
    def is_done(self) -> bool:
        return self.status is IssueStatus.DONE

    @property
    def is_active(self) -> bool:
        return self.status in {
            IssueStatus.IN_PROGRESS,
            IssueStatus.REVIEW_REQUIRED,
        }

    @property
    def is_backlog(self) -> bool:
        return self.status is IssueStatus.BACKLOG

    @property
    def has_dependencies(self) -> bool:
        return bool(self.depends_on)

    @property
    def has_scope(self) -> bool:
        return bool(self.scope)

    @property
    def has_delivery_contract(self) -> bool:
        return bool(
            self.change_spec
            or self.acceptance_criteria
            or self.definition_of_ready
            or self.definition_of_done
        )

    @property
    def hierarchy_keys(self) -> tuple[str, ...]:
        return tuple(key for key in (self.epic, self.milestone) if key is not None)


@dataclass(frozen=True, slots=True)
class PlanContract:
    version: int
    project: ProjectPlan
    global_policy: GlobalPolicy
    pr_policy: PRPolicy
    roadmaps: tuple[Roadmap, ...] = ()
    epics: tuple[Epic, ...] = ()
    milestones: tuple[Milestone, ...] = ()
    issues: tuple[IssuePlan, ...] = ()
    native_automation: tuple[NativeAutomation, ...] = ()
    agent_workflow: AgentWorkflow | None = None


@dataclass(frozen=True, slots=True)
class DependencyContext:
    key: str
    title: str
    status: IssueStatus


@dataclass(frozen=True, slots=True)
class AgentIssueContext:
    key: str
    title: str
    type: IssueType
    status: IssueStatus
    priority: Priority
    complexity: Complexity
    epic: str | None
    milestone: str | None
    depends_on: tuple[DependencyContext, ...]
    blocked_by: tuple[DependencyContext, ...]
    areas_of_responsibility: tuple[str, ...]
    scope: tuple[str, ...]
    context: tuple[str, ...]
    change_spec: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    definition_of_ready: tuple[str, ...]
    definition_of_done: tuple[str, ...]
    pr_policy: PRPolicy
    agent_workflow: AgentWorkflow | None = None


@dataclass(frozen=True, slots=True)
class ProjectFieldShape:
    name: str
    type: str
    options: tuple[str | int, ...] = ()


@dataclass(frozen=True, slots=True)
class ProjectShape:
    project: ProjectPlan
    fields: tuple[ProjectFieldShape, ...]
    roadmaps: tuple[Roadmap, ...]
    epics: tuple[Epic, ...]
    milestones: tuple[Milestone, ...]
    issue_count: int
    native_automation: tuple[NativeAutomation, ...]
    pr_policy: PRPolicy
    agent_workflow: AgentWorkflow | None = None


def enum_values(enum_type: type[StrEnum] | type[IntEnum]) -> list[str | int]:
    return [member.value for member in enum_type]


def contract_from_mapping(data: Mapping[str, Any]) -> PlanContract:
    _reject_unknown(
        data,
        {
            "version",
            "project",
            "global",
            "pr_policy",
            "roadmaps",
            "epics",
            "milestones",
            "issues",
            "native_automation",
            "agent_workflow",
        },
        "$",
    )

    version = _int(data, "version", "$")
    if version != 1:
        raise ValueError("version must be 1")

    return PlanContract(
        version=version,
        project=_project(_mapping(data, "project", "$"), "$.project"),
        global_policy=_global_policy(_optional_mapping(data, "global", "$"), "$.global"),
        pr_policy=_pr_policy(_optional_mapping(data, "pr_policy", "$"), "$.pr_policy"),
        roadmaps=tuple(
            _roadmap(item, f"$.roadmaps[{index}]")
            for index, item in enumerate(_sequence(data, "roadmaps", "$", default=()))
        ),
        epics=tuple(
            _epic(item, f"$.epics[{index}]")
            for index, item in enumerate(_sequence(data, "epics", "$", default=()))
        ),
        milestones=tuple(
            _milestone(item, f"$.milestones[{index}]")
            for index, item in enumerate(_sequence(data, "milestones", "$", default=()))
        ),
        issues=tuple(
            _issue(item, f"$.issues[{index}]")
            for index, item in enumerate(_sequence(data, "issues", "$", default=()))
        ),
        native_automation=tuple(
            _native_automation(item, f"$.native_automation[{index}]")
            for index, item in enumerate(_sequence(data, "native_automation", "$", default=()))
        ),
        agent_workflow=_agent_workflow(
            _optional_mapping(data, "agent_workflow", "$"), "$.agent_workflow"
        ),
    )


def _project(data: Mapping[str, Any], path: str) -> ProjectPlan:
    _reject_unknown(data, {"key", "name", "description"}, path)
    return ProjectPlan(
        key=_string(data, "key", path),
        name=_string(data, "name", path),
        description=_optional_string(data, "description", path),
    )


def _global_policy(data: Mapping[str, Any], path: str) -> GlobalPolicy:
    _reject_unknown(
        data, {"acceptance_criteria", "definition_of_ready", "definition_of_done"}, path
    )
    return GlobalPolicy(
        acceptance_criteria=_strings(data, "acceptance_criteria", path, default=()),
        definition_of_ready=_strings(data, "definition_of_ready", path, default=()),
        definition_of_done=_strings(data, "definition_of_done", path, default=()),
    )


def _pr_policy(data: Mapping[str, Any], path: str) -> PRPolicy:
    _reject_unknown(data, {"require_issue_link", "required_checks", "merge_strategy"}, path)
    return PRPolicy(
        require_issue_link=_bool(data, "require_issue_link", path, default=True),
        required_checks=_strings(data, "required_checks", path, default=()),
        merge_strategy=_string(data, "merge_strategy", path, default="squash"),
    )


def _roadmap(data: Any, path: str) -> Roadmap:
    mapping = _as_mapping(data, path)
    _reject_unknown(mapping, {"key", "title", "epics"}, path)
    return Roadmap(
        key=_string(mapping, "key", path),
        title=_string(mapping, "title", path),
        epics=_strings(mapping, "epics", path, default=()),
    )


def _epic(data: Any, path: str) -> Epic:
    mapping = _as_mapping(data, path)
    _reject_unknown(mapping, {"key", "title", "roadmap", "milestones"}, path)
    return Epic(
        key=_string(mapping, "key", path),
        title=_string(mapping, "title", path),
        roadmap=_string(mapping, "roadmap", path),
        milestones=_strings(mapping, "milestones", path, default=()),
    )


def _milestone(data: Any, path: str) -> Milestone:
    mapping = _as_mapping(data, path)
    _reject_unknown(mapping, {"key", "title", "epic", "target"}, path)
    return Milestone(
        key=_string(mapping, "key", path),
        title=_string(mapping, "title", path),
        epic=_string(mapping, "epic", path),
        target=_optional_string(mapping, "target", path),
    )


def _native_automation(data: Any, path: str) -> NativeAutomation:
    mapping = _as_mapping(data, path)
    _reject_unknown(mapping, {"key", "name", "trigger", "action", "enabled"}, path)
    return NativeAutomation(
        key=_string(mapping, "key", path),
        name=_string(mapping, "name", path),
        trigger=_string(mapping, "trigger", path),
        action=_string(mapping, "action", path),
        enabled=_bool(mapping, "enabled", path, default=True),
    )


def _agent_workflow(data: Mapping[str, Any], path: str) -> AgentWorkflow | None:
    if not data:
        return None

    _reject_unknown(data, {"model", "workers", "codex_cli"}, path)
    model = _string(data, "model", path)
    return AgentWorkflow(
        model=model,
        workers=tuple(
            _agent_worker(item, f"{path}.workers[{index}]", default_model=model)
            for index, item in enumerate(_sequence(data, "workers", path, default=()))
        ),
        codex_cli=_codex_cli_workflow(
            _optional_mapping(data, "codex_cli", path), f"{path}.codex_cli"
        ),
    )


def _agent_worker(data: Any, path: str, *, default_model: str) -> AgentWorker:
    mapping = _as_mapping(data, path)
    _reject_unknown(mapping, {"key", "name", "model", "owns", "change_spec"}, path)
    return AgentWorker(
        key=_string(mapping, "key", path),
        name=_string(mapping, "name", path),
        model=_string(mapping, "model", path, default=default_model),
        owns=_strings(mapping, "owns", path, default=()),
        change_spec=_strings(mapping, "change_spec", path, default=()),
    )


def _codex_cli_workflow(data: Mapping[str, Any], path: str) -> CodexCliWorkflow | None:
    if not data:
        return None

    _reject_unknown(data, {"max_threads", "max_depth", "agents"}, path)
    return CodexCliWorkflow(
        max_threads=_int_default(data, "max_threads", path, default=6),
        max_depth=_int_default(data, "max_depth", path, default=1),
        agents=tuple(
            _codex_cli_agent(item, f"{path}.agents[{index}]")
            for index, item in enumerate(_sequence(data, "agents", path, default=()))
        ),
    )


def _codex_cli_agent(data: Any, path: str) -> CodexCliAgent:
    mapping = _as_mapping(data, path)
    _reject_unknown(
        mapping,
        {"key", "name", "description", "developer_instructions", "sandbox_mode"},
        path,
    )
    return CodexCliAgent(
        key=_codex_agent_key(mapping, "key", path),
        name=_string(mapping, "name", path),
        description=_string(mapping, "description", path),
        developer_instructions=_string(mapping, "developer_instructions", path),
        sandbox_mode=_string(mapping, "sandbox_mode", path, default="read-only"),
    )


def _issue(data: Any, path: str) -> IssuePlan:
    mapping = _as_mapping(data, path)
    _reject_unknown(
        mapping,
        {
            "key",
            "title",
            "type",
            "status",
            "priority",
            "complexity",
            "epic",
            "milestone",
            "depends_on",
            "areas_of_responsibility",
            "scope",
            "context",
            "change_spec",
            "acceptance_criteria",
            "definition_of_ready",
            "definition_of_done",
        },
        path,
    )
    return IssuePlan(
        key=_string(mapping, "key", path),
        title=_string(mapping, "title", path),
        type=_enum(IssueType, mapping, "type", path),
        status=_enum(IssueStatus, mapping, "status", path),
        priority=_enum(Priority, mapping, "priority", path),
        complexity=_complexity(mapping, "complexity", path),
        epic=_optional_string(mapping, "epic", path),
        milestone=_optional_string(mapping, "milestone", path),
        depends_on=_strings(mapping, "depends_on", path, default=()),
        areas_of_responsibility=_strings(mapping, "areas_of_responsibility", path, default=()),
        scope=_strings(mapping, "scope", path, default=()),
        context=_strings(mapping, "context", path, default=()),
        change_spec=_strings(mapping, "change_spec", path, default=()),
        acceptance_criteria=_strings(mapping, "acceptance_criteria", path, default=()),
        definition_of_ready=_strings(mapping, "definition_of_ready", path, default=()),
        definition_of_done=_strings(mapping, "definition_of_done", path, default=()),
    )


def _codex_agent_key(data: Mapping[str, Any], key: str, path: str) -> str:
    value = _string(data, key, path)
    if _CODEX_AGENT_KEY_RE.fullmatch(value):
        return value
    raise ValueError(
        f"{path}.{key} must start with an ASCII letter or digit and contain only "
        "ASCII letters, digits, hyphens, or underscores"
    )


def _mapping(data: Mapping[str, Any], key: str, path: str) -> Mapping[str, Any]:
    value = data.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"{path}.{key} must be a mapping")
    return value


def _optional_mapping(data: Mapping[str, Any], key: str, path: str) -> Mapping[str, Any]:
    value = data.get(key)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{path}.{key} must be a mapping")
    return value


def _as_mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} must be a mapping")
    return value


def _sequence(
    data: Mapping[str, Any],
    key: str,
    path: str,
    *,
    default: Sequence[Any] | None = None,
) -> Sequence[Any]:
    value = data.get(key, default)
    if value is None:
        raise ValueError(f"{path}.{key} must be a sequence")
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        raise ValueError(f"{path}.{key} must be a sequence")
    return value


def _string(data: Mapping[str, Any], key: str, path: str, *, default: str | None = None) -> str:
    value = data.get(key, default)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{path}.{key} must be a non-empty string")
    return value


def _optional_string(data: Mapping[str, Any], key: str, path: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"{path}.{key} must be a non-empty string")
    return value


def _strings(
    data: Mapping[str, Any],
    key: str,
    path: str,
    *,
    default: Sequence[str] | None = None,
) -> tuple[str, ...]:
    values = _sequence(data, key, path, default=default)
    result: list[str] = []
    for index, value in enumerate(values):
        if not isinstance(value, str) or not value:
            raise ValueError(f"{path}.{key}[{index}] must be a non-empty string")
        result.append(value)
    return tuple(result)


def _int(data: Mapping[str, Any], key: str, path: str) -> int:
    value = data.get(key)
    if not isinstance(value, int):
        raise ValueError(f"{path}.{key} must be an integer")
    return value


def _int_default(data: Mapping[str, Any], key: str, path: str, *, default: int) -> int:
    value = data.get(key, default)
    if not isinstance(value, int):
        raise ValueError(f"{path}.{key} must be an integer")
    return value


def _bool(data: Mapping[str, Any], key: str, path: str, *, default: bool) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{path}.{key} must be a boolean")
    return value


def _enum(
    enum_type: type[IssueType] | type[IssueStatus] | type[Priority],
    data: Mapping[str, Any],
    key: str,
    path: str,
) -> Any:
    value = _string(data, key, path)
    try:
        return enum_type(value)
    except ValueError as error:
        options = ", ".join(str(option) for option in enum_values(enum_type))
        raise ValueError(f"{path}.{key} must be one of: {options}") from error


def _complexity(data: Mapping[str, Any], key: str, path: str) -> Complexity:
    value = data.get(key)
    if not isinstance(value, int):
        raise ValueError(f"{path}.{key} must be one of: 1, 3, 5, 8, 13")
    try:
        return Complexity(value)
    except ValueError as error:
        raise ValueError(f"{path}.{key} must be one of: 1, 3, 5, 8, 13") from error


def _reject_unknown(data: Mapping[str, Any], allowed: set[str], path: str) -> None:
    unknown = sorted(str(key) for key in data if key not in allowed)
    if unknown:
        raise ValueError(f"{path} has unknown field(s): {', '.join(unknown)}")
