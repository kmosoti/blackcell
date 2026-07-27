from __future__ import annotations

import json
import re
from pathlib import Path
from typing import cast

from blackcell.cli.app import app
from blackcell.config.verification import (
    VERIFICATION_CONFIG_FILE_ENV,
    VerificationWorkerRuntimeConfig,
    load_verification_config,
)
from blackcell.interfaces.http import (
    CancelRunRequest,
    IntentRequest,
    PlanRequest,
    ProjectRequest,
    RunQueryRequest,
    RunRequest,
    decode_contract,
)
from tests.cli_runner import CycloptsCliRunner

ROOT = Path(__file__).parents[2]
REQUEST_ROOT = ROOT / "examples" / "runtime" / "requests"
QUICKSTART_PATH = ROOT / "docs" / "guides" / "runtime-quickstart.md"
VERIFY_GUIDE_PATH = ROOT / "docs" / "guides" / "verification-configuration.md"
VERIFY_EXAMPLE_PATH = ROOT / "examples" / "runtime" / "verification.json"


def test_runtime_request_templates_decode_and_cross_bind() -> None:
    project = decode_contract((REQUEST_ROOT / "project.template.json").read_bytes(), ProjectRequest)
    intent = decode_contract((REQUEST_ROOT / "intent.template.json").read_bytes(), IntentRequest)
    plan = decode_contract((REQUEST_ROOT / "plan.template.json").read_bytes(), PlanRequest)
    run = decode_contract((REQUEST_ROOT / "run.template.json").read_bytes(), RunRequest)
    query = decode_contract((REQUEST_ROOT / "query.template.json").read_bytes(), RunQueryRequest)
    cancel = decode_contract((REQUEST_ROOT / "cancel.template.json").read_bytes(), CancelRunRequest)

    assert project.root == "/ABSOLUTE/PATH/TO/PROJECT"
    assert project.configuration_digest == "sha256:" + "0" * 64
    assert plan.base_commit == "0" * 40
    assert intent.project_id == plan.project_id == run.project_id == project.project_id
    assert plan.intent_id == run.intent_id == intent.intent_id
    assert run.plan_id == plan.plan_id
    assert plan.planning_mode == "generated"
    assert run.run_id == "run"
    assert query.project_ids == (project.project_id,)
    assert query.intent_ids == (intent.intent_id,)
    assert query.plan_ids == (plan.plan_id,)
    assert query.run_ids == (run.run_id,)
    assert cancel.idempotency_key == "run-cancel"

    assert len(plan.nodes) == 1
    node = plan.nodes[0]
    assert node.effects == ("repository-read", "repository-write", "process")
    assert node.allowed_paths == ("src/example.py",)
    assert node.budget.max_changed_files == 1
    assert node.checks[0].argv[0] == "python"
    assert (
        len(
            {
                project.idempotency_key,
                intent.idempotency_key,
                plan.idempotency_key,
                run.idempotency_key,
                cancel.idempotency_key,
            }
        )
        == 5
    )


def test_runtime_guides_bind_live_commands_config_and_nonclaims(tmp_path: Path) -> None:
    quickstart = QUICKSTART_PATH.read_text(encoding="utf-8")
    verify_guide = VERIFY_GUIDE_PATH.read_text(encoding="utf-8")
    normalized_quickstart = " ".join(quickstart.split())

    required_commands = {
        "uv run blackcell daemon foreground": ("daemon", "foreground"),
        "uv run blackcell daemon status": ("daemon", "status"),
        "uv run blackcell project register": ("project", "register"),
        "uv run blackcell intent accept": ("intent", "accept"),
        "uv run blackcell plan accept": ("plan", "accept"),
        "uv run blackcell run submit": ("run", "submit"),
        "uv run blackcell run status": ("run", "status"),
        "uv run blackcell run query": ("run", "query"),
        "uv run blackcell run replay": ("run", "replay"),
        "uv run blackcell run cancel": ("run", "cancel"),
        "uv run blackcell events list": ("events", "list"),
        "uv run blackcell tui": ("tui",),
    }
    runner = CycloptsCliRunner()
    for command, tokens in required_commands.items():
        assert command in quickstart
        help_result = runner.invoke(app, [*tokens, "--help"], catch_exceptions=False)
        assert help_result.exit_code == 0, command

    assert "uv run blackcell-runtime verification-worker --once" in verify_guide
    assert "http://127.0.0.1:8080/ui" in quickstart
    assert "`POST /api/v1/runs` is asynchronous" in normalized_quickstart
    assert "not a runnable project or live-provider proof" in normalized_quickstart
    assert "does not build a package, create a tag, publish a release, or deploy" in (
        normalized_quickstart
    )
    assert "With no worker configuration the daemon is API-only" in normalized_quickstart

    for guide_path, guide in (
        (QUICKSTART_PATH, quickstart),
        (VERIFY_GUIDE_PATH, verify_guide),
    ):
        for target in _local_links(guide):
            linked = (guide_path.parent / target).resolve()
            assert linked.is_relative_to(ROOT.resolve())
            assert linked.exists(), target

    block = re.search(r"```json\n(?P<document>.*?)\n```", verify_guide, re.DOTALL)
    assert block is not None
    documented_config = json.loads(block.group("document"))
    example_config = json.loads(VERIFY_EXAMPLE_PATH.read_text(encoding="utf-8"))
    assert documented_config == example_config

    repository = tmp_path / "repository"
    repository.mkdir()
    source = tmp_path / "verification.json"
    source.write_bytes(VERIFY_EXAMPLE_PATH.read_bytes())
    source.chmod(0o600)
    config = load_verification_config(
        {VERIFICATION_CONFIG_FILE_ENV: str(source)},
        repository_root=repository,
    )
    assert isinstance(config, VerificationWorkerRuntimeConfig)
    assert config.worker.worker_id == "verifier.local-1"
    assert config.worker.supervisor_id == "verification-supervisor.local-1"
    assert config.worker.worker_id != config.worker.supervisor_id


def _local_links(text: str) -> tuple[str, ...]:
    targets = {
        target.split("#", maxsplit=1)[0]
        for target in re.findall(r"(?<!!)\[[^]]+\]\(([^)]+)\)", text)
        if target and not target.startswith(("https://", "http://", "mailto:", "#"))
    }
    return tuple(sorted(cast("set[str]", targets)))
