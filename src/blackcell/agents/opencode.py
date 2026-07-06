from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml

from blackcell.agents.models import (
    AgentArtifactAction,
    AgentArtifactSummary,
    AgentCommand,
    AgentDefinition,
    AgentDoctorCheck,
    AgentDoctorReport,
    AgentProjectionResult,
    ConfigScope,
    RenderedAgentArtifact,
)
from blackcell.agents.registry import blackcell_agent_commands, blackcell_agents
from blackcell.config import find_repo_root

OPENCODE_TARGET = "opencode"
ARTIFACT_ACTION_CREATE = "create"
ARTIFACT_ACTION_UPDATE = "update"
ARTIFACT_ACTION_NOOP = "noop"
ARTIFACT_ACTION_CONFLICT = "conflict"
MARKDOWN_START_PREFIX = "<!-- blackcell:opencode:start digest="
MARKDOWN_END_MARKER = "<!-- blackcell:opencode:end -->"

_MARKDOWN_SECTION_RE = re.compile(
    r"(?m)^<!-- blackcell:opencode:start digest=(?P<digest>sha256:[0-9a-f]{64}) -->$"
)


@dataclass(frozen=True, slots=True)
class _PlannedArtifactAction:
    artifact: RenderedAgentArtifact
    action: AgentArtifactAction
    next_content: str | None


def render_opencode_artifacts(
    *, scope: ConfigScope | str = ConfigScope.PROJECT
) -> tuple[RenderedAgentArtifact, ...]:
    parsed_scope = _parse_scope(scope)
    artifacts: list[RenderedAgentArtifact] = []
    artifacts.extend(_render_opencode_agent(agent) for agent in blackcell_agents())
    artifacts.extend(_render_opencode_command(command) for command in blackcell_agent_commands())
    return tuple(_with_display_path(artifact, parsed_scope) for artifact in artifacts)


def install_opencode_agent_pack(
    *,
    scope: ConfigScope | str = ConfigScope.PROJECT,
    start: Path | None = None,
    apply_changes: bool = False,
) -> AgentProjectionResult:
    return _project_opencode_agent_pack(
        scope=scope,
        start=start,
        operation="install",
        apply_changes=apply_changes,
    )


def check_opencode_agent_pack_drift(
    *,
    scope: ConfigScope | str = ConfigScope.PROJECT,
    start: Path | None = None,
) -> AgentProjectionResult:
    return _project_opencode_agent_pack(
        scope=scope,
        start=start,
        operation="check-drift",
        apply_changes=False,
    )


def doctor_opencode_agent_pack(
    *,
    scope: ConfigScope | str = ConfigScope.PROJECT,
    start: Path | None = None,
) -> AgentDoctorReport:
    parsed_scope = _parse_scope(scope)
    config_root = resolve_opencode_config_root(scope=parsed_scope, start=start)
    executable = _opencode_executable()
    checks = [
        AgentDoctorCheck(
            key="opencode-binary",
            ok=executable is not None,
            message=executable or "opencode executable was not found on PATH or ~/.opencode/bin",
        ),
        AgentDoctorCheck(
            key="config-root",
            ok=config_root.exists(),
            message=str(config_root),
        ),
    ]

    if executable is None:
        checks.append(
            AgentDoctorCheck(
                key="providers-list",
                ok=False,
                message="skipped because opencode is not installed",
            )
        )
    else:
        checks.append(_providers_list_check(executable))

    expected = tuple(
        resolve_artifact_path(config_root, artifact) for artifact in _render_raw_artifacts()
    )
    missing = [path for path in expected if not path.exists()]
    checks.append(
        AgentDoctorCheck(
            key="managed-artifacts",
            ok=not missing,
            message="all managed artifacts are present"
            if not missing
            else f"missing {len(missing)} managed artifact(s)",
        )
    )

    if executable and not missing:
        checks.extend(_debug_agent_checks(executable, blackcell_agents()))

    return AgentDoctorReport(
        target=OPENCODE_TARGET,
        scope=parsed_scope.value,
        config_root=config_root,
        executable=executable,
        checks=tuple(checks),
    )


def resolve_opencode_config_root(
    *,
    scope: ConfigScope | str = ConfigScope.PROJECT,
    start: Path | None = None,
) -> Path:
    parsed_scope = _parse_scope(scope)
    if parsed_scope is ConfigScope.PROJECT:
        return find_repo_root(start) / ".opencode"
    return Path.home() / ".config" / "opencode"


def resolve_artifact_path(config_root: Path, artifact: RenderedAgentArtifact) -> Path:
    return config_root / _relative_artifact_path(artifact.path)


