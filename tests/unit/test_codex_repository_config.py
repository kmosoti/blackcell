from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tomllib
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CODEX_ROOT = ROOT / ".codex"
ORCHESTRATION_ROOT = CODEX_ROOT / "orchestration"
VALIDATOR = ORCHESTRATION_ROOT / "validate_contract.py"
RULES = CODEX_ROOT / "rules/blackcell.rules"
CODEX = shutil.which("codex")
SKILLS_ROOT = ROOT / ".agents/skills"


def test_project_config_and_named_agents_are_explicit() -> None:
    config = tomllib.loads((CODEX_ROOT / "config.toml").read_text(encoding="utf-8"))

    assert config["model"] == "gpt-5.6-terra"
    assert config["model_reasoning_effort"] == "high"
    assert config["plan_mode_reasoning_effort"] == "high"
    assert config["model_verbosity"] == "low"
    assert config["approval_policy"] == "on-request"
    assert config["approvals_reviewer"] == "user"
    assert config["agents"] == {
        "max_threads": 9,
        "max_depth": 1,
        "interrupt_message": False,
    }
    assert not {
        "model_provider",
        "profile",
        "sandbox_mode",
        "service_tier",
        "web_search",
    } & set(config)

    expected = {
        "k_spark_worker": ("gpt-5.3-codex-spark", "medium", "workspace-write"),
        "k_pr_explorer": ("gpt-5.6-terra", "medium", "read-only"),
        "k_reviewer": ("gpt-5.6-sol", "high", "read-only"),
        "k_verifier": ("gpt-5.6-terra", "medium", "workspace-write"),
    }
    agent_paths = sorted((CODEX_ROOT / "agents").glob("*.toml"))

    assert {path.stem for path in agent_paths} == set(expected)
    for path in agent_paths:
        agent = tomllib.loads(path.read_text(encoding="utf-8"))
        model, effort, sandbox = expected[path.stem]
        assert agent["name"] == path.stem
        assert agent["model"] == model
        assert agent["model_reasoning_effort"] == effort
        assert agent["sandbox_mode"] == sandbox
        assert agent["description"]
        instructions = " ".join(agent["developer_instructions"].split())
        assert ".codex/orchestration/validate_contract.py" in instructions
        assert "Return only JSON" in instructions
        assert "spawn another agent" in instructions


def test_repo_instructions_and_skills_require_no_history_spawns() -> None:
    paths = [
        ROOT / "AGENTS.md",
        ROOT / ".agents/skills/blackcell-change/SKILL.md",
        ROOT / ".agents/skills/blackcell-spark-sweep/SKILL.md",
    ]

    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert 'fork_turns = "none"' in text, path
        assert "workflow defect" in text, path
        assert "parent" in text.lower(), path
        assert "raw logs" in text, path
        assert "agent_type" in text, path

    agents_text = " ".join((ROOT / "AGENTS.md").read_text(encoding="utf-8").split())
    assert "Five to eight workers require explicit user instruction" in agents_text
    assert "only one micro-edit worker" in agents_text
    assert "alternate Git refspec" in agents_text


def test_lifecycle_skills_are_discoverable_and_bounded() -> None:
    expected_prompts = {
        "blackcell-plan": "Plan",
        "blackcell-review": "Review",
        "blackcell-verify": "Verify",
        "blackcell-publish": "Publish",
    }

    for skill_name, display_suffix in expected_prompts.items():
        skill_root = SKILLS_ROOT / skill_name
        skill_text = (skill_root / "SKILL.md").read_text(encoding="utf-8")
        metadata = yaml.safe_load((skill_root / "agents/openai.yaml").read_text(encoding="utf-8"))[
            "interface"
        ]

        assert f"name: {skill_name}" in skill_text
        assert metadata["display_name"] == f"BlackCell {display_suffix}"
        assert 25 <= len(metadata["short_description"]) <= 64
        assert f"${skill_name}" in metadata["default_prompt"]

    plan = (SKILLS_ROOT / "blackcell-plan/SKILL.md").read_text(encoding="utf-8")
    assert "Do not edit tracked files" in plan
    assert "<proposed_plan>" in plan

    for skill_name, agent_name, packet_mode in (
        ("blackcell-review", "k_reviewer", "review"),
        ("blackcell-verify", "k_verifier", "verify"),
    ):
        text = (SKILLS_ROOT / skill_name / "SKILL.md").read_text(encoding="utf-8")
        assert agent_name in text
        assert f"`{packet_mode}`" in text
        assert 'fork_turns = "none"' in text
        assert "Do not edit tracked files" in text

    publish = (SKILLS_ROOT / "blackcell-publish/SKILL.md").read_text(encoding="utf-8")
    assert "agent/runtime-v1" in publish
    assert "git push origin agent/runtime-v1" in publish
    assert "Never use `--force`" in publish
    assert "do not merge, rebase, reset, or rewrite history" in publish


