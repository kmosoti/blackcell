from blackcell.nesy.models import Rule, RuleAtom, RuleSet, ValidationMessage, ValidationResult
from blackcell.world.models import WorldSnapshot


def build_default_rules(snapshot: WorldSnapshot) -> RuleSet:
    root = str(snapshot.repo_root)
    return RuleSet(
        rules=(
            Rule(
                key="rule:python-harness",
                head=RuleAtom("repo", "supports", "python-harness"),
                body=(
                    RuleAtom("repo", "has_path", "pyproject.toml"),
                    RuleAtom("repo", "has_path", "src"),
                ),
                rationale=f"{root} already has Python package structure.",
            ),
            Rule(
                key="rule:docs-boundary",
                head=RuleAtom("repo", "supports", "split-docs"),
                body=(
                    RuleAtom("repo", "has_path", "README.md"),
                    RuleAtom("repo", "has_path", "docs"),
                ),
                rationale="The entrypoint can stay concise while deeper docs move into docs/.",
            ),
        )
    )


def validate_ruleset(rule_set: RuleSet) -> ValidationResult:
    errors: list[ValidationMessage] = []
    seen_keys: set[str] = set()
    for index, rule in enumerate(rule_set.rules):
        path = f"$.rules[{index}]"
        if rule.key in seen_keys:
            errors.append(
                ValidationMessage(
                    level="error",
                    code="duplicate_rule_key",
                    message=f"duplicate rule key: {rule.key}",
                    path=f"{path}.key",
                )
            )
        seen_keys.add(rule.key)
        if not rule.body:
            errors.append(
                ValidationMessage(
                    level="error",
                    code="empty_rule_body",
                    message="rule body must contain at least one atom",
                    path=f"{path}.body",
                )
            )
        for atom_name, atom in (
            ("head", rule.head),
            *[(f"body[{i}]", item) for i, item in enumerate(rule.body)],
        ):
            if not atom.subject or not atom.predicate or not atom.object:
                errors.append(
                    ValidationMessage(
                        level="error",
                        code="invalid_atom",
                        message="rule atoms must contain non-empty subject, predicate, and object",
                        path=f"{path}.{atom_name}",
                    )
                )
    return ValidationResult(valid=not errors, errors=tuple(errors), warnings=())
