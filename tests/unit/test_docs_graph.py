import re
from pathlib import Path
from typing import Any, cast

import yaml

DOCS_ROOT = Path("docs")
ROOT = Path(__file__).parents[2]
README_PATH = ROOT / "README.md"


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
        "docs/adr/0005-durable-run-and-execution-protocol.md",
        "docs/adr/0006-versioned-run-feedback-protocol.md",
        "docs/adr/0007-runtime-security-boundary.md",
        "docs/adr/0008-architecture-consolidation.md",
        "docs/spec/index.md",
        "docs/spec/bcp-0028-charter-reset.md",
        "docs/spec/bcp-0029-event-kernel.md",
        "docs/spec/bcp-0030-repository-state.md",
        "docs/spec/bcp-0031-context-and-control.md",
        "docs/spec/bcp-0032-repository-operator.md",
        "docs/spec/bcp-0033-operator-bench.md",
        "docs/spec/bcp-0034-evolutionary-runtime.md",
        "docs/guides/runtime-v1-release.md",
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


def test_decision_atlas_records_every_adr_node() -> None:
    decision_log = _frontmatter(Path("docs/atlas/decisions.md"))
    edges = cast("dict[str, Any]", decision_log["edges"])
    records = cast("list[str]", edges["records"])
    adr_nodes = {
        cast("str", _frontmatter(path)["node"]) for path in (ROOT / "docs/adr").glob("*.md")
    }

    assert adr_nodes <= set(records)


def test_readme_local_links_and_recorded_quickstart_are_maintained() -> None:
    text = README_PATH.read_text(encoding="utf-8")
    local_targets = {
        target.split("#", maxsplit=1)[0]
        for target in re.findall(r"(?<!!)\[[^]]+\]\(([^)]+)\)", text)
        if target and not target.startswith(("https://", "http://", "mailto:", "#"))
    }

    assert local_targets
    for target in sorted(local_targets):
        path = (ROOT / target).resolve()
        assert path.is_relative_to(ROOT.resolve()), f"README link escapes repository: {target}"
        assert path.exists(), f"missing README link target: {target}"

    assert "uv sync --locked --all-groups" in text
    assert "bash examples/runtime-v1/recorded-operator.sh" in text
    assert '"schema_version": "runtime-v1-recorded-example/v1"' in text


def _frontmatter(path: Path) -> dict[str, object]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"missing frontmatter: {path}"
    _, frontmatter, _ = text.split("---\n", 2)
    metadata = yaml.safe_load(frontmatter)
    assert isinstance(metadata, dict)
    return metadata