def test_contract_schemas_are_closed_and_parseable() -> None:
    expected_versions = {
        "change-spec.schema.json": "blackcell-change-spec/v1",
        "worker-packet.schema.json": "blackcell-worker-packet/v1",
        "worker-result.schema.json": "blackcell-worker-result/v1",
    }

    for filename, version in expected_versions.items():
        schema = json.loads((ORCHESTRATION_ROOT / filename).read_text(encoding="utf-8"))
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert schema["properties"]["schema_version"]["const"] == version
        for object_schema in _object_schemas(schema):
            assert object_schema["additionalProperties"] is False


def test_contract_validator_accepts_linked_good_documents(
    contract_tree: dict[str, Any],
) -> None:
    commands = (
        ("change-spec", contract_tree["change_spec_path"], None),
        ("worker-packet", contract_tree["packet_path"], None),
        ("worker-result", contract_tree["result_path"], contract_tree["packet_path"]),
    )

    for kind, path, packet in commands:
        completed, payload = _run_validator(kind, path, packet=packet)
        assert completed.returncode == 0, completed.stdout
        assert payload == {"errors": [], "kind": kind, "path": str(path), "valid": True}


def test_contract_validator_rejects_unknown_fields(
    contract_tree: dict[str, Any],
) -> None:
    change_spec = contract_tree["change_spec"]
    change_spec["parent_history"] = []
    _write_json(contract_tree["change_spec_path"], change_spec)

    completed, payload = _run_validator("change-spec", contract_tree["change_spec_path"])

    assert completed.returncode == 1
    assert "unknown_field" in _error_codes(payload)


def test_contract_validator_rejects_traversal_and_missing_micro_edit_controls(
    contract_tree: dict[str, Any],
) -> None:
    packet = contract_tree["packet"]
    packet["mode"] = "micro_edit"
    packet["allowed_paths"] = ["../outside"]
    packet["required_reads"] = []
    packet["verification_commands"] = []
    contract_tree["change_spec"]["acceptance_criteria"] = []
    _write_json(contract_tree["change_spec_path"], contract_tree["change_spec"])
    _write_json(contract_tree["packet_path"], packet)

    completed, payload = _run_validator("worker-packet", contract_tree["packet_path"])

    assert completed.returncode == 1
    assert {
        "invalid_path",
        "micro_edit_acceptance",
        "micro_edit_verification",
        "outside_change_scope",
    } <= _error_codes(payload)


def test_contract_validator_enforces_result_scope_and_size_limits(
    contract_tree: dict[str, Any],
) -> None:
    packet = contract_tree["packet"]
    packet["limits"]["max_summary_chars"] = 10
    result = contract_tree["result"]
    result["summary"] = "This summary is deliberately longer than ten characters."
    result["changed_files"] = ["src/blackcell/agents/opencode.py"]
    _write_json(contract_tree["packet_path"], packet)
    _write_json(contract_tree["result_path"], result)

    completed, payload = _run_validator(
        "worker-result",
        contract_tree["result_path"],
        packet=contract_tree["packet_path"],
    )

    assert completed.returncode == 1
    assert {"summary_limit", "writes_forbidden"} <= _error_codes(payload)


