import re
from dataclasses import dataclass, replace
from pathlib import Path

from blackcell.config import find_repo_root
from blackcell.control_plane.agent_rendering import (
    CODEX_CLI_TARGET,
    MARKDOWN_END_MARKER,
    MARKDOWN_START_PREFIX,
    TOML_DIGEST_PREFIX,
    TOML_MANAGED_MARKER,
    RenderedAgentWorkflowArtifact,
    render_codex_cli_artifacts,
    sha256_digest,
)
from blackcell.control_plane.agent_workflow import validate_agent_workflow
from blackcell.control_plane.models import PlanContract

ARTIFACT_ACTION_CREATE = "create"
ARTIFACT_ACTION_UPDATE = "update"
ARTIFACT_ACTION_NOOP = "noop"
ARTIFACT_ACTION_CONFLICT = "conflict"

_MARKDOWN_SECTION_RE = re.compile(
    r"(?ms)^<!-- blackcell:agent-workflow:start digest=(?P<digest>sha256:[0-9a-f]{64}) -->\n"
    r"(?P<body>.*?)"
    r"^<!-- blackcell:agent-workflow:end -->\n?"
)


@dataclass(frozen=True, slots=True)
class AgentWorkflowArtifactSummary:
    exists: bool
    managed: bool
    digest: str | None = None
    body_digest: str | None = None
    size_bytes: int = 0


@dataclass(frozen=True, slots=True)
class AgentWorkflowArtifactAction:
    path: str
    action: str
    digest: str
    current: AgentWorkflowArtifactSummary
    rendered: AgentWorkflowArtifactSummary
    applied: bool = False
    message: str = ""


@dataclass(frozen=True, slots=True)
class AgentWorkflowProjectionResult:
    target: str
    operation: str
    dry_run: bool
    drift: bool
    conflicts: bool
    actions: tuple[AgentWorkflowArtifactAction, ...]


@dataclass(frozen=True, slots=True)
class _PlannedArtifactAction:
    artifact: RenderedAgentWorkflowArtifact
    action: AgentWorkflowArtifactAction
    next_content: str | None


@dataclass(frozen=True, slots=True)
class _MarkdownSection:
    start: int
    end: int
    digest: str
    body: str


@dataclass(frozen=True, slots=True)
class _MarkdownSectionLookup:
    section: _MarkdownSection | None = None
    malformed: bool = False


def diff_codex_cli_agent_workflow(
    contract: PlanContract,
    *,
    start: Path | None = None,
    target: str = CODEX_CLI_TARGET,
) -> AgentWorkflowProjectionResult:
    return _project_codex_cli_agent_workflow(
        contract,
        start=start,
        target=target,
        operation="diff",
        apply_changes=False,
    )


def install_codex_cli_agent_workflow(
    contract: PlanContract,
    *,
    start: Path | None = None,
    target: str = CODEX_CLI_TARGET,
    apply_changes: bool = False,
) -> AgentWorkflowProjectionResult:
    return _project_codex_cli_agent_workflow(
        contract,
        start=start,
        target=target,
        operation="install",
        apply_changes=apply_changes,
    )


def check_codex_cli_agent_workflow_drift(
    contract: PlanContract,
    *,
    start: Path | None = None,
    target: str = CODEX_CLI_TARGET,
) -> AgentWorkflowProjectionResult:
    return _project_codex_cli_agent_workflow(
        contract,
        start=start,
        target=target,
        operation="check-drift",
        apply_changes=False,
    )