def sha256_digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _project_opencode_agent_pack(
    *,
    scope: ConfigScope | str,
    start: Path | None,
    operation: str,
    apply_changes: bool,
) -> AgentProjectionResult:
    parsed_scope = _parse_scope(scope)
    config_root = resolve_opencode_config_root(scope=parsed_scope, start=start)
    artifacts = render_opencode_artifacts(scope=parsed_scope)
    planned = tuple(_plan_artifact(config_root, artifact) for artifact in artifacts)

    actions: list[AgentArtifactAction] = []
    for plan in planned:
        action = plan.action
        if (
            apply_changes
            and action.action in {ARTIFACT_ACTION_CREATE, ARTIFACT_ACTION_UPDATE}
            and plan.next_content is not None
        ):
            path = resolve_artifact_path(config_root, plan.artifact)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(plan.next_content, encoding="utf-8")
            action = replace(action, applied=True)
        actions.append(action)

    conflicts = any(action.action == ARTIFACT_ACTION_CONFLICT for action in actions)
    drift = any(action.action != ARTIFACT_ACTION_NOOP for action in actions)
    if apply_changes:
        drift = conflicts

    return AgentProjectionResult(
        target=OPENCODE_TARGET,
        scope=parsed_scope.value,
        operation=operation,
        dry_run=not apply_changes,
        drift=drift,
        conflicts=conflicts,
        config_root=config_root,
        actions=tuple(actions),
    )


def _plan_artifact(config_root: Path, artifact: RenderedAgentArtifact) -> _PlannedArtifactAction:
    path = resolve_artifact_path(config_root, artifact)
    rendered = _rendered_summary(artifact)
    if not path.exists():
        current = AgentArtifactSummary(exists=False, managed=False)
        action = _action(
            artifact,
            ARTIFACT_ACTION_CREATE,
            current,
            rendered,
            message="managed OpenCode artifact is missing",
        )
        return _PlannedArtifactAction(artifact, action, artifact.content)

    text = path.read_text(encoding="utf-8")
    current = _markdown_summary(text)
    if not current.managed:
        action = _action(
            artifact,
            ARTIFACT_ACTION_CONFLICT,
            current,
            rendered,
            message="existing OpenCode artifact is not BlackCell managed",
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
        message="managed OpenCode artifact differs from rendered content",
    )
    return _PlannedArtifactAction(artifact, action, artifact.content)


def _render_opencode_agent(agent: AgentDefinition) -> RenderedAgentArtifact:
    frontmatter: dict[str, Any] = {
        "description": agent.description,
        "mode": agent.mode,
        "permission": agent.permission,
    }
    if agent.model:
        frontmatter["model"] = agent.model
    if agent.temperature is not None:
        frontmatter["temperature"] = agent.temperature
    if agent.color:
        frontmatter["color"] = agent.color
    body = _markdown_body(frontmatter, agent.prompt)
    return _markdown_artifact(f"agents/{agent.key}.md", "opencode-agent", body, key=agent.key)


def _render_opencode_command(command: AgentCommand) -> RenderedAgentArtifact:
    frontmatter: dict[str, Any] = {
        "description": command.description,
        "agent": command.agent,
    }
    if command.subtask:
        frontmatter["subtask"] = command.subtask
    if command.model:
        frontmatter["model"] = command.model
    body = _markdown_body(frontmatter, command.template)
    return _markdown_artifact(
        f"commands/{command.key}.md", "opencode-command", body, key=command.key
    )


def _markdown_artifact(
    path: str,
    kind: str,
    body: str,
    *,
    key: str,
) -> RenderedAgentArtifact:
    normalized_body = _normalize_body(body)
    digest = sha256_digest(normalized_body)
    content = _insert_managed_marker(normalized_body, digest)
    return RenderedAgentArtifact(
        path=path,
        kind=kind,
        body=normalized_body,
        digest=digest,
        content=content,
        key=key,
    )


def _markdown_body(frontmatter: dict[str, Any], prompt: str) -> str:
    yaml_body = yaml.safe_dump(frontmatter, sort_keys=False, default_flow_style=False).strip()
    return f"---\n{yaml_body}\n---\n{prompt}"


def _insert_managed_marker(body: str, digest: str) -> str:
    if body.startswith("---\n"):
        _, frontmatter, rest = body.split("---\n", 2)
        return (
            f"---\n{frontmatter}---\n"
            f"{MARKDOWN_START_PREFIX}{digest} -->\n"
            f"{rest.rstrip()}\n"
            f"{MARKDOWN_END_MARKER}\n"
        )
    return f"{MARKDOWN_START_PREFIX}{digest} -->\n{body.rstrip()}\n{MARKDOWN_END_MARKER}\n"


