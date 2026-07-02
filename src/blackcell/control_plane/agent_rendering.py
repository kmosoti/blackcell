import hashlib
from dataclasses import dataclass

from blackcell.control_plane.models import AgentWorkflow, CodexCliAgent, PlanContract

CODEX_CLI_TARGET = "codex-cli"
TOML_MANAGED_MARKER = "# BlackCell managed: codex-cli agent workflow"
TOML_DIGEST_PREFIX = "# blackcell:digest "
MARKDOWN_START_PREFIX = "<!-- blackcell:agent-workflow:start digest="
MARKDOWN_END_MARKER = "<!-- blackcell:agent-workflow:end -->"


@dataclass(frozen=True, slots=True)
class RenderedCodexAgent:
    key: str
    name: str
    description: str
    developer_instructions: str
    sandbox_mode: str = "read-only"


@dataclass(frozen=True, slots=True)
class RenderedAgentWorkflowArtifact:
    path: str
    kind: str
    body: str
    digest: str
    content: str
    agent_key: str | None = None


def codex_cli_agents(workflow: AgentWorkflow | None = None) -> tuple[RenderedCodexAgent, ...]:
    if workflow and workflow.codex_cli and workflow.codex_cli.agents:
        return tuple(_rendered_codex_agent(agent) for agent in workflow.codex_cli.agents)

    return (
        RenderedCodexAgent(
            key="spark-evidence-drafter",
            name="spark-evidence-drafter",
            description=(
                "Drafts evidence summaries from repository context without approving behavior."
            ),
            developer_instructions=(
                "You are the BlackCell Spark evidence drafter for this repository.\n"
                "Operate in read-only mode. Inspect repository-authored planning context and "
                "summarize evidence only.\n"
                "Do not approve behavior, draft fixes, edit files, run mutating commands, or "
                "request remote state changes.\n"
                "Return concise notes that separate observed facts from open questions.\n"
            ),
        ),
        RenderedCodexAgent(
            key="quality-reviewer",
            name="quality-reviewer",
            description="Reviews repository changes for contract, test, and documentation risks.",
            developer_instructions=(
                "You are the BlackCell quality reviewer for repository changes.\n"
                "Operate in read-only review mode. Inspect diffs, tests, docs, and contract "
                "context.\n"
                "Report defects, missing coverage, and contract risks. Do not enter fix mode, "
                "edit files, commit changes, push branches, merge pull requests, close issues, "
                "or run remote-mutating workflows.\n"
                "When suggesting verification, use non-mutating check commands only.\n"
            ),
        ),
    )


def render_codex_cli_artifacts(contract: PlanContract) -> tuple[RenderedAgentWorkflowArtifact, ...]:
    if contract.agent_workflow is None:
        raise ValueError("agent_workflow is required to render Codex CLI artifacts")

    workflow = contract.agent_workflow
    codex_cli = workflow.codex_cli
    agents = codex_cli_agents(workflow)
    return (
        render_codex_cli_config(
            max_threads=codex_cli.max_threads if codex_cli else 6,
            max_depth=codex_cli.max_depth if codex_cli else 1,
        ),
        *(
            render_codex_agent_toml(agent, path=f".codex/agents/{agent.key}.toml")
            for agent in agents
        ),
        render_agents_markdown(workflow, agents),
        render_code_review_markdown(workflow),
    )


def render_codex_cli_config(
    *,
    max_threads: int = 6,
    max_depth: int = 1,
) -> RenderedAgentWorkflowArtifact:
    body = (
        "# Generated from blackcell.plan.yaml; edit the contract and reinstall.\n"
        "[agents]\n"
        f"max_threads = {max_threads}\n"
        f"max_depth = {max_depth}\n"
    )
    return _toml_artifact(".codex/config.toml", body)


def render_codex_agent_toml(
    agent: RenderedCodexAgent,
    *,
    path: str,
) -> RenderedAgentWorkflowArtifact:
    body = (
        f"name = {_toml_string(agent.name)}\n"
        f"description = {_toml_string(agent.description)}\n"
        f"developer_instructions = {_toml_string(agent.developer_instructions)}\n"
        f"sandbox_mode = {_toml_string(agent.sandbox_mode)}\n"
    )
    return _toml_artifact(path, body, agent_key=agent.key)