def _project_codex_cli_agent_workflow(
    contract: PlanContract,
    *,
    start: Path | None,
    target: str,
    operation: str,
    apply_changes: bool,
) -> AgentWorkflowProjectionResult:
    _validate_target(target)
    artifacts = render_codex_cli_artifacts(contract)
    validation = validate_agent_workflow(contract, artifacts=artifacts)
    if not validation.valid:
        codes = ", ".join(error.code for error in validation.errors)
        raise ValueError(f"agent workflow is invalid: {codes}")

    root = find_repo_root(start)
    planned = tuple(_plan_artifact(root, artifact) for artifact in artifacts)

    actions: list[AgentWorkflowArtifactAction] = []
    for plan in planned:
        action = plan.action
        if (
            apply_changes
            and action.action in {ARTIFACT_ACTION_CREATE, ARTIFACT_ACTION_UPDATE}
            and plan.next_content is not None
        ):
            path = root / plan.artifact.path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(plan.next_content, encoding="utf-8")
            action = replace(action, applied=True)
        actions.append(action)

    conflicts = any(action.action == ARTIFACT_ACTION_CONFLICT for action in actions)
    drift = any(action.action != ARTIFACT_ACTION_NOOP for action in actions)
    if apply_changes:
        drift = conflicts

    return AgentWorkflowProjectionResult(
        target=target,
        operation=operation,
        dry_run=not apply_changes,
        drift=drift,
        conflicts=conflicts,
        actions=tuple(actions),
    )


def _plan_artifact(
    root: Path,
    artifact: RenderedAgentWorkflowArtifact,
) -> _PlannedArtifactAction:
    path = root / artifact.path
    if artifact.kind == "toml":
        return _plan_toml_artifact(path, artifact)
    if artifact.kind == "markdown":
        return _plan_markdown_artifact(path, artifact)
    raise ValueError(f"unsupported rendered artifact kind: {artifact.kind}")


def _plan_toml_artifact(
    path: Path,
    artifact: RenderedAgentWorkflowArtifact,
) -> _PlannedArtifactAction:
    rendered = _rendered_summary(artifact)
    if not path.exists():
        current = AgentWorkflowArtifactSummary(exists=False, managed=False)
        action = _action(
            artifact,
            ARTIFACT_ACTION_CREATE,
            current,
            rendered,
            message="managed TOML artifact is missing",
        )
        return _PlannedArtifactAction(artifact, action, artifact.content)

    text = path.read_text(encoding="utf-8")
    current = _toml_summary(text)
    if not current.managed:
        action = _action(
            artifact,
            ARTIFACT_ACTION_CONFLICT,
            current,
            rendered,
            message="existing TOML artifact is not BlackCell managed",
        )
        return _PlannedArtifactAction(artifact, action, None)

    if text == artifact.content:
        action = _action(artifact, ARTIFACT_ACTION_NOOP, current, rendered)
        return _PlannedArtifactAction(artifact, action, None)

    action = _action(
        artifact,
        ARTIFACT_ACTION_UPDATE,
        current,
        rendered,
        message="managed TOML artifact differs from rendered content",
    )
    return _PlannedArtifactAction(artifact, action, artifact.content)


def _plan_markdown_artifact(
    path: Path,
    artifact: RenderedAgentWorkflowArtifact,
) -> _PlannedArtifactAction:
    rendered = _rendered_summary(artifact)
    if not path.exists():
        current = AgentWorkflowArtifactSummary(exists=False, managed=False)
        action = _action(
            artifact,
            ARTIFACT_ACTION_CREATE,
            current,
            rendered,
            message="managed Markdown section is missing",
        )
        return _PlannedArtifactAction(artifact, action, artifact.content)

    text = path.read_text(encoding="utf-8")
    lookup = _find_markdown_section(text)
    if lookup.malformed:
        current = AgentWorkflowArtifactSummary(
            exists=True,
            managed=False,
            size_bytes=len(text.encode("utf-8")),
        )
        action = _action(
            artifact,
            ARTIFACT_ACTION_CONFLICT,
            current,
            rendered,
            message="existing Markdown artifact has malformed BlackCell markers",
        )
        return _PlannedArtifactAction(artifact, action, None)

    if lookup.section is None:
        current = AgentWorkflowArtifactSummary(
            exists=True,
            managed=False,
            size_bytes=len(text.encode("utf-8")),
        )
        next_content = _append_markdown_section(text, artifact.content)
        action = _action(
            artifact,
            ARTIFACT_ACTION_UPDATE,
            current,
            rendered,
            message="managed Markdown section will be appended",
        )
        return _PlannedArtifactAction(artifact, action, next_content)

    section = lookup.section
    current = AgentWorkflowArtifactSummary(
        exists=True,
        managed=True,
        digest=section.digest,
        body_digest=sha256_digest(section.body),
        size_bytes=len(text.encode("utf-8")),
    )
    next_content = text[: section.start] + artifact.content + text[section.end :]
    if next_content == text:
        action = _action(artifact, ARTIFACT_ACTION_NOOP, current, rendered)
        return _PlannedArtifactAction(artifact, action, None)

    action = _action(
        artifact,
        ARTIFACT_ACTION_UPDATE,
        current,
        rendered,
        message="managed Markdown section differs from rendered content",
    )
    return _PlannedArtifactAction(artifact, action, next_content)


