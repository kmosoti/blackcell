from pathlib import Path

import yaml

DOCS_ROOT = Path("docs")


def test_docs_graph_entrypoints_exist() -> None:
    expected = {
        "docs/index.md",
        "docs/atlas/graph.md",
        "docs/atlas/glossary.md",
        "docs/charter.md",
        "docs/architecture.md",
        "docs/scientific-basis.md",
        "docs/evaluation-methodology.md",
        "docs/adr/0001-event-sourced-kernel.md",
        "docs/adr/0002-domain-scoped-state.md",
        "docs/adr/0003-model-execution-boundary.md",
        "docs/adr/0004-evolutionary-runtime-architecture.md",
        "docs/spec/index.md",
        "docs/spec/bcp-0028-charter-reset.md",
        "docs/spec/bcp-0029-event-kernel.md",
        "docs/spec/bcp-0030-repository-state.md",
        "docs/spec/bcp-0031-context-and-control.md",
        "docs/spec/bcp-0032-repository-operator.md",
        "docs/spec/bcp-0033-operator-bench.md",
        "docs/spec/bcp-0034-evolutionary-runtime.md",
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


def test_docs_graph_map_links_canonical_nodes() -> None:
    text = Path("docs/atlas/graph.md").read_text(encoding="utf-8")

    assert "Charter" in text
    assert "Runtime Architecture" in text
    assert "Scientific Basis" in text
    assert "OperatorBench" in text


def _frontmatter(path: Path) -> dict[str, object]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"missing frontmatter: {path}"
    _, frontmatter, _ = text.split("---\n", 2)
    metadata = yaml.safe_load(frontmatter)
    assert isinstance(metadata, dict)
    return metadata
