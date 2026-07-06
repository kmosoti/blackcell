from pathlib import Path

from blackcell.world import observe_repo


def test_observe_repo_builds_snapshot_from_repo_layout(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / "README.md").write_text("# Test\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")

    snapshot = observe_repo(tmp_path)

    assert snapshot.repo_root == tmp_path
    assert any(
        fact.predicate == "has_path" and fact.object == "README.md" for fact in snapshot.facts
    )
    assert any(belief.key == "belief:python-package" for belief in snapshot.beliefs)
    assert any(
        expectation.key == "expectation:docs-boundary" for expectation in snapshot.expectations
    )
    assert any(
        fact.predicate == "has_adapter" and fact.object == "dry-run" for fact in snapshot.facts
    )
    assert any(observation.key == "runtime:dry-run" for observation in snapshot.observations)