def render_agents_markdown(
    workflow: AgentWorkflow,
    agents: tuple[RenderedCodexAgent, ...] | None = None,
) -> RenderedAgentWorkflowArtifact:
    rendered_agents = agents or codex_cli_agents()
    codex_cli = workflow.codex_cli
    max_threads = codex_cli.max_threads if codex_cli else 6
    max_depth = codex_cli.max_depth if codex_cli else 1
    lines = [
        "# BlackCell Agent Workflow",
        "",
        "This managed section is rendered from `blackcell.plan.yaml` for Codex CLI project "
        "configuration.",
        "",
        f"- Workflow model: `{workflow.model}`",
        f"- Max worker threads: `{max_threads}`",
        f"- Max delegation depth: `{max_depth}`",
        f"- Managed agents: {', '.join(agent.key for agent in rendered_agents)}",
        "",
        "## Repo-authored Workers",
        "",
    ]
    if workflow.workers:
        for worker in workflow.workers:
            lines.append(f"- `{worker.key}`: {worker.name}")
            if worker.owns:
                lines.append(f"  - Owns: {', '.join(f'`{item}`' for item in worker.owns)}")
            if worker.change_spec:
                lines.append(f"  - Change spec: {'; '.join(worker.change_spec)}")
    else:
        lines.append("- None configured.")

    lines.extend(
        [
            "",
            "## Codex CLI Projection",
            "",
            "- `.codex/config.toml` constrains agent fan-out.",
            "- `.codex/agents/spark-evidence-drafter.toml` is evidence-only and read-only.",
            "- `.codex/agents/quality-reviewer.toml` is review-only and read-only.",
            "",
            "## Managed Codex Agents",
            "",
        ]
    )
    for agent in rendered_agents:
        guidance = " ".join(agent.developer_instructions.split())
        lines.extend(
            [
                f"- `{agent.key}`",
                f"  - Name: `{agent.name}`",
                f"  - Description: {agent.description}",
                f"  - Sandbox mode: `{agent.sandbox_mode}`",
                f"  - Developer instructions: {guidance}",
            ]
        )
    lines.append("")
    return _markdown_artifact("AGENTS.md", "\n".join(lines))


def render_code_review_markdown(workflow: AgentWorkflow) -> RenderedAgentWorkflowArtifact:
    lines = [
        "# BlackCell Code Review",
        "",
        "This managed section describes the review posture projected from `blackcell.plan.yaml`.",
        "",
        f"- Workflow model: `{workflow.model}`",
        "- Reviews are read-only.",
        "- Findings should distinguish observed defects from open questions.",
        "- Verification suggestions must stay non-mutating.",
        "- Remote state changes remain outside the managed review role.",
        "",
    ]
    return _markdown_artifact("docs/agent/code_review.md", "\n".join(lines))


def render_markdown_section(body: str) -> tuple[str, str]:
    normalized_body = _normalize_body(body)
    digest = sha256_digest(normalized_body)
    content = f"{MARKDOWN_START_PREFIX}{digest} -->\n{normalized_body}{MARKDOWN_END_MARKER}\n"
    return content, digest


def sha256_digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _toml_artifact(
    path: str,
    body: str,
    *,
    agent_key: str | None = None,
) -> RenderedAgentWorkflowArtifact:
    normalized_body = _normalize_body(body)
    digest = sha256_digest(normalized_body)
    content = f"{TOML_MANAGED_MARKER}\n{TOML_DIGEST_PREFIX}{digest}\n{normalized_body}"
    return RenderedAgentWorkflowArtifact(
        path=path,
        kind="toml",
        body=normalized_body,
        digest=digest,
        content=content,
        agent_key=agent_key,
    )


def _rendered_codex_agent(agent: CodexCliAgent) -> RenderedCodexAgent:
    return RenderedCodexAgent(
        key=agent.key,
        name=agent.name,
        description=agent.description,
        developer_instructions=agent.developer_instructions,
        sandbox_mode=agent.sandbox_mode,
    )


def _markdown_artifact(path: str, body: str) -> RenderedAgentWorkflowArtifact:
    normalized_body = _normalize_body(body)
    content, digest = render_markdown_section(normalized_body)
    return RenderedAgentWorkflowArtifact(
        path=path,
        kind="markdown",
        body=normalized_body,
        digest=digest,
        content=content,
    )


def _normalize_body(body: str) -> str:
    return body.rstrip() + "\n"


def _toml_string(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\b", "\\b")
        .replace("\t", "\\t")
        .replace("\n", "\\n")
        .replace("\f", "\\f")
        .replace("\r", "\\r")
    )
    return f'"{escaped}"'
