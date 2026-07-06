from pathlib import Path

from blackcell.nesy import Rule, RuleAtom, RuleSet, build_default_rules, validate_ruleset
from blackcell.world import observe_repo


def test_default_rules_validate_against_observed_repo(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / "README.md").write_text("# Test\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")

    result = validate_ruleset(build_default_rules(observe_repo(tmp_path)))

    assert result.valid is True
    assert result.errors == ()


def test_validate_ruleset_rejects_duplicate_keys() -> None:
    rule = Rule(
        key="rule:dup",
        head=RuleAtom("repo", "supports", "harness"),
        body=(RuleAtom("repo", "has_path", "src"),),
        rationale="duplicate",
    )

    result = validate_ruleset(RuleSet(rules=(rule, rule)))

    assert result.valid is False
    assert result.errors[0].code == "duplicate_rule_key"
