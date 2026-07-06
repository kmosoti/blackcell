from pathlib import Path

import yaml

DOCS_ROOT = Path("docs")


def test_docs_graph_entrypoints_exist() -> None:
    expected = {
        "docs/index.md",
        "docs/atlas/graph.md",
        "docs/atlas/glossary.md",
        "docs/concepts/world-model.md",
        "docs/concepts/nesy.md",
        "docs/concepts/harness.md",
        "docs/concepts/custom-agents.md",
        "docs/concepts/agent-operating-model.md",
        "docs/targets/opencode.md",
        "docs/targets/containers.md",
    }

    assert all(Path(path).exists() for path in expected)


def test_docs_graph_nodes_have_frontmatter() -> None:
    docs = list(DOCS_ROOT.rglob("*.md"))

    assert docs
    for path in docs:
        metadata = _frontmatter(path)
        assert metadata["node"]
        assert metadata["kind"]
        assert isinstance(metadata.get("edges", {}), dict)


def test_docs_graph_map_links_core_nodes() -> None:
    text = Path("docs/atlas/graph.md").read_text(encoding="utf-8")

    assert "World Model" in text
    assert "NeSy Rules" in text
    assert "OpenCode Target" in text
    assert "Container Runtime" in text


def _frontmatter(path: Path) -> dict[str, object]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"missing frontmatter: {path}"
    _, frontmatter, _ = text.split("---\n", 2)
    metadata = yaml.safe_load(frontmatter)
    assert isinstance(metadata, dict)
    return metadata
