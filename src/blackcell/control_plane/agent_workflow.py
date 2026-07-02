import tomllib

from blackcell.control_plane.agent_rendering import (
    RenderedAgentWorkflowArtifact,
    render_codex_cli_artifacts,
)
from blackcell.control_plane.models import (
    PlanContract,
    ValidationLevel,
    ValidationMessage,
    ValidationResult,
)

EXPECTED_CODEX_AGENTS = frozenset({"spark-evidence-drafter", "quality-reviewer"})
BANNED_GUIDANCE = (
    "ruff check --fix-only",
    "ruff check --fix",
    "git commit",
    "git push",
    "gh pr merge",
    "gh issue close",
    "blackcell control-plane sync --apply",
    "blackcell control-plane pr sync --apply",
    "blackcell control-plane pr ready --apply",
)


def validate_agent_workflow(
    contract: PlanContract,
    *,
    artifacts: tuple[RenderedAgentWorkflowArtifact, ...] | None = None,
) -> ValidationResult:
    messages: list[ValidationMessage] = []

    if contract.agent_workflow is None:
        messages.append(
            _error(
                "missing_agent_workflow",
                "agent_workflow is required for Codex CLI projection",
                "$.agent_workflow",
            )
        )
        return ValidationResult.from_messages(messages)

    rendered_artifacts = artifacts or render_codex_cli_artifacts(contract)
    artifacts_by_path = {artifact.path: artifact for artifact in rendered_artifacts}

    _validate_config(artifacts_by_path.get(".codex/config.toml"), messages)
    _validate_agents(rendered_artifacts, messages)

    return ValidationResult.from_messages(messages)


def _validate_config(
    artifact: RenderedAgentWorkflowArtifact | None,
    messages: list[ValidationMessage],
) -> None:
    if artifact is None:
        messages.append(
            _error(
                "missing_codex_config",
                "rendered Codex CLI config is missing",
                "$.rendered.codex_cli.config",
            )
        )
        return

    data = _load_toml(artifact, "$.rendered.codex_cli.config", messages)
    if data is None:
        return

    agents = data.get("agents")
    if not isinstance(agents, dict):
        messages.append(
            _error(
                "invalid_codex_config",
                "rendered Codex CLI config must contain an [agents] table",
                "$.rendered.codex_cli.config.agents",
            )
        )
        return

    max_depth = agents.get("max_depth")
    if not isinstance(max_depth, int):
        messages.append(
            _error(
                "invalid_codex_max_depth",
                "rendered Codex CLI config agents.max_depth must be an integer",
                "$.rendered.codex_cli.config.agents.max_depth",
            )
        )
    elif max_depth > 1:
        messages.append(
            _error(
                "invalid_codex_max_depth",
                "rendered Codex CLI config must not allow delegation depth greater than 1",
                "$.rendered.codex_cli.config.agents.max_depth",
            )
        )


def _validate_agents(
    artifacts: tuple[RenderedAgentWorkflowArtifact, ...],
    messages: list[ValidationMessage],
) -> None:
    read_only_agents: set[str] = set()
    agent_artifacts = tuple(
        artifact
        for artifact in artifacts
        if artifact.kind == "toml" and artifact.path.startswith(".codex/agents/")
    )

    if len(agent_artifacts) < len(EXPECTED_CODEX_AGENTS):
        messages.append(
            _error(
                "missing_codex_agent",
                "rendered Codex CLI projection must include at least two agents",
                "$.rendered.codex_cli.agents",
            )
        )

    for artifact in agent_artifacts:
        path = f"$.rendered.codex_cli.agents.{artifact.path}"
        data = _load_toml(artifact, path, messages)
        if data is None:
            continue

        for field in ("name", "description", "developer_instructions", "sandbox_mode"):
            if not isinstance(data.get(field), str) or not data[field]:
                messages.append(
                    _error(
                        "invalid_codex_agent",
                        f"rendered Codex CLI agent must contain non-empty {field}",
                        f"{path}.{field}",
                    )
                )

        name = data.get("name")
        sandbox_mode = data.get("sandbox_mode")
        if isinstance(name, str) and sandbox_mode == "read-only":
            read_only_agents.add(name)
        elif isinstance(name, str):
            messages.append(
                _error(
                    "codex_agent_not_read_only",
                    f"rendered Codex CLI agent {name} must use read-only sandbox mode",
                    f"{path}.sandbox_mode",
                )
            )

        developer_instructions = data.get("developer_instructions")
        if isinstance(developer_instructions, str):
            _validate_guidance(developer_instructions, f"{path}.developer_instructions", messages)

    missing_read_only = EXPECTED_CODEX_AGENTS - read_only_agents
    for agent_name in sorted(missing_read_only):
        messages.append(
            _error(
                "missing_read_only_codex_agent",
                f"rendered Codex CLI agent {agent_name} must exist and be read-only",
                "$.rendered.codex_cli.agents",
            )
        )


def _validate_guidance(
    value: str,
    path: str,
    messages: list[ValidationMessage],
) -> None:
    lowered = value.lower()
    for banned in BANNED_GUIDANCE:
        if banned in lowered:
            messages.append(
                _error(
                    "mutating_agent_guidance",
                    f"rendered agent guidance must not contain `{banned}`",
                    path,
                )
            )

    for line in lowered.splitlines():
        if "ruff format" in line and "--check" not in line:
            messages.append(
                _error(
                    "mutating_agent_guidance",
                    "rendered agent guidance must not contain `ruff format` without `--check`",
                    path,
                )
            )


def _load_toml(
    artifact: RenderedAgentWorkflowArtifact,
    path: str,
    messages: list[ValidationMessage],
) -> dict[str, object] | None:
    try:
        return tomllib.loads(artifact.content)
    except tomllib.TOMLDecodeError as error:
        messages.append(
            _error(
                "invalid_rendered_toml",
                f"rendered TOML artifact is invalid: {error}",
                path,
            )
        )
        return None


def _error(code: str, message: str, path: str) -> ValidationMessage:
    return ValidationMessage(
        level=ValidationLevel.ERROR,
        code=code,
        message=message,
        path=path,
    )