def _toml_summary(text: str) -> AgentWorkflowArtifactSummary:
    digest = _extract_toml_digest(text)
    body = _toml_body(text)
    return AgentWorkflowArtifactSummary(
        exists=True,
        managed=TOML_MANAGED_MARKER in text.splitlines() and digest is not None,
        digest=digest,
        body_digest=sha256_digest(body),
        size_bytes=len(text.encode("utf-8")),
    )


def _toml_body(text: str) -> str:
    lines = []
    for line in text.splitlines(keepends=True):
        if line.rstrip("\n") == TOML_MANAGED_MARKER:
            continue
        if line.startswith(TOML_DIGEST_PREFIX):
            continue
        lines.append(line)
    return "".join(lines)


def _extract_toml_digest(text: str) -> str | None:
    for line in text.splitlines():
        if line.startswith(TOML_DIGEST_PREFIX):
            return line.removeprefix(TOML_DIGEST_PREFIX).strip()
    return None


def _find_markdown_section(text: str) -> _MarkdownSectionLookup:
    start_count = text.count(MARKDOWN_START_PREFIX)
    end_count = text.count(MARKDOWN_END_MARKER)
    if start_count == 0 and end_count == 0:
        return _MarkdownSectionLookup()
    if start_count != 1 or end_count != 1:
        return _MarkdownSectionLookup(malformed=True)

    match = _MARKDOWN_SECTION_RE.search(text)
    if match is None:
        return _MarkdownSectionLookup(malformed=True)

    return _MarkdownSectionLookup(
        section=_MarkdownSection(
            start=match.start(),
            end=match.end(),
            digest=match.group("digest"),
            body=match.group("body"),
        )
    )


def _append_markdown_section(text: str, section: str) -> str:
    if not text:
        return section
    prefix = text
    if not prefix.endswith("\n"):
        prefix += "\n"
    if not prefix.endswith("\n\n"):
        prefix += "\n"
    return prefix + section


def _rendered_summary(
    artifact: RenderedAgentWorkflowArtifact,
) -> AgentWorkflowArtifactSummary:
    return AgentWorkflowArtifactSummary(
        exists=True,
        managed=True,
        digest=artifact.digest,
        body_digest=artifact.digest,
        size_bytes=len(artifact.content.encode("utf-8")),
    )


def _action(
    artifact: RenderedAgentWorkflowArtifact,
    action: str,
    current: AgentWorkflowArtifactSummary,
    rendered: AgentWorkflowArtifactSummary,
    *,
    message: str = "",
) -> AgentWorkflowArtifactAction:
    return AgentWorkflowArtifactAction(
        path=artifact.path,
        action=action,
        digest=artifact.digest,
        current=current,
        rendered=rendered,
        message=message,
    )


def _validate_target(target: str) -> None:
    if target != CODEX_CLI_TARGET:
        raise ValueError(f"unsupported agent workflow target: {target}")
