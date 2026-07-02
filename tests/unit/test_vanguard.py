from dataclasses import replace

from blackcell.vanguard import (
    DEFAULT_QA_COMMANDS,
    ChangeSpec,
    ExecutorScope,
    VerificationPlan,
    changespec_from_mapping,
    changespec_to_mapping,
    plan_qa,
    validate_changespec,
    validate_changespec_mapping,
)


def test_valid_changespec_parses_and_validates() -> None:
    spec = changespec_from_mapping(changespec_to_mapping(_valid_spec()))
    result = validate_changespec(spec)

    assert result.valid
    assert spec.verification.required == DEFAULT_QA_COMMANDS


def test_validate_changespec_rejects_missing_intent() -> None:
    result = validate_changespec(replace(_valid_spec(), intent=""))

    assert not result.valid
    assert result.errors[0].code == "missing_intent"


def test_validate_changespec_rejects_empty_acceptance_criteria() -> None:
    result = validate_changespec(replace(_valid_spec(), acceptance_criteria=()))

    assert not result.valid
    assert result.errors[0].code == "empty_acceptance_criteria"


def test_validate_changespec_rejects_empty_executor_allowed_files() -> None:
    spec = replace(_valid_spec(), executor_scope=ExecutorScope(allowed_files=()))
    result = validate_changespec(spec)

    assert not result.valid
    assert result.errors[0].code == "empty_allowed_files"


def test_validate_changespec_rejects_fix_mode_verification_command() -> None:
    spec = replace(
        _valid_spec(),
        verification=VerificationPlan(required=("uv run ruff check --fix .",)),
    )
    result = validate_changespec(spec)

    assert not result.valid
    assert result.errors[0].code == "mutating_verification_command"


def test_validate_changespec_mapping_requires_explicit_verification_strings() -> None:
    mapping = changespec_to_mapping(_valid_spec())
    mapping["verification"]["required"] = [{"command": "uv run pytest"}]

    result = validate_changespec_mapping(mapping)

    assert not result.valid
    assert result.errors[0].code == "invalid_verification_command"


def test_candidate_invariants_are_preserved_separately_from_behavior_contract() -> None:
    mapping = changespec_to_mapping(_valid_spec())
    mapping["candidate_invariants"] = ["observed source behavior"]
    mapping["behavior_contract"] = ["intended reviewed behavior"]

    spec = changespec_from_mapping(mapping)

    assert spec.candidate_invariants == ("observed source behavior",)
    assert spec.behavior_contract == ("intended reviewed behavior",)


def test_qa_plan_uses_deterministic_non_mutating_commands() -> None:
    plan = plan_qa(_valid_spec())

    assert [command.command for command in plan.commands] == list(DEFAULT_QA_COMMANDS)
    assert [command.name for command in plan.commands] == [
        "required-1",
        "required-2",
        "required-3",
        "required-4",
    ]
    assert all(not command.mutating for command in plan.commands)


def _valid_spec() -> ChangeSpec:
    return ChangeSpec(
        change_id="BCP-0006",
        issue_key="BCP-0006",
        intent="Add Vanguard CLI scope",
        non_goals=("remote mutation",),
        candidate_invariants=("control-plane owns GitHub projection",),
        behavior_contract=("Vanguard emits ChangeSpecs",),
        preserved_contracts=("control-plane sync remains unchanged",),
        acceptance_criteria=("commands emit JSON",),
        verification=VerificationPlan(required=DEFAULT_QA_COMMANDS),
        executor_scope=ExecutorScope(
            allowed_files=("src/blackcell/vanguard/", "tests/unit/test_vanguard.py"),
            forbidden=("GitHub mutations",),
        ),
        escalation_rules=("Ask before expanding scope",),
    )