def test_contract_validator_rejects_raw_result_output(
    contract_tree: dict[str, Any],
) -> None:
    result = contract_tree["result"]
    result["raw_command_output"] = "unbounded log"
    _write_json(contract_tree["result_path"], result)

    completed, payload = _run_validator(
        "worker-result",
        contract_tree["result_path"],
        packet=contract_tree["packet_path"],
    )

    assert completed.returncode == 1
    assert "unknown_field" in _error_codes(payload)


def test_contract_validator_uses_json_boolean_equality(
    contract_tree: dict[str, Any],
) -> None:
    packet = contract_tree["packet"]
    packet["limits"]["include_raw_command_output"] = 0
    _write_json(contract_tree["packet_path"], packet)

    completed, payload = _run_validator("worker-packet", contract_tree["packet_path"])

    assert completed.returncode == 1
    assert {"const", "type"} <= _error_codes(payload)


def test_contract_validator_checks_evidence_line_bounds(
    contract_tree: dict[str, Any],
) -> None:
    result = contract_tree["result"]
    result["observations"][0]["evidence"][0]["line_end"] = 999_999
    _write_json(contract_tree["result_path"], result)

    completed, payload = _run_validator(
        "worker-result",
        contract_tree["result_path"],
        packet=contract_tree["packet_path"],
    )

    assert completed.returncode == 1
    assert "line_out_of_range" in _error_codes(payload)


def test_contract_validator_requires_verify_commands(
    contract_tree: dict[str, Any],
) -> None:
    packet = contract_tree["packet"]
    packet["mode"] = "verify"
    packet["verification_commands"] = []
    _write_json(contract_tree["packet_path"], packet)

    completed, payload = _run_validator("worker-packet", contract_tree["packet_path"])

    assert completed.returncode == 1
    assert "verify_verification" in _error_codes(payload)


@pytest.mark.parametrize(
    "argv",
    (
        ["uv", "run", "uv", "publish"],
        ["bash", "-c", "pytest"],
        ["python", "-c", "print('not a bounded check')"],
        ["git", "push", "origin", "main"],
    ),
)
def test_contract_validator_rejects_unsafe_verification_commands(
    contract_tree: dict[str, Any], argv: list[str]
) -> None:
    packet = contract_tree["packet"]
    packet["verification_commands"] = [
        {"argv": argv, "reason": "This command is deliberately unsafe."}
    ]
    _write_json(contract_tree["packet_path"], packet)

    completed, payload = _run_validator("worker-packet", contract_tree["packet_path"])

    assert completed.returncode == 1
    assert "unsafe_verification" in _error_codes(payload)


