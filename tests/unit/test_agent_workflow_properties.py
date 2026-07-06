import tempfile
import tomllib
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from blackcell.control_plane import LocalControlPlane
from blackcell.control_plane.agent_rendering import (
    MARKDOWN_END_MARKER,
    MARKDOWN_START_PREFIX,
    RenderedCodexAgent,
    render_codex_agent_toml,
    render_markdown_section,
    sha256_digest,
)


def _toml_safe_text() -> st.SearchStrategy[str]:
    alphabet = st.one_of(
        st.characters(blacklist_categories=("Cc", "Cs")),
        st.sampled_from(("\b", "\t", "\n", "\f", "\r")),
    )
    return st.text(alphabet=alphabet, min_size=1, max_size=60)


@given(
    name=_toml_safe_text(),
    description=_toml_safe_text(),
    instructions=_toml_safe_text(),
    sandbox_mode=_toml_safe_text(),
)
@settings(max_examples=50)
def test_rendered_agent_toml_round_trips_parseable_fields(
    name: str,
    description: str,
    instructions: str,
    sandbox_mode: str,
) -> None:
    agent = RenderedCodexAgent(
        key="generated",
        name=name,
        description=description,
        developer_instructions=instructions,
        sandbox_mode=sandbox_mode,
    )

    artifact = render_codex_agent_toml(agent, path=".codex/agents/generated.toml")
    data = tomllib.loads(artifact.content)

    assert data["name"] == name
    assert data["description"] == description
    assert data["developer_instructions"] == instructions
    assert data["sandbox_mode"] == sandbox_mode


@given(body=st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=120))
@settings(max_examples=50)
def test_rendered_markdown_section_digest_normalizes_body(body: str) -> None:
    content, digest = render_markdown_section(body)
    normalized_body = body.rstrip() + "\n"

    assert digest == sha256_digest(normalized_body)
    expected = f"{MARKDOWN_START_PREFIX}{digest} -->\n{normalized_body}{MARKDOWN_END_MARKER}\n"
    assert content == expected


@given(
    prefix=st.text(
        alphabet=st.characters(blacklist_categories=("Cc", "Cs"), blacklist_characters="<"),
        max_size=50,
    ),
    suffix=st.text(
        alphabet=st.characters(blacklist_categories=("Cc", "Cs"), blacklist_characters="<"),
        max_size=50,
    ),
)
@settings(max_examples=25, deadline=None)
def test_agent_workflow_markdown_replacement_preserves_unmanaged_text(
    prefix: str,
    suffix: str,
) -> None:
    prefix_text = f"{prefix}\n" if prefix else ""
    suffix_text = f"\n{suffix}" if suffix else ""
    stale_section, _ = render_markdown_section("Stale managed content\n")

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _write_contract(root)
        agents_path = root / "AGENTS.md"
        agents_path.write_text(prefix_text + stale_section + suffix_text, encoding="utf-8")

        result = LocalControlPlane(start=root).agent_workflow_install(
            "codex-cli",
            apply_changes=True,
        )
        text = agents_path.read_text(encoding="utf-8")

    action_by_path = {action.path: action for action in result.actions}
    assert action_by_path["AGENTS.md"].action == "update"
    assert action_by_path["AGENTS.md"].applied is True
    assert action_by_path["AGENTS.md"].current.exists is True
    assert action_by_path["AGENTS.md"].current.managed is True
    assert text.startswith(prefix_text)
    assert text.endswith(suffix_text)
    assert "# BlackCell Agent Workflow" in text
    assert text.count(MARKDOWN_START_PREFIX) == 1


def _write_contract(path: Path) -> None:
    (path / ".git").mkdir()
    (path / "blackcell.plan.yaml").write_text(_contract_yaml(), encoding="utf-8")


def _contract_yaml() -> str:
    return """
version: 1
project:
  key: BCP
  name: BlackCell
issues:
  - key: BCP-0008
    title: Render Codex CLI agent workflow artifacts
    type: feature
    status: Todo
    priority: P0
    complexity: 5
agent_workflow:
  model: gpt-5.3-codex-spark
  workers:
    - key: agent-workflow
      name: Agent workflow worker
"""
