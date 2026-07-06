from typing import Any

from blackcell.agents.models import AgentCommand, AgentDefinition, AgentSummary


def blackcell_agents() -> tuple[AgentDefinition, ...]:
    return (
        AgentDefinition(
            key="blackcell-astrophage",
            mode="primary",
            description=(
                "Primary BlackCell planner that turns world state into constrained work packets."
            ),
            color="primary",
            permission={
                "edit": "ask",
                "bash": "ask",
                "task": {"*": "deny", "blackcell-*": "ask"},
                "external_directory": "deny",
            },
            prompt=(
                "You are blackcell-astrophage, the BlackCell harness planner.\n"
                "Build plans from repository evidence, typed world facts, NeSy constraints, and "
                "runtime capability reports.\n"
                "Prefer small, reversible work packets with explicit verification. Treat runtimes "
                "as adapters, not as the product identity.\n"
                "Ask before destructive or broad changes. Delegate observation, graph curation, "
                "constraint review, and quality review to the BlackCell subagents when useful.\n"
            ),
        ),
        AgentDefinition(
            key="blackcell-mycelium",
            mode="subagent",
            description="Maintains the BlackCell docs graph and cross-links project knowledge.",
            color="success",
            permission={
                "edit": "ask",
                "bash": "ask",
                "external_directory": "deny",
            },
            prompt=(
                "You are blackcell-mycelium, the BlackCell knowledge graph curator.\n"
                "Organize documentation as linked nodes with explicit graph metadata, concise "
                "edges, and clear entry points.\n"
                "Preserve project voice. Prefer moving detail into categorized docs over expanding "
                "the root README.\n"
            ),
        ),
        AgentDefinition(
            key="blackcell-spore",
            mode="subagent",
            description="Read-only repository observer and typed fact extractor.",
            color="info",
            permission={
                "edit": "deny",
                "bash": {
                    "*": "ask",
                    "git status*": "allow",
                    "git diff*": "allow",
                    "git log*": "allow",
                    "uv run blackcell world*": "allow",
                    "uv run blackcell nesy validate*": "allow",
                },
                "external_directory": "deny",
            },
            prompt=(
                "You are blackcell-spore, the BlackCell observer.\n"
                "Operate read-only. Inspect repository state, summarize observed facts, and "
                "separate evidence from open questions.\n"
                "Do not edit files, draft fixes as final answers, or request remote state "
                "changes.\n"
            ),
        ),
        AgentDefinition(
            key="blackcell-lumen",
            mode="subagent",
            description="Reviews NeSy rules, contracts, and constraints for inconsistency.",
            color="warning",
            permission={
                "edit": "deny",
                "bash": {
                    "*": "ask",
                    "uv run blackcell nesy validate*": "allow",
                    "uv run blackcell harness plan*": "allow",
                },
                "external_directory": "deny",
            },
            prompt=(
                "You are blackcell-lumen, the BlackCell constraint reviewer.\n"
                "Review rules, schemas, contracts, invariants, and generated artifacts for "
                "logical gaps or contradictions.\n"
                "Report defects and missing coverage before summaries. Stay read-only.\n"
            ),
        ),
        AgentDefinition(
            key="blackcell-umbra",
            mode="subagent",
            description="Quality and security reviewer for repository changes.",
            color="error",
            permission={
                "edit": "deny",
                "bash": {
                    "*": "ask",
                    "git status*": "allow",
                    "git diff*": "allow",
                    "uv run ruff check*": "allow",
                    "uv run pytest*": "allow",
                    "uv run ty check*": "allow",
                },
                "external_directory": "deny",
            },
            prompt=(
                "You are blackcell-umbra, the BlackCell quality and security reviewer.\n"
                "Prioritize behavioral bugs, security risks, contract drift, regression risk, and "
                "missing tests.\n"
                "Findings come first, ordered by severity with file references. Stay read-only and "
                "do not enter fix mode.\n"
            ),
        ),
        AgentDefinition(
            key="blackcell-chimera",
            mode="subagent",
            description="Guarded executor for explicit write-capable implementation tasks.",
            color="secondary",
            permission={
                "edit": "ask",
                "bash": "ask",
                "external_directory": "deny",
            },
            prompt=(
                "You are blackcell-chimera, the guarded BlackCell executor.\n"
                "Only modify files when the requested implementation scope is explicit. Keep "
                "changes minimal, preserve user work, and verify before reporting completion.\n"
                "Never perform destructive git operations or remote state changes without direct "
                "user approval.\n"
            ),
        ),
    )


def blackcell_agent_commands() -> tuple[AgentCommand, ...]:
    return (
        AgentCommand(
            key="blackcell-observe",
            description="Observe the repo and report typed BlackCell facts.",
            agent="blackcell-spore",
            subtask=True,
            template=(
                "Observe this repository using BlackCell world-model commands when available.\n"
                "Run `uv run blackcell world observe`, `uv run blackcell world facts`, and "
                "`uv run blackcell nesy validate` if they are safe in this workspace.\n"
                "Return observed facts, surprises, and open questions."
            ),
        ),
        AgentCommand(
            key="blackcell-plan",
            description="Plan a constrained BlackCell work packet.",
            agent="blackcell-astrophage",
            template=(
                "Use the current repository evidence and BlackCell harness concepts to plan the "
                "next work packet.\n"
                "Incorporate any arguments: $ARGUMENTS\n"
                "Return a concise plan with verification and risk notes."
            ),
        ),
        AgentCommand(
            key="blackcell-review",
            description="Review changes through the BlackCell quality posture.",
            agent="blackcell-umbra",
            subtask=True,
            template=(
                "Review the current repository changes. Prioritize defects, regression risks, "
                "contract drift, and missing tests.\n"
                "Use non-mutating checks only. Findings first, then residual risks."
            ),
        ),
        AgentCommand(
            key="blackcell-graph",
            description="Curate or inspect the BlackCell documentation graph.",
            agent="blackcell-mycelium",
            subtask=True,
            template=(
                "Inspect the docs graph and improve or report on its node metadata, edges, and "
                "entry points.\n"
                "Respect arguments: $ARGUMENTS"
            ),
        ),
    )


def list_agent_summaries() -> tuple[AgentSummary, ...]:
    return tuple(_summary(agent) for agent in blackcell_agents())


def _summary(agent: AgentDefinition) -> AgentSummary:
    return AgentSummary(
        key=agent.key,
        mode=agent.mode,
        description=agent.description,
        writes=_write_posture(agent.permission),
    )


def _write_posture(permission: dict[str, Any]) -> str:
    edit_permission = permission.get("edit")
    if edit_permission == "deny":
        return "deny"
    if edit_permission == "ask":
        return "ask"
    if edit_permission == "allow":
        return "allow"
    return "unspecified"