def test_contract_validator_canonicalizes_symlink_scope(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)
    orchestration = repo_root / ".codex/orchestration"
    orchestration.mkdir(parents=True)
    shutil.copy(ORCHESTRATION_ROOT / "worker-result.schema.json", orchestration)
    secret = repo_root / "secret"
    secret.mkdir()
    (secret / "file.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo_root / "public").symlink_to(secret, target_is_directory=True)

    work_id = f"pytest-{uuid.uuid4().hex[:12]}"
    work_root = Path("/tmp/blackcell-codex") / work_id
    workers = work_root / "workers"
    workers.mkdir(parents=True)
    change_spec_path = work_root / "change-spec.json"
    packet_path = workers / "worker-01.json"
    change_spec = {
        "schema_version": "blackcell-change-spec/v1",
        "work_id": work_id,
        "objective": "Reject an allowed symlink that resolves into forbidden scope.",
        "base_sha": "a" * 40,
        "acceptance_criteria": ["The packet is rejected."],
        "in_scope": ["public"],
        "out_of_scope": ["secret"],
        "constraints": ["Read only."],
        "assumptions": [],
        "unknowns": [],
    }
    packet = {
        "schema_version": "blackcell-worker-packet/v1",
        "work_id": work_id,
        "worker_id": "worker-01",
        "mode": "evidence",
        "change_spec_path": str(change_spec_path),
        "task": "Read the assigned path without crossing the forbidden boundary.",
        "allowed_paths": ["public"],
        "forbidden_paths": ["secret"],
        "required_reads": [
            {
                "path": "public/file.py",
                "reason": "This path resolves through the in-repository symlink.",
            }
        ],
        "verification_commands": [],
        "result_schema_path": str((orchestration / "worker-result.schema.json").resolve()),
        "limits": {
            "max_findings": 5,
            "max_summary_chars": 500,
            "include_raw_command_output": False,
        },
    }
    _write_json(change_spec_path, change_spec)
    _write_json(packet_path, packet)

    try:
        completed, payload = _run_validator("worker-packet", packet_path, repo_root=repo_root)
    finally:
        shutil.rmtree(work_root, ignore_errors=True)

    assert completed.returncode == 1
    assert {"forbidden_change_scope", "forbidden_worker_scope"} <= _error_codes(payload)


@pytest.mark.skipif(CODEX is None, reason="Codex CLI is not installed")
def test_installed_codex_accepts_project_config_strictly() -> None:
    completed = subprocess.run(
        [CODEX or "codex", "--strict-config", "--version"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "codex-cli" in completed.stdout


@pytest.mark.skipif(CODEX is None, reason="Codex CLI is not installed")
@pytest.mark.parametrize(
    ("argv", "expected"),
    (
        (("git", "fetch", "--prune"), "allow"),
        (("git", "commit", "-m", "test"), "allow"),
        (("git", "push", "-u", "origin", "feature"), "allow"),
        (("gh", "pr", "create", "--draft"), "allow"),
        (("uv", "sync"), "allow"),
        (("uv", "run", "pytest"), "allow"),
        (("python", ".codex/orchestration/validate_contract.py", "--help"), "allow"),
        (("git", "reset", "--hard", "HEAD"), "prompt"),
        (("git", "clean", "-fd"), "prompt"),
        (("git", "checkout", "main"), "prompt"),
        (("git", "push", "--force", "origin", "feature"), "prompt"),
        (("git", "push", "origin", "--force", "feature"), "prompt"),
        (("git", "push", "--delete", "origin", "feature"), "prompt"),
        (("git", "push", "--mirror", "origin"), "prompt"),
        (("git", "prune"), "prompt"),
        (("gh", "pr", "merge", "1"), "prompt"),
        (("gh", "release", "create", "v1.0.0"), "prompt"),
        (("gh", "cache", "delete", "123"), "prompt"),
        (("gh", "api", "--method", "DELETE", "repos/o/r"), "prompt"),
        (("gh", "secret", "set", "TOKEN"), "prompt"),
        (("uv", "publish"), "prompt"),
        (("rm", "user-data.txt"), "prompt"),
        (("docker", "volume", "rm", "data"), "prompt"),
    ),
)
def test_execpolicy_decisions(argv: tuple[str, ...], expected: str) -> None:
    completed = subprocess.run(
        [CODEX or "codex", "execpolicy", "check", "--rules", str(RULES), "--", *argv],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["decision"] == expected


def test_research_proposal_records_context_costs_and_non_goals() -> None:
    path = ROOT / "docs/research/spark-repository-perception.md"
    text = path.read_text(encoding="utf-8")
    index = (ROOT / "docs/index.md").read_text(encoding="utf-8")

    for field in (
        "fork_mode",
        "change_spec_bytes",
        "worker_packet_bytes",
        "child_initial_input_tokens",
        "duplicated_context_bytes",
        "parent_context_bytes_avoided",
        "worker_result_bytes",
        "synthesis_input_tokens",
        "worker_wall_time_ms",
    ):
        assert f"`{field}`" in text
    assert 'fork_turns = "none"' in text
    assert "GraphRAG" in text
    assert "persistent\nagent memory" in text
    assert "spark-repository-perception.md" in index


@pytest.fixture
def contract_tree() -> Iterator[dict[str, Any]]:
    work_id = f"pytest-{uuid.uuid4().hex[:12]}"
    work_root = Path("/tmp/blackcell-codex") / work_id
    workers = work_root / "workers"
    results = work_root / "results"
    workers.mkdir(parents=True)
    results.mkdir()

    change_spec_path = work_root / "change-spec.json"
    packet_path = workers / "worker-01.json"
    result_path = results / "worker-01.json"
    verification_argv = ["uv", "run", "pytest", "tests/unit/test_agents.py"]
    change_spec: dict[str, Any] = {
        "schema_version": "blackcell-change-spec/v1",
        "work_id": work_id,
        "objective": "Map the existing OpenCode agent projection without changing it.",
        "base_sha": "a" * 40,
        "acceptance_criteria": ["Return exact path and line evidence."],
        "in_scope": ["src/blackcell/agents"],
        "out_of_scope": ["src/blackcell/agents/registry.py"],
        "constraints": ["Read-only evidence."],
        "assumptions": [],
        "unknowns": [],
    }
    packet: dict[str, Any] = {
        "schema_version": "blackcell-worker-packet/v1",
        "work_id": work_id,
        "worker_id": "worker-01",
        "mode": "evidence",
        "change_spec_path": str(change_spec_path),
        "task": "Identify the OpenCode artifact rendering entry point.",
        "allowed_paths": ["src/blackcell/agents/opencode.py"],
        "forbidden_paths": ["src/blackcell/agents/registry.py"],
        "required_reads": [
            {
                "path": "src/blackcell/agents/opencode.py",
                "symbol": "render_opencode_artifacts",
                "reason": "This function owns artifact rendering.",
            }
        ],
        "verification_commands": [
            {"argv": verification_argv, "reason": "Check the existing agent contract."}
        ],
        "result_schema_path": str((ORCHESTRATION_ROOT / "worker-result.schema.json").resolve()),
        "limits": {
            "max_findings": 20,
            "max_summary_chars": 2000,
            "include_raw_command_output": False,
        },
    }
    result: dict[str, Any] = {
        "schema_version": "blackcell-worker-result/v1",
        "work_id": work_id,
        "worker_id": "worker-01",
        "status": "completed",
        "summary": "The OpenCode module owns the artifact rendering entry point.",
        "observations": [
            {
                "kind": "fact",
                "summary": "render_opencode_artifacts returns the managed artifacts.",
                "evidence": [
                    {
                        "path": "src/blackcell/agents/opencode.py",
                        "symbol": "render_opencode_artifacts",
                        "line_start": 47,
                        "line_end": 54,
                    }
                ],
            }
        ],
        "conflicts": [],
        "unknowns": [],
        "changed_files": [],
        "verification": [
            {"argv": verification_argv, "exit_code": 0, "summary": "Agent tests passed."}
        ],
    }
    _write_json(change_spec_path, change_spec)
    _write_json(packet_path, packet)
    _write_json(result_path, result)

    try:
        yield {
            "change_spec": change_spec,
            "change_spec_path": change_spec_path,
            "packet": packet,
            "packet_path": packet_path,
            "result": result,
            "result_path": result_path,
        }
    finally:
        shutil.rmtree(work_root, ignore_errors=True)


def _object_schemas(schema: dict[str, Any]) -> Iterator[dict[str, Any]]:
    if schema.get("type") == "object":
        yield schema
    properties = schema.get("properties", {})
    if isinstance(properties, dict):
        for nested in properties.values():
            if isinstance(nested, dict):
                yield from _object_schemas(nested)
    items = schema.get("items")
    if isinstance(items, dict):
        yield from _object_schemas(items)


def _run_validator(
    kind: str,
    path: Path,
    *,
    packet: Path | None = None,
    repo_root: Path = ROOT,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    command = [
        sys.executable,
        str(VALIDATOR),
        kind,
        str(path),
        "--repo-root",
        str(repo_root),
    ]
    if packet is not None:
        command.extend(("--packet", str(packet)))
    completed = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed, json.loads(completed.stdout)


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _error_codes(payload: dict[str, Any]) -> set[str]:
    return {error["code"] for error in payload["errors"]}