def _markdown_summary(text: str) -> AgentArtifactSummary:
    marker_count = text.count(MARKDOWN_START_PREFIX)
    end_count = text.count(MARKDOWN_END_MARKER)
    match = _MARKDOWN_SECTION_RE.search(text)
    if marker_count != 1 or end_count != 1 or match is None:
        return AgentArtifactSummary(
            exists=True,
            managed=False,
            size_bytes=len(text.encode("utf-8")),
        )

    body = _strip_managed_markers(text)
    return AgentArtifactSummary(
        exists=True,
        managed=True,
        digest=match.group("digest"),
        body_digest=sha256_digest(body),
        size_bytes=len(text.encode("utf-8")),
    )


def _strip_managed_markers(text: str) -> str:
    lines = []
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\n")
        if stripped.startswith(MARKDOWN_START_PREFIX):
            continue
        if stripped == MARKDOWN_END_MARKER:
            continue
        lines.append(line)
    return _normalize_body("".join(lines))


def _rendered_summary(artifact: RenderedAgentArtifact) -> AgentArtifactSummary:
    return AgentArtifactSummary(
        exists=True,
        managed=True,
        digest=artifact.digest,
        body_digest=artifact.digest,
        size_bytes=len(artifact.content.encode("utf-8")),
    )


def _action(
    artifact: RenderedAgentArtifact,
    action: str,
    current: AgentArtifactSummary,
    rendered: AgentArtifactSummary,
    *,
    message: str = "",
) -> AgentArtifactAction:
    return AgentArtifactAction(
        path=artifact.path,
        action=action,
        digest=artifact.digest,
        current=current,
        rendered=rendered,
        message=message,
    )


def _render_raw_artifacts() -> tuple[RenderedAgentArtifact, ...]:
    return (
        *(_render_opencode_agent(agent) for agent in blackcell_agents()),
        *(_render_opencode_command(command) for command in blackcell_agent_commands()),
    )


def _with_display_path(
    artifact: RenderedAgentArtifact,
    scope: ConfigScope,
) -> RenderedAgentArtifact:
    if scope is ConfigScope.PROJECT:
        display_path = f".opencode/{artifact.path}"
    else:
        display_path = f"~/.config/opencode/{artifact.path}"
    return replace(artifact, path=display_path)


def _relative_artifact_path(path: str) -> Path:
    for prefix in (".opencode/", "~/.config/opencode/"):
        if path.startswith(prefix):
            return Path(path.removeprefix(prefix))
    return Path(path)


def _parse_scope(scope: ConfigScope | str) -> ConfigScope:
    if isinstance(scope, ConfigScope):
        return scope
    try:
        return ConfigScope(scope)
    except ValueError as error:
        raise ValueError("scope must be one of: project, global") from error


def _normalize_body(body: str) -> str:
    return body.rstrip() + "\n"


def _opencode_executable() -> str | None:
    executable = shutil.which("opencode")
    if executable:
        return executable
    local = Path.home() / ".opencode" / "bin" / "opencode"
    if local.exists():
        return str(local)
    return None


def _providers_list_check(executable: str) -> AgentDoctorCheck:
    try:
        result = subprocess.run(
            [executable, "providers", "list"],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
    except OSError as error:
        return AgentDoctorCheck("providers-list", False, str(error))
    except subprocess.TimeoutExpired:
        return AgentDoctorCheck("providers-list", False, "opencode providers list timed out")

    if result.returncode == 0:
        return AgentDoctorCheck("providers-list", True, "opencode providers list succeeded")
    message = (result.stderr or result.stdout or "opencode providers list failed").strip()
    return AgentDoctorCheck("providers-list", False, message)


def _debug_agent_checks(
    executable: str,
    agents: tuple[AgentDefinition, ...],
) -> tuple[AgentDoctorCheck, ...]:
    checks: list[AgentDoctorCheck] = []
    for agent in agents:
        try:
            result = subprocess.run(
                [executable, "debug", "agent", agent.key],
                capture_output=True,
                check=False,
                text=True,
                timeout=10,
            )
        except OSError as error:
            checks.append(AgentDoctorCheck(f"debug-agent:{agent.key}", False, str(error)))
            continue
        except subprocess.TimeoutExpired:
            checks.append(
                AgentDoctorCheck(f"debug-agent:{agent.key}", False, "opencode debug timed out")
            )
            continue

        checks.append(
            AgentDoctorCheck(
                key=f"debug-agent:{agent.key}",
                ok=result.returncode == 0,
                message="visible to opencode debug"
                if result.returncode == 0
                else (result.stderr or result.stdout or "not visible to opencode debug").strip(),
            )
        )
    return tuple(checks)
